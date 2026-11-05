"""Ready cue: a short system sound when OmonFlow starts listening.

Bluetooth headsets need 0.6-1.5 s after the microphone opens before they
deliver audio (macOS switches them to the call profile); words spoken
earlier are not captured. The cue tells the user when to start speaking
without watching the widget.
"""

from __future__ import annotations

import logging

LOG = logging.getLogger("omonflow.sound")
READY_SOUND = "Tink"  # short and soft; /System/Library/Sounds
_cache: dict = {}


def play_ready(volume: float = 0.5) -> None:
    try:
        from AppKit import NSSound

        snd = _cache.get(READY_SOUND)
        if snd is None:
            snd = _cache[READY_SOUND] = NSSound.soundNamed_(READY_SOUND)
        if snd is None:
            return
        snd.stop()  # restart if a previous cue is still playing
        snd.setVolume_(volume)
        snd.play()
    except Exception:  # noqa: BLE001 - a missing cue must never affect dictation
        LOG.debug("ready sound unavailable", exc_info=True)
