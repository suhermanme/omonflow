"""OmonFlow entry point and application wiring."""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import signal
import sys

from PySide6.QtCore import QPoint, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

import omonflow
from omonflow.app.orchestrator import Orchestrator
from omonflow.app.single_instance import InstanceServer, send_to_running
from omonflow.audio.stub import StubAudioBackend
from omonflow.core.states import ACTIVE_STATES, State
from omonflow.shortcuts.service import ShortcutService
from omonflow.storage.settings import SettingsStore, config_dir, log_dir
from omonflow.ui import visualizers
from omonflow.ui.settings.window import SettingsWindow
from omonflow.ui.tray.tray import TrayController
from omonflow.ui.widget.floating import FloatingWidget

LOG = logging.getLogger("omonflow")


def setup_logging(level: str) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        log_dir().mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            log_dir() / "omonflow.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(level=getattr(logging, level), handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")


class OmonFlowApp:
    def __init__(self, app: QApplication, store: SettingsStore, server: InstanceServer | None,
                 shortcut_backend=None, audio=None, delays: dict | None = None) -> None:
        self.app = app
        self.store = store
        self.settings = store.load()
        self.server = server
        self.platform_report: dict = {}

        if sys.platform == "darwin":
            from omonflow.platform.macos.window import set_accessory_app
            set_accessory_app()

        self.audio = audio or StubAudioBackend()
        self.orch = Orchestrator(self.audio, delays)
        self.widget = FloatingWidget(can_cancel=self.orch.can_cancel,
                                     always_on_top=self.settings.always_on_top)
        self.tray = TrayController()
        self.shortcut = ShortcutService(shortcut_backend) if shortcut_backend else ShortcutService()
        self.settings_win = SettingsWindow(self.settings, self.diagnostics())

        # --- wiring: UI sends intents, orchestrator owns state
        self.widget.intent.connect(self._widget_intent)
        self.widget.visualizer_error.connect(self._visualizer_error)
        self.widget.moved.connect(self._widget_moved)
        self.orch.state_changed.connect(self._state_changed)
        self.orch.level_changed.connect(self.widget.set_level)
        self.orch.notice.connect(self._notice)
        self.orch.controls_changed.connect(lambda: self._state_changed(self.orch.state.value))
        self.tray.toggle_widget.connect(self.toggle_widget)
        self.tray.open_settings.connect(self.settings_win.show_tab)
        self.tray.start.connect(self.orch.start)
        self.tray.stop.connect(self.orch.stop)
        self.tray.cancel.connect(self.orch.cancel)
        self.tray.quit.connect(self.quit)
        self.shortcut.activated.connect(self.toggle_widget)
        self.shortcut.status_changed.connect(self._shortcut_status)
        self.settings_win.visualizer_selected.connect(self.select_visualizer)
        self.settings_win.always_on_top_changed.connect(self._always_on_top)
        self.settings_win.reset_position.connect(self._reset_position)
        self.settings_win.shortcut_requested.connect(self._apply_shortcut)
        self.settings_win.behavior_changed.connect(self._save)
        if server is not None:
            server.command.connect(self._remote_command)

        # --- start-up
        self.widget.set_visualizer(self.settings.visualizer)
        self._place_widget()
        self.tray.show()
        self.shortcut.apply(self.settings.shortcut, self.settings.shortcut_enabled)
        if self.settings.widget_visible_on_launch or not self.tray.available:
            self.show_widget()
        else:
            self.tray.set_widget_visible(False)
        LOG.info("diagnostics: %s", self.diagnostics())
        if store.recovered_from:
            self._notice("warning", f"Settings were unreadable and have been reset. Backup: {store.recovered_from}")

    # ------------------------------------------------------------ widget visibility
    def show_widget(self) -> None:
        self.widget.show()
        if (sys.platform == "darwin" and not self.platform_report
                and not os.environ.get("OMONFLOW_DISABLE_NONACTIVATING")):  # focus-spike control run
            from omonflow.platform.macos.window import make_non_activating
            self.platform_report = make_non_activating(self.widget)
            LOG.info("macOS widget window: %s", self.platform_report)
            # Reconfiguring the NSWindow can drop its contents; with a static
            # idle widget nothing would repaint it, and a transparent window
            # with no content lets clicks fall through to the app behind.
            self.widget.repaint()
            self.settings_win.set_diagnostics(self.diagnostics())
        self.tray.set_widget_visible(True)

    def hide_widget(self) -> None:
        self.widget.hide()  # never touches the dictation session
        self.tray.set_widget_visible(False)

    def toggle_widget(self) -> None:
        self.hide_widget() if self.widget.isVisible() else self.show_widget()

    def _place_widget(self) -> None:
        if self.settings.widget_pos:
            pos = QPoint(*self.settings.widget_pos)
            screen = self.app.screenAt(pos)
            if screen is not None:
                self.widget.move(pos)
                return
        self._reset_position(save=False)

    def _reset_position(self, save: bool = True) -> None:
        geo = self.app.primaryScreen().availableGeometry()
        self.widget.move(geo.center().x() - self.widget.width() // 2, geo.bottom() - self.widget.height() - 48)
        if save:
            self.settings.widget_pos = None
            self._save()

    def _widget_moved(self, pos: QPoint) -> None:
        self.settings.widget_pos = [pos.x(), pos.y()]
        self._save()

    # ------------------------------------------------------------ settings
    def select_visualizer(self, key: str) -> None:
        if self.widget.set_visualizer(key):
            self.settings.visualizer = key
            self.settings_win.show_visualizer_error("")
            self._save()
        if sys.platform == "darwin" and self.widget.isVisible():
            from omonflow.platform.macos.window import make_non_activating
            make_non_activating(self.widget)
            self.widget.repaint()

    def _visualizer_error(self, msg: str) -> None:
        # Fallback is display-only: the saved preference is left unchanged.
        self.settings_win.show_visualizer_error(msg)
        self.settings_win.select_visualizer(self.settings.visualizer)
        self._notice("error", msg)

    def _always_on_top(self, on: bool) -> None:
        self.settings.always_on_top = on
        self.widget.set_always_on_top(on)
        if sys.platform == "darwin" and self.widget.isVisible():
            from omonflow.platform.macos.window import make_non_activating
            self.platform_report = make_non_activating(self.widget)
            self.widget.repaint()
        self._save()

    def _apply_shortcut(self, spec: str, enabled: bool) -> None:
        previous = self.settings.shortcut
        self.settings.shortcut = spec
        self.settings.shortcut_enabled = enabled
        self.shortcut.apply(spec, enabled)
        # Keep the user's choice even if registration failed: never substitute.
        self._save()
        LOG.info("shortcut changed %s -> %s (%s)", previous, spec, self.shortcut.status.value)

    def _shortcut_status(self, status: str, message: str) -> None:
        self.settings_win.show_shortcut_status(status, message)

    def _save(self) -> None:
        try:
            self.store.save(self.settings)
        except OSError as exc:
            self._notice("error", f"Could not save settings: {exc}")

    # ------------------------------------------------------------ dictation state
    def _widget_intent(self, name: str) -> None:
        if name == "primary":
            self.orch.primary()
        elif name == "cancel":
            self.orch.dismiss() if self.orch.state == State.ERROR else self.orch.cancel()

    def _state_changed(self, state: str) -> None:
        self.widget.set_error_payload(bool(self.orch.transcript), self.orch.audio_kept)
        self.widget.set_state(state)
        self.tray.set_state(state, self.orch.can_cancel())
        if state == State.ERROR.value and self.orch.error_text:
            self.widget.setToolTip(self.orch.error_text)
        else:
            self.widget.setToolTip("")

    def _notice(self, severity: str, text: str) -> None:
        LOG.log(logging.ERROR if severity == "error" else logging.INFO, "notice: %s", text)
        if self.settings.notifications:
            self.tray.notify("OmonFlow", text, error=severity == "error")

    def _remote_command(self, cmd: str) -> None:
        if cmd == "toggle-widget":
            self.toggle_widget()
        elif cmd == "settings":
            self.settings_win.show_tab("general")
        else:
            self.show_widget()

    def diagnostics(self) -> dict:
        try:
            from omonflow import _core
            core = f"loaded ({_core.version()})"
        except ImportError as exc:
            core = f"not loaded: {exc}"
        d = {
            "Python": sys.version.split()[0],
            "Platform": sys.platform,
            "Native core": core,
            "Audio backend": f"{self.audio.name} (no microphone access in M02)",
            "Shortcut": f"{self.shortcut.status.value}: {self.shortcut.message}" if hasattr(self, "shortcut") else "",
            "Settings file": str(self.store.path),
            "Logs": str(log_dir()),
        }
        if self.platform_report:
            d["Widget window (macOS)"] = ", ".join(f"{k}={v}" for k, v in self.platform_report.items())
        return d

    # ------------------------------------------------------------ quit
    def quit(self, interactive: bool = True) -> None:
        """Tray Quit asks during a session (SPEC §8). Signals (Ctrl-C, SIGTERM)
        come from a terminal or the OS: discard and exit without a dialog."""
        if self.orch.state in ACTIVE_STATES and not interactive:
            LOG.info("quit by signal during %s: discarding session", self.orch.state.value)
            self.orch.cancel()
        elif self.orch.state in ACTIVE_STATES:
            box = QMessageBox()
            box.setWindowTitle("Dictation in progress")
            box.setText("A dictation is in progress.")
            finish = box.addButton("Finish and insert", QMessageBox.AcceptRole)
            discard = box.addButton("Discard and quit", QMessageBox.DestructiveRole)
            box.addButton("Keep running", QMessageBox.RejectRole)
            box.exec()
            clicked = box.clickedButton()
            if clicked is finish:
                self.orch.stop()
                self._quit_when_idle()
                return
            if clicked is not discard:
                return
            self.orch.cancel()
        self._shutdown()

    def _quit_when_idle(self) -> None:
        if self.orch.state in ACTIVE_STATES:
            QTimer.singleShot(100, self._quit_when_idle)
        else:
            self._shutdown()

    def dispose(self) -> None:
        """Release everything in a safe order (also used by tests)."""
        self.shortcut.shutdown()
        self.orch.shutdown()
        self.widget.timer.stop()
        self.widget.hide()
        self.settings_win.hide()
        self.tray.dispose()
        self.widget.deleteLater()
        self.settings_win.deleteLater()
        if self.server:
            self.server.close()

    def _shutdown(self) -> None:
        LOG.info("shutting down")
        self.dispose()
        self.app.quit()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="omonflow", description="OmonFlow dictation")
    ap.add_argument("--toggle-widget", action="store_true", help="show/hide the widget of the running instance")
    ap.add_argument("--settings", action="store_true", help="open Settings in the running instance")
    ap.add_argument("--log-level", default=os.environ.get("OMONFLOW_LOG_LEVEL", "INFO").upper(),
                    choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args, qt_args = ap.parse_known_args(argv)
    setup_logging(args.log_level)

    app = QApplication([sys.argv[0], *qt_args])
    app.setApplicationName(omonflow.APP_NAME)
    app.setOrganizationName(omonflow.ORG_NAME)
    app.setQuitOnLastWindowClosed(False)

    command = "toggle-widget" if args.toggle_widget else ("settings" if args.settings else "activate")
    if send_to_running(command):
        LOG.info("OmonFlow is already running; sent %s", command)
        return 0

    server = InstanceServer()
    LOG.info("OmonFlow %s starting; config %s", omonflow.__version__, config_dir())
    omon = OmonFlowApp(app, SettingsStore(), server)
    signal.signal(signal.SIGINT, lambda *_: omon.quit(interactive=False))
    signal.signal(signal.SIGTERM, lambda *_: omon.quit(interactive=False))
    keepalive = QTimer()  # lets Python handle Ctrl-C
    keepalive.start(200)
    keepalive.timeout.connect(lambda: None)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
