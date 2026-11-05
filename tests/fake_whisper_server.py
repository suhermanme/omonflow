"""Stand-in for whisper-server (same CLI flags and HTTP API) for tests.

Behaviour via environment variables:
  FAKE_LOAD_S       seconds spent "loading" before /health is ok (default 0.2)
  FAKE_CRASH_AFTER  exit(3) this many seconds after becoming ready
  FAKE_BACKEND      backend name in the log (default MTL0); "none" → no GPU
  FAKE_DEVICES      comma list of NAME:TYPE listed at start (default "BLAS:3,<FAKE_BACKEND>:1")
  FAKE_AUTO_LANG    language whisper picks in auto mode (default en)
  FAKE_LOGPROBS     e.g. "en:-0.02,id:-0.40,ms:-0.5" — mean log-prob when decoding in each language
"""

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ap = argparse.ArgumentParser()
ap.add_argument("--model")
ap.add_argument("--host", default="127.0.0.1")
ap.add_argument("--port", type=int)
ap.add_argument("--threads")
ap.add_argument("--inference-path", default="/inference")
ap.add_argument("--no-gpu", action="store_true")
ap.add_argument("--device", type=int, default=0)
args, _ = ap.parse_known_args()

state = {"ready": False, "model": args.model}
backend = os.environ.get("FAKE_BACKEND", "MTL0")
devices = [d.split(":") for d in os.environ.get("FAKE_DEVICES", f"BLAS:3,{backend}:1").split(",") if d and backend != "none"]
gpus = [n for n, t in devices if t in ("1", "2")]
print(f"whisper_init_with_params_no_state: use gpu    = {0 if args.no_gpu else 1}", flush=True)
for i, (n, t) in enumerate(devices):
    print(f"whisper_backend_init_gpu: device {i}: {n} (type: {t})", flush=True)
if args.no_gpu or not gpus or args.device >= len(gpus):
    print("whisper_backend_init_gpu: no GPU found", flush=True)
else:
    print(f"whisper_backend_init_gpu: using {gpus[args.device]} backend", flush=True)


def load(path):
    state["ready"] = False
    time.sleep(float(os.environ.get("FAKE_LOAD_S", "0.2")))
    head = open(path, "rb").read(64) if os.path.exists(path) else b""
    if b"crash" in head:
        os._exit(1)  # the real whisper-server dies on some corrupt model files
    ok = bool(head) and b"bad" not in head
    if ok:
        state["model"] = path
    state["ready"] = True
    return ok


def field(body: bytes, name: str) -> str:
    marker = f'name="{name}"'.encode()
    i = body.find(marker)
    if i < 0:
        return ""
    start = body.find(b"\r\n\r\n", i) + 4
    return body[start:body.find(b"\r\n--", start)].decode()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            self._json(200, {"status": "ok"}) if state["ready"] else self._json(503, {"status": "loading model"})
        else:
            self._json(404, {})

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        if self.path == "/load":
            ok = load(field(body, "model"))
            self._json(200, {}) if ok else self._json(500, {"error": "failed to load model"})
        elif self.path == args.inference_path:
            names = {"en": "english", "id": "indonesian", "ms": "malay"}
            lps = dict((k, float(v)) for k, v in (p.split(":") for p in
                       os.environ.get("FAKE_LOGPROBS", "en:-0.02,id:-0.40,ms:-0.50").split(",")))
            lang = field(body, "language") or "auto"
            used = os.environ.get("FAKE_AUTO_LANG", "en") if lang == "auto" else lang
            # Contains the self-test words of the bundled jfk.wav sample.
            text = f" And so, ask not what your country can do for you. [{os.path.basename(state['model'])}] [lang={used}] "
            if field(body, "response_format") == "verbose_json":
                self._json(200, {"text": text, "language": names.get(used, used),
                                 "segments": [{"text": text[:40], "tokens": [1] * 5, "avg_logprob": lps.get(used, -1.0)},
                                              {"text": text[40:], "tokens": [1] * 5,
                                               "avg_logprob": lps.get(used, -1.0)}]})
            else:
                self._json(200, {"text": text})
        else:
            self._json(404, {})


srv = ThreadingHTTPServer((args.host, args.port), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
if not load(args.model):
    print("error: failed to load model", flush=True)
    sys.exit(2)
crash = os.environ.get("FAKE_CRASH_AFTER")
if crash:
    time.sleep(float(crash))
    os._exit(3)
while True:
    time.sleep(1)
