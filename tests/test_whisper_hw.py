"""Real whisper-server checks (OMONFLOW_HW_TESTS=1, a whisper-server on PATH,
and ggml-small.bin + jfk.wav under OMONFLOW_WHISPER_DIR or ~/local-ai/whisper.cpp)."""

import os
from pathlib import Path

import pytest

from omonflow.stt.server import WhisperServer, find_executable

ROOT = Path(os.environ.get("OMONFLOW_WHISPER_DIR", Path.home() / "local-ai/whisper.cpp"))
MODEL, SAMPLE = ROOT / "models/ggml-small.bin", ROOT / "samples/jfk.wav"
pytestmark = pytest.mark.skipif(
    os.environ.get("OMONFLOW_HW_TESTS") != "1" or not find_executable() or not MODEL.exists() or not SAMPLE.exists(),
    reason="needs OMONFLOW_HW_TESTS=1, whisper-server and ggml-small.bin")


def test_real_server_transcribes_and_reports_backend(tmp_path):
    s = WhisperServer(find_executable(), tmp_path, tmp_path / "logs")
    st = s.start(MODEL)
    try:
        assert st.state == "ready" and st.start_ms < 10_000
        text = s.transcribe(SAMPLE.read_bytes(), language="en", prompt=None)["text"].lower()
        assert "ask not what your country can do for you" in text
        import time
        time.sleep(1.5)
        s.refresh_evidence()
        assert s.status.backend in ("Metal", "CUDA", "Vulkan", "CPU", "CPU (BLAS)")
    finally:
        s.stop()
    assert s.status.state == "stopped"
