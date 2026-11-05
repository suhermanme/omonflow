#!/usr/bin/env python3
"""Measure OmonFlow's CPU use while idle (widget visible, then hidden).

Usage: uv run python tools/spikes/idle_cpu.py [seconds] [visualizer]
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def cpu_seconds(pid):
    out = subprocess.run(["ps", "-o", "time=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    parts = out.replace("-", ":").split(":")  # [[dd-]hh:]mm:ss.cc
    secs = 0.0
    for part in parts:
        secs = secs * 60 + float(part)
    return secs


def measure(pid, seconds):
    c0, t0 = cpu_seconds(pid), time.monotonic()
    time.sleep(seconds)
    c1, t1 = cpu_seconds(pid), time.monotonic()
    return round(100 * (c1 - c0) / (t1 - t0), 2)


def widget_bounds(pid):
    import Quartz
    wins = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID)
    for w in wins:
        b = w.get("kCGWindowBounds", {})
        if w.get("kCGWindowOwnerPID") == pid and int(b.get("Width", 0)) in (312, 220):
            return b
    return None


def click(x, y):
    import Quartz
    for kind in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
        ev = Quartz.CGEventCreateMouseEvent(None, kind, (x, y), Quartz.kCGMouseButtonLeft)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        time.sleep(0.05)


def main():
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 30
    vis = sys.argv[2] if len(sys.argv) > 2 else "spectrum-pill"
    tmp = Path(tempfile.mkdtemp(prefix="omonflow-cpu-"))
    (tmp / "settings.json").write_text(json.dumps({"visualizer": vis, "shortcut_enabled": False}))
    env = {**os.environ, "OMONFLOW_CONFIG_DIR": str(tmp)}
    env.pop("QT_QPA_PLATFORM", None)
    p = subprocess.Popen([sys.executable, "-m", "omonflow"], env=env, cwd=ROOT,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(4)
        visible = measure(p.pid, seconds)
        recording = None
        b = widget_bounds(p.pid)
        if b is not None:  # start a stub recording by clicking the mic (no real mic in M02)
            click(b["X"] + b["Width"] / 2 if vis != "spectrum-pill" else b["X"] + 32, b["Y"] + b["Height"] / 2)
            time.sleep(1.5)
            recording = measure(p.pid, seconds)
            click(b["X"] + b["Width"] / 2 if vis != "spectrum-pill" else b["X"] + 32, b["Y"] + b["Height"] / 2)
            time.sleep(4)
        subprocess.run([sys.executable, "-m", "omonflow", "--toggle-widget"], env=env, cwd=ROOT,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1)
        hidden = measure(p.pid, seconds)
    finally:
        p.terminate()
        p.wait(timeout=10)
    print(json.dumps({"visualizer": vis, "seconds": seconds,
                      "cpu_percent_of_one_core_widget_visible_idle": visible,
                      "cpu_percent_of_one_core_recording_animation": recording,
                      "cpu_percent_of_one_core_widget_hidden": hidden}))


if __name__ == "__main__":
    main()
