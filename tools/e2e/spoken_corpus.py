#!/usr/bin/env python3
"""M05 acoustic end-to-end: macOS `say` → speakers → microphone → OmonFlow
(Rust capture → whisper.cpp → MacInserter) → a test app. Measures word
accuracy, first-word preservation and Stop→inserted latency.

Plays speech aloud. Needs: unlocked screen, speakers on (not headphones),
Accessibility for this process, a whisper-server and a model.

Usage: uv run python tools/e2e/spoken_corpus.py [--model PATH] [--rounds 1]
"""

import argparse
import difflib
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from PySide6.QtWidgets import QApplication  # noqa: E402

from omonflow.app.orchestrator import Orchestrator  # noqa: E402
from omonflow.audio.rust_backend import RustAudio  # noqa: E402
from omonflow.core.states import State  # noqa: E402
from omonflow.platform.macos.insert import MacInserter  # noqa: E402
from omonflow.stt.server import VOCAB_PROMPT, WhisperServer, find_executable  # noqa: E402

CORPUS = [
    ("en", "Samantha", "Please deploy the new Docker image to the staging server today."),
    ("en", "Samantha", "Check the GPU memory usage before you start the training job."),
    ("id", "Damayanti", "Tolong kirim laporan keuangan bulan ini sebelum hari Jumat."),
    ("id", "Damayanti", "Saya akan mengecek server Linux setelah rapat selesai."),
    ("mixed", "Damayanti", "Besok kita deploy aplikasi ke server production pakai Docker."),
    ("mixed", "Damayanti", "Tolong cek VRAM di GPU sebelum training model baru."),
]


def words(s):
    return re.findall(r"[\w']+", s.lower())


def wer(ref, hyp):
    r, h = words(ref), words(hyp)
    sm = difflib.SequenceMatcher(a=r, b=h)
    errors = sum(max(i2 - i1, j2 - j1) for tag, i1, i2, j1, j2 in sm.get_opcodes() if tag != "equal")
    return errors / max(1, len(r))


def pump(app, cond, timeout):
    end = time.monotonic() + timeout
    while time.monotonic() < end and not cond():
        app.processEvents()
        time.sleep(0.01)
    return cond()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=str(Path.home() / "local-ai/whisper.cpp/models/ggml-small.bin"))
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--only", type=int, default=None, help="index of one corpus phrase")
    args = ap.parse_args()
    app = QApplication([])
    tmp = Path(tempfile.mkdtemp(prefix="omonflow-spoken-"))
    srv = WhisperServer(find_executable(), tmp, tmp / "logs")
    srv.start(Path(args.model))
    ins = MacInserter()
    real_insert = ins.insert
    seen = {}

    def spy(text, target, *a, **kw):
        seen["text"] = text
        r = real_insert(text, target, *a, **kw)
        seen["verified"] = r.verified
        return r
    ins.insert = spy
    if not ins.trusted():
        sys.exit("Accessibility permission required")
    orch = Orchestrator(RustAudio(), {"success_hold": 50}, transcriber=lambda wav, t: srv.transcribe_auto(
        wav, prompt=VOCAB_PROMPT, timeout_s=t)["text"], inserter=ins,
        options=lambda: {"strategy": "auto"})
    stamps = {}
    orch.state_changed.connect(lambda s: stamps.setdefault(s, time.perf_counter()))
    from test_macos_insert_hw import TargetApp

    rows = []
    for rnd in range(args.rounds):
        for lang, voice, phrase in (CORPUS if args.only is None else [CORPUS[args.only]]):
            target = TargetApp(tmp, "line", "OmonFlow dictation target")
            stamps.clear()
            seen.clear()
            t_click = time.perf_counter()
            orch.start()
            pump(app, lambda: orch.state in (State.CAPTURE_READY, State.RECORDING, State.ERROR), 10)
            ready_ms = (time.perf_counter() - t_click) * 1000
            # Speak the moment OmonFlow says "Speak now".
            say = subprocess.Popen(["say", "-v", voice, phrase])
            while say.poll() is None:
                app.processEvents()
                time.sleep(0.01)
            pump(app, lambda: False, 0.4)  # natural pause before clicking Stop
            t_stop = time.perf_counter()
            orch.stop()
            pump(app, lambda: orch.state in (State.SUCCESS, State.IDLE, State.ERROR) and "INSERTING" in stamps
                 or orch.state == State.ERROR, 60)
            done = stamps.get("SUCCESS") or time.perf_counter()
            got = target.values()["line"]
            target.close()
            pump(app, lambda: orch.state in (State.IDLE, State.ERROR), 5)
            if orch.state == State.ERROR:
                err = orch.error_text
                orch.dismiss()
            else:
                err = None
            first_ok = bool(words(got)) and words(got)[0] == words(phrase)[0]
            row = {"round": rnd + 1, "lang": lang, "voice": voice, "expected": phrase, "inserted": got,
                   "wer": round(wer(phrase, got), 3), "first_word_ok": first_ok,
                   "click_to_ready_ms": round(ready_ms), "stop_to_inserted_ms": round((done - t_stop) * 1000),
                   "recording_s": round((orch.last_recording or {}).get("duration_s", 0), 2),
                   "method": getattr(orch.last_insert, "method", None), "transcript": seen.get("text"),
                   "verified": seen.get("verified"), "error": err}
            rows.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
    srv.stop()
    orch.shutdown()
    out = ROOT / "docs" / "m05" / ("spoken_corpus.json" if args.only is None else f"spoken_corpus_only{args.only}.json")
    summary = {
        "runs": len(rows), "errors": sum(1 for r in rows if r["error"]),
        "first_word_ok": sum(r["first_word_ok"] for r in rows),
        "mean_wer_by_lang": {l: round(sum(r["wer"] for r in rows if r["lang"] == l) /
                                      max(1, sum(1 for r in rows if r["lang"] == l)), 3) for l in ("en", "id", "mixed")},
        "median_stop_to_inserted_ms": sorted(r["stop_to_inserted_ms"] for r in rows)[len(rows) // 2],
    }
    out.write_text(json.dumps({"date": time.strftime("%Y-%m-%d %H:%M"), "model": Path(args.model).name,
                               "summary": summary, "rows": rows}, indent=2, ensure_ascii=False))
    print("SUMMARY", json.dumps(summary), "\nsaved", out)


if __name__ == "__main__":
    main()
