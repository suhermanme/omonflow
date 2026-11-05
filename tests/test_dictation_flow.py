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

FAST = {"success_hold": 5, "success_copy_hold": 300}


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


def make(transcriber, inserter=None, history=None, activity=True, refiner=None):
    o = Orchestrator(Audio(activity), FAST, transcriber=transcriber, inserter=inserter or FakeInserter(),
                     history=(lambda: history), options=lambda: {"strategy": "auto", "model": "small"},
                     refiner=refiner)
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
    assert o.error_label == "Copied — ⌘V"  # the widget says what happened, not just "Error"
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


def test_unverifiable_insertion_offers_copy_for_a_moment(qapp):
    ins = FakeInserter(InsertResult("inserted", "paste", None))  # e.g. Chrome: cannot confirm
    o = make(FakeTranscriber(["teks panjang dari Chrome"]), ins)
    dictate(qapp, o)
    wait_until(qapp, lambda: o.state == State.SUCCESS, 5000)
    assert o.transcript == "teks panjang dari Chrome" and o.widget_label == "Done · click to copy"
    o.primary()  # one click copies
    from PySide6.QtGui import QGuiApplication
    assert QGuiApplication.clipboard().text() == "teks panjang dari Chrome"
    wait_until(qapp, lambda: o.state == State.IDLE, 5000)
    assert o.transcript is None and o.widget_label is None


def test_verified_insertion_clears_text_immediately(qapp):
    o = make(FakeTranscriber(["ok"]), FakeInserter(InsertResult("inserted", "accessibility", True)))
    dictate(qapp, o)
    wait_until(qapp, lambda: o.state == State.SUCCESS, 5000)
    assert o.transcript is None and o.widget_label is None


# ------------------------------------------------------------------ refinement (M07, SPEC §5.3 / §8 / §10)
class FakeRefiner:
    def __init__(self, mode, result=None, delay_s=0.0):
        self._mode, self.result, self.delay_s = mode, result, delay_s
        self.calls = []

    def mode(self):
        return self._mode

    def refine(self, text, mode):
        from omonflow.refinement.refiner import local_refine
        self.calls.append((text, mode))
        if mode != "ai":
            return local_refine(text, mode)
        time.sleep(self.delay_s)
        return self.result


def _ai(text, reason=""):
    from omonflow.refinement.refiner import RefineResult
    return RefineResult(text, "clean" if reason else "ai", "ai", 300.0, reason)


def test_clean_mode_is_applied_without_a_refining_state(qapp, tmp_path):
    h = History(tmp_path / "h.db")
    ins = FakeInserter()
    o = make(FakeTranscriber(["um so the the build is green"]), ins, h, refiner=FakeRefiner("clean"))
    dictate(qapp, o)
    wait_until(qapp, lambda: o.state == State.IDLE, 5000)
    assert ins.inserted == ["So the build is green."]
    assert "REFINING" not in o.seen
    e = h.list()[0]
    assert (e.text, e.raw_text, e.outcome) == ("So the build is green.", "um so the the build is green", "inserted")


def test_raw_mode_inserts_the_transcript(qapp):
    ins = FakeInserter()
    o = make(FakeTranscriber(["um the the test"]), ins, refiner=FakeRefiner("raw"))
    dictate(qapp, o)
    wait_until(qapp, lambda: o.state == State.IDLE, 5000)
    assert ins.inserted == ["um the the test"]


def test_ai_refinement_goes_through_refining(qapp, tmp_path):
    h = History(tmp_path / "h.db")
    ins = FakeInserter()
    o = make(FakeTranscriber(["saya cek dulu then I deploy"]), ins, h,
             refiner=FakeRefiner("ai", _ai("Saya cek dulu, then I deploy.")))
    notices = []
    o.notice.connect(lambda sev, t: notices.append(sev))
    dictate(qapp, o)
    wait_until(qapp, lambda: o.state == State.IDLE, 5000)
    assert ins.inserted == ["Saya cek dulu, then I deploy."]
    i = o.seen.index("REFINING")
    assert o.seen[i - 1] == "TRANSCRIBING" and o.seen[i + 1] == "INSERTING"
    assert "warning" not in notices
    assert h.list()[0].raw_text == "saya cek dulu then I deploy"


def test_ai_failure_inserts_clean_text_with_a_notice(qapp):
    ins = FakeInserter()
    o = make(FakeTranscriber(["um the the server is down"]), ins,
             refiner=FakeRefiner("ai", _ai("The server is down.", "No answer within 10 s.")))
    notices = []
    o.notice.connect(lambda sev, t: notices.append((sev, t)))
    dictate(qapp, o)
    wait_until(qapp, lambda: o.state == State.IDLE, 5000)
    assert ins.inserted == ["The server is down."]
    assert any(sev == "warning" and "No answer within 10 s." in t for sev, t in notices)
    assert o.sm.history[[e for _s, e, _n in o.sm.history].index("refine_failed")][2] == State.INSERTING


def test_cancel_during_refinement_keeps_raw_text_and_inserts_nothing(qapp, tmp_path):
    h = History(tmp_path / "h.db")
    ins = FakeInserter()
    o = make(FakeTranscriber(["um the the raw words"]), ins, h,
             refiner=FakeRefiner("ai", _ai("The raw words."), delay_s=0.4))
    dictate(qapp, o)
    wait_until(qapp, lambda: o.state == State.REFINING, 5000)
    assert o.can_cancel() and o.cancel()
    assert o.state == State.ERROR and o.transcript == "um the the raw words"
    qapp.processEvents()
    time.sleep(0.6)  # the late AI result must be ignored
    qapp.processEvents()
    assert o.state == State.ERROR and ins.inserted == []
    e = h.list()[0]
    assert (e.text, e.outcome) == ("um the the raw words", "cancelled")
