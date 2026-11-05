"""Real macOS insertion into a separate app (OMONFLOW_HW_TESTS=1, unlocked screen,
Accessibility granted to the process running the tests)."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin" or os.environ.get("OMONFLOW_HW_TESTS") != "1",
                                reason="needs macOS, a GUI session and OMONFLOW_HW_TESTS=1")


def _unlocked():
    import Quartz
    return not (Quartz.CGSessionCopyCurrentDictionary() or {}).get("CGSSessionScreenIsLocked", 0)


@pytest.fixture(scope="module")
def inserter():
    from omonflow.platform.macos.insert import MacInserter
    ins = MacInserter()
    if not ins.trusted() or not _unlocked():
        pytest.skip("Accessibility not granted to this process, or the screen is locked")
    return ins


class TargetApp:
    def __init__(self, tmp_path, field, title="OmonFlow insertion target"):
        self.out = tmp_path / f"{field}-{time.monotonic_ns()}.json"
        self.proc = subprocess.Popen([sys.executable, str(Path(__file__).with_name("insert_target.py")),
                                      str(self.out), field, title])
        end = time.monotonic() + 10
        while time.monotonic() < end and not self.frontmost():
            time.sleep(0.1)
        time.sleep(0.3)

    def frontmost(self):
        from AppKit import NSWorkspace
        from Foundation import NSDate, NSRunLoop
        NSRunLoop.currentRunLoop().runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.05))
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return app is not None and app.processIdentifier() == self.proc.pid

    def values(self):
        time.sleep(0.3)
        return json.loads(self.out.read_text())

    def close(self):
        self.proc.terminate()
        self.proc.wait(5)


TEXTS = {
    "ascii": "Deploy the Docker container to the Proxmox server.",
    "unicode": "Halo! Rapat jam 10.30 — café naïve ✓ 日本 🎙️",
}


@pytest.mark.parametrize("strategy", ["auto", "paste", "type"])
@pytest.mark.parametrize("kind", list(TEXTS))
def test_single_line(inserter, tmp_path, strategy, kind):
    app = TargetApp(tmp_path, "line")
    try:
        target = inserter.capture_target()
        assert target.pid == app.proc.pid
        res = inserter.insert(TEXTS[kind], target, strategy)
        assert res.ok, res
        assert app.values()["line"] == TEXTS[kind]
        print(f"\n{strategy}/{kind}: method={res.method} verified={res.verified} {res.ms} ms")
    finally:
        app.close()


def test_multiline_goes_into_text_area(inserter, tmp_path):
    app = TargetApp(tmp_path, "text")
    text = "Baris pertama.\nSecond line with GPU and VRAM.\nBaris ketiga."
    try:
        res = inserter.insert(text, inserter.capture_target(), "type")  # multi-line is pasted, never typed
        assert res.ok and res.method in ("paste", "accessibility")
        assert app.values()["text"] == text
    finally:
        app.close()


def test_clipboard_is_restored(inserter, tmp_path):
    from AppKit import NSPasteboard
    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    pb.setString_forType_("ISI CLIPBOARD ASLI", "public.utf8-plain-text")
    pb.setData_forType_(b"custom-bytes", "com.example.custom")
    app = TargetApp(tmp_path, "line")
    try:
        res = inserter.insert("ditempel", inserter.capture_target(), "paste", restore_clipboard=True)
        assert res.ok and res.ms < 250  # completes without waiting for the clipboard restore
        assert app.values()["line"] == "ditempel"
        time.sleep(0.5)  # the restore happens in the background
        assert pb.stringForType_("public.utf8-plain-text") == "ISI CLIPBOARD ASLI"
        assert bytes(pb.dataForType_("com.example.custom")) == b"custom-bytes"
    finally:
        app.close()


def test_password_field_is_refused(inserter, tmp_path):
    app = TargetApp(tmp_path, "password")
    try:
        target = inserter.capture_target()
        res = inserter.insert("secret", target)
        assert not res.ok and "password" in res.error.lower()
        assert app.values()["password"] == ""
    finally:
        app.close()


def test_focus_is_restored_to_the_captured_app(inserter, tmp_path):
    a = TargetApp(tmp_path, "line", "Target A")
    target = inserter.capture_target()
    b = TargetApp(tmp_path, "line", "Other app B")  # user switched away before Stop
    try:
        assert b.frontmost()
        res = inserter.insert("kembali ke A", target, "paste")
        assert res.ok
        assert a.values()["line"] == "kembali ke A" and b.values()["line"] == ""
    finally:
        a.close()
        b.close()


def test_target_quit_is_reported_and_nothing_is_typed_elsewhere(inserter, tmp_path):
    other = TargetApp(tmp_path, "line", "Bystander app")  # stands in for whatever comes to the front
    app = TargetApp(tmp_path, "line")
    target = inserter.capture_target()
    app.close()
    other_front = TargetApp.frontmost(other)  # macOS brings the bystander forward
    try:
        res = inserter.insert("tidak ada", target)
        assert res.outcome == "not_started" and "no longer running" in res.error
        assert other.values()["line"] == ""
    finally:
        other.close()
    del other_front


def test_never_inserts_when_another_app_took_focus(inserter, tmp_path):
    a = TargetApp(tmp_path, "line", "Target A")
    target = inserter.capture_target()
    b = TargetApp(tmp_path, "line", "Bystander B")
    try:
        # Simulate focus restore "succeeding" but focus moving again right before injection.
        inserter._ensure_focus = lambda t: None
        res = inserter.insert("salah alamat", target, "paste")
        assert res.outcome == "not_started" and "no longer the active app" in res.error
        assert b.values()["line"] == "" and a.values()["line"] == ""
    finally:
        del inserter._ensure_focus
        a.close()
        b.close()
