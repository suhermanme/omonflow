"""The Python fallback applies the same rules as the Rust core."""

import numpy as np
import pytest

from omonflow import _core
from omonflow.audio.session import Params, Session, resample


def signal(rate=48000, seed=0):
    rng = np.random.default_rng(seed)
    parts = [np.zeros(int(rate * 0.12)), 0.003 * rng.uniform(-1, 1, int(rate * 0.4)),
             0.1 * np.sin(2 * np.pi * 220 * np.arange(int(rate * 0.5)) / rate), 0.003 * rng.uniform(-1, 1, rate // 5)]
    return np.concatenate(parts).astype(np.float32)


@pytest.mark.parametrize("block", [64, 480, 4096])
@pytest.mark.parametrize("seed", [0, 1, 2])
def test_session_rules_match_rust(block, seed):
    x = signal(seed=seed)
    rust = _core.analyze_session(x.tolist(), 48000, block)
    s = Session(Params(rate=48000))
    for i in range(0, len(x), block):
        s.push(x[i:i + block])
    assert (s.ready_at, s.first_nonzero, s.active_at, s.silent_warning) == (
        rust["ready_at"], rust["first_nonzero"], rust["active_at"], rust["silent_warning"])
    assert s.floor == pytest.approx(rust["floor"], rel=1e-3)


@pytest.mark.parametrize("src", [48000, 44100, 22050])
def test_resamplers_agree_and_filter(src):
    t = np.arange(src) / src
    x = (0.5 * np.sin(2 * np.pi * 1000 * t) + 0.5 * np.sin(2 * np.pi * 9500 * t)).astype(np.float32)
    py = resample(x, src, 16000)
    rs = np.array(_core.resample_audio(x.tolist(), src, 16000), dtype=np.float32)
    assert len(py) == len(rs) == 16000
    assert np.max(np.abs(py[200:-200] - rs[200:-200])) < 1e-3
    # 1 kHz kept (rms 0.354), 9.5 kHz (above 8 kHz Nyquist) removed.
    assert abs(np.sqrt(np.mean(rs[500:-500] ** 2)) - 0.3536) < 0.01
