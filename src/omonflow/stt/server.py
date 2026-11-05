"""Managed whisper.cpp server (SPEC §8.1).

- Finds a whisper-server executable (env override → bundled → PATH).
- Starts it on a free localhost port, waits for /health = ok.
- Records its PID/port/executable in a pidfile, so a crashed earlier run's
  server can be cleaned up, and so only OmonFlow-owned processes are ever
  stopped (another whisper-server the user runs is never touched).
- Watches for unexpected exits and restarts with a bounded policy.
- Captures logs to <log dir>/whisper-server.log and extracts the backend
  actually used (evidence lines from ggml/whisper, never assumed).
- Switches models with /load; rolls back to the previous model on failure.
- VAD is never enabled (push-to-talk boundaries are explicit, SPEC §5).
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path

LOG = logging.getLogger("omonflow.whisper")

# Vocabulary hint only. It deliberately names no language: an earlier hint that
# began "Bahasa Indonesia and English mixed conversation" nudged English
# dictation into Indonesian. The language is decided separately (transcribe_auto).
VOCAB_PROMPT = ("GPU, VRAM, CPU, Docker, Proxmox, Linux, macOS, server, utilization, memory, AI, LLM, model, "
                "deploy, production, coding, network.")
BILINGUAL_PROMPT = VOCAB_PROMPT  # backwards-compatible name
CONFIDENT_LOGPROB = -0.15  # mean token log-prob above which Whisper's language choice is accepted
TIE_LOGPROB = 0.02         # differences below this are ties (the preferred language wins)
LANG_CODES = {"english": "en", "indonesian": "id", "malay": "ms", "javanese": "jw", "sundanese": "su",
              "tagalog": "tl", "dutch": "nl", "welsh": "cy", "chinese": "zh", "japanese": "ja", "korean": "ko",
              "arabic": "ar", "spanish": "es", "french": "fr", "german": "de"}
EVIDENCE = re.compile(r"using (\w+) backend|ggml_metal_init: found device|ggml_cuda_init: found|"
                      r"ggml_vulkan: Found|system_info|no GPU found|whisper_backend_init", re.I)
RESTART_LIMIT = 3            # restarts allowed ...
RESTART_WINDOW_S = 300       # ... within this window


class WhisperError(Exception):
    pass


@dataclass
class ServerInfo:
    executable: str
    source: str               # env | bundled | path
    version: str | None = None


@dataclass
class ServerStatus:  # noqa: D101
    state: str = "stopped"    # stopped | starting | loading | ready | restarting | failed
    pid: int | None = None
    port: int | None = None
    model: str | None = None
    backend: str | None = None
    evidence: list[str] = field(default_factory=list)
    start_ms: float | None = None   # process start → ready (includes model load)
    load_ms: float | None = None    # last /load duration
    restarts: int = 0
    error: str | None = None
    devices: list = field(default_factory=list)  # stt.backends.Device list from the log


def find_executable(bundle_dir: Path | None = None) -> ServerInfo | None:
    env = os.environ.get("OMONFLOW_WHISPER_SERVER")
    if env:
        return ServerInfo(env, "env") if Path(env).exists() else None
    exe = "whisper-server.exe" if sys.platform == "win32" else "whisper-server"
    candidates = []
    if bundle_dir:
        candidates.append(bundle_dir / exe)
    if getattr(sys, "frozen", False):  # PyInstaller bundle
        candidates.append(Path(sys.executable).parent.parent / "Resources" / "whisper" / exe)
        candidates.append(Path(sys.executable).parent / "whisper" / exe)
    for c in candidates:
        if c.exists():
            return ServerInfo(str(c), "bundled")
    found = shutil.which(exe)
    if not found and sys.platform != "win32":
        # Apps started from Finder get a minimal PATH; until whisper-server is
        # bundled (M09) also look in the standard package-manager locations.
        for d in ("/opt/homebrew/bin", "/usr/local/bin"):
            if Path(d, exe).exists():
                found = str(Path(d, exe))
                break
    if found:
        real = os.path.realpath(found)
        m = re.search(r"/Cellar/whisper[.-]cpp/([^/]+)/", real)
        return ServerInfo(found, "path", m.group(1) if m else None)
    return None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _cmdline(pid: int) -> str:
    if sys.platform == "win32":
        return ""
    try:
        return subprocess.run(["ps", "-p", str(pid), "-o", "command="], capture_output=True,
                              text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


class WhisperServer:
    def __init__(self, info: ServerInfo, state_dir: Path, log_dir: Path, threads: int | None = None,
                 use_gpu: bool = True, on_status=None, extra_args: list[str] | None = None,
                 log_name: str = "whisper-server.log", pid_name: str = "whisper-server.pid.json") -> None:
        self.info = info
        self.state_dir = state_dir
        self.log_dir = log_dir
        self.threads = threads or max(1, min(8, (os.cpu_count() or 4) - 2))
        self.use_gpu = use_gpu
        self.extra_args = extra_args or []
        self.on_status = on_status or (lambda st: None)
        self.status = ServerStatus()
        self.pidfile = state_dir / pid_name
        self.log_path = log_dir / log_name
        self._proc: subprocess.Popen | None = None
        self._lock = threading.RLock()
        self._stopping = False
        self._restart_times: list[float] = []
        self._monitor: threading.Thread | None = None
        self._token = uuid.uuid4().hex[:12]

    # ------------------------------------------------------------ lifecycle
    def start(self, model_path: Path, timeout_s: float = 120) -> ServerStatus:
        with self._lock:
            if self._proc and self._proc.poll() is None:
                raise WhisperError("whisper-server is already running")
            self.cleanup_stale()
            self._stopping = False
            self._spawn(Path(model_path))
        self._wait_ready(timeout_s)
        self._start_monitor()
        return self.status

    def _spawn(self, model_path: Path) -> None:
        if not model_path.exists():
            raise WhisperError(f"model file not found: {model_path}")
        port = free_port()
        args = [self.info.executable, "--model", str(model_path), "--host", "127.0.0.1", "--port", str(port),
                "--threads", str(self.threads), "--inference-path", "/inference"]
        if not self.use_gpu and "--no-gpu" not in self.extra_args:
            args.append("--no-gpu")
        args += self.extra_args
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)  # also the server's working directory
        if self.log_path.exists():
            self.log_path.replace(self.log_path.with_suffix(".log.1"))
        log = open(self.log_path, "w", encoding="utf-8", errors="replace")
        kwargs = {}
        if sys.platform != "win32":
            kwargs["start_new_session"] = True  # never receives our terminal's Ctrl-C
        self._t0 = time.perf_counter()
        self._proc = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                      cwd=str(self.state_dir), **kwargs)
        log.close()
        self.status = ServerStatus(state="starting", pid=self._proc.pid, port=port, model=str(model_path),
                                   restarts=self.status.restarts)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.pidfile.write_text(json.dumps({"pid": self._proc.pid, "port": port, "exe": self.info.executable,
                                            "token": self._token, "started": time.time()}))
        LOG.info("whisper-server started pid=%s port=%s model=%s", self._proc.pid, port, model_path.name)
        self._emit()

    def _wait_ready(self, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                code = None if proc is None else proc.returncode
                self.status.state, self.status.error = "failed", f"whisper-server exited (code {code}) during start"
                self._read_evidence()
                self._emit()
                raise WhisperError(f"{self.status.error}. See {self.log_path}")
            h = self.health()
            if h == "ok":
                self.status.start_ms = round((time.perf_counter() - self._t0) * 1000, 1)
                self.status.state = "ready"
                self._read_evidence()
                self._emit()
                return
            if h == "loading" and self.status.state != "loading":
                self.status.state = "loading"
                self._emit()
            time.sleep(0.1)
        self.stop()
        self.status.state, self.status.error = "failed", f"whisper-server not ready within {timeout_s:.0f} s"
        self._emit()
        raise WhisperError(self.status.error)

    def stop(self, timeout_s: float = 5) -> None:
        """Stop the OmonFlow-owned server only."""
        with self._lock:
            self._stopping = True
            proc, self._proc = self._proc, None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout_s)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(5)
        self.pidfile.unlink(missing_ok=True)
        if self.status.state != "failed":
            self.status.state = "stopped"
        self.status.pid = None
        self._emit()

    def cleanup_stale(self) -> str | None:
        """Stop a server left behind by an earlier OmonFlow run (crash), if it
        is provably ours: same PID, still our executable on our recorded port."""
        try:
            rec = json.loads(self.pidfile.read_text())
        except (OSError, ValueError):
            return None
        pid, port, exe = rec.get("pid"), rec.get("port"), rec.get("exe")
        self.pidfile.unlink(missing_ok=True)
        if not pid or not _pid_alive(pid):
            return None
        cmd = _cmdline(pid)
        if exe and exe in cmd and f"--port {port}" in cmd:
            LOG.warning("stopping stale OmonFlow whisper-server pid=%s from an earlier run", pid)
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                return None
            for _ in range(50):
                if not _pid_alive(pid):
                    break
                time.sleep(0.1)
            return f"stopped stale server pid {pid}"
        LOG.info("pidfile pid=%s is not an OmonFlow whisper-server; left alone", pid)
        return None

    # ------------------------------------------------------------ monitor / restart
    def _start_monitor(self) -> None:
        if self._monitor and self._monitor.is_alive():
            return
        self._monitor = threading.Thread(target=self._watch, name="omonflow-whisper-watch", daemon=True)
        self._monitor.start()

    def _watch(self) -> None:
        while True:
            time.sleep(0.25)
            with self._lock:
                if self._stopping:
                    return
                proc = self._proc
                if proc is None or proc.poll() is None:
                    continue
                code = proc.returncode
                model = Path(self.status.model) if self.status.model else None
                now = time.monotonic()
                self._restart_times = [t for t in self._restart_times if now - t < RESTART_WINDOW_S]
                if len(self._restart_times) >= RESTART_LIMIT or model is None:
                    self.status.state = "failed"
                    self.status.error = (f"whisper-server exited unexpectedly (code {code}); restart limit "
                                         f"reached ({RESTART_LIMIT} in {RESTART_WINDOW_S // 60} min)")
                    self._proc = None
                    self._emit()
                    LOG.error(self.status.error)
                    return
                self._restart_times.append(now)
                self.status.restarts += 1
                self.status.state = "restarting"
                self.status.error = f"whisper-server exited unexpectedly (code {code}); restarting"
                LOG.warning(self.status.error)
                self._emit()
                backoff = 2 ** (len(self._restart_times) - 1)
            time.sleep(backoff)
            try:
                with self._lock:
                    if self._stopping:
                        return
                    self._spawn(model)
                self._wait_ready(120)
                self.status.error = None
                self._emit()
            except WhisperError as exc:
                LOG.error("restart failed: %s", exc)

    # ------------------------------------------------------------ HTTP
    def _url(self, path: str) -> str:
        if not self.status.port:
            raise WhisperError("whisper-server is not running")
        return f"http://127.0.0.1:{self.status.port}{path}"

    def health(self) -> str:
        """ok | loading | down"""
        try:
            with urllib.request.urlopen(self._url("/health"), timeout=2) as r:
                return "ok" if json.load(r).get("status") == "ok" else "loading"
        except urllib.error.HTTPError as exc:
            return "loading" if exc.code == 503 else "down"
        except (urllib.error.URLError, OSError, ValueError, WhisperError):
            return "down"

    def load_model(self, model_path: Path, timeout_s: float = 180) -> float:
        """Switch models via /load. On failure the previous model is reloaded."""
        previous = self.status.model
        t0 = time.perf_counter()
        try:
            self._post_load(Path(model_path), timeout_s)
        except WhisperError as exc:
            if not previous or previous == str(model_path):
                raise
            time.sleep(0.3)
            if self._proc is None or self._proc.poll() is not None or self.status.state == "restarting":
                # whisper-server crashes on some bad model files; the monitor
                # restarts it with the previous model (status.model unchanged).
                LOG.warning("whisper-server crashed loading %s; waiting for restart on %s",
                            Path(model_path).name, Path(previous).name)
                deadline = time.monotonic() + timeout_s
                while time.monotonic() < deadline and not (self.status.state == "ready" and self.health() == "ok"):
                    if self.status.state == "failed":
                        raise WhisperError(f"{exc}; the engine could not be restarted") from exc
                    time.sleep(0.1)
                raise WhisperError(f"{Path(model_path).name} could not be loaded (the engine crashed); "
                                   f"restored {Path(previous).name}") from exc
            LOG.warning("model switch failed; restoring %s", previous)
            try:
                self._post_load(Path(previous), timeout_s)
            except WhisperError:
                LOG.exception("rollback failed")
            raise
        self.status.load_ms = round((time.perf_counter() - t0) * 1000, 1)
        self.status.model = str(model_path)
        self._read_evidence()
        self._emit()
        return self.status.load_ms

    def _post_load(self, model_path: Path, timeout_s: float) -> None:
        if not model_path.exists():
            raise WhisperError(f"model file not found: {model_path}")
        body, ctype = _multipart({"model": str(model_path)}, {})
        req = urllib.request.Request(self._url("/load"), data=body, headers={"Content-Type": ctype})
        self.status.state = "loading"
        self._emit()
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as r:
                r.read()
        except urllib.error.HTTPError as exc:
            self.status.state = "failed" if self.health() != "ok" else "ready"
            raise WhisperError(f"model load failed: HTTP {exc.code} {exc.read()[:200]!r}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise WhisperError(f"model load failed: {exc}") from exc
        if self.health() != "ok":
            self.status.state = "failed"
            raise WhisperError("model load failed: server not healthy afterwards")
        self.status.state = "ready"

    def transcribe(self, wav: bytes, language: str = "auto", prompt: str | None = BILINGUAL_PROMPT,
                   timeout_s: float = 120) -> dict:
        fields = {"language": language, "response_format": "json", "temperature": "0.0"}
        if prompt:
            fields.update(prompt=prompt, carry_initial_prompt="true")
        body, ctype = _multipart(fields, {"file": ("audio.wav", wav, "audio/wav")})
        req = urllib.request.Request(self._url("/inference"), data=body, headers={"Content-Type": ctype})
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as r:
                data = json.load(r)
        except urllib.error.HTTPError as exc:
            raise WhisperError(f"transcription failed: HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise WhisperError(f"transcription failed: {exc}") from exc
        if "error" in data:
            raise WhisperError(f"transcription failed: {data['error']}")
        return {"text": (data.get("text") or "").strip(), "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1)}

    def transcribe_auto(self, wav: bytes, allowed: tuple[str, ...] = ("en", "id"), prefer: str | None = None,
                        prompt: str | None = VOCAB_PROMPT, timeout_s: float = 120,
                        confident: float = CONFIDENT_LOGPROB) -> dict:
        """Transcribe in the language actually spoken; never translate.

        When Whisper decodes in the wrong language it *translates* (seen in M06:
        English speech written as Indonesian and vice versa). So:
          1. Let Whisper choose. If it chose one of `allowed` with confidence
             (mean token log-probability >= `confident`), use it: one pass.
          2. Otherwise also transcribe in the other allowed language(s) and keep
             the most confident version (a translation scores clearly lower).
          3. `prefer` only breaks near-ties; it can never force a translation.
        `allowed=()` keeps Whisper's own choice (any language).
        """
        def run(lang: str, fallback: bool) -> tuple[str, float, str | None, float]:
            fields = {"language": lang, "response_format": "verbose_json", "temperature": "0.0",
                      "no_language_probabilities": "true"}
            if not fallback:
                fields["temperature_inc"] = "0.0"  # comparison pass: no slow temperature fallback
            if prompt:
                fields.update(prompt=prompt, carry_initial_prompt="true")
            data, ms = self._post_inference(wav, fields, timeout_s)
            segs = data.get("segments") or []
            n = sum(len(x.get("tokens") or []) for x in segs)
            lp = sum((x.get("avg_logprob") or 0.0) * len(x.get("tokens") or []) for x in segs) / n if n else -9.0
            code = LANG_CODES.get(str(data.get("language", "")).lower(), lang if lang != "auto" else None)
            return (data.get("text") or "").strip(), lp, code, ms

        text, lp, used, ms = run("auto", True)
        results = {used: (lp, text)}
        total_ms, passes = ms, 1
        if allowed and not (used in allowed and lp >= confident):
            for lang in allowed:
                if lang not in results:
                    t2, lp2, _, ms2 = run(lang, False)
                    results[lang] = (lp2, t2)
                    total_ms += ms2
                    passes += 1
        candidates = {k: v for k, v in results.items() if not allowed or k in allowed} or results
        chosen = max(candidates, key=lambda k: candidates[k][0])
        if prefer in candidates and prefer != chosen and candidates[chosen][0] - candidates[prefer][0] < TIE_LOGPROB:
            chosen = prefer
        scores = {k: round(v[0], 3) for k, v in results.items()}
        LOG.info("language: whisper=%s chosen=%s logprob=%s passes=%d", used, chosen, scores, passes)  # never text
        return {"text": candidates[chosen][1], "elapsed_ms": round(total_ms, 1), "language": used, "chosen": chosen,
                "logprobs": scores, "passes": passes}

    def _post_inference(self, wav: bytes, fields: dict, timeout_s: float) -> tuple[dict, float]:
        body, ctype = _multipart(fields, {"file": ("audio.wav", wav, "audio/wav")})
        req = urllib.request.Request(self._url("/inference"), data=body, headers={"Content-Type": ctype})
        t0 = time.perf_counter()
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as r:
                data = json.load(r)
        except urllib.error.HTTPError as exc:
            raise WhisperError(f"transcription failed: HTTP {exc.code}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise WhisperError(f"transcription failed: {exc}") from exc
        if "error" in data:
            raise WhisperError(f"transcription failed: {data['error']}")
        return data, round((time.perf_counter() - t0) * 1000, 1)

    # ------------------------------------------------------------ helpers
    def _read_evidence(self) -> None:
        try:
            lines = self.log_path.read_text(errors="replace").splitlines()
        except OSError:
            return
        self.status.evidence = [l.strip() for l in lines if EVIDENCE.search(l)][-8:]
        self.status.backend = parse_backend(lines, self.use_gpu)
        from .backends import parse_devices
        self.status.devices = parse_devices(lines)

    def refresh_evidence(self) -> None:
        """The server's log is written with buffering: re-read after a while."""
        self._read_evidence()
        self._emit()

    def _emit(self) -> None:
        try:
            self.on_status(self.status)
        except Exception:  # noqa: BLE001
            LOG.debug("status callback failed", exc_info=True)


