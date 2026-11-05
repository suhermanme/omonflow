"""AudioBackend over the Rust core (omonflow._core.AudioCore)."""

from __future__ import annotations

from contextlib import contextmanager

from omonflow.core import interfaces as I


@contextmanager
def _mapped():
    from omonflow import _core

    try:
        yield
    except _core.DeviceTimeout as e:
        raise I.DeviceTimeout(str(e)) from e
    except _core.DeviceNotFound as e:
        raise I.DeviceNotFound(str(e)) from e
    except _core.SessionActive as e:
        raise I.SessionActive(str(e)) from e
    except _core.CaptureFailed as e:
        raise I.CaptureFailed(str(e)) from e
    except _core.AudioError as e:
        raise I.AudioError(str(e)) from e


class RustAudio:
    name = "rust"
    degraded = False

    def __init__(self, device_id: str | None = None, fake: dict | None = None, warmup_cap_ms: int = 2500,
                 max_seconds: int = 600) -> None:
        from omonflow import _core

        self._core = _core.AudioCore(device_id=device_id, warmup_cap_ms=warmup_cap_ms,
                                     max_seconds=max_seconds, fake=fake)

    def enumerate_devices(self) -> list[dict]:
        from omonflow import _core

        with _mapped():
            return _core.AudioCore.enumerate_devices()

    def select_device(self, device_id):
        with _mapped():
            self._core.select_device(device_id)

    def start_capture(self):
        with _mapped():
            self._core.start_capture()

    def wait_until_ready(self, timeout_ms: int) -> dict:
        with _mapped():
            return self._core.wait_until_ready(timeout_ms)

    def get_input_level(self) -> float:
        return self._core.get_input_level()

    def stop_capture(self):
        self._core.stop_capture()

    def finalize_audio(self, timeout_ms: int = 3000) -> dict:
        with _mapped():
            return self._core.finalize_audio(timeout_ms)

    def cancel_capture(self):
        self._core.cancel_capture()

    def state(self) -> str:
        return self._core.state()

    def poll_events(self) -> list[dict]:
        return self._core.poll_events()

    def stream_open(self) -> bool:
        return self._core.stream_open()

    def retired_count(self) -> int:
        return self._core.retired_count()

    def shutdown(self):
        self._core.shutdown()
