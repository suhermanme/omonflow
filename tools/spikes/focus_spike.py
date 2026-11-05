#!/usr/bin/env python3
"""M02 focus spike (macOS): does clicking the OmonFlow widget steal focus?

Starts a target app with a focused text field, makes it frontmost, starts
OmonFlow (stub audio), then posts a real mouse click on the widget's mic
button. Passes if OmonFlow received the click (state INITIALIZING) while the
target app stayed active and its text field kept focus.

Runs twice: with the non-activating panel (product behavior) and, as a
control, with it disabled (OMONFLOW_DISABLE_NONACTIVATING=1).

Needs: permission for this terminal to post events (Accessibility).
Usage:  uv run python tools/spikes/focus_spike.py
"""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import Quartz
from AppKit import NSWorkspace
from Foundation import NSDate, NSRunLoop

ROOT = Path(__file__).resolve().parents[2]
WIDGET_POS = (300, 300)


def pump(seconds):
    NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(seconds))


def click(x, y):
    for kind in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
        ev = Quartz.CGEventCreateMouseEvent(None, kind, (x, y), Quartz.kCGMouseButtonLeft)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
        time.sleep(0.05)


def widget_bounds(pid):
    """On-screen bounds of OmonFlow's widget (the 312x72 Spectrum Pill window)."""
    wins = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID)
    for w in wins:
        b = w.get("kCGWindowBounds", {})
        if w.get("kCGWindowOwnerPID") == pid and int(b.get("Width", 0)) == 312 and int(b.get("Height", 0)) == 72:
            return b
    return None


def frontmost_pid():
    pump(0.2)
    app = NSWorkspace.sharedWorkspace().frontmostApplication()
    return app.processIdentifier() if app else None


def run(nonactivating: bool) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="omonflow-focus-"))
    (tmp / "settings.json").write_text(json.dumps({"widget_pos": list(WIDGET_POS), "visualizer": "spectrum-pill",
                                                   "notifications": False, "shortcut_enabled": False}))
    tlog = tmp / "target.log"
    env = {**os.environ, "OMONFLOW_CONFIG_DIR": str(tmp)}
    env.pop("QT_QPA_PLATFORM", None)
    if not nonactivating:
        env["OMONFLOW_DISABLE_NONACTIVATING"] = "1"
    # OmonFlow first, then the target, which activates itself (macOS 14+
    # no longer lets one process force-activate another).
    omon = subprocess.Popen([sys.executable, "-m", "omonflow", "--log-level", "DEBUG"], env=env, cwd=ROOT,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3.0)
    target = subprocess.Popen([sys.executable, str(Path(__file__).with_name("focus_target.py")), str(tlog)])
    time.sleep(2.5)
    before = frontmost_pid()
    b = widget_bounds(omon.pid)
    if b is None:  # safety: never click blindly on the user's screen
        omon.send_signal(signal.SIGINT)
        target.terminate()
        raise SystemExit("OmonFlow widget window not found on screen; not clicking.")
    pt = (b["X"] + 32, b["Y"] + 36)  # Spectrum Pill primary button centre
    mark = time.monotonic()
    click(*pt)
    time.sleep(1.0)
    after = frontmost_pid()
    click(*pt)  # Stop, so the stub pipeline finishes
    log_path = tmp / "logs" / "omonflow.log"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and "state IDLE (done)" not in log_path.read_text():
        time.sleep(0.2)
    for proc in (omon, target):
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    events = [line.split(" ", 1)[1].strip() for line in tlog.read_text().splitlines()
              if float(line.split(" ", 1)[0]) >= mark]
    olog = log_path.read_text()
    widget_report = next((l.split("macOS widget window: ", 1)[1] for l in olog.splitlines()
                          if "macOS widget window:" in l), "n/a")
    return {
        "nonactivating": nonactivating,
        "target_frontmost_before": before == target.pid,
        "target_frontmost_after_click": after == target.pid,
        "click_reached_omonflow": "state INITIALIZING (start)" in olog,
        "stop_click_reached_omonflow": "state FINALIZING (stop)" in olog,
        "target_events_after_click": events,
        "widget_bounds": dict(b),
        "requested_pos": WIDGET_POS,
        "widget_window": widget_report,
        "omonflow_mouse_log": [l.split(": ", 1)[-1] for l in olog.splitlines() if "mouse " in l][:4],
    }


def main():
    if not Quartz.CGPreflightPostEventAccess():
        sys.exit("This terminal may not post mouse events. Grant it Accessibility in System Settings.")
    results = [run(True), run(False)]
    for r in results:
        print(json.dumps(r, indent=2))
    prod = results[0]
    ok = (prod["click_reached_omonflow"] and prod["target_frontmost_before"] and prod["target_frontmost_after_click"]
          and not any("Inactive" in e or "->NoneType" in e for e in prod["target_events_after_click"]))
    print("\nRESULT:", "PASS: widget click did not take focus" if ok else "FAIL: see details above")


if __name__ == "__main__":
    main()
