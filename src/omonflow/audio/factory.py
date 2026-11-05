"""Choose the audio backend: Rust core, else the labelled Python fallback."""

from __future__ import annotations

import logging
import os

LOG = logging.getLogger("omonflow.audio")


def create_backend(device_id: str | None = None, prefer: str | None = None):
    """prefer: 'rust' | 'python' | 'stub' (env OMONFLOW_AUDIO_BACKEND)."""
    prefer = prefer or os.environ.get("OMONFLOW_AUDIO_BACKEND", "rust")
    if prefer == "stub":
        from .stub import StubAudioBackend

        return StubAudioBackend(), None
    if prefer == "rust":
        try:
            from .rust_backend import RustAudio

            return RustAudio(device_id=device_id), None
        except ImportError as exc:
            reason = f"native audio core unavailable ({exc}); using the Python fallback"
            LOG.warning(reason)
            from .sounddevice_backend import SoundDeviceAudio

            return SoundDeviceAudio(device_id=device_id), reason
    from .sounddevice_backend import SoundDeviceAudio

    return SoundDeviceAudio(device_id=device_id), None
