"""Interfaces shared by implementations (ARCHITECTURE §3.2)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class AudioError(Exception):
    pass


class PermissionDenied(AudioError):
    pass


class DeviceNotFound(AudioError):
    pass


class DeviceTimeout(AudioError):
    pass


class SessionActive(AudioError):
    pass


@dataclass
class DeviceInfo:
    id: str
    name: str
    is_default: bool = False
    sample_rate: int = 0
    channels: int = 0


@dataclass
class Recording:
    wav: bytes
    duration_s: float
    trimmed_ms: float = 0.0
    device_lost: bool = False
    overflowed: bool = False
    extra: dict = field(default_factory=dict)


class AudioBackend(Protocol):
    name: str

    def enumerate_devices(self) -> list[DeviceInfo]: ...
    def select_device(self, device_id: str | None) -> None: ...
    def start_capture(self) -> None: ...
    def wait_until_ready(self, timeout_ms: int) -> dict: ...
    def get_input_level(self) -> float: ...
    def stop_capture(self) -> None: ...
    def finalize_audio(self) -> Recording: ...
    def cancel_capture(self) -> None: ...
    def state(self) -> str: ...
    def shutdown(self) -> None: ...
