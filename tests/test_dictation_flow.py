"""Full dictation pipeline with fake audio, transcriber and inserter (SPEC §8, §5.3, §11)."""

import threading
import time

import pytest
from conftest import wait_until

from omonflow.app.orchestrator import Orchestrator, clean_transcript, is_silence_phrase
from omonflow.audio.stub import StubAudioBackend
from omonflow.core.states import State
from omonflow.insertion.base import InsertResult, Target
from omonflow.storage.history import History

FAST = {"success_hold": 5}


class FakeTranscriber:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def __call__(self, wav, timeout_s):
        self.calls.append(wav)
        r = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(r, Exception):
            raise r
        return r


class FakeInserter:
    def __init__(self, result=None, delay_before_start=0.0, delay_after_start=0.0):
        self.result = result or InsertResult("inserted", "accessibility", True)
        self.inserted: list[str] = []
        self.delay_before, self.delay_after = delay_before_start, delay_after_start
        self.started = threading.Event()

    def capture_target(self):
        return Target(4242, "com.example.editor", "Editor")

    def insert(self, text, target, strategy="auto", restore_clipboard=True, on_start=None, cancelled=None):
        time.sleep(self.delay_before)  # e.g. restoring focus
        if (cancelled and cancelled()) or (on_start and on_start() is False):
            return InsertResult("not_started", error="cancelled")
        self.started.set()
        time.sleep(self.delay_after)
        if self.result.ok:
            self.inserted.append(text)
        return self.result

    def permission(self):
        return {"accessibility": True}


class Audio(StubAudioBackend):
    def __init__(self, activity=True):
        super().__init__(activity_after_s=0.05)
        self.activity = activity

    def finalize_audio(self, timeout_ms=3000):
        rec = super().finalize_audio(timeout_ms)
        rec.update(wav=b"RIFF-fake-audio", activity=self.activity)
        return rec


def make(transcriber, inserter=None, history=None, activity=True):
    o = Orchestrator(Audio(activity), FAST, transcriber=transcriber, inserter=inserter or FakeInserter(),
                     history=(lambda: history), options=lambda: {"strategy": "auto", "model": "small"})
    o.seen = []
    o.state_changed.connect(o.seen.append)
    return o


def dictate(qapp, o):
    assert o.start()
    wait_until(qapp, lambda: o.state in (State.CAPTURE_READY, State.RECORDING))
    o.stop()


def test_success_inserts_into_captured_target_and_records_history(qapp, tmp_path):
    h = History(tmp_path / "h.db")
    ins = FakeInserter()
    o = make(FakeTranscriber([" Halo, ini tes dikte.  "]), ins, h)
    dictate(qapp, o)
    wait_until(qapp, lambda: o.state == State.IDLE, 5000)
    assert ins.inserted == ["Halo, ini tes dikte."]
    assert o.target.name == "Editor"
    e = h.list()[0]
    assert (e.text, e.app, e.outcome) == ("Halo, ini tes dikte.", "Editor", "inserted")
    assert "INSERTING" in o.seen and "SUCCESS" in o.seen and o.transcript is None and o._wav is None


def test_transcription_failure_keeps_audio_for_retry(qapp):
    tr = FakeTranscriber([RuntimeError("engine down"), "second try works"])
    ins = FakeInserter()
    o = make(tr, ins)
    dictate(qapp, o)
    wait_until(qapp, lambda: o.state == State.ERROR, 5000)
    assert o.retry_action == "transcribe" and o._wav == b"RIFF-fake-audio"
    o.primary()  # Retry
    wait_until(qapp, lambda: o.state == State.IDLE, 5000)
    assert tr.calls == [b"RIFF-fake-audio", b"RIFF-fake-audio"]
    assert ins.inserted == ["second try works"] and o._wav is None


def test_dismiss_releases_kept_audio(qapp):
    o = make(FakeTranscriber([RuntimeError("x")]))
    dictate(qapp, o)
    wait_until(qapp, lambda: o.state == State.ERROR, 5000)
    o.dismiss()
    assert o._wav is None and o.state == State.IDLE


