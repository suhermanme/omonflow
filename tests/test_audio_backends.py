"""Same contract for the Rust core and the Python fallback (fake microphones)."""

import io
import time
import wave

import pytest
from fakes import fake_opener

from omonflow.audio.rust_backend import RustAudio
from omonflow.audio.sounddevice_backend import SoundDeviceAudio
from omonflow.core import interfaces as I


def make(kind, **spec):
    if kind == "rust":
        return RustAudio(fake=spec)
    return SoundDeviceAudio(open_stream=fake_opener(spec))


BACKENDS = ["rust", "python"]


def wait_event(a, kind, timeout=3.0):
    end = time.monotonic() + timeout
    seen = []
    while time.monotonic() < end:
        seen += [e["type"] for e in a.poll_events()]
        if kind in seen:
            return seen
        time.sleep(0.02)
    raise AssertionError(f"{kind} not seen, got {seen}")


@pytest.mark.parametrize("kind", BACKENDS)
def test_lifecycle_ready_activity_finalize(kind):
    a = make(kind, warmup_zero_ms=150, speech_after_ms=400)
    assert a.state() == "idle" and not a.stream_open()
    a.start_capture()
    info = a.wait_until_ready(3000)
    assert 140 <= info["warmup_ms"] <= 170, info  # CAPTURE_READY only after real signal
    assert not info["silent_warning"]
    wait_event(a, "activity")
    a.stop_capture()
    rec = a.finalize_audio()
    w = wave.open(io.BytesIO(rec["wav"]))
    assert w.getframerate() == 16000 and w.getnchannels() == 1
    assert abs(w.getnframes() / 16000 - rec["duration_s"]) < 0.01
    assert 140 <= rec["trimmed_ms"] <= 170  # warm-up zeros trimmed, nothing after them
    assert rec["activity"] and rec["dropped"] == 0
    assert a.state() == "idle" and not a.stream_open()
    a.shutdown()


@pytest.mark.parametrize("kind", BACKENDS)
def test_no_audio_lost_after_first_callback(kind):
    a = make(kind, warmup_zero_ms=0, speech_after_ms=None)
    a.start_capture()
    a.wait_until_ready(3000)
    time.sleep(0.6)
    a.stop_capture()
    rec = a.finalize_audio()
    # Every captured device sample becomes output: 48k → 16k exactly by ratio.
    assert abs(rec["captured_samples"] / 3 - rec["duration_s"] * 16000) <= 1
    assert rec["captured_samples"] >= 48000 * 0.55
    a.shutdown()


@pytest.mark.parametrize("kind", BACKENDS)
def test_silent_device_ready_with_warning(kind):
    a = make(kind, warmup_zero_ms=10_000)
    a.start_capture()
    info = a.wait_until_ready(3000)
    assert info["silent_warning"]
    a.cancel_capture()
    a.shutdown()


@pytest.mark.parametrize("kind", BACKENDS)
def test_bluetooth_headset_profile_switch(kind):
    """Seen with a WH-1000XM5: a slow open (up to 2.4 s) followed by ~1.5 s of exact
    zeros while macOS switches the headset to its call profile. "Speak now" must wait
    for real signal (first words were lost with a 300 ms cap), and a slow but working
    open must not become a timeout."""
    a = make(kind, open_delay_ms=1500, warmup_zero_ms=1500, speech_after_ms=1700)
    a.start_capture()
    info = a.wait_until_ready(2000)  # deadline passes while the device is warming
    assert not info["silent_warning"]
    assert 1450 <= info["warmup_ms"] <= 1600, info
    a.cancel_capture()
    a.shutdown()


@pytest.mark.parametrize("kind", BACKENDS)
def test_cancel_discards_and_closes(kind):
    a = make(kind)
    a.start_capture()
    a.wait_until_ready(3000)
    a.cancel_capture()
    assert a.state() == "idle" and not a.stream_open()
    with pytest.raises(I.AudioError):
        a.finalize_audio()
    a.shutdown()


@pytest.mark.parametrize("kind", BACKENDS)
def test_duplicate_start_rejected(kind):
    a = make(kind)
    a.start_capture()
    with pytest.raises(I.SessionActive):
        a.start_capture()
    a.cancel_capture()
    a.shutdown()


@pytest.mark.parametrize("kind", BACKENDS)
def test_hung_open_times_out_retires_and_retry_works(kind):
    a = make(kind, hang_ms=1500, hang_times=1)
    a.start_capture()
    t = time.perf_counter()
    with pytest.raises(I.DeviceTimeout):
        a.wait_until_ready(300)
    elapsed = (time.perf_counter() - t) * 1000
    assert elapsed < 300 + 100, elapsed  # acceptance: ERROR within timeout + 100 ms
    assert a.retired_count() == 1 and a.state() == "idle"
    a.start_capture()  # Retry with a fresh worker
    assert a.wait_until_ready(2000)["warmup_ms"] is not None
    a.cancel_capture()
    t = time.perf_counter()
    a.shutdown()  # retired worker still stuck: must not block > 2 s
    assert time.perf_counter() - t < 2.0


@pytest.mark.parametrize("kind", BACKENDS)
def test_failed_open(kind):
    a = make(kind, fail_open="no permission")
    a.start_capture()
    with pytest.raises(I.CaptureFailed):
        a.wait_until_ready(2000)
    assert a.state() == "idle"
    a.shutdown()


@pytest.mark.parametrize("kind", BACKENDS)
def test_device_loss_keeps_partial_audio(kind):
    a = make(kind, lose_after_ms=500)
    a.start_capture()
    a.wait_until_ready(3000)
    wait_event(a, "device_lost")
    rec = a.finalize_audio()
    assert rec["device_lost"] or "device_lost" in rec["events"]
    assert rec["duration_s"] >= 0.45
    assert not a.stream_open()
    a.shutdown()


@pytest.mark.parametrize("kind", BACKENDS)
def test_repeated_cycles_leave_nothing_open(kind):
    a = make(kind, open_delay_ms=1, speech_after_ms=None)
    for i in range(60):
        a.start_capture()
        a.wait_until_ready(2000)
        if i % 3 == 0:
            a.cancel_capture()
        else:
            a.stop_capture()
            a.finalize_audio()
        assert not a.stream_open() and a.state() == "idle"
    assert a.retired_count() == 0
    a.shutdown()


def test_bounded_session_length():
    a = RustAudio(fake={"speech_after_ms": None}, max_seconds=1)
    a.start_capture()
    a.wait_until_ready(2000)
    time.sleep(1.5)
    a.stop_capture()
    rec = a.finalize_audio()
    assert rec["overflowed"] and rec["duration_s"] <= 1.0 + 1e-6
