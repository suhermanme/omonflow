#!/usr/bin/env python3
"""M05 insertion matrix on real macOS apps.

Safety: only apps that are NOT already running are used; each opens only a
temporary file created here; apps this script started are quit at the end.
Every insertion goes through MacInserter's guards (target must be the live
frontmost app right before injection).

Usage: uv run python tools/e2e/insertion_matrix.py [--apps TextEdit,Safari,Terminal,iTerm]
"""

import argparse
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from AppKit import NSWorkspace  # noqa: E402

from omonflow.platform.macos.insert import MacInserter  # noqa: E402

TEXTS = {
    "unicode": "Halo, rapat jam 10.30 — café ✓ GPU VRAM 🎙️",
    "multiline": "Baris satu.\nLine two with Docker.",
}
STRATEGIES = ["auto", "paste", "type"]
BUNDLES = {"TextEdit": "com.apple.TextEdit", "Safari": "com.apple.Safari", "Terminal": "com.apple.Terminal",
           "iTerm": "com.googlecode.iterm2"}


def running(bundle):
    return any(a.bundleIdentifier() == bundle for a in NSWorkspace.sharedWorkspace().runningApplications())


def wait_front(ins, bundle, timeout=15):
    """Wait for the app to run, then bring it forward (apps launched from a
    background process stay behind under macOS's cooperative activation)."""
    from AppKit import NSRunningApplication

    from omonflow.insertion.base import Target

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        apps = NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle)
        if apps:
            time.sleep(1.0)  # let it open the document window
            ins._ensure_focus(Target(apps[0].processIdentifier(), bundle, None))
            break
        time.sleep(0.2)
    while time.monotonic() < end:
        pid = ins.front_pid()
        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid) if pid else None
        if app is not None and app.bundleIdentifier() == bundle:
            time.sleep(1.0)
            return True
        time.sleep(0.2)
    return False


def quit_app(name):
    subprocess.run(["pkill", "-x", name], capture_output=True)
    time.sleep(1.5)


def focused_value(ins):
    pid = ins.front_pid()
    el = ins._focused_element(pid) if pid else None
    return ins._value(el) if el is not None else None


def run_doc_app(ins, app, tmp, opener):
    """TextEdit / Safari: one fresh document per case, read back via Accessibility."""
    results = []
    for strategy in STRATEGIES:
        for kind, text in TEXTS.items():
            path = opener(tmp, f"{app}-{strategy}-{kind}")
            subprocess.run(["open", "-a", app, str(path)])
            if not wait_front(ins, BUNDLES[app]):
                results.append({"app": app, "strategy": strategy, "text": kind, "result": "app did not open"})
                continue
            target = ins.capture_target()
            before = focused_value(ins) or ""
            res = ins.insert(text, target, strategy)
            time.sleep(0.5)
            after = focused_value(ins)
            got = None if after is None else after.replace(before, "", 1) if before else after
            want = text.replace("\n", "\r") if app == "Safari" else text
            correct = None if got is None else (got.replace("\r", "\n") == text or got == want)
            results.append({"app": app, "strategy": strategy, "text": kind, "outcome": res.outcome,
                            "method": res.method, "verified": res.verified, "readback_correct": correct,
                            "ms": res.ms, "error": res.error})
            print(results[-1], flush=True)
    return results


def textedit_doc(tmp, name):
    p = tmp / f"{name}.txt"
    p.write_text("")
    return p


def safari_doc(tmp, name):
    p = tmp / f"{name}.html"
    p.write_text("<!doctype html><title>OmonFlow test</title><textarea id=t autofocus rows=6 cols=60></textarea>"
                 "<script>document.getElementById('t').focus()</script>")
    return p


def run_terminal(ins, app, tmp):
    """Terminal apps: a script reads one line and writes it to a file."""
    results = []
    for strategy in STRATEGIES:
        text = TEXTS["unicode"]
        out = tmp / f"{app}-{strategy}.out"
        script = tmp / f"{app}-{strategy}.command"
        script.write_text(f"#!/bin/bash\nIFS= read -r line\nprintf '%s' \"$line\" > '{out}'\n")
        script.chmod(0o755)
        subprocess.run(["open", "-a", app, str(script)])
        if not wait_front(ins, BUNDLES[app]):
            results.append({"app": app, "strategy": strategy, "result": "app did not open"})
            continue
        time.sleep(1.0)
        target = ins.capture_target()
        res = ins.insert(text + "\n", target, strategy)
        time.sleep(1.0)
        got = out.read_text() if out.exists() else None
        results.append({"app": app, "strategy": strategy, "text": "unicode + Return", "outcome": res.outcome,
                        "method": res.method, "verified": res.verified, "readback_correct": got == text,
                        "readback": got, "ms": res.ms, "error": res.error})
        print(results[-1], flush=True)
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apps", default="TextEdit,Safari,Terminal,iTerm")
    args = ap.parse_args()
    ins = MacInserter()
    if not ins.trusted():
        sys.exit("Accessibility permission is required for this process.")
    tmp = Path(tempfile.mkdtemp(prefix="omonflow-matrix-"))
    report = {"date": time.strftime("%Y-%m-%d %H:%M"), "skipped": {}, "results": []}
    for app in args.apps.split(","):
        bundle = BUNDLES[app]
        if running(bundle):
            report["skipped"][app] = "already running (left alone)"
            continue
        if not Path(f"/Applications/{app}.app").exists() and not Path(f"/System/Applications/{app}.app").exists() \
                and not Path(f"/System/Applications/Utilities/{app}.app").exists():
            report["skipped"][app] = "not installed"
            continue
        try:
            if app == "TextEdit":
                report["results"] += run_doc_app(ins, app, tmp, textedit_doc)
            elif app == "Safari":
                report["results"] += run_doc_app(ins, app, tmp, safari_doc)
            else:
                report["results"] += run_terminal(ins, app, tmp)
        finally:
            quit_app(app if app != "iTerm" else "iTerm2")
    out = ROOT / "docs" / "m05" / "insertion_matrix.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print("saved", out)


if __name__ == "__main__":
    main()
