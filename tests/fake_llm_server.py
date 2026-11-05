"""In-process fake OpenAI-compatible server for refinement tests.

`reply` decides the answer: a function (transcript -> content) or a fixed string.
"""

from __future__ import annotations

import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeLLM:
    def __init__(self, reply=lambda t: t, models=("test-model",), delay_s=0.0, reject_extras=False,
                 status=200, finish_reason="stop", api_key=None):
        self.reply, self.models, self.delay_s = reply, list(models), delay_s
        self.reject_extras, self.status, self.finish_reason, self.api_key = reject_extras, status, finish_reason, api_key
        self.requests: list[dict] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _auth_ok(self):
                return fake.api_key is None or self.headers.get("Authorization") == f"Bearer {fake.api_key}"

            def do_GET(self):  # noqa: N802
                if not self._auth_ok():
                    return self._send(401, {"error": "bad key"})
                if self.path == "/v1/models":
                    return self._send(200, {"object": "list", "data": [{"id": m} for m in fake.models]})
                self._send(404, {})

            def do_POST(self):  # noqa: N802
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append({"body": body, "auth": self.headers.get("Authorization")})
                if not self._auth_ok():
                    return self._send(401, {"error": "bad key"})
                if fake.reject_extras and "chat_template_kwargs" in body:
                    return self._send(400, {"error": "Unrecognized request argument: chat_template_kwargs"})
                if fake.status != 200:
                    return self._send(fake.status, {"error": "boom"})
                time.sleep(fake.delay_s)
                last = body["messages"][-1]["content"]
                transcript = re.sub(r"^<transcript>\n|\n</transcript>$", "", last)
                content = fake.reply(transcript) if callable(fake.reply) else fake.reply
                self._send(200, {"choices": [{"message": {"role": "assistant", "content": content},
                                              "finish_reason": fake.finish_reason}]})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
