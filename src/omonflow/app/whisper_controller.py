"""Speech-recognition engine controller: models + managed whisper-server.

All slow work (downloads, hashing, starting the server, loading models) runs
on worker threads; results reach the Qt thread through signals.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import asdict
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from omonflow.stt.catalog import BY_ID
from omonflow.stt.models import DownloadCancelled, ModelError, ModelStore
from omonflow.stt.server import WhisperError, WhisperServer, find_executable

LOG = logging.getLogger("omonflow.whisper")


class WhisperController(QObject):
    status_changed = Signal(dict)            # ServerStatus as dict (+ executable info)
    models_changed = Signal()
    progress = Signal(str, str, float, float)  # (model id, phase, fraction 0..1 or -1, bytes/s)
    task_done = Signal(str, str, bool, str)    # (model id, task, ok, message)

    def __init__(self, store: ModelStore, state_dir: Path, log_dir: Path, server_factory=None,
                 executable_finder=find_executable, parent=None) -> None:
        super().__init__(parent)
        self.store = store
        self.state_dir = state_dir
        self.log_dir = log_dir
        self.info = executable_finder()
        self._factory = server_factory or (lambda info: WhisperServer(info, state_dir, log_dir,
                                                                      on_status=self._on_status))
        self.server: WhisperServer | None = None
        self._cancel: dict[str, threading.Event] = {}
        self._busy: set[str] = set()
        self._op_lock = threading.Lock()  # serialises start/switch/stop

    # ------------------------------------------------------------ status
    def snapshot(self) -> dict:
        st = asdict(self.server.status) if self.server else {"state": "stopped"}
        st["executable"] = self.info.executable if self.info else None
        st["source"] = self.info.source if self.info else None
        st["version"] = self.info.version if self.info else None
        if not self.info:
            st["state"] = "unavailable"
            st["error"] = ("whisper-server was not found. Development builds use one on PATH "
                           "(e.g. Homebrew whisper-cpp) or OMONFLOW_WHISPER_SERVER; packaged builds bundle it.")
        return st

    def _on_status(self, _status) -> None:
        self.status_changed.emit(self.snapshot())

    def busy(self, model_id: str) -> bool:
        return model_id in self._busy

    # ------------------------------------------------------------ engine
    def ensure_running(self, model_id: str) -> None:
        """Start the server with `model_id`, or switch the running server to it."""
        if not self.info:
            self.status_changed.emit(self.snapshot())
            return
        mst = self.store.status(model_id)
        if not mst.installed:
            self.status_changed.emit({**self.snapshot(), "state": "no_model",
                                      "error": f"Model '{model_id}' is not installed."})
            return
        if mst.state == "corrupt":
            msg = f"Model '{model_id}' is damaged ({mst.detail}). Download it again."
            self.status_changed.emit({**self.snapshot(), "error": msg})
            self.task_done.emit(model_id, "engine", False, msg)
            return

        def work():
            with self._op_lock:
                path = self.store.path(model_id)
                try:
                    if self.server and self.server.status.state in ("ready", "loading", "restarting"):
                        if self.server.status.model != str(path):
                            self.server.load_model(path)
                    else:
                        self.server = self._factory(self.info)
                        self.server.start(path)
                    threading.Timer(2.0, self._refresh).start()  # buffered log lines
                    self.task_done.emit(model_id, "engine", True, "Speech recognition is ready.")
                except WhisperError as exc:
                    LOG.error("engine: %s", exc)
                    self.task_done.emit(model_id, "engine", False, str(exc))
                self.status_changed.emit(self.snapshot())

        threading.Thread(target=work, name="omonflow-whisper-start", daemon=True).start()

    def _refresh(self) -> None:
        if self.server:
            self.server.refresh_evidence()

    def transcribe(self, wav: bytes, timeout_s: float, language: str = "auto", prompt: str | None = None) -> str:
        """Blocking (call from a worker thread). Waits briefly if the engine is still loading."""
        import time

        deadline = time.monotonic() + 20
        while True:
            srv = self.server
            state = srv.status.state if srv else "stopped"
            if state == "ready":
                break
            if state in ("starting", "loading", "restarting") and time.monotonic() < deadline:
                time.sleep(0.1)
                continue
            snap = self.snapshot()
            reason = snap.get("error") or f"engine is {state}"
            raise WhisperError(f"speech recognition is not ready ({reason}). See Settings → Speech Recognition")
        return srv.transcribe(wav, language=language, prompt=prompt, timeout_s=timeout_s)["text"]

    def restart(self, model_id: str) -> None:
        self.stop()
        self.ensure_running(model_id)

    def stop(self) -> None:
        with self._op_lock:
            if self.server:
                self.server.stop()
        self.status_changed.emit(self.snapshot())

    # ------------------------------------------------------------ models
    def refresh_catalog(self) -> None:
        """Fetch sizes/checksums in the background (cached for a week)."""
        def work():
            self.store.meta.get("ggml-small.bin", refresh=True, network=True)
            self.models_changed.emit()

        threading.Thread(target=work, name="omonflow-catalog", daemon=True).start()

    def download(self, model_id: str) -> None:
        self._run(model_id, "download", lambda cancel, prog: self.store.download(model_id, prog, cancel))

    def verify(self, model_id: str) -> None:
        self._run(model_id, "verify", lambda cancel, prog: self.store.verify(model_id, prog))

    def import_file(self, path: Path) -> None:
        spec = next((m for m in BY_ID.values() if m.filename == Path(path).name), None)
        key = spec.id if spec else Path(path).name
        self._run(key, "import", lambda cancel, prog: self.store.import_file(Path(path), prog))

    def cancel(self, model_id: str) -> None:
        ev = self._cancel.get(model_id)
        if ev:
            ev.set()

    def remove(self, model_id: str, active_model: str) -> str | None:
        """Returns an error message, or None."""
        if model_id == active_model and self.server and self.server.status.state in ("ready", "loading"):
            return "This model is in use. Choose another model first."
        if self.busy(model_id):
            return "Wait for the current download or check to finish."
        self.store.remove(model_id)
        self.models_changed.emit()
        return None

    def _run(self, model_id: str, task: str, fn) -> None:
        if model_id in self._busy:
            return
        self._busy.add(model_id)
        cancel = threading.Event()
        self._cancel[model_id] = cancel
        self.models_changed.emit()

        def prog(done, total, rate):
            self.progress.emit(model_id, task, (done / total) if total else -1.0, rate)

        def work():
            ok, msg = True, ""
            try:
                fn(cancel, prog)
                msg = {"download": "Downloaded and verified.", "verify": "Checksum verified.",
                       "import": "Imported and verified."}[task]
            except DownloadCancelled as exc:
                ok, msg = False, str(exc)
            except (ModelError, OSError) as exc:
                ok, msg = False, str(exc)
            finally:
                self._busy.discard(model_id)
                self._cancel.pop(model_id, None)
            self.task_done.emit(model_id, task, ok, msg)
            self.models_changed.emit()

        threading.Thread(target=work, name=f"omonflow-model-{task}", daemon=True).start()

    def shutdown(self) -> None:
        for ev in self._cancel.values():
            ev.set()
        if self.server:
            self.server.stop()
