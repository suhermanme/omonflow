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
from omonflow.audio.factory import create_backend
from omonflow.core.states import ACTIVE_STATES, State
from omonflow.shortcuts.service import ShortcutService
from omonflow.storage.settings import SettingsStore, config_dir, data_dir, log_dir
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


class AppRefiner:
    """Refinement settings → orchestrator. AI is used only when a server is set
    and, for a server that is not this computer, the user agreed to send text there."""

    def __init__(self, app: "OmonFlowApp") -> None:
        self.app = app

    def mode(self) -> str:
        s = self.app.settings
        if s.refine_mode != "ai":
            return s.refine_mode if s.refine_mode in ("raw", "clean") else "clean"
        problem = ai_problem(s)
        if problem:
            self.app._notice("warning", f"AI refinement is not ready ({problem}); used Clean instead.")
            return "clean"
        return "ai"

    def refine(self, text: str, mode: str):
        from omonflow.refinement.refiner import ai_refine, local_refine
        if mode != "ai":
            return local_refine(text, mode)
        return ai_refine(text, self.app.ai_config())


def ai_problem(s) -> str | None:
    from omonflow.refinement.ai import endpoint_location, normalize_base_url
    url = normalize_base_url(s.ai_base_url)
    if not url:
        return "no server is set"
    if endpoint_location(url) != "local" and s.ai_consent_url != url:
        return "sending text to this server has not been confirmed"
    return None


