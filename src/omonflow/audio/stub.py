"""Stub audio backend for M02. Never touches a microphone.

Produces a synthetic level so the widget can be exercised. Replaced by the
Rust core and the sounddevice fallback in M03.
"""

from __future__ import annotations

import math
import time

from omonflow.core.interfaces import DeviceInfo, Recording, SessionActive


class StubAudioBackend:
    name = "stub"

    def __init__(self) -> None:
        self._state = "idle"
        self._t0 = 0.0
        self.start_calls = 0  # tests assert on this

    def enumerate_devices(self) -> list[DeviceInfo]:
        return [DeviceInfo(id="stub", name="Stub microphone (no audio)", is_default=True)]

    def select_device(self, device_id: str | None) -> None:
        pass

    def start_capture(self) -> None:
        if self._state != "idle":
            raise SessionActive("capture already active")
        self.start_calls += 1
        self._state = "active"
        self._t0 = time.monotonic()

    def wait_until_ready(self, timeout_ms: int) -> dict:
        return {"open_ms": 0.0, "ready_ms": 0.0, "warmup_ms": 0.0, "silent_warning": False}

    def get_input_level(self) -> float:
        if self._state != "active":
            return 0.0
        t = time.monotonic() - self._t0
        return max(0.0, 0.35 + 0.3 * math.sin(t * 7.0) + 0.2 * math.sin(t * 2.3))

    def stop_capture(self) -> None:
        self._state = "stopping"

    def finalize_audio(self) -> Recording:
        dur = time.monotonic() - self._t0
        self._state = "idle"
        return Recording(wav=b"", duration_s=dur)

    def cancel_capture(self) -> None:
        self._state = "idle"

    def state(self) -> str:
        return self._state

    def shutdown(self) -> None:
        self._state = "idle"
