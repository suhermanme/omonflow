#!/usr/bin/env python3
"""M02 shortcut spike (macOS).

1. Starts OmonFlow (stub audio) with ⌘⇧Space, posts a real ⌘⇧Space key event
   twice and checks the widget hid and re-appeared (from OmonFlow's log and
   the on-screen window list).
2. Conflict check: while OmonFlow holds ⌘⇧Space, a second process registers
   the same combination. Reports whether macOS rejects it, i.e. whether
   conflicts with other apps are detectable at all.

Usage: uv run python tools/spikes/shortcut_spike.py
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import Quartz

ROOT = Path(__file__).resolve().parents[2]
KEY_SPACE = 49


def press_cmd_shift_space():
    flags = Quartz.kCGEventFlagMaskCommand | Quartz.kCGEventFlagMaskShift
    for down in (True, False):
        ev = Quartz.CGEventCreateKeyboardEvent(None, KEY_SPACE, down)
        Quartz.CGEventSetFlags(ev, flags)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        time.sleep(0.05)


def widget_on_screen(pid):
    wins = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID)
    return any(w.get("kCGWindowOwnerPID") == pid and int(w["kCGWindowBounds"]["Width"]) == 312 for w in wins)


SECOND = r"""
import sys
from omonflow import _core
m = _core.HotkeyManager()
try:
    m.register("cmd+shift+space")
    print("registered")
except Exception as e:
    print(type(e).__name__ + ": " + str(e))
"""


def main():
    tmp = Path(tempfile.mkdtemp(prefix="omonflow-sc-"))
    (tmp / "settings.json").write_text(json.dumps({"notifications": False, "widget_pos": [300, 300]}))
    env = {**os.environ, "OMONFLOW_CONFIG_DIR": str(tmp)}
    env.pop("QT_QPA_PLATFORM", None)
    omon = subprocess.Popen([sys.executable, "-m", "omonflow"], env=env, cwd=ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(3.0)
        log = tmp / "logs" / "omonflow.log"
        registered = "shortcut registered" in log.read_text()
        visible0 = widget_on_screen(omon.pid)
        press_cmd_shift_space()
        time.sleep(0.8)
        visible1 = widget_on_screen(omon.pid)
        press_cmd_shift_space()
        time.sleep(0.8)
        visible2 = widget_on_screen(omon.pid)
        second = subprocess.run([sys.executable, "-c", SECOND], capture_output=True, text=True, cwd=ROOT, env=env)
        result = {
            "registered_in_omonflow": registered,
            "visible_start_after_1st_after_2nd": [visible0, visible1, visible2],
            "toggle_works": visible0 and not visible1 and visible2,
            "second_process_same_shortcut": second.stdout.strip() or second.stderr.strip()[-300:],
        }
    finally:
        omon.terminate()
        omon.wait(timeout=10)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
