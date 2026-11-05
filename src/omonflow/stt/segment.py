"""Split a finished dictation at pauses so each part can get its own language.

Whisper decodes a whole window (up to 30 s) in one language, so a dictation
that switches between English and Indonesian gets one part translated, or
even dropped (seen in testing). Splitting at natural pauses lets every part be
transcribed in the language actually spoken.

This is NOT voice-activity detection for recording boundaries: the recording
is untouched, parts are contiguous and cover all samples, and cuts are only
made in the middle of pauses.
"""

from __future__ import annotations

import io
import wave

import numpy as np

RATE = 16000
FRAME_MS = 20


def wav_to_pcm(wav: bytes) -> np.ndarray:
    with wave.open(io.BytesIO(wav)) as w:
        if w.getframerate() != RATE or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError("expected 16 kHz mono 16-bit WAV")
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")


def pcm_to_wav(pcm: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(np.asarray(pcm, dtype="<i2").tobytes())
    return buf.getvalue()


def split_on_pauses(pcm: np.ndarray, min_pause_ms: int = 300, min_part_ms: int = 1200,
                    max_parts: int = 12) -> list[tuple[int, int]]:
    """Contiguous (start, end) sample ranges covering all of `pcm`."""
    n = len(pcm)
    frame = RATE * FRAME_MS // 1000
    if n < 2 * frame:
        return [(0, n)]
    frames = n // frame
    x = pcm[: frames * frame].astype(np.float64).reshape(frames, frame)
    rms = np.sqrt((x * x).mean(axis=1))
    floor, loud = np.percentile(rms, 10), np.percentile(rms, 95)
    if loud <= 0:
        return [(0, n)]
    quiet = rms < max(floor * 2.5, floor + 0.08 * (loud - floor), 50.0)
    cuts, run_start = [], None
    min_run = max(1, min_pause_ms // FRAME_MS)
    for i, q in enumerate(list(quiet) + [False]):
        if q and run_start is None:
            run_start = i
        elif not q and run_start is not None:
            if i - run_start >= min_run and run_start > 0 and i < frames:  # interior pauses only
                cuts.append(((run_start + i) // 2) * frame)  # middle of the pause
            run_start = None
    bounds = [0] + cuts + [n]
    parts = [(bounds[k], bounds[k + 1]) for k in range(len(bounds) - 1)]
    min_len = RATE * min_part_ms // 1000
    merged: list[tuple[int, int]] = []
    for p in parts:  # short parts join their predecessor (a word is never cut, only grouped)
        if merged and (p[1] - p[0] < min_len or merged[-1][1] - merged[-1][0] < min_len):
            merged[-1] = (merged[-1][0], p[1])
        else:
            merged.append(p)
    while len(merged) > max_parts:  # join the shortest neighbouring pair
        k = min(range(len(merged) - 1), key=lambda i: merged[i + 1][1] - merged[i][0])
        merged[k:k + 2] = [(merged[k][0], merged[k + 1][1])]
    return merged
