# shell.py — safe shell helpers and display utilities
# This file is part of the batocera distribution (https://batocera.org).
# Copyright (c) 2025-2026 lbrpdx for the Batocera team
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License
# as published by the Free Software Foundation, version 3.
#
# YOU MUST KEEP THIS HEADER AS IT IS
import os
import shlex
import subprocess
import threading
import time

import gi
gi.require_version('Gdk', '3.0')
from gi.repository import Gdk

# Default to disable AT-SPI DBus chatter for performance/stability
os.environ.setdefault("NO_AT_BRIDGE", "1")

def normalize_bool_str(s) -> bool:
    if s is None:
        return False
    # Handle boolean input directly
    if isinstance(s, bool):
        return s
    # Handle string input
    if not isinstance(s, str):
        s = str(s)
    s = s.strip().lower()
    return s in ("1", "true", "on", "yes", "enabled")

def extract_commands(s: str) -> list[str]:
    """
    Return the list of shell commands embedded in *s* as ${...} substitutions,
    in order of appearance. Brace depth is tracked so nested braces inside a
    command are handled. Returns [] when *s* has no ${...}.
    """
    if not s or "${" not in s:
        return []
    cmds: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        if i < n - 1 and s[i:i+2] == "${":
            i += 2
            depth = 1
            cmd_start = i
            while i < n and depth > 0:
                if s[i] == '{':
                    depth += 1
                elif s[i] == '}':
                    depth -= 1
                i += 1
            if depth == 0:
                cmds.append(s[cmd_start:i-1].strip())
            else:
                break  # unmatched; stop
        else:
            i += 1
    return cmds


def _expand_with(s: str, resolve) -> str:
    """
    Core ${...} expansion. *resolve(cmd) -> str* is called for each embedded
    command; the rest of *s* is returned verbatim. Brace depth is tracked so
    nested braces inside a command are handled.
    """
    if not s or "${" not in s:
        return s
    out: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        if i < n - 1 and s[i:i+2] == "${":
            start = i
            i += 2
            depth = 1
            cmd_start = i
            while i < n and depth > 0:
                if s[i] == '{':
                    depth += 1
                elif s[i] == '}':
                    depth -= 1
                i += 1
            if depth == 0:
                cmd = s[cmd_start:i-1].strip()
                out.append(resolve(cmd))
            else:
                # Unmatched braces; emit the remainder verbatim.
                out.append(s[start:])
                break
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def expand_command_string(s: str) -> str:
    """
    Expand command substitutions in a string.
    Example: "${batocera-audio getSystemVolume}%" -> "80%"
    Supports multiple ${...} in one string, including nested braces.
    """
    return _expand_with(s, lambda c: run_shell_capture_cached(c.strip()))


def expand_command_string_cached(s: str, ttl_sec: float = 1.0,
                                 timeout_sec: float = 3.0,
                                 allow_block: bool = True) -> str:
    """
    Like expand_command_string, but each embedded command is resolved via the
    shared TTL cache (run_shell_capture_cached). *allow_block=False* makes
    every lookup non-forking (run_shell_cache_lookup): no subprocess is ever
    spawned on the calling/UI thread, so a cold cache yields "" and a
    background refresh warms the cache for the next call. Intended for
    read-only display strings polled on the main loop.
    """
    if not s or "${" not in s:
        return s
    if allow_block:
        def _r(c: str) -> str:
            return run_shell_capture_cached(c.strip(), ttl_sec=ttl_sec,
                                            timeout_sec=timeout_sec)
    else:
        def _r(c: str) -> str:
            return run_shell_cache_lookup(c.strip(), ttl_sec=ttl_sec)
    return _expand_with(s, _r)


def shell_cache_has_all(cmds, ttl_sec: float) -> bool:
    """True iff every command in *cmds* has a fresh (within *ttl_sec*) cached
    result. Empty *cmds* -> True. Main-loop-safe (no fork, no I/O)."""
    if not cmds:
        return True
    now = time.monotonic()
    with _shell_cache_lock:
        for c in cmds:
            cached = _shell_cache.get(c)
            if not cached or (now - cached[0]) >= ttl_sec:
                return False
    return True


def warm_shell_cache(cmds, timeout_sec: float = 3.0):
    """
    Spawn background workers to (re)compute every command in *cmds* and store
    the results in the shared cache. Dedupes against refreshes already in
    flight. Fire-and-forget; safe to call from the UI thread.
    """
    if not cmds:
        return
    to_run: list[str] = []
    with _shell_cache_lock:
        for c in cmds:
            if not c:
                continue
            if c not in _refresh_in_flight:
                _refresh_in_flight.add(c)
                to_run.append(c)
    if not to_run:
        return

    def _bg(cmd: str):
        try:
            result = run_shell_capture(cmd, timeout_sec=timeout_sec)
            with _shell_cache_lock:
                _shell_cache[cmd] = (time.monotonic(), result)
        except Exception:
            pass
        finally:
            with _shell_cache_lock:
                _refresh_in_flight.discard(cmd)

    for c in to_run:
        threading.Thread(target=_bg, args=(c,), daemon=True).start()