@pytest.mark.parametrize("text,activity,inserted", [
    ("", True, False),
    ("[BLANK_AUDIO]", True, False),
    ("Thank you.", False, False),       # typical hallucination on silence
    ("Terima kasih.", False, False),
    ("Thank you.", True, True),          # but real speech can be "thank you"
    ("Halo", False, True),               # quiet speaker: activity missed, real text kept
])
def test_no_speech_handling(qapp, text, activity, inserted):
    ins = FakeInserter()
    o = make(FakeTranscriber([text]), ins, activity=activity)
    dictate(qapp, o)
    wait_until(qapp, lambda: o.state == State.IDLE, 5000)
    assert bool(ins.inserted) == inserted


def test_insertion_failure_keeps_transcript_for_copy(qapp, tmp_path):
    h = History(tmp_path / "h.db")
    ins = FakeInserter(InsertResult("not_started", error="Secure input is active"))
    o = make(FakeTranscriber(["penting sekali"]), ins, h)
    dictate(qapp, o)
    wait_until(qapp, lambda: o.state == State.ERROR, 5000)
    assert o.transcript == "penting sekali" and "Secure input" in o.error_text
    assert h.list()[0].outcome == "failed"
    o.primary()  # Copy
    from PySide6.QtGui import QGuiApplication
    assert QGuiApplication.clipboard().text() == "penting sekali"


def test_copied_without_permission_is_reported(qapp, tmp_path):
    h = History(tmp_path / "h.db")
    ins = FakeInserter(InsertResult("copied", "clipboard", None, "Accessibility permission is needed …"))
    o = make(FakeTranscriber(["teks"]), ins, h)
    dictate(qapp, o)
    wait_until(qapp, lambda: o.state == State.ERROR, 5000)
    assert "Accessibility" in o.error_text and o.transcript == "teks"
    assert h.list()[0].outcome == "copied"


def test_cancel_before_insertion_starts_never_inserts(qapp):
    ins = FakeInserter(delay_before_start=0.4)
    o = make(FakeTranscriber(["jangan diketik"]), ins)
    dictate(qapp, o)
    wait_until(qapp, lambda: o.state == State.INSERTING, 5000)
    assert o.cancel()
    assert o.state == State.ERROR and o.transcript == "jangan diketik"
    time.sleep(0.6)
    qapp.processEvents()
    assert ins.inserted == [] and o.state == State.ERROR


def test_no_cancel_once_insertion_started(qapp):
    ins = FakeInserter(delay_after_start=0.4)
    o = make(FakeTranscriber(["sedang diketik"]), ins)
    dictate(qapp, o)
    wait_until(qapp, lambda: ins.started.is_set(), 5000)
    assert not o.cancel()
    wait_until(qapp, lambda: o.state == State.IDLE, 5000)
    assert ins.inserted == ["sedang diketik"]


def test_cancel_during_transcription_discards(qapp):
    class Slow(FakeTranscriber):
        def __call__(self, wav, timeout_s):
            time.sleep(0.4)
            return "terlambat"
    ins = FakeInserter()
    o = make(Slow(["x"]), ins)
    dictate(qapp, o)
    wait_until(qapp, lambda: o.state == State.TRANSCRIBING, 5000)
    assert o.cancel() and o.state == State.IDLE and o._wav is None
    time.sleep(0.6)
    qapp.processEvents()
    assert ins.inserted == [] and o.state == State.IDLE


def test_history_off_stores_nothing(qapp, tmp_path):
    ins = FakeInserter()
    o = make(FakeTranscriber(["tidak disimpan"]), ins, history=None)
    dictate(qapp, o)
    wait_until(qapp, lambda: o.state == State.IDLE, 5000)
    assert ins.inserted == ["tidak disimpan"]


def test_transcript_cleanup():
    assert clean_transcript(" [BLANK_AUDIO]  halo   dunia (music) ") == "halo dunia"
    assert is_silence_phrase("Thank you.") and not is_silence_phrase("Thank you for the PR")
