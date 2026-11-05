import os
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([sys.argv[0]])
    yield app


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("OMONFLOW_CONFIG_DIR", str(tmp_path))
    return tmp_path


def wait_until(qapp, predicate, timeout_ms=3000):
    from PySide6.QtCore import QDeadlineTimer

    deadline = QDeadlineTimer(timeout_ms)
    while not predicate():
        if deadline.hasExpired():
            raise AssertionError("condition not reached in time")
        qapp.processEvents()
    return True
