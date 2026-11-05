"""Speech-recognition engine controller: models + managed whisper-server +
backends (SPEC §8, §9).

All slow work runs on worker threads; results reach the Qt thread via signals.

Configuration changes (model, backend, threads) follow SPEC §9.8: the new
configuration is started *next to* the running one, checked (ready, model
loaded, requested backend actually active, sample transcribed), and only
then swapped in. On any failure the running engine is left untouched.
"""

from __future__ import annotations

import logging
import statistics
import threading
import time
import wave
from dataclasses import asdict
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from omonflow.stt.backends import CPU, LABELS, Validation, ValidationStore, now, server_args
from omonflow.stt.catalog import BY_ID
from omonflow.stt.models import DownloadCancelled, ModelError, ModelStore
from omonflow.stt.server import WhisperError, WhisperServer, find_executable

LOG = logging.getLogger("omonflow.whisper")
SAMPLE = Path(__file__).resolve().parent.parent / "resources" / "jfk.wav"
SAMPLE_WORDS = ("ask not", "your country")


def sample_audio() -> tuple[bytes, float]:
    data = SAMPLE.read_bytes()
    with wave.open(str(SAMPLE)) as w:
        return data, w.getnframes() / w.getframerate()


class WhisperController(QObject):
    status_changed = Signal(dict)
    models_changed = Signal()
    progress = Signal(str, str, float, float)  # (model id, phase, fraction 0..1 or -1, bytes/s)
    task_done = Signal(str, str, bool, str)    # (subject, task, ok, message)
    backends_changed = Signal()

    def __init__(self, store: ModelStore, state_dir: Path, log_dir: Path, server_factory=None,
                 executable_finder=find_executable, parent=None) -> None:
        super().__init__(parent)
        self.store = store
        self.state_dir = state_dir
        self.log_dir = log_dir
        self.info = executable_finder()
        self._factory = server_factory or self._make_server
        self.server: WhisperServer | None = None
        self.devices = [CPU]                 # learned from whisper.cpp's own log
        self.validations = ValidationStore(state_dir / "backend-validations.json")
        self.config = {"model": None, "device": "auto", "threads": 0}
        self.last_transcription: dict | None = None
        self.unload_idle_min = 0
        self._idle_timer: threading.Timer | None = None
        self._cancel: dict[str, threading.Event] = {}
        self._busy: set[str] = set()
        self._op_lock = threading.Lock()  # serialises start/switch/stop/validate

    def _make_server(self, info, threads=0, extra_args=None, log_name="whisper-server.log",
                     pid_name="whisper-server.pid.json"):
        return WhisperServer(info, self.state_dir, self.log_dir, threads=threads or None, on_status=self._on_status,
                             extra_args=extra_args or [], log_name=log_name, pid_name=pid_name)

    # ------------------------------------------------------------ status
    def snapshot(self) -> dict:
        st = asdict(self.server.status) if self.server else {"state": "stopped"}
        st["devices"] = [d.key for d in self.devices]
        st["executable"] = self.info.executable if self.info else None
        st["source"] = self.info.source if self.info else None
        st["version"] = self.info.version if self.info else None
        st["selected"] = dict(self.config)
        st["last_transcription"] = self.last_transcription
        if not self.info:
            st["state"] = "unavailable"
            st["error"] = ("whisper-server was not found. Development builds use one on PATH "
                           "(e.g. Homebrew whisper-cpp) or OMONFLOW_WHISPER_SERVER; packaged builds bundle it.")
        return st

    def _on_status(self, status) -> None:
        if status.devices and [d.key for d in status.devices] != [d.key for d in self.devices]:
            self.devices = list(status.devices)
            self.backends_changed.emit()
        self.status_changed.emit(self.snapshot())

    def busy(self, model_id: str) -> bool:
        return model_id in self._busy

    def device(self, key: str):
        return next((d for d in self.devices if d.key == key), None)

    def validation(self, key: str) -> Validation | None:
        if not self.info:
            return None
        return self.validations.latest(key, self.info.executable, self.info.version)

    # ------------------------------------------------------------ engine
    def ensure_running(self, model_id: str, device: str | None = None, threads: int | None = None) -> None:
        """Start the engine, or switch it to a new configuration (with rollback)."""
        if not self.info:
            self.status_changed.emit(self.snapshot())
            return
        target = {"model": model_id, "device": device if device is not None else self.config["device"],
                  "threads": threads if threads is not None else self.config["threads"]}
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
                try:
                    running = self.server is not None and self.server.status.state in ("ready", "loading", "restarting")
                    same_backend = (target["device"] == self.config["device"]
                                    and target["threads"] == self.config["threads"])
                    if running and same_backend:
                        if self.config["model"] != model_id:
                            self.server.load_model(self.store.path(model_id))  # /load with its own rollback
                    elif running:
                        self._swap(target)
                    else:
                        self._start(target)
                    self.config = target
                    self._arm_idle_timer()
                    threading.Timer(2.0, self._refresh).start()
                    self.task_done.emit(model_id, "engine", True, "Speech recognition is ready.")
                except WhisperError as exc:
                    LOG.error("engine: %s", exc)
                    self.task_done.emit(model_id, "engine", False, str(exc))
                self.status_changed.emit(self.snapshot())

        threading.Thread(target=work, name="omonflow-whisper-start", daemon=True).start()

    def _args_for(self, device_key: str) -> list[str]:
        if device_key == "auto":
            return []
        dev = self.device(device_key)
        if dev is None:
            raise WhisperError(f"The selected backend ({device_key}) is not available in this whisper.cpp build. "
                               "Choose Automatic or CPU in Settings → Hardware & Acceleration.")
        return server_args(dev)

    def _start(self, target: dict) -> None:
        srv = self._factory(self.info, target["threads"], self._args_for(target["device"]))
        self.server = srv
        srv.start(self.store.path(target["model"]))
        self._check_backend(srv, target["device"])

    def _swap(self, target: dict) -> None:
        """Start the candidate next to the running server; swap only if it passes."""
        cand = self._factory(self.info, target["threads"], self._args_for(target["device"]),
                             log_name="whisper-server-candidate.log", pid_name="whisper-server-candidate.pid.json")
        try:
            cand.start(self.store.path(target["model"]))
            self._check_backend(cand, target["device"])
            self._self_test(cand)
        except WhisperError:
            cand.stop()
            raise
        old, self.server = self.server, cand
        main_pid, main_log = old.pidfile, old.log_path
        old.stop()
        # The promoted server takes over the main pidfile and log names, so the
        # next candidate's stale-process cleanup can never mistake it for a
        # leftover and kill the running engine. (Renaming an open log is fine on
        # POSIX: the server keeps writing to the same file.)
        try:
            cand.pidfile.replace(main_pid)
            cand.pidfile = main_pid
            cand.log_path.replace(main_log)
            cand.log_path = main_log
        except OSError:
            LOG.warning("could not rename promoted server files", exc_info=True)
        LOG.info("engine switched to %s", target)

    @staticmethod
    def _check_backend(srv: WhisperServer, device_key: str) -> None:
        """The requested backend must be the one whisper.cpp reports using."""
        if device_key == "auto":
            return
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            srv.refresh_evidence()  # the server's log is written with buffering
            if srv.status.backend:
                break
            time.sleep(0.2)
        want = device_key.split(":")[0]
        got = (srv.status.backend or "").lower()
        ok = got.startswith("cpu") if want == "cpu" else LABELS[want].split()[-1].lower() in got
        if not ok:
            raise WhisperError(f"{LABELS.get(want, want)} was requested but whisper.cpp reports "
                               f"'{srv.status.backend or 'unknown'}'. The previous configuration was kept.")

    @staticmethod
    def _self_test(srv: WhisperServer) -> dict:
        wav, secs = sample_audio()
        r = srv.transcribe(wav, language="en", prompt=None, timeout_s=120)
        text = r["text"].lower()
        if not all(w in text for w in SAMPLE_WORDS):
            raise WhisperError(f"self-test transcription was wrong: {r['text'][:80]!r}")
        return {**r, "audio_s": secs}

    def _refresh(self) -> None:
        if self.server:
            self.server.refresh_evidence()

    def restart(self, model_id: str) -> None:
        self.stop()
        self.ensure_running(model_id)

    def stop(self) -> None:
        with self._op_lock:
            if self.server:
                self.server.stop()
        self.status_changed.emit(self.snapshot())

    # ------------------------------------------------------------ transcription
    def transcribe(self, wav: bytes, timeout_s: float, language: str = "auto", prompt: str | None = None,
                   allowed: tuple[str, ...] = ("en", "id"), prefer: str | None = None) -> str:
        """Blocking (worker thread). Reloads an engine unloaded while idle, then waits for it."""
        if (self.server is None or self.server.status.state == "stopped") and self.config["model"]:
            self.ensure_running(self.config["model"])
        deadline = time.monotonic() + 30
        while True:
            srv = self.server
            state = srv.status.state if srv else "stopped"
            if state == "ready":
                break
            if state in ("starting", "loading", "restarting", "stopped") and time.monotonic() < deadline:
                time.sleep(0.1)
                continue
            snap = self.snapshot()
            reason = snap.get("error") or f"engine is {state}"
            raise WhisperError(f"speech recognition is not ready ({reason}). See Settings → Speech Recognition")
        audio_s = max(0.0, (len(wav) - 44) / 32000)  # 16 kHz mono s16 WAV
        if language == "auto" and len(allowed) >= 2:
            # Per sentence: a dictation may switch between English and Indonesian.
            r = srv.transcribe_mixed(wav, allowed=allowed, prefer=prefer, prompt=prompt, timeout_s=timeout_s)
        elif language == "auto":  # "any language": Whisper's own choice
            r = srv.transcribe_auto(wav, allowed=allowed, prefer=prefer, prompt=prompt, timeout_s=timeout_s)
        else:
            r = srv.transcribe(wav, language=language, prompt=prompt, timeout_s=timeout_s)
        self.last_transcription = {"ms": r["elapsed_ms"], "audio_s": round(audio_s, 2),
                                   "rtf": round(r["elapsed_ms"] / 1000 / audio_s, 3) if audio_s else None,
                                   "model": Path(srv.status.model or "").name, "backend": srv.status.backend,
                                   "language": r.get("chosen") or language, "passes": r.get("passes", 1),
                                   "at": time.strftime("%H:%M:%S")}
        self._arm_idle_timer()
        self.status_changed.emit(self.snapshot())
        return r["text"]

    def prepare(self) -> None:
        """Called when dictation starts: reload an unloaded engine in the background."""
        if self.config["model"] and (self.server is None or self.server.status.state == "stopped"):
            self.ensure_running(self.config["model"])

    # ------------------------------------------------------------ idle unload
    def set_unload_idle(self, minutes: int) -> None:
        self.unload_idle_min = max(0, int(minutes))
        self._arm_idle_timer()

    def _arm_idle_timer(self) -> None:
        if self._idle_timer:
            self._idle_timer.cancel()
            self._idle_timer = None
        if self.unload_idle_min > 0:
            self._idle_timer = threading.Timer(self.unload_idle_min * 60, self._unload)
            self._idle_timer.daemon = True
            self._idle_timer.start()

    def _unload(self) -> None:
        LOG.info("unloading the model after %d idle minute(s)", self.unload_idle_min)
        self.stop()

    # ------------------------------------------------------------ validation / benchmark
    def validate(self, device_key: str, model_id: str, runs: int = 3) -> None:
        """Benchmark a backend in a separate temporary server (never disturbs dictation)."""
        def work():
            v = self._validate(device_key, model_id, runs)
            self.task_done.emit(device_key, "validate", v.ok, v.summary)
            self.backends_changed.emit()

        threading.Thread(target=work, name="omonflow-validate", daemon=True).start()

    def _validate(self, device_key: str, model_id: str, runs: int) -> Validation:
        err, start_ms, tms, secs, backend = None, None, None, None, None
        srv = None
        try:
            if not self.info:
                raise WhisperError("whisper-server not found")
            if not self.store.status(model_id).installed:
                raise WhisperError(f"model '{model_id}' is not installed")
            srv = self._factory(self.info, self.config["threads"], self._args_for(device_key),
                                log_name="whisper-server-validate.log", pid_name="whisper-server-validate.pid.json")
            srv.on_status = lambda st: None
            st = srv.start(self.store.path(model_id))
            start_ms = st.start_ms
            self._check_backend(srv, device_key)
            self._self_test(srv)  # warm-up + correctness
            times = []
            for _ in range(runs):
                r = self._self_test(srv)
                times.append(r["elapsed_ms"])
                secs = r["audio_s"]
            tms = statistics.median(times)
            srv.refresh_evidence()
            backend = srv.status.backend
        except WhisperError as exc:
            err = str(exc)
        finally:
            if srv:
                srv.stop()
        v = Validation(device_key, self.info.executable if self.info else "", self.info.version if self.info else None,
                       model_id, err is None, backend, start_ms, tms, secs,
                       round(tms / 1000 / secs, 4) if tms and secs else None, err, now())
        if self.info:
            self.validations.add(v)
        LOG.info("validation %s/%s: %s", device_key, model_id, v.summary)
        return v

    # ------------------------------------------------------------ models
    def refresh_catalog(self) -> None:
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
            except (DownloadCancelled, ModelError, OSError) as exc:
                ok, msg = False, str(exc)
            finally:
                self._busy.discard(model_id)
                self._cancel.pop(model_id, None)
            self.task_done.emit(model_id, task, ok, msg)
            self.models_changed.emit()

        threading.Thread(target=work, name=f"omonflow-model-{task}", daemon=True).start()

    def shutdown(self) -> None:
        if self._idle_timer:
            self._idle_timer.cancel()
        for ev in self._cancel.values():
            ev.set()
        if self.server:
            self.server.stop()