def run_shell_capture(cmd: str, timeout_sec: float = 3.0) -> str:
    """
    Execute a command and capture stdout safely.
    - Uses shell=True only when shell metacharacters are present.
    - Kills child via process group when timing out.
    - Returns decoded UTF-8 text (errors ignored), stripped.
    """
    if not cmd:
        return ""
    use_shell = any(c in cmd for c in ['$', '|', '&', ';', '`', '>', '<'])
    try:
        if use_shell:
            proc = subprocess.Popen(
                cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                preexec_fn=os.setsid,
            )
        else:
            proc = subprocess.Popen(
                shlex.split(cmd),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                preexec_fn=os.setsid,
            )
        out, _ = proc.communicate(timeout=timeout_sec)
        return out.decode("utf-8", errors="ignore").strip()
    except subprocess.TimeoutExpired:
        try:
            # Best-effort terminate process group
            os.killpg(os.getpgid(proc.pid), 9)
        except Exception:
            pass
        return ""
    except Exception:
        return ""

_shell_cache_lock = threading.Lock()
_shell_cache: dict[str, tuple[float, str]] = {}
# Commands with a background refresh in flight (dedupes concurrent refreshes).
_refresh_in_flight: set[str] = set()

def run_shell_capture_cached(cmd: str, ttl_sec: float = 1.0, timeout_sec: float = 3.0) -> str:
    """
    Same as run_shell_capture, but reuses a recent result for an identical
    command within ttl_sec instead of spawning a new process.

    Intended only for read-only display/condition commands, where several
    widgets may poll the exact same command on overlapping intervals (e.g.
    multiple elements querying the same local API). Do NOT use this for
    commands with side effects (button actions, afterclick, etc.) — those
    must always execute fresh.
    """
    if not cmd:
        return ""
    with _shell_cache_lock:
        cached = _shell_cache.get(cmd)
        if cached and (time.monotonic() - cached[0]) < ttl_sec:
            return cached[1]
    result = run_shell_capture(cmd, timeout_sec=timeout_sec)
    with _shell_cache_lock:
        _shell_cache[cmd] = (time.monotonic(), result)
    return result


def run_shell_capture_lines(cmd: str, ttl_sec: float = 1.0,
                            timeout_sec: float = 3.0) -> list[str]:
    """
    Run *cmd* via the shared TTL cache and return its stdout split into
    non-empty lines. Intended for read-only commands that produce a list
    (e.g. ``batocera-audio list-profiles``). Empty/whitespace-only lines are
    dropped. Returns [] on failure or empty output.
    """
    if not cmd:
        return []
    out = run_shell_capture_cached(cmd, ttl_sec=ttl_sec, timeout_sec=timeout_sec)
    if not out:
        return []
    return [ln for ln in out.splitlines() if ln.strip()]


def run_shell_cache_lookup(cmd: str, ttl_sec: float = 5.0) -> str:
    """
    Return a cached result for *cmd* without ever spawning a subprocess on
    the calling thread — main-loop-safe (no fork, no I/O). If the value is
    stale or missing, a background refresh is scheduled and the stale value
    (or "") is returned. The RefreshTask workers keep the cache warm, so in
    steady state this is a pure dict lookup.
    """
    if not cmd:
        return ""
    now = time.monotonic()
    with _shell_cache_lock:
        cached = _shell_cache.get(cmd)
        if cached:
            ts, val = cached
            if (now - ts) < ttl_sec:
                return val
            stale_val = val
            already_refreshing = cmd in _refresh_in_flight
            if not already_refreshing:
                _refresh_in_flight.add(cmd)
            else:
                return stale_val  # refresh already running; keep stale value
        else:
            stale_val = ""
            already_refreshing = cmd in _refresh_in_flight
            if not already_refreshing:
                _refresh_in_flight.add(cmd)
            else:
                return ""

    # Schedule a background refresh (off the calling/UI thread)
    def _bg_refresh():
        try:
            result = run_shell_capture(cmd)
            with _shell_cache_lock:
                _shell_cache[cmd] = (time.monotonic(), result)
        except Exception:
            pass
        finally:
            with _shell_cache_lock:
                _refresh_in_flight.discard(cmd)

    threading.Thread(target=_bg_refresh, daemon=True).start()
    return stale_val

def ensure_display() -> bool:
    return bool(os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"))

def get_primary_geometry():
    """
    Returns (x, y, width, height) for the primary monitor.
    Falls back to monitor 0, and to 1280x720 if unavailable.
    """
    display = Gdk.Display.get_default()
    mon = None
    try:
        mon = display.get_primary_monitor()
    except Exception:
        mon = None
    if mon is None:
        try:
            mon = display.get_monitor(0)
        except Exception:
            mon = None
    if mon and hasattr(mon, "get_geometry"):
        g = mon.get_geometry()
        return g.x, g.y, g.width, g.height
    return (0, 0, 1280, 720)

