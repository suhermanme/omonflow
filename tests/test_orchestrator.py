import pytest
from conftest import wait_until

from omonflow.app.orchestrator import Orchestrator
from omonflow.audio.stub import StubAudioBackend
from omonflow.core.states import State

FAST = {"init": 5, "finalize": 5, "transcribe": 5, "focus_restore": 30, "insert": 5, "success_hold": 5}


@pytest.fixture
def orch(qapp):
    o = Orchestrator(StubAudioBackend(), FAST)
    seen = []
    o.state_changed.connect(seen.append)
    o.seen = seen
    return o


def test_full_stub_flow(qapp, orch):
    assert orch.start()
    wait_until(qapp, lambda: orch.state == State.RECORDING)  # stub level → activity
    assert orch.stop()
    wait_until(qapp, lambda: orch.state == State.IDLE)
    assert orch.seen == ["INITIALIZING", "CAPTURE_READY", "RECORDING", "FINALIZING",
                         "TRANSCRIBING", "INSERTING", "SUCCESS", "IDLE"]
    assert orch.audio.state() == "idle"


def test_duplicate_start_rejected(qapp, orch):
    assert orch.start()
    assert not orch.start()
    assert orch.audio.start_calls == 1


@pytest.mark.parametrize("target", [State.INITIALIZING, State.CAPTURE_READY, State.RECORDING])
def test_cancel_during_capture_discards(qapp, orch, target):
    orch.start()
    wait_until(qapp, lambda: orch.state == target)
    assert orch.cancel()
    assert orch.state == State.IDLE and orch.transcript is None
    assert orch.audio.state() == "idle"


def test_cancel_during_transcribing_discards(qapp, qtbot=None):
    o = Orchestrator(StubAudioBackend(), {**FAST, "transcribe": 400})
    o.start()
    wait_until(qapp, lambda: o.state in (State.CAPTURE_READY, State.RECORDING))
    o.stop()
    wait_until(qapp, lambda: o.state == State.TRANSCRIBING)
    assert o.cancel()
    assert o.state == State.IDLE and o.transcript is None
    qapp.processEvents()
    wait_until(qapp, lambda: True)
    # The cancelled transcription timer must not revive the session.
    from PySide6.QtCore import QThread
    for _ in range(50):
        qapp.processEvents(); QThread.msleep(10)
    assert o.state == State.IDLE


def test_cancel_before_insertion_starts_keeps_transcript(qapp):
    o = Orchestrator(StubAudioBackend(), {**FAST, "focus_restore": 400})
    o.start()
    wait_until(qapp, lambda: o.state in (State.CAPTURE_READY, State.RECORDING))
    o.stop()
    wait_until(qapp, lambda: o.state == State.INSERTING)
    assert o.can_cancel()
    assert o.cancel()
    assert o.state == State.ERROR and o.transcript


def test_no_cancel_once_insertion_started(qapp):
    o = Orchestrator(StubAudioBackend(), {**FAST, "insert": 400})
    o.start()
    wait_until(qapp, lambda: o.state in (State.CAPTURE_READY, State.RECORDING))
    o.stop()
    wait_until(qapp, lambda: o.insertion_started)
    assert not o.can_cancel() and not o.cancel()
    wait_until(qapp, lambda: o.state in (State.SUCCESS, State.IDLE))


def test_copy_from_error(qapp):
    o = Orchestrator(StubAudioBackend(), {**FAST, "focus_restore": 400})
    o.start()
    wait_until(qapp, lambda: o.state in (State.CAPTURE_READY, State.RECORDING))
    o.stop()
    wait_until(qapp, lambda: o.state == State.INSERTING)
    o.cancel()
    o.primary()  # in ERROR with a transcript → copy
    from PySide6.QtGui import QGuiApplication
    assert QGuiApplication.clipboard().text() == o.transcript
    o.dismiss()
    assert o.state == State.IDLE and o.transcript is None
