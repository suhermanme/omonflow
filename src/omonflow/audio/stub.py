"""Stub audio backend: never touches a microphone (tests, demo).

Implements the full AudioBackend interface with synthetic timings/levels.
"""

from __future__ import annotations

import math
import time

from omonflow.core.interfaces import SessionActive


class StubAudioBackend:
    name = "stub"
    degraded = False

    def __init__(self, activity_after_s: float = 0.15) -> None:
        self._state = "idle"
        self._t0 = 0.0
        self._events: list[dict] = []
        self._activity_after = activity_after_s
        self._active_sent = False
        self.start_calls = 0  # tests assert on this

    def enumerate_devices(self) -> list[dict]:
        return [{"id": "stub", "name": "Stub microphone (no audio)", "is_default": True,
                 "sample_rate": 16000, "channels": 1, "format": "F32"}]

    def select_device(self, device_id) -> None:
        pass

    def start_capture(self) -> None:
        if self._state != "idle":
            raise SessionActive("capture already active")
        self.start_calls += 1
        self._state = "ready"
        self._t0 = time.monotonic()
        self._active_sent = False
        self._events = [{"type": "ready", "detail": ""}]

    def wait_until_ready(self, timeout_ms: int) -> dict:
        return {"open_ms": 0.0, "ready_ms": 0.0, "warmup_ms": 0.0, "silent_warning": False,
                "device_name": "stub", "device_rate": 16000}

    def get_input_level(self) -> float:
        if self._state not in ("ready", "active"):
            return 0.0
        t = time.monotonic() - self._t0
        return max(0.0, 0.35 + 0.3 * math.sin(t * 7.0) + 0.2 * math.sin(t * 2.3))

    def poll_events(self) -> list[dict]:
        if (self._state == "ready" and not self._active_sent
                and time.monotonic() - self._t0 >= self._activity_after):
            self._active_sent = True
            self._state = "active"
            self._events.append({"type": "activity", "detail": ""})
        ev, self._events = self._events, []
        return ev

    def stop_capture(self) -> None:
        self._state = "stopping"

    def finalize_audio(self, timeout_ms: int = 3000) -> dict:
        dur = time.monotonic() - self._t0
        self._state = "idle"
        return {"wav": b"", "sample_rate": 16000, "duration_s": dur, "trimmed_ms": 0.0, "device_lost": False,
                "dropped": 0, "xruns": 0, "silent_warning": False, "activity": True, "events": []}

    def cancel_capture(self) -> None:
        self._state = "idle"

    def state(self) -> str:
        return self._state

    def stream_open(self) -> bool:
        return False

    def retired_count(self) -> int:
        return 0

    def shutdown(self) -> None:
        self._state = "idle"
