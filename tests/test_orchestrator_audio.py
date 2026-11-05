"""Orchestrator with the real Rust core and a fake microphone."""

import time

import pytest
from conftest import wait_until

from omonflow.app.orchestrator import Orchestrator
from omonflow.audio.rust_backend import RustAudio
from omonflow.core.states import State

FAST = {"transcribe": 5, "focus_restore": 5, "insert": 5, "success_hold": 5}


def orch(fake, timeout_ms=1500, permission=None):
    o = Orchestrator(RustAudio(fake=fake), FAST, ready_timeout_ms=timeout_ms, permission_check=permission)
    o.seen = []
    o.state_changed.connect(o.seen.append)
    return o


def test_states_follow_real_signal(qapp):
    o = orch({"warmup_zero_ms": 150, "speech_after_ms": 500})
    o.start()
    assert o.state == State.INITIALIZING
    wait_until(qapp, lambda: o.state == State.CAPTURE_READY)
    t_ready = time.monotonic()
    wait_until(qapp, lambda: o.state == State.RECORDING)
    assert time.monotonic() - t_ready > 0.25  # RECORDING waits for speech, not the device
    o.stop()
    wait_until(qapp, lambda: o.state == State.IDLE, 5000)
    assert o.seen[:4] == ["INITIALIZING", "CAPTURE_READY", "RECORDING", "FINALIZING"]
    # ~0.6 s captured minus 150 ms trimmed warm-up
    assert o.last_recording["duration_s"] > 0.35 and "wav" not in o.last_recording  # audio not retained
    assert not o.audio.stream_open()
    o.shutdown()


def test_stop_before_speech_is_allowed(qapp):
    o = orch({"speech_after_ms": None})
    o.start()
    wait_until(qapp, lambda: o.state == State.CAPTURE_READY)
    o.stop()
    wait_until(qapp, lambda: o.state == State.IDLE, 5000)
    o.shutdown()


def test_hung_device_errors_then_retry_succeeds(qapp):
    o = orch({"hang_ms": 2000, "hang_times": 1}, timeout_ms=300)
    o.start()
    wait_until(qapp, lambda: o.state == State.ERROR, 2000)
    assert "not responding" in o.error_text and o.retry_action == "start"
    o.primary()  # Retry
    wait_until(qapp, lambda: o.state == State.CAPTURE_READY, 3000)
    o.cancel()
    o.shutdown()


def test_cancel_while_initializing(qapp):
    o = orch({"open_delay_ms": 400})
    o.start()
    assert o.cancel()
    assert o.state == State.IDLE
    for _ in range(60):
        qapp.processEvents()
        time.sleep(0.01)
    assert o.state == State.IDLE  # late "ready" from the cancelled open is ignored
    wait_until(qapp, lambda: not o.audio.stream_open())
    assert o.start()  # a new session can start right away
    wait_until(qapp, lambda: o.state == State.CAPTURE_READY)
    o.cancel()
    o.shutdown()


def test_device_loss_finalizes_partial(qapp):
    o = orch({"lose_after_ms": 600, "speech_after_ms": 200})
    notices = []
    o.notice.connect(lambda sev, text: notices.append(text))
    o.start()
    wait_until(qapp, lambda: o.state in (State.FINALIZING, State.TRANSCRIBING, State.INSERTING,
                                         State.SUCCESS, State.IDLE), 4000)
    wait_until(qapp, lambda: o.state == State.IDLE, 5000)
    assert any("disconnected" in n for n in notices)
    assert o.last_recording["duration_s"] >= 0.5
    o.shutdown()


@pytest.mark.parametrize("perm", ["denied", "restricted"])
def test_permission_denied_never_opens_microphone(qapp, perm):
    o = orch({}, permission=lambda: perm)
    assert not o.start()
    assert o.state == State.ERROR and "Privacy" in o.error_text
    assert o.audio.state() == "idle" and not o.audio.stream_open()
    o.shutdown()


def test_mic_test_uses_and_releases_device(qapp):
    o = orch({"speech_after_ms": 100})
    results = []
    o.mic_test_done.connect(results.append)
    assert o.mic_test(0.5)
    assert not o.start()  # dictation blocked during the test
    wait_until(qapp, lambda: results, 4000)
    assert results[0]["ok"] and results[0]["recording"]["activity"]
    assert "wav" not in results[0]["recording"]
    assert not o.audio.stream_open()
    o.shutdown()
