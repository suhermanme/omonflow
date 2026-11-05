#!/usr/bin/env python3
"""Run OmonFlow's own hardware detection and backend validation (M06) on this
machine and write a JSON report. Uses the same code as Settings → Hardware.

Needs the OmonFlow dev environment (uv sync), a whisper-server build
(e.g. from whisper_bench.py's work dir) and a ggml model file.

Usage:
  uv run python tools/validation/omonflow_validate.py \\
      --whisper-server tools/validation/work/whisper.cpp/build-vulkan/bin/whisper-server \\
      --model tools/validation/work/models/ggml-small.bin [--runs 3]
Run once per whisper-server build (cpu / cuda / vulkan / metal).
"""

import argparse
import json
import os
import platform
import socket
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication  # noqa: E402

from omonflow.app.whisper_controller import WhisperController  # noqa: E402
from omonflow.hardware.detect import detect  # noqa: E402
from omonflow.stt.catalog import CATALOG, MetadataCache  # noqa: E402
from omonflow.stt.models import ModelStore  # noqa: E402
from omonflow.stt.server import ServerInfo  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--whisper-server", required=True)
    ap.add_argument("--model", required=True, help="ggml model file, e.g. ggml-small.bin")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--out", default=str(Path(__file__).with_name("results")))
    args = ap.parse_args()
    app = QCoreApplication([])
    model = Path(args.model)
    spec = next((m for m in CATALOG if m.filename == model.name), None)
    if spec is None:
        sys.exit(f"{model.name} is not a known whisper.cpp model name")
    tmp = Path(tempfile.mkdtemp(prefix="omonflow-validate-"))
    store = ModelStore(tmp / "models", MetadataCache(tmp / "models" / ".c.json", fetch=lambda: {}, head=False))
    store.import_file(model)  # hard link when possible
    exe = str(Path(args.whisper_server).resolve())
    c = WhisperController(store, tmp, tmp / "logs", executable_finder=lambda: ServerInfo(exe, "env", None))
    events = []
    c.task_done.connect(lambda *a: events.append(a))

    def wait(n, t=600):
        end = time.monotonic() + t
        while len(events) < n and time.monotonic() < end:
            app.processEvents()
            time.sleep(0.05)

    report = {"host": socket.gethostname(), "platform": platform.platform(), "date": time.strftime("%Y-%m-%d %H:%M"),
              "whisper_server": exe, "model": model.name, "hardware": detect().to_dict(), "backends": []}
    c.ensure_running(spec.id)
    wait(1, 300)
    report["engine_start"] = {"ok": events[-1][2], "message": events[-1][3], "backend_auto": c.snapshot().get("backend"),
                              "devices": [d.key for d in c.devices]}
    print("devices reported by whisper.cpp:", report["engine_start"]["devices"], flush=True)
    for d in c.devices:
        c.validate(d.key, spec.id, args.runs)
        wait(len(events) + 1, 900)
        v = c.validation(d.key)
        report["backends"].append({"device": d.key, "label": d.label, **(v.__dict__ if v else {"ok": False})})
        print(f"{d.label}: {v.summary if v else 'no result'}", flush=True)
    c.shutdown()
    out = Path(args.out) / f"{socket.gethostname()}-omonflow-{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print("saved", out)


if __name__ == "__main__":
    main()
