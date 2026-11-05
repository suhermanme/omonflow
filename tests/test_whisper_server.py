"""WhisperServer lifecycle against a fake whisper-server (same CLI + HTTP API)."""

import json
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from omonflow.stt import server as srv
from omonflow.stt.server import ServerInfo, WhisperError, WhisperServer


@pytest.fixture
def fake_exe(tmp_path):
    exe = tmp_path / "whisper-server"
    exe.write_text(f"#!{sys.executable}\nimport runpy\nrunpy.run_path({str(Path(__file__).with_name('fake_whisper_server.py'))!r}, run_name='__main__')\n")
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return ServerInfo(str(exe), "env")


@pytest.fixture
def model(tmp_path):
    m = tmp_path / "ggml-tiny.bin"
    m.write_bytes(b"lmgg" + b"\0" * 100)
    return m


def make(fake_exe, tmp_path, **kw):
    return WhisperServer(fake_exe, tmp_path / "state", tmp_path / "logs", **kw)


def wait(pred, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_start_ready_backend_and_transcribe(fake_exe, tmp_path, model):
    s = make(fake_exe, tmp_path)
    st = s.start(model)
    assert st.state == "ready" and st.port and st.pid and st.start_ms > 0
    assert st.backend == "Metal"
    assert json.loads(s.pidfile.read_text())["pid"] == st.pid
    assert "[ggml-tiny.bin]" in s.transcribe(b"RIFF....")["text"]
    s.stop()
    assert s.status.state == "stopped" and not s.pidfile.exists()


def test_reports_loading_state(fake_exe, tmp_path, model, monkeypatch):
    monkeypatch.setenv("FAKE_LOAD_S", "1.0")
    seen = []
    s = make(fake_exe, tmp_path, on_status=lambda st: seen.append(st.state))
    s.start(model)
    assert "loading" in seen and seen[-1] == "ready"
    s.stop()


def test_cpu_only_backend(fake_exe, tmp_path, model):
    s = make(fake_exe, tmp_path, use_gpu=False)
    assert s.start(model).backend == "CPU"
    s.stop()


def test_duplicate_start_rejected(fake_exe, tmp_path, model):
    s = make(fake_exe, tmp_path)
    s.start(model)
    with pytest.raises(WhisperError):
        s.start(model)
    s.stop()


def test_crash_is_detected_and_restarted(fake_exe, tmp_path, model, monkeypatch):
    monkeypatch.setenv("FAKE_CRASH_AFTER", "0.5")
    s = make(fake_exe, tmp_path)
    first = s.start(model).pid
    monkeypatch.delenv("FAKE_CRASH_AFTER")  # the restarted server stays up
    assert wait(lambda: s.status.restarts == 1 and s.status.state == "ready" and s.status.pid != first)
    assert s.health() == "ok"
    s.stop()


def test_restart_limit(fake_exe, tmp_path, model, monkeypatch):
    monkeypatch.setenv("FAKE_CRASH_AFTER", "0.2")
    monkeypatch.setattr(srv, "RESTART_LIMIT", 2)
    s = make(fake_exe, tmp_path)
    s.start(model)
    assert wait(lambda: s.status.state == "failed", 20)
    assert "restart limit" in s.status.error
    s.stop()


def test_kill_is_restarted(fake_exe, tmp_path, model):
    s = make(fake_exe, tmp_path)
    pid = s.start(model).pid
    os.kill(pid, signal.SIGKILL)
    assert wait(lambda: s.status.state == "ready" and s.status.pid != pid)
    s.stop()


def test_failed_start_reports(fake_exe, tmp_path):
    bad = tmp_path / "ggml-bad.bin"
    bad.write_bytes(b"bad model")
    s = make(fake_exe, tmp_path)
    with pytest.raises(WhisperError, match="exited"):
        s.start(bad)
    assert s.status.state == "failed"


def test_model_switch_and_rollback(fake_exe, tmp_path, model):
    other = tmp_path / "ggml-base.bin"
    other.write_bytes(b"lmgg" + b"\0" * 10)
    bad = tmp_path / "ggml-small.bin"
    bad.write_bytes(b"bad")
    s = make(fake_exe, tmp_path)
    s.start(model)
    assert s.load_model(other) > 0
    assert "ggml-base.bin" in s.transcribe(b"x")["text"]
    with pytest.raises(WhisperError):
        s.load_model(bad)
    assert s.health() == "ok"
    assert "ggml-base.bin" in s.transcribe(b"x")["text"]  # previous model restored
    assert s.status.model == str(other)
    s.stop()


def test_stale_owned_server_is_cleaned_up(fake_exe, tmp_path, model):
    a = make(fake_exe, tmp_path)
    old_pid = a.start(model).pid
    a._stopping = True  # simulate a crashed OmonFlow: process + pidfile left behind
    a._proc = None
    b = make(fake_exe, tmp_path)
    b.start(model)
    assert wait(lambda: not srv._pid_alive(old_pid) or _zombie(old_pid), 5)
    b.stop()


def _zombie(pid):
    try:
        return os.waitpid(pid, os.WNOHANG)[0] == pid
    except ChildProcessError:
        return True


def test_foreign_processes_are_never_touched(fake_exe, tmp_path, model):
    # Another whisper-server the user runs (not ours), plus a pidfile pointing at
    # an unrelated process: neither may be stopped.
    port = srv.free_port()
    foreign = subprocess.Popen([fake_exe.executable, "--model", str(model), "--port", str(port)])
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        state = tmp_path / "state"
        state.mkdir()
        (state / "whisper-server.pid.json").write_text(json.dumps(
            {"pid": unrelated.pid, "port": 1234, "exe": fake_exe.executable}))
        s = make(fake_exe, tmp_path)
        s.start(model)
        s.stop()
        assert foreign.poll() is None and unrelated.poll() is None
    finally:
        foreign.kill()
        unrelated.kill()


def test_model_that_crashes_server_restores_previous(fake_exe, tmp_path, model):
    crash = tmp_path / "ggml-small.bin"
    crash.write_bytes(b"lmgg crash")
    s = make(fake_exe, tmp_path)
    s.start(model)
    with pytest.raises(WhisperError, match="restored ggml-tiny.bin"):
        s.load_model(crash)
    assert s.status.state == "ready" and s.status.model == str(model) and s.status.restarts == 1
    assert "ggml-tiny.bin" in s.transcribe(b"x")["text"]
    s.stop()


@pytest.mark.parametrize("auto,logprobs,allowed,prefer,chosen,passes", [
    ("en", "en:-0.02,id:-0.40", ("en", "id"), None, "en", 1),    # confident English: one pass
    ("id", "en:-0.40,id:-0.03", ("en", "id"), None, "id", 1),    # confident Indonesian: one pass
    ("id", "en:-0.04,id:-0.30", ("en", "id"), None, "en", 2),    # English misheard as Indonesian: fixed
    ("en", "en:-0.35,id:-0.05", ("en", "id"), None, "id", 2),    # Indonesian misheard as English: fixed
    ("id", "en:-0.20,id:-0.21", ("en", "id"), "en", "en", 2),    # near-tie: the preference decides
    ("id", "en:-0.60,id:-0.20", ("en", "id"), "en", "id", 2),    # preference never forces a translation
    ("ms", "en:-0.50,id:-0.10,ms:-0.05", ("en", "id"), None, "id", 3),  # Malay is not one of the user's languages
    ("ms", "ms:-0.05", (), None, "ms", 1),                         # "any language" keeps Whisper's choice
])
def test_language_is_the_one_spoken(fake_exe, tmp_path, model, monkeypatch, auto, logprobs, allowed, prefer,
                                    chosen, passes):
    monkeypatch.setenv("FAKE_AUTO_LANG", auto)
    monkeypatch.setenv("FAKE_LOGPROBS", logprobs)
    s = make(fake_exe, tmp_path)
    s.start(model)
    try:
        r = s.transcribe_auto(b"RIFF", allowed=allowed, prefer=prefer)
        assert (r["chosen"], r["passes"]) == (chosen, passes)
        assert f"[lang={chosen}]" in r["text"]  # the inserted text is the one in the chosen language
    finally:
        s.stop()


def test_logs_never_contain_the_text(fake_exe, tmp_path, model, caplog):
    import logging
    caplog.set_level(logging.INFO, logger="omonflow.whisper")
    s = make(fake_exe, tmp_path)
    s.start(model)
    try:
        s.transcribe_auto(b"RIFF")
        assert any("language:" in r.message for r in caplog.records)
        assert not any("ask not" in r.getMessage() for r in caplog.records)
    finally:
        s.stop()
