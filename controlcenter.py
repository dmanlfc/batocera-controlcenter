#!/usr/bin/env python3
# controlcenter.py — Batocera Control Center
# This file is part of the batocera distribution (https://batocera.org).
# Copyright (c) 2025 lbrpdx for the Batocera team
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License
# as published by the Free Software Foundation, version 3.
#
# YOU MUST KEEP THIS HEADER AS IT IS
#
import os
import sys
import signal

os.environ.setdefault("NO_AT_BRIDGE", "1")

import gi
gi.require_version('Gtk', '3.0')
gi.require_version('Gdk', '3.0')
from gi.repository import Gtk

from xml_utils import parse_xml, validate_xml
from ui_core import ControlCenterApp

def ensure_display():
    return bool(os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"))

def gtk_init_check():
    try:
        ok, _ = Gtk.init_check(sys.argv)
        return bool(ok)
    except Exception:
        return False

def main():
    signal.signal(signal.SIGINT, lambda *_: Gtk.main_quit())

    if not ensure_display():
        sys.stderr.write("ERROR: No GUI display detected. Set DISPLAY or WAYLAND_DISPLAY.\n")
        sys.exit(1)
    if not gtk_init_check():
        sys.stderr.write("ERROR: Gtk couldn't be initialized.\n")
        sys.exit(1)

    # Helper function to find files in priority order
    def find_file(filename, default_path):
        """Find file in priority order:
        1. /userdata/system/configs/controlcenter/
        2. /usr/share/batocera/controlcenter/
        3. Same directory as controlcenter.py (default_path)
        """
        search_paths = [
            f"/userdata/system/configs/controlcenter/{filename}",
            f"/usr/share/batocera/controlcenter/{filename}",
            default_path
        ]
        
        for path in search_paths:
            if os.path.exists(path):
                return path
        
        # Return default path even if it doesn't exist (for error messages)
        return default_path
    
    # Get script directory for default paths
    script_dir = os.path.dirname(os.path.abspath(__file__))
    
    # Paths and parameters
    auto_close_seconds = 0  # 0 = never auto-close
    
    # Parse command line arguments
    if len(sys.argv) >= 2 and sys.argv[1] not in ("-h", "--help"):
        xml_path = sys.argv[1]
    else:
        # No argument - search in priority order
        xml_path = find_file("controlcenter.xml", os.path.join(script_dir, "controlcenter.xml"))
    
    if len(sys.argv) >= 3:
        css_path = sys.argv[2]
    else:
        # No argument - search in priority order
        css_path = find_file("style.css", os.path.join(script_dir, "style.css"))
    
    if len(sys.argv) >= 4:
        try:
            auto_close_seconds = int(sys.argv[3])
        except ValueError:
            sys.stderr.write(f"WARNING: Invalid auto-close timeout '{sys.argv[3]}', using 0 (no auto-close)\n")
            auto_close_seconds = 0

    if not os.path.exists(xml_path):
        sys.stderr.write(f"ERROR: XML file not found: {xml_path}\n")
        sys.exit(1)
    if not os.path.exists(css_path):
        sys.stderr.write(f"WARNING: CSS file not found: {css_path} — running without custom styles.\n")

    xml_root = parse_xml(xml_path)
    errs, warns = validate_xml(xml_root)
    if warns:
        sys.stderr.write("XML warnings:\n")
        for w in warns:
            sys.stderr.write(f" - {w}\n")
    if errs:
        sys.stderr.write("XML errors:\n")
        for e in errs:
            sys.stderr.write(f" - {e}\n")
        sys.exit(2)

    app = ControlCenterApp(xml_root, css_path, auto_close_seconds)
    app.run()

if __name__ == "__main__":
    main()

