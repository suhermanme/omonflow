"""Python audio fallback (sounddevice/PortAudio) behind the AudioBackend API.

Same lifecycle and rules as the Rust core (on-demand, warm-up handling,
retire-on-timeout), but the PortAudio callback runs Python code and needs the
GIL. It is the degraded fallback and the M03 comparison baseline.
"""

from __future__ import annotations

import collections
import threading
import time

import numpy as np

from omonflow.core import interfaces as I

from .session import Params, Session, resample, wav_s16

TARGET_RATE = 16000


class _Run:
    """One capture attempt (one worker thread)."""

    def __init__(self, open_stream, params: Params) -> None:
        self.open_stream = open_stream  # callable(callback) -> (stream, rate, name)
        self.params = params
        self.t0 = time.perf_counter()
        self.cv = threading.Condition()
        self.phase = "opening"
        self.queue: collections.deque = collections.deque()
        self.stop = threading.Event()
        self.cancel = threading.Event()
        self.stream_open = False
        self.session: Session | None = None
        self.timing: dict = {}
        self.events: collections.deque = collections.deque(maxlen=64)
        self.fail: tuple[type, str] | None = None
        self.overflows = 0
        self.level = 0.0
        self.device_lost = False
        self.device_name = ""
        self.rate = 0
        self.silent_warning = False
        self.thread = threading.Thread(target=self._run, name="omonflow-sd-capture", daemon=True)
        self.thread.start()

    def ms(self) -> float:
        return round((time.perf_counter() - self.t0) * 1000, 1)

    def _set(self, phase: str) -> None:
        with self.cv:
            self.phase = phase
            self.cv.notify_all()

    def _callback(self, indata, frames, time_info, status) -> None:
        # Runs on PortAudio's thread and needs the GIL: keep it tiny.
        if status and status.input_overflow:
            self.overflows += 1
        self.queue.append(indata.mean(axis=1) if indata.shape[1] > 1 else indata[:, 0].copy())

    def _run(self) -> None:
        try:
            stream, rate, name = self.open_stream(self._callback)
        except I.AudioError as e:
            self.fail = (type(e), str(e))
            self._set("failed")
            return
        except Exception as e:  # noqa: BLE001 - PortAudio errors
            self.fail = (I.CaptureFailed, str(e))
            self._set("failed")
            return
        self.stream_open = True
        if self.cancel.is_set():
            self._close(stream)
            self._set("stopped")
            return
        self.params.rate = rate
        self.session = Session(self.params)
        self.device_name, self.rate = name, rate
        self.timing["open_ms"] = self.ms()
        self._set("warming")
        while True:
            if not getattr(stream, "active", True) and not self.stop.is_set():
                self.device_lost = True  # PortAudio stopped the stream (device gone)
            if self.stop.is_set() or self.cancel.is_set() or self.device_lost:
                self._close(stream)
                self.timing["stream_closed_ms"] = self.ms()
                if self.cancel.is_set():
                    self.session = None
                else:
                    self._drain()
                if self.device_lost and not self.cancel.is_set():
                    self.events.append({"type": "device_lost", "detail": "input device disconnected"})
                self._set("stopped")
                return
            self._drain()
            time.sleep(0.005)

    def _close(self, stream) -> None:
        try:
            stream.stop()
            stream.close()
        finally:
            self.stream_open = False

    def _drain(self) -> None:
        while self.queue:
            block = self.queue.popleft()
            if "first_audio_ms" not in self.timing:
                self.timing["first_audio_ms"] = self.ms()
            ready, active = self.session.push(block)
            self.level = min(1.0, self.session.last_rms * 6.0)
            if ready:
                s = self.session
                self.timing["ready_ms"] = self.ms()
                self.timing["warmup_ms"] = round(s.ready_at * 1000 / s.p.rate, 1)
                self.silent_warning = s.silent_warning
                self.events.append({"type": "ready", "detail": "silent" if s.silent_warning else ""})
                self._set("ready")
            if active:
                self.timing["activity_ms"] = self.ms()
                self.events.append({"type": "activity", "detail": ""})
                self._set("active")


def _default_open(device):
    def open_stream(callback):
        import sounddevice as sd

        info = sd.query_devices(device, "input") if device is not None else sd.query_devices(kind="input")
        rate = int(info["default_samplerate"])
        stream = sd.InputStream(device=device, channels=max(1, min(2, int(info["max_input_channels"]))),
                                samplerate=rate, dtype="float32", callback=callback)
        stream.start()
        return stream, rate, info["name"]

    return open_stream


