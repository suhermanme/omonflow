#!/usr/bin/env python3
"""M04 real-hardware checks: whisper-server lifecycle, model import/switch,
crash recovery, foreign-process safety, real download with resume.

Usage: uv run python tools/bench/whisper_m04.py [--models-from DIR] [--sample WAV] [--skip-download]
"""

import argparse
import json
import os
import platform
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from omonflow.stt.catalog import MetadataCache  # noqa: E402
from omonflow.stt.models import DownloadCancelled, ModelError, ModelStore  # noqa: E402
from omonflow.stt.server import WhisperServer, find_executable, free_port  # noqa: E402


def timed(fn, *a, **kw):
    t = time.perf_counter()
    r = fn(*a, **kw)
    return r, round((time.perf_counter() - t) * 1000, 1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models-from", default=str(Path.home() / "local-ai/whisper.cpp/models"))
    ap.add_argument("--sample", default=str(Path.home() / "local-ai/whisper.cpp/samples/jfk.wav"))
    ap.add_argument("--skip-download", action="store_true")
    ap.add_argument("--out", default=str(ROOT / "docs" / "m04"))
    args = ap.parse_args()
    info = find_executable()
    if not info:
        sys.exit("whisper-server not found")
    tmp = Path(tempfile.mkdtemp(prefix="omonflow-m04-"))
    store = ModelStore(tmp / "models", MetadataCache(tmp / "models" / ".catalog.json"))
    rep = {"machine": platform.platform(), "executable": info.executable, "version": info.version,
           "date": time.strftime("%Y-%m-%d %H:%M"), "import": {}, "runs": {}}
    wav = Path(args.sample).read_bytes()
    with wave.open(args.sample) as w:
        audio_s = w.getnframes() / w.getframerate()
    rep["sample_seconds"] = round(audio_s, 2)

    # 1. Import existing models (hard links, checksum verified)
    for mid in ("small", "large-v3-turbo-q5_0"):
        src = Path(args.models_from) / f"ggml-{mid}.bin"
        if not src.exists():
            rep["import"][mid] = "not available locally"
            continue
        try:
            st, ms = timed(store.import_file, src)
            rep["import"][mid] = {"state": st.state, "ms": ms, "hardlink": store.path(mid).stat().st_ino == src.stat().st_ino}
        except ModelError as exc:
            rep["import"][mid] = f"failed: {exc}"
        print("import", mid, rep["import"][mid], flush=True)
    installed = store.installed_ids()

    # 2. Cold start, transcription, model switch
    for mid in installed:
        starts, trans = [], []
        text = ""
        for _ in range(3):
            s = WhisperServer(info, tmp / "state", tmp / "logs")
            st = s.start(store.path(mid))
            starts.append(st.start_ms)
            s.transcribe(wav, language="en", prompt=None)  # warm-up
            for _ in range(3):
                r = s.transcribe(wav, language="en", prompt=None)
                trans.append(r["elapsed_ms"])
                text = r["text"]
            time.sleep(1.5)
            s.refresh_evidence()
            backend = s.status.backend
            s.stop()
        rep["runs"][mid] = {"start_to_ready_ms": starts, "transcribe_ms_median": statistics.median(trans),
                            "rtf": round(statistics.median(trans) / 1000 / audio_s, 3), "backend": backend, "text": text}
        print(mid, rep["runs"][mid], flush=True)
    if len(installed) >= 2:
        s = WhisperServer(info, tmp / "state", tmp / "logs")
        s.start(store.path(installed[0]))
        sw = [s.load_model(store.path(installed[1])), s.load_model(store.path(installed[0]))]
        rep["model_switch_ms"] = sw
        # rollback: a corrupt file must fail and leave the current model working
        bad = tmp / "ggml-small.en.bin"
        bad.write_bytes(b"lmgg" + b"\0" * 1000)
        try:
            s.load_model(bad)
            rep["rollback"] = "unexpected: bad model loaded"
        except Exception as exc:  # noqa: BLE001
            ok = s.health() == "ok" and bool(s.transcribe(wav, language="en", prompt=None)["text"])
            rep["rollback"] = {"error": str(exc)[:120], "previous_model_still_works": ok}
        # 3. crash recovery
        pid = s.status.pid
        t0 = time.perf_counter()
        os.kill(pid, signal.SIGKILL)
        while not (s.status.state == "ready" and s.status.pid != pid) and time.perf_counter() - t0 < 60:
            time.sleep(0.05)
        rep["kill_to_ready_ms"] = round((time.perf_counter() - t0) * 1000)
        rep["after_restart_transcribes"] = bool(s.transcribe(wav, language="en", prompt=None)["text"])
        # 4. foreign whisper-server left alone
        port = free_port()
        foreign = subprocess.Popen([info.executable, "--model", str(store.path(installed[0])), "--port", str(port)],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(2)
        s.stop()
        s2 = WhisperServer(info, tmp / "state", tmp / "logs")
        s2.start(store.path(installed[0]))
        s2.stop()
        rep["foreign_server_untouched"] = foreign.poll() is None
        foreign.terminate()
        foreign.wait(10)
        print("switch", rep.get("model_switch_ms"), "rollback", rep.get("rollback"), "kill→ready",
              rep.get("kill_to_ready_ms"), "foreign ok", rep.get("foreign_server_untouched"), flush=True)

    # 5. Real download with cancel + resume
    if not args.skip_download:
        cancel = threading.Event()
        marks = {}

        def prog(done, total, rate):
            marks["total"], marks["rate"] = total, rate
            if total and done > total * 0.3:
                cancel.set()
        try:
            t0 = time.perf_counter()
            try:
                store.download("tiny", prog, cancel)
            except DownloadCancelled:
                marks["partial"] = store.status("tiny").partial_bytes
            st = store.download("tiny")
            rep["download_tiny"] = {"state": st.state, "seconds": round(time.perf_counter() - t0, 1),
                                    "resumed_from_bytes": marks.get("partial"), "size": st.installed_size}
        except ModelError as exc:
            rep["download_tiny"] = f"failed: {exc}"
        print("download", rep["download_tiny"], flush=True)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "whisper_m04.json").write_text(json.dumps(rep, indent=2))
    print("saved", out / "whisper_m04.json")


if __name__ == "__main__":
    main()
