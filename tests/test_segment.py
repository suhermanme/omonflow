"""Pause splitting and per-part language choice (mixed English/Indonesian dictation)."""

import numpy as np
import pytest

from omonflow.stt.segment import RATE, pcm_to_wav, split_on_pauses, wav_to_pcm
from omonflow.stt.server import ServerInfo, WhisperServer


def tone(sec, amp, freq=220):
    t = np.arange(int(RATE * sec)) / RATE
    return (amp * np.sin(2 * np.pi * freq * t)).astype("<i2")


def pause(sec, noise=20):
    return np.random.default_rng(1).normal(0, noise, int(RATE * sec)).astype("<i2")


def test_parts_cover_everything_and_cut_inside_pauses():
    pcm = np.concatenate([tone(2.0, 6000), pause(0.6), tone(2.5, 6000), pause(0.6), tone(1.8, 6000)])
    parts = split_on_pauses(pcm)
    assert len(parts) == 3
    assert parts[0][0] == 0 and parts[-1][1] == len(pcm)
    assert all(a[1] == b[0] for a, b in zip(parts, parts[1:]))  # contiguous: no sample dropped
    for cut in (parts[0][1], parts[1][1]):  # cuts land in the quiet stretches
        assert np.abs(pcm[cut - 100:cut + 100]).max() < 200


def test_short_pauses_and_short_parts_are_not_split():
    pcm = np.concatenate([tone(2.0, 6000), pause(0.15), tone(2.0, 6000)])  # 150 ms: within a sentence
    assert split_on_pauses(pcm) == [(0, len(pcm))]
    pcm = np.concatenate([tone(2.0, 6000), pause(0.5), tone(0.4, 6000)])   # "ok" after a pause stays attached
    assert len(split_on_pauses(pcm)) == 1


def test_wav_round_trip():
    pcm = tone(0.5, 1000)
    assert np.array_equal(wav_to_pcm(pcm_to_wav(pcm)), pcm)


class LanguageByLoudness(WhisperServer):
    """Stand-in server: loud parts are 'English', quiet parts 'Indonesian'."""

    def __init__(self):
        from pathlib import Path
        super().__init__(ServerInfo("none", "env"), Path("."), Path("."))  # never started
        self.calls = []

    def _post_inference(self, wav, fields, timeout_s):
        spoken = "en" if np.abs(wav_to_pcm(wav)).max() > 4000 else "id"
        lang = fields["language"]
        self.calls.append(lang)
        lp = -0.03 if lang == spoken else -0.40  # the translation scores lower
        word = {"en": "English", "id": "Indonesia"}[spoken] if lang == spoken else f"translated-{lang}"
        return {"text": word, "segments": [{"tokens": [1] * 8, "avg_logprob": lp}]}, 10.0


@pytest.mark.parametrize("prefer", [None, "en", "id"])
def test_mixed_dictation_keeps_each_language(prefer):
    pcm = np.concatenate([tone(2.0, 8000), pause(0.6), tone(2.4, 2500), pause(0.6), tone(2.0, 8000)])
    s = LanguageByLoudness()
    r = s.transcribe_mixed(pcm_to_wav(pcm), prefer=prefer)
    assert r["text"] == "English Indonesia English" and r["languages"] == ["en", "id", "en"]
    assert "translated" not in r["text"]
