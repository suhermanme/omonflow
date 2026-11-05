"""Real-microphone privacy checks (macOS). Run with OMONFLOW_HW_TESTS=1.

Uses CoreAudio's "device is running somewhere" property, independent of
OmonFlow's own bookkeeping. Assumes no other app is using a microphone.
"""

import os
import sys
import time

import pytest
from conftest import wait_until

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("OMONFLOW_HW_TESTS") != "1",
    reason="needs a real microphone on macOS (OMONFLOW_HW_TESTS=1)")


@pytest.fixture
def app(qapp, config_dir):
    from test_shortcuts import FakeBackend

    from omonflow.app.main import OmonFlowApp
    from omonflow.platform.macos.audio import any_input_running
    from omonflow.storage.settings import SettingsStore

    assert not any_input_running(), "another app is using a microphone; close it first"
    a = OmonFlowApp(qapp, SettingsStore(), None, shortcut_backend=lambda: FakeBackend(),
                    delays={"transcribe": 5, "focus_restore": 5, "insert": 5, "success_hold": 5},
                    start_engine=False)
    yield a
    a.dispose()
    qapp.processEvents()


def test_idle_app_does_not_use_microphone(qapp, app):
    from omonflow.platform.macos.audio import any_input_running

    assert app.audio.name == "rust"
    for _ in range(3):
        app.toggle_widget()
        app.settings_win.show_tab("audio")  # listing devices must not open them
        app.settings_win.hide()
    time.sleep(0.5)
    assert not any_input_running()


def test_microphone_runs_only_during_dictation(qapp, app):
    from omonflow.core.states import State
    from omonflow.platform.macos.audio import any_input_running

    app.orch.start()
    wait_until(qapp, lambda: app.orch.state == State.CAPTURE_READY, 5000)
    assert any_input_running()
    time.sleep(0.4)
    app.orch.stop()
    wait_until(qapp, lambda: app.orch.state in (State.TRANSCRIBING, State.INSERTING, State.SUCCESS, State.IDLE), 5000)
    time.sleep(0.2)
    assert not any_input_running(), "microphone still in use after finalize"
    wait_until(qapp, lambda: app.orch.state == State.IDLE, 5000)


def test_cancel_releases_microphone(qapp, app):
    from omonflow.core.states import State
    from omonflow.platform.macos.audio import any_input_running

    app.orch.start()
    wait_until(qapp, lambda: app.orch.state == State.CAPTURE_READY, 5000)
    app.orch.cancel()
    wait_until(qapp, lambda: not any_input_running(), 2000)
