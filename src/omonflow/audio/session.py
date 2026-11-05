"""Python mirror of the Rust session rules (rust/src/audio/session.py ↔ .rs).

Used by the sounddevice fallback. Same readiness, noise-floor and activity
rules; tests compare both implementations on identical synthetic input.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class Params:
    rate: int
    warmup_cap_ms: int = 2500  # Bluetooth headsets: up to ~1.5 s of exact zeros while switching profile
    max_seconds: int = 600
    floor_windows: int = 15
    activity_factor: float = 4.0
    activity_cap: float = 0.05
    activity_min_rms: float = 0.01
    hold_windows: int = 6


@dataclass
class Session:
    p: Params
    chunks: list = field(default_factory=list)
    stored: int = 0
    total_in: int = 0
    overflowed: bool = False
    first_nonzero: int | None = None
    ready_at: int | None = None
    silent_warning: bool = False
    active_at: int | None = None
    floor: float | None = None
    last_rms: float = 0.0
    _win: list = field(default_factory=list)
    _win_n: int = 0
    _floor_rms: list = field(default_factory=list)
    _hold: int = 0

    @property
    def win_len(self) -> int:
        return max(1, self.p.rate // 100)

    def push(self, block: np.ndarray) -> tuple[bool, bool]:
        """Returns (became_ready, became_active)."""
        block = np.asarray(block, dtype=np.float32)
        became_ready = became_active = False
        room = self.p.rate * self.p.max_seconds - self.stored
        if len(block) > room:
            self.overflowed = True
        keep = block[: max(0, min(len(block), room))]
        if len(keep):
            self.chunks.append(keep.copy())
            self.stored += len(keep)

        start = self.total_in
        if self.first_nonzero is None:
            nz = np.flatnonzero(block)
            if len(nz):
                self.first_nonzero = start + int(nz[0])
        if self.ready_at is None:
            cap = self.p.rate * self.p.warmup_cap_ms // 1000
            candidates = []
            if self.first_nonzero is not None:
                candidates.append(self.first_nonzero)
            if start + len(block) >= cap:
                candidates.append(max(start, cap - 1))
            if candidates:
                self.ready_at = min(candidates)
                self.silent_warning = self.first_nonzero is None or self.first_nonzero > self.ready_at
                became_ready = True

        # 10 ms RMS windows over the absolute sample index.
        pos = 0
        while pos < len(block):
            take = min(self.win_len - self._win_n, len(block) - pos)
            self._win.append(block[pos:pos + take])
            self._win_n += take
            pos += take
            if self._win_n == self.win_len:
                w = np.concatenate(self._win)
                self._win, self._win_n = [], 0
                rms = float(np.sqrt(np.mean(w.astype(np.float64) ** 2)))
                self.last_rms = rms
                end_idx = start + pos - 1
                if (self.ready_at is not None and end_idx >= self.ready_at + self.win_len - 1
                        and self._analyse(rms) and self.active_at is None):
                    self.active_at = end_idx
                    became_active = True
        self.total_in += len(block)
        return became_ready, became_active

    def _analyse(self, rms: float) -> bool:
        if self.floor is None:
            self._floor_rms.append(rms)
            if len(self._floor_rms) < self.p.floor_windows:
                return False
            v = sorted(self._floor_rms)
            self.floor = v[len(v) // 5]
        threshold = max(min(self.floor * self.p.activity_factor, self.p.activity_cap), self.p.activity_min_rms)
        self._hold = self._hold + 1 if rms > threshold else 0
        return self._hold >= self.p.hold_windows

    def samples(self) -> np.ndarray:
        return np.concatenate(self.chunks) if self.chunks else np.zeros(0, np.float32)

    def trimmed(self) -> tuple[np.ndarray, int]:
        s = self.samples()
        lead = len(s) if self.first_nonzero is None else min(self.first_nonzero, len(s))
        return s[lead:], lead


def resample(x: np.ndarray, src: int, dst: int) -> np.ndarray:
    """Hann-windowed sinc resampler (same design as the Rust one), vectorized."""
    if src == dst or len(x) == 0:
        return x.astype(np.float32)
    ratio = dst / src
    cutoff = 0.5 * min(ratio, 1.0) * 0.95
    scale = 2 * cutoff
    zc = 16.0
    half = zc / scale
    n_out = int(round(len(x) * ratio))
    t = np.arange(n_out) / ratio
    offs = np.arange(-int(np.ceil(half)), int(np.ceil(half)) + 1)
    out = np.zeros(n_out, dtype=np.float64)
    base = np.floor(t).astype(np.int64)
    xd = x.astype(np.float64)
    for o in offs:  # ~100 iterations, each vectorized over all outputs
        idx = base + o
        d = (t - idx) * scale
        k = np.sinc(d) * np.where(np.abs(d) < zc, 0.5 + 0.5 * np.cos(np.pi * d / zc), 0.0)
        valid = (idx >= 0) & (idx < len(xd))
        out[valid] += xd[idx[valid]] * k[valid]
    return (out * scale).astype(np.float32)


def wav_s16(samples: np.ndarray, rate: int) -> bytes:
    import io
    import wave

    pcm = (np.clip(samples, -1, 1) * 32767).round().astype("<i2")
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()
