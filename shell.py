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

BATOCERA_CONF = "/userdata/system/batocera.conf"

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

def run_shell_capture(cmd: str, timeout_sec: float = 3.0, get_output = True) -> str:
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
        proc_stdout = subprocess.DEVNULL
        if get_output:
            proc_stdout = subprocess.PIPE

        if use_shell:
            proc = subprocess.Popen(
                cmd,
                shell=True,
                stdout=proc_stdout,
                stderr=subprocess.DEVNULL,
                preexec_fn=os.setsid,
            )
        else:
            proc = subprocess.Popen(
                shlex.split(cmd),
                stdout=proc_stdout,
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

def invalidate_shell_cache(cmd: str):
    with _shell_cache_lock:
        _shell_cache.pop(cmd, None)

def run_shell_capture_set_to_cache(cmd: str, timeout_sec: float = 3.0) -> str:
    """
    Same as run_shell_capture, but save the result to cache by forcing a negative ttl
    """
    return run_shell_capture_cached(cmd, -1.0, timeout_sec)

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

def settings_get(key: str) -> str | None:
    try:
        result = subprocess.run(["batocera-settings-get", key],
                                capture_output=True, text=True, timeout=2)
        if result.returncode == 0:
            value = (result.stdout or "").strip()
            value = value.strip('"').strip("'").strip()
            return value if value else None
    except Exception:
        pass
    return None

def settings_set(key: str, value: str) -> bool:
    try:
        result = subprocess.run(["batocera-settings-set", key, value],
                                capture_output=True, text=True, timeout=5)
        return result.returncode == 0
    except Exception:
        return False

def get_output_list() -> list[str]:
    """
    Return the list of connected video outputs (connector names, e.g.
    ["HDMI-A-1", "HDMI-A-2"]). Tries `batocera-resolution listOutputs`,
    then `wlr-randr`/`xrandr`, then GDK monitors. Empty list on failure.
    """
    try:
        result = subprocess.run(["batocera-resolution", "listOutputs"],
                                capture_output=True, text=True, timeout=2)
        if result.returncode == 0:
            outs = [l.strip() for l in (result.stdout or "").splitlines()
                    if l.strip()]
            if outs:
                return outs
    except Exception:
        pass
    for cmdline in ("wlr-randr", "xrandr"):
        try:
            result = subprocess.run([cmdline], capture_output=True, text=True, timeout=2)
            if result.returncode == 0:
                outs = [l.split()[0] for l in (result.stdout or "").splitlines()
                        if l.strip() and (" connected" in l or "CONNECTED" in l)]
                if outs:
                    return outs
        except Exception:
            pass
    # Last resort: GDK connector names
    try:
        display = Gdk.Display.get_default()
        if display:
            outs = []
            for i in range(display.get_n_monitors()):
                mon = display.get_monitor(i)
                if hasattr(mon, "get_connector"):
                    c = mon.get_connector()
                    if c:
                        outs.append(c)
            if outs:
                return outs
    except Exception:
        pass
    return []

def _wayland_output_names(n: int) -> list[str]:
    """
    Best-effort list of connected output names, in compositor order, to map a
    connector name (e.g. "DSI-2") to a GDK monitor index. Used when GDK lacks
    get_connector(). Tries wlr-randr, then batocera-resolution listOutputs.
    """
    # wlr-randr: "Output: HDMI-A-1 ..." blocks, only "ENABLED"/connected ones
    try:
        result = subprocess.run(["wlr-randr"], capture_output=True, text=True, timeout=2)
        if result.returncode == 0:
            names = []
            cur = None
            enabled = False
            for line in (result.stdout or "").splitlines():
                if line.startswith("Output: "):
                    if cur and enabled:
                        names.append(cur)
                    cur = line[len("Output: "):].strip().split()[0]
                    enabled = "ENABLED" in line
                elif cur and "Enabled: yes" in line:
                    enabled = True
                elif cur and "Enabled: no" in line:
                    enabled = False
            if cur and enabled:
                names.append(cur)
            if names:
                return names
    except Exception:
        pass
    # batocera-resolution listOutputs prints connected outputs in order
    try:
        result = subprocess.run(["batocera-resolution", "listOutputs"],
                                capture_output=True, text=True, timeout=2)
        if result.returncode == 0:
            names = [l.strip() for l in (result.stdout or "").splitlines() if l.strip()]
            if names:
                return names
    except Exception:
        pass
    return []

def select_monitor(display: Gdk.Display, screen_arg: str | None = None) -> Gdk.Monitor | None:
    """
    Pick the monitor the Control Center should start on.

    Priority:
      1. *screen_arg* (from --screen CLI override): connector name or index
      2. controlcenter.screen from batocera.conf (connector name or index)
      3. Legacy default: monitor 1 when multiple monitors, else monitor 0

    Matching by name uses Gdk.Monitor.get_connector() when available; on X11
    monitors have no connector name from GDK, so we try an xrandr name->index
    lookup. Returns None if no monitor could be selected (caller keeps its
    own default).
    """
    n = display.get_n_monitors() if display else 0
    if n == 0:
        return None

    def by_name(name: str) -> Gdk.Monitor | None:
        if not name:
            return None
        # Wayland: GDK knows the connector (when available)
        for i in range(n):
            mon = display.get_monitor(i)
            if hasattr(mon, "get_connector"):
                try:
                    if (mon.get_connector() or "") == name:
                        return mon
                except Exception:
                    pass
        # Wayland without get_connector(): map output name to monitor index
        # via the compositor's connected-output list (compositor order).
        names = _wayland_output_names(n)
        if name in names and names.index(name) < n:
            return display.get_monitor(names.index(name))
        # X11: match xrandr output name to monitor index via geometry
        try:
            result = subprocess.run(["xrandr", "--query"],
                                    capture_output=True, text=True, timeout=2)
            if result.returncode == 0:
                # index of the *connected* output among connected ones
                connected = [l.split()[0] for l in (result.stdout or "").splitlines()
                             if " connected" in l]
                if name in connected:
                    idx = connected.index(name)
                    if idx < n:
                        return display.get_monitor(idx)
        except Exception:
            pass
        return None

    def by_index(idx_str: str) -> Gdk.Monitor | None:
        try:
            idx = int(idx_str)
        except (TypeError, ValueError):
            return None
        if 0 <= idx < n:
            return display.get_monitor(idx)
        return None

    # 1. CLI override
    if screen_arg:
        mon = by_name(screen_arg) or by_index(screen_arg)
        if mon is not None:
            return mon

    # 2. Saved setting
    saved = settings_get("controlcenter.screen")
    if saved:
        mon = by_name(saved) or by_index(saved)
        if mon is not None:
            return mon

    # 3. Legacy default: 2nd screen if it exists (backglass), else primary
    if n > 1:
        return display.get_monitor(1)
    return display.get_monitor(0)