class OmonFlowApp:
    def __init__(self, app: QApplication, store: SettingsStore, server: InstanceServer | None,
                 shortcut_backend=None, audio=None, delays: dict | None = None, whisper=None,
                 start_engine: bool = True, inserter=None, transcriber=None, keys=None) -> None:
        self.app = app
        self.store = store
        self.settings = store.load()
        self.server = server
        self.platform_report: dict = {}

        if sys.platform == "darwin":
            from omonflow.platform.macos.window import set_accessory_app
            set_accessory_app()

        self.audio_fallback_reason = None
        if audio is None:
            audio, self.audio_fallback_reason = create_backend()
            self._apply_device(audio)
        self.audio = audio
        permission = None
        if sys.platform == "darwin":
            from omonflow.platform.macos.audio import mic_permission
            permission = mic_permission
        self.permission_check = permission
        if whisper is None:
            from omonflow.app.whisper_controller import WhisperController
            from omonflow.stt.models import ModelStore
            whisper = WhisperController(ModelStore(data_dir() / "models"), data_dir(), log_dir())
        self.whisper = whisper
        if inserter is None and start_engine:
            from omonflow.insertion.base import create_inserter
            try:
                inserter = create_inserter()
            except Exception:  # noqa: BLE001
                LOG.warning("text insertion unavailable", exc_info=True)
        self.inserter = inserter
        self._history = None
        from omonflow.refinement.secrets import KeyStore
        self.keys = keys if keys is not None else KeyStore()
        self.orch = Orchestrator(
            self.audio, delays, ready_timeout_ms=self.settings.mic_timeout_ms, permission_check=permission,
            transcriber=transcriber if transcriber is not None else (self._transcribe if start_engine else None),
            inserter=self.inserter, history=self.history, options=self._options, refiner=AppRefiner(self))
        self.widget = FloatingWidget(can_cancel=self.orch.can_cancel,
                                     always_on_top=self.settings.always_on_top)
        self.tray = TrayController()
        self.shortcut = ShortcutService(shortcut_backend) if shortcut_backend else ShortcutService()
        self.settings_win = SettingsWindow(self.settings, self.diagnostics(), audio_info=self.audio_info,
                                           whisper=self.whisper, insertion_info=self.insertion_info,
                                           keys=self.keys, ai_config=self.ai_config)

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
        self.settings_win.device_selected.connect(self._device_selected)
        self.settings_win.timeout_changed.connect(self._timeout_changed)
        self.settings_win.mic_test_requested.connect(self._mic_test)
        self.settings_win.model_selected.connect(self._model_selected)
        self.settings_win.engine_config_requested.connect(self._engine_config)
        self.settings_win.unload_changed.connect(self._unload_changed)
        self.whisper.task_done.connect(self._commit_engine_config)
        self.orch.state_changed.connect(lambda st: st == State.INITIALIZING.value and self.whisper.prepare())
        self._pending_engine: dict | None = None
        self.settings_win.request_accessibility.connect(self._request_accessibility)
        self.settings_win.open_history.connect(self.show_history)
        self.settings_win.clear_history.connect(self._clear_history)
        self.tray.open_history.connect(self.show_history)
        self.whisper.task_done.connect(self._whisper_task_done)
        self.orch.mic_test_progress.connect(self.settings_win.show_test_level)
        self.orch.mic_test_done.connect(self._mic_test_done)
        self.orch.startup_recorded.connect(lambda _e: self.settings_win.show_startup_stats(
            list(self.orch.startup_stats), self.orch.startup_summary()))
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
        if self.audio_fallback_reason:
            self._notice("warning", f"Audio: {self.audio_fallback_reason}")
        self.whisper.set_unload_idle(self.settings.unload_idle_min)
        if start_engine:
            self.whisper.refresh_catalog()
            self.whisper.ensure_running(self.settings.whisper_model, device=self.settings.whisper_backend,
                                        threads=self.settings.whisper_threads)
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

    # ------------------------------------------------------------ audio input
    def _apply_device(self, audio) -> None:
        dev = self.settings.audio_device_name if audio.name == "python" else self.settings.audio_device_id
        try:
            audio.select_device(dev)
        except Exception:  # noqa: BLE001
            LOG.warning("could not select saved microphone %r", dev, exc_info=True)

    def audio_info(self) -> dict:
        try:
            devices = self.audio.enumerate_devices()
        except Exception as exc:  # noqa: BLE001
            devices, err = [], str(exc)
        else:
            err = None
        perm = self.permission_check() if self.permission_check else None
        return {"backend": self.audio.name, "degraded": getattr(self.audio, "degraded", False),
                "fallback_reason": self.audio_fallback_reason, "devices": devices, "error": err,
                "permission": perm, "selected_id": self.settings.audio_device_id,
                "selected_name": self.settings.audio_device_name, "timeout_ms": self.settings.mic_timeout_ms}

    def _device_selected(self, device_id, name) -> None:
        if self.orch.state != State.IDLE or self.orch.testing:
            self._notice("warning", "Finish the current dictation before changing the microphone.")
            return
        self.settings.audio_device_id, self.settings.audio_device_name = device_id, name
        self._apply_device(self.audio)
        self._save()

    def _timeout_changed(self, ms: int) -> None:
        self.settings.mic_timeout_ms = ms
        self.orch.ready_timeout_ms = ms
        self._save()

    def _mic_test(self) -> None:
        if not self.orch.mic_test(3.0):
            self.settings_win.show_test_result({"ok": False, "error": "Busy: finish the current dictation first."})

    def _mic_test_done(self, result: dict) -> None:
        self.settings_win.show_test_result(result)

    # ------------------------------------------------------------ dictation pipeline
    def _transcribe(self, wav: bytes, timeout_s: float) -> str:
        from omonflow.stt.server import VOCAB_PROMPT
        prompt = VOCAB_PROMPT if self.settings.bilingual_prompt else None
        lang = self.settings.language
        if lang in ("auto", "any"):
            allowed = ("en", "id") if lang == "auto" else ()
            prefer = None if self.settings.prefer_language == "none" else self.settings.prefer_language
            return self.whisper.transcribe(wav, timeout_s, language="auto", prompt=prompt, allowed=allowed,
                                           prefer=prefer)
        return self.whisper.transcribe(wav, timeout_s, language=lang, prompt=prompt)

    def _options(self) -> dict:
        return {"strategy": self.settings.insertion_strategy, "restore_clipboard": self.settings.restore_clipboard,
                "model": self.settings.whisper_model}

    def ai_config(self, **overrides):
        from omonflow.refinement.ai import AIConfig
        s = self.settings
        cfg = AIConfig(base_url=s.ai_base_url, model=s.ai_model, api_key=self.keys.get(s.ai_base_url),
                       timeout_s=float(s.ai_timeout_s), temperature=float(s.ai_temperature),
                       instructions=s.ai_instructions, language=s.language)
        for k, v in overrides.items():
            setattr(cfg, k, v)
        return cfg

    def history(self):
        if not self.settings.history_enabled:
            return None
        if self._history is None:
            from omonflow.storage.history import History
            self._history = History(data_dir() / "history.sqlite3")
        return self._history

    def show_history(self) -> None:
        from omonflow.ui.history import HistoryWindow
        if getattr(self, "_history_win", None) is None:
            self._history_win = HistoryWindow(self.history, self.settings)
        self._history_win.refresh()
        self._history_win.show()
        self._history_win.raise_()
        self._history_win.activateWindow()

    def _clear_history(self) -> None:
        h = self.history()
        if h is not None:
            h.clear()
        elif (data_dir() / "history.sqlite3").exists():
            from omonflow.storage.history import History
            History(data_dir() / "history.sqlite3").clear()

    def insertion_info(self) -> dict:
        perm = self.inserter.permission() if self.inserter is not None else {"accessibility": None}
        last = self.orch.last_insert if hasattr(self, "orch") else None
        return {"adapter": getattr(self.inserter, "name", None), **perm,
                "last": None if last is None else {"outcome": last.outcome, "method": last.method,
                                                   "verified": last.verified, "ms": last.ms, "error": last.error,
                                                   "steps": last.steps}}

    def _request_accessibility(self) -> None:
        if self.inserter is not None and hasattr(self.inserter, "request_permission"):
            self.inserter.request_permission()

    # ------------------------------------------------------------ speech recognition
    def _model_selected(self, model_id: str) -> None:
        # A hand-picked model makes the profile "custom" (profiles never override choices).
        self._engine_config(model_id, self.settings.whisper_backend, self.settings.whisper_threads, "custom")

    def _engine_config(self, model: str, device: str, threads: int, profile: str) -> None:
        """Request a new engine configuration; settings are saved only once it works (SPEC §9.8)."""
        self._pending_engine = {"whisper_model": model, "whisper_backend": device, "whisper_threads": threads,
                                "profile": profile}
        self.whisper.ensure_running(model, device=device, threads=threads)

    def _commit_engine_config(self, subject: str, task: str, ok: bool, message: str) -> None:
        if task != "engine" or self._pending_engine is None or subject != self._pending_engine["whisper_model"]:
            return
        pending, self._pending_engine = self._pending_engine, None
        if ok:
            for k, v in pending.items():
                setattr(self.settings, k, v)
            self._save()
            self.settings_win.refresh_models()
        else:
            LOG.info("engine configuration not applied: %s", message)

    def _unload_changed(self, minutes: int) -> None:
        self.settings.unload_idle_min = minutes
        self.whisper.set_unload_idle(minutes)
        self._save()

    def _whisper_task_done(self, model_id: str, task: str, ok: bool, message: str) -> None:
        if not ok and task in ("engine", "download", "import"):
            self._notice("error", f"Speech recognition: {message}")
        if ok and task in ("download", "import") and model_id == self.settings.whisper_model:
            self.whisper.ensure_running(model_id)  # the chosen model just became available

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
        label = self.orch.widget_label if state == State.SUCCESS.value else self.orch.error_label
        self.widget.set_error_payload(bool(self.orch.transcript), self.orch.retry_action is not None, label)
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
            "Audio backend": self.audio.name + (" (degraded fallback)" if getattr(self.audio, "degraded", False) else "")
            + (f": {self.audio_fallback_reason}" if getattr(self, "audio_fallback_reason", None) else ""),
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
        self.whisper.shutdown()  # stops only the OmonFlow-owned whisper-server
        if self._history is not None:
            self._history.close()
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
    code = app.exec()
    stuck = _audio_stuck(omon.audio)
    if stuck:
        # A capture is stuck inside the audio driver (seen with PortAudio on
        # macOS). Normal interpreter shutdown could block on it and keep the
        # microphone open; ending the process releases the device.
        LOG.warning("audio capture still stuck at exit (%s); exiting immediately", stuck)
        logging.shutdown()
        os._exit(code)
    return code


def _audio_stuck(audio) -> str | None:
    try:
        if audio.stream_open():
            return "stream still open"
        if audio.retired_count():
            return f"{audio.retired_count()} retired capture worker(s)"
    except Exception as exc:  # noqa: BLE001
        return f"state unknown: {exc}"
    return None


if __name__ == "__main__":
    sys.exit(main())
