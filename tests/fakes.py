"""Fake microphone for the Python fallback (mirrors the Rust FakeSpec)."""

import threading
import time

import numpy as np


class FakeStream:
    def __init__(self, callback, spec):
        self.cb, self.spec = callback, spec
        self.active = True
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        s = self.spec
        rate, block, n = s.get("rate", 48000), s.get("block", 480), 0
        rng = np.random.default_rng(1)
        t0 = time.perf_counter()
        while not self._stop.is_set():
            ms = n * 1000 / rate
            if s.get("lose_after_ms") is not None and ms >= s["lose_after_ms"]:
                self.active = False
                return
            idx = np.arange(n, n + block)
            t_ms = idx * 1000 / rate
            x = s.get("noise", 0.003) * rng.uniform(-1, 1, block)
            sp = s.get("speech_after_ms", 300)
            if sp is not None:
                x = x + np.where(t_ms >= sp, s.get("speech_amp", 0.1) * np.sin(2 * np.pi * 300 * idx / rate), 0)
            x = np.where(t_ms < s.get("warmup_zero_ms", 0), 0.0, x).astype(np.float32)
            self.cb(x.reshape(-1, 1), block, None, None)
            n += block
            due = t0 + n / rate
            time.sleep(max(0.0, due - time.perf_counter()))

    def stop(self):
        self._stop.set()
        self._t.join(1)

    def close(self):
        self.active = False


def fake_opener(spec):
    hangs = {"left": spec.get("hang_times", 10**9)}

    def open_stream(callback):
        from omonflow.core import interfaces as I

        if spec.get("hang_ms") and hangs["left"] > 0:
            hangs["left"] -= 1
            time.sleep(spec["hang_ms"] / 1000)
        time.sleep(spec.get("open_delay_ms", 20) / 1000)
        if spec.get("fail_open"):
            raise I.CaptureFailed(spec["fail_open"])
        return FakeStream(callback, spec), spec.get("rate", 48000), "Fake microphone"

    return open_stream
