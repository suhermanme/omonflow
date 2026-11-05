"""App-level wiring: visibility vs dictation, persistence, single instance."""

import uuid

import pytest
from conftest import wait_until
from test_shortcuts import FakeBackend

from omonflow.app.main import OmonFlowApp
from omonflow.app.single_instance import InstanceServer, send_to_running
from omonflow.audio.stub import StubAudioBackend
from omonflow.core.states import State
from omonflow.storage.settings import SettingsStore

FAST = {"init": 5, "finalize": 5, "transcribe": 5, "focus_restore": 5, "insert": 5, "success_hold": 5}


@pytest.fixture
def make_app(qapp, config_dir):
    apps = []

    def make():
        a = OmonFlowApp(qapp, SettingsStore(), None, shortcut_backend=lambda: FakeBackend(),
                        audio=StubAudioBackend(), delays=FAST, start_engine=False)
        apps.append(a)
        return a
    yield make
    for a in apps:
        a.dispose()
    qapp.processEvents()


def test_fresh_install_shows_spectrum_pill(make_app):
    a = make_app()
    assert a.widget.active_key == "spectrum-pill" and a.widget.isVisible()


def test_visibility_never_touches_microphone(make_app):
    a = make_app()
    for _ in range(3):
        a.toggle_widget()
        a.settings_win.show_tab("general")
        a.settings_win.close()
    assert a.audio.start_calls == 0 and a.orch.state == State.IDLE


def test_hiding_does_not_stop_recording(qapp, make_app):
    a = make_app()
    a.orch.start()
    wait_until(qapp, lambda: a.orch.state in (State.CAPTURE_READY, State.RECORDING))
    a.hide_widget()
    a.show_widget()
    a.hide_widget()
    assert a.orch.state in (State.CAPTURE_READY, State.RECORDING)
    assert a.tray.stop_action.isEnabled()  # tray still offers Stop while hidden


def test_visualizer_choice_persists_across_restart(make_app):
    a = make_app()
    a.select_visualizer("circular-wave")
    assert a.widget.active_key == "circular-wave"
    b = make_app()
    assert b.widget.active_key == "circular-wave"


def test_failed_visualizer_keeps_saved_preference(make_app, config_dir):
    import json
    (config_dir / "settings.json").write_text(json.dumps({"visualizer": "from-a-future-version"}))
    a = make_app()
    assert a.widget.active_key == "spectrum-pill"
    a._save()
    assert json.loads((config_dir / "settings.json").read_text())["visualizer"] == "from-a-future-version"


def test_shortcut_failure_keeps_users_choice(qapp, config_dir):
    from omonflow.shortcuts.service import ShortcutStatus
    a = OmonFlowApp(qapp, SettingsStore(), None,
                    shortcut_backend=lambda: FakeBackend(fail=ShortcutStatus.CONFLICT), delays=FAST, start_engine=False)
    a._apply_shortcut("cmd+alt+k", True)
    assert a.shortcut.status == ShortcutStatus.CONFLICT
    assert SettingsStore().load().shortcut == "cmd+alt+k"
    a.dispose()
    qapp.processEvents()


def test_settings_window_close_only_hides(make_app):
    a = make_app()
    a.settings_win.show_tab("about")
    a.settings_win.close()
    assert not a.settings_win.isVisible()


def test_single_instance_forwards_commands(qapp):
    name = f"omonflow-test-{uuid.uuid4().hex[:8]}"
    server = InstanceServer(name)
    got = []
    server.command.connect(got.append)
    assert send_to_running("toggle-widget", name=name)
    wait_until(qapp, lambda: got == ["toggle-widget"])
    server.close()
    assert not send_to_running("activate", name=name, timeout_ms=200)


def test_engine_settings_saved_only_after_success(make_app):
    a = make_app()
    calls = []
    a.whisper.ensure_running = lambda model, device=None, threads=None: calls.append((model, device, threads))
    a._engine_config("large-v3-turbo-q5_0", "cuda:0", 4, "accurate")
    assert calls == [("large-v3-turbo-q5_0", "cuda:0", 4)]
    a.whisper.task_done.emit("large-v3-turbo-q5_0", "engine", False, "CUDA not available")
    s = SettingsStore().load()
    assert (s.whisper_model, s.whisper_backend, s.profile) == ("small", "auto", "balanced")  # unchanged
    a._engine_config("base", "cpu", 0, "custom")
    a.whisper.task_done.emit("base", "engine", True, "ready")
    s = SettingsStore().load()
    assert (s.whisper_model, s.whisper_backend, s.profile) == ("base", "cpu", "custom")