def parse_backend(lines: list[str], use_gpu: bool = True) -> str | None:
    """Backend actually used, from whisper/ggml's own log. None = not known yet.

    "loaded X backend" only means a library was loaded; the decision is the
    "whisper_backend_init_gpu: using X backend" / "no GPU found" line.
    """
    names = {"MTL": "Metal", "METAL": "Metal", "CUDA": "CUDA", "VULKAN": "Vulkan", "BLAS": "CPU (BLAS)",
             "CPU": "CPU", "SYCL": "SYCL", "OPENCL": "OpenCL"}
    chosen = None
    for line in lines:
        if "use gpu" in line and line.rstrip().endswith("= 0"):
            chosen = "CPU"
        m = re.search(r"whisper_backend_init_gpu: using (\S+) backend", line)
        if m:
            key = re.sub(r"\d+$", "", m.group(1)).upper()
            chosen = names.get(key, m.group(1))
        elif "no GPU found" in line:
            chosen = "CPU"
    if chosen is None and not use_gpu:
        chosen = "CPU"
    return chosen


def _multipart(fields: dict, files: dict) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    out = []
    for k, v in fields.items():
        out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    for k, (fname, data, ctype) in files.items():
        out.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{fname}"\r\n'
                   f"Content-Type: {ctype}\r\n\r\n".encode() + data + b"\r\n")
    out.append(f"--{boundary}--\r\n".encode())
    return b"".join(out), f"multipart/form-data; boundary={boundary}"
