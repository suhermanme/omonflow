"""Backends: device discovery, explicit selection, swap with rollback, validation, profiles, idle unload."""

import stat
import sys
import time
from pathlib import Path

import pytest

from omonflow.app.whisper_controller import WhisperController
from omonflow.stt.backends import CPU, Device, parse_devices, server_args
from omonflow.stt.catalog import MetadataCache
from omonflow.stt.models import ModelStore
from omonflow.stt.profiles import recommend
from omonflow.stt.server import ServerInfo


def test_parse_devices_and_args():
    lines = ["whisper_backend_init_gpu: device 0: BLAS (type: 3)",
             "whisper_backend_init_gpu: device 1: CUDA0 (type: 1)",
             "whisper_backend_init_gpu: device 2: Vulkan0 (type: 1)"]
    d = parse_devices(lines)
    assert [x.key for x in d] == ["cpu", "cuda:0", "vulkan:1"]  # BLAS is an accelerator, not a GPU choice
    assert server_args(d[0]) == ["--no-gpu"] and server_args(d[2]) == ["--device", "1"]
    assert parse_devices([]) == [CPU]


def test_profiles_never_recommend_unreported_gpus():
    gpu = [CPU, Device("metal", 0, "MTL0")]
    assert recommend("accurate", 24576, gpu, unified_memory=True).model == "large-v3-turbo-q5_0"
    cpu_only = recommend("accurate", 32768, [CPU])
    assert cpu_only.device_key == "cpu" and cpu_only.model == "medium-q5_0" and "no GPU backend" in cpu_only.reason
    assert recommend("accurate", 8192, gpu, unified_memory=True).model == "small"
    assert recommend("fast", 8192, [CPU]).model == "base"
    assert recommend("balanced", 8192, [CPU]).model == "small"
    assert recommend("custom", 8192, gpu) is None


@pytest.fixture
def controller(tmp_path, qapp, monkeypatch):
    exe = tmp_path / "whisper-server"
    exe.write_text(f"#!{sys.executable}\nimport runpy\nrunpy.run_path({str(Path(__file__).with_name('fake_whisper_server.py'))!r}, run_name='__main__')\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    models = tmp_path / "models"
    models.mkdir()
    (models / "ggml-tiny.bin").write_bytes(b"lmgg" + b"\0" * 100)
    (models / "ggml-base.bin").write_bytes(b"lmgg" + b"\0" * 100)
    store = ModelStore(models, MetadataCache(models / ".c.json", fetch=lambda: {}, head=False))
    c = WhisperController(store, tmp_path / "state", tmp_path / "logs",
                          executable_finder=lambda: ServerInfo(str(exe), "env", "test"))
    c.events = []
    c.task_done.connect(lambda *a: c.events.append(a))
    yield c
    c.shutdown()


def wait_events(qapp, c, n, timeout=20):
    end = time.monotonic() + timeout
    while len(c.events) < n and time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.02)
    assert len(c.events) >= n, c.events
    return c.events[n - 1]


def test_discovers_devices_and_switches_backend_without_restarts(qapp, controller):
    c = controller
    c.ensure_running("tiny")
    assert wait_events(qapp, c, 1)[2]
    assert [d.key for d in c.devices] == ["cpu", "metal:0"] and c.snapshot()["backend"] == "Metal"
    first = c.server.status.pid
    c.ensure_running("tiny", device="cpu")
    assert wait_events(qapp, c, 2)[2]
    assert c.snapshot()["backend"] == "CPU" and c.server.status.pid != first and c.server.status.restarts == 0
    assert sorted(p.name for p in c.state_dir.glob("*.pid.json")) == ["whisper-server.pid.json"]
    c.ensure_running("base", device="metal:0")  # model + backend change in one go
    assert wait_events(qapp, c, 3)[2]
    assert c.snapshot()["backend"] == "Metal" and c.config == {"model": "base", "device": "metal:0", "threads": 0}


def test_unavailable_backend_is_refused_and_engine_kept(qapp, controller):
    c = controller
    c.ensure_running("tiny")
    wait_events(qapp, c, 1)
    pid = c.server.status.pid
    c.ensure_running("tiny", device="cuda:0")
    ok, msg = wait_events(qapp, c, 2)[2:]
    assert not ok and "not available" in msg
    assert c.server.status.pid == pid and c.server.health() == "ok" and c.config["device"] == "auto"


def test_backend_that_silently_falls_back_is_rejected(qapp, controller, monkeypatch):
    # whisper.cpp lists the GPU but ends up on CPU: must not be accepted as "Metal".
    c = controller
    c.ensure_running("tiny", device="cpu")
    wait_events(qapp, c, 1)
    c.devices = [CPU, Device("metal", 0, "MTL0"), Device("metal", 5, "MTL5")]  # MTL5 does not exist
    pid = c.server.status.pid
    c.ensure_running("tiny", device="metal:5")
    ok, msg = wait_events(qapp, c, 2)[2:]
    assert not ok and "whisper.cpp reports 'CPU'" in msg
    assert c.server.status.pid == pid and c.snapshot()["backend"] == "CPU"


def test_validation_records_results_without_touching_the_engine(qapp, controller):
    c = controller
    c.ensure_running("tiny")
    wait_events(qapp, c, 1)
    pid = c.server.status.pid
    c.validate("metal:0", "tiny", runs=2)
    subject, task, ok, summary = wait_events(qapp, c, 2)
    assert (subject, task, ok) == ("metal:0", "validate", True) and "real time" in summary
    v = c.validation("metal:0")
    assert v.ok and v.active_backend == "Metal" and v.rtf is not None
    assert c.server.status.pid == pid
    c.validate("cuda:0", "tiny")
    assert not wait_events(qapp, c, 3)[2] and not c.validation("cuda:0").ok


def test_idle_unload_and_reload_on_dictation(qapp, controller):
    c = controller
    c.ensure_running("tiny")
    wait_events(qapp, c, 1)
    c.unload_idle_min = 0.01  # 0.6 s
    c._arm_idle_timer()
    end = time.monotonic() + 5
    while time.monotonic() < end and c.server.status.state != "stopped":
        time.sleep(0.05)
    assert c.server.status.state == "stopped"
    text = c.transcribe(b"RIFF" + b"\0" * 32040, 30)  # reloads automatically
    assert "ask not" in text and c.server.status.state == "ready"
    assert c.last_transcription["audio_s"] == pytest.approx(1.0, abs=0.01)