class SoundDeviceAudio:
    name = "python"
    degraded = True

    def __init__(self, device_id: str | None = None, warmup_cap_ms: int = 2500, max_seconds: int = 600,
                 open_stream=None) -> None:
        self.device_id = device_id
        self.warmup_cap_ms = warmup_cap_ms
        self.max_seconds = max_seconds
        self._open_override = open_stream  # tests inject a fake stream
        self._cur: _Run | None = None
        self._retired: list[_Run] = []
        self._lock = threading.Lock()

    # ---- devices
    def enumerate_devices(self) -> list[dict]:
        import sounddevice as sd

        default_in = sd.default.device[0]
        out = []
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0:
                out.append({"id": d["name"], "name": d["name"], "is_default": i == default_in,
                            "sample_rate": int(d["default_samplerate"]), "channels": d["max_input_channels"],
                            "format": "F32"})
        return out

    def select_device(self, device_id):
        if self._cur is not None:
            raise I.SessionActive("cannot change device during a capture")
        self.device_id = device_id

    # ---- lifecycle
    def start_capture(self):
        with self._lock:
            self._retired = [r for r in self._retired if r.thread.is_alive()]
            if self._cur is not None:
                raise I.SessionActive("a capture is already active")
            p = Params(rate=48000, warmup_cap_ms=self.warmup_cap_ms, max_seconds=self.max_seconds)
            self._cur = _Run(self._open_override or _default_open(self.device_id), p)

    def wait_until_ready(self, timeout_ms: int) -> dict:
        run = self._cur
        if run is None:
            raise I.AudioError("no capture in progress")
        with run.cv:
            done = lambda: run.phase in ("ready", "active", "stopped", "failed") or run.cancel.is_set()  # noqa: E731
            run.cv.wait_for(done, timeout_ms / 1000)
            if run.phase == "warming":  # open and delivering silence: readiness is bounded by the warm-up cap
                run.cv.wait_for(done, (self.warmup_cap_ms + 500) / 1000)
            phase = run.phase
        if phase in ("ready", "active"):
            return {**{k: run.timing.get(k) for k in ("open_ms", "first_audio_ms", "ready_ms", "warmup_ms")},
                    "silent_warning": run.silent_warning, "device_name": run.device_name, "device_rate": run.rate}
        if phase == "failed":
            with self._lock:
                if self._cur is run:  # a new capture may have started meanwhile
                    self._cur = None
            exc, msg = run.fail
            raise exc(msg)
        if phase == "stopped":
            raise I.AudioError("capture stopped before it became ready")
        with self._lock:
            run.cancel.set()
            if run not in self._retired:
                self._retired.append(run)
            if self._cur is run:
                self._cur = None
        raise I.DeviceTimeout(f"microphone did not {'open' if phase == 'opening' else 'deliver audio'} "
                              f"within {timeout_ms} ms")

    def get_input_level(self) -> float:
        return self._cur.level if self._cur else 0.0

    def stop_capture(self):
        if self._cur:
            self._cur.timing["stop_requested_ms"] = self._cur.ms()
            self._cur.stop.set()

    def finalize_audio(self, timeout_ms: int = 3000) -> dict:
        run, self._cur = self._cur, None
        if run is None:
            raise I.AudioError("no capture to finalize")
        run.stop.set()
        run.thread.join(timeout_ms / 1000)
        if run.thread.is_alive():
            self._retired.append(run)
            raise I.DeviceTimeout("microphone did not close in time")
        s = run.session
        if s is None:
            raise I.AudioError("no audio captured")
        t0 = time.perf_counter()
        samples, lead = s.trimmed()
        out = resample(samples, s.p.rate, TARGET_RATE)
        wav = wav_s16(out, TARGET_RATE)
        t = run.timing
        stop_closed = (round(t["stream_closed_ms"] - t["stop_requested_ms"], 1)
                       if "stream_closed_ms" in t and "stop_requested_ms" in t else None)
        return {"wav": wav, "sample_rate": TARGET_RATE, "duration_s": len(out) / TARGET_RATE,
                "trimmed_ms": lead * 1000 / s.p.rate, "device_rate": s.p.rate, "captured_samples": s.total_in,
                "overflowed": s.overflowed, "device_lost": run.device_lost, "dropped": 0,
                "xruns": run.overflows, "silent_warning": run.silent_warning, "activity": s.active_at is not None,
                "events": [e["type"] for e in run.events], "open_ms": t.get("open_ms"), "ready_ms": t.get("ready_ms"),
                "warmup_ms": t.get("warmup_ms"), "activity_ms": t.get("activity_ms"),
                "stop_to_closed_ms": stop_closed,
                "finalize_ms": round((time.perf_counter() - t0) * 1000, 1),
                "stream_ms": (round(t["stream_closed_ms"] - t["open_ms"], 1)
                              if "stream_closed_ms" in t and "open_ms" in t else None)}

    def cancel_capture(self):
        run, self._cur = self._cur, None
        if run:
            run.cancel.set()
            with run.cv:
                run.cv.notify_all()
            run.thread.join(0.3)
            if run.thread.is_alive():
                self._retired.append(run)

    def state(self) -> str:
        return self._cur.phase if self._cur else "idle"

    def poll_events(self) -> list[dict]:
        run = self._cur
        if not run:
            return []
        out = []
        while run.events:
            out.append(run.events.popleft())
        return out

    def stream_open(self) -> bool:
        return any(r.stream_open for r in ([self._cur] if self._cur else []) + self._retired)

    def retired_count(self) -> int:
        self._retired = [r for r in self._retired if r.thread.is_alive()]
        return len(self._retired)

    def shutdown(self):
        runs = ([self._cur] if self._cur else []) + self._retired
        self._cur, self._retired = None, []
        for r in runs:
            r.cancel.set()
        deadline = time.monotonic() + 1.0
        for r in runs:
            r.thread.join(max(0.0, deadline - time.monotonic()))
