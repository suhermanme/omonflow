"""ModelStore: download/resume/verify/import/remove against a local server."""

import hashlib
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from omonflow.stt.catalog import MetadataCache
from omonflow.stt.models import DownloadCancelled, ModelError, ModelStore


class RangeHandler(SimpleHTTPRequestHandler):
    """Static files with HTTP Range support (like the Hugging Face CDN)."""
    stall_after = None  # bytes; simulate a dropped connection

    def log_message(self, *a):
        pass

    def do_GET(self):
        path = Path(self.translate_path(self.path))
        if not path.exists():
            return self.send_error(404)
        data = path.read_bytes()
        start = 0
        rng = self.headers.get("Range")
        if rng:
            start = int(rng.split("=")[1].split("-")[0])
            if start >= len(data):
                self.send_response(416)
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
        else:
            self.send_response(200)
        body = data[start:]
        if RangeHandler.stall_after is not None:
            body = body[:RangeHandler.stall_after]
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def remote(tmp_path):
    root = tmp_path / "remote"
    root.mkdir()
    content = b"lmgg" + bytes(range(256)) * 20000  # ~5 MB fake model
    (root / "ggml-tiny.bin").write_bytes(content)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), partial(RangeHandler, directory=str(root)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    meta = {"ggml-tiny.bin": {"size": len(content), "sha256": hashlib.sha256(content).hexdigest()}}
    yield {"url": f"http://127.0.0.1:{srv.server_address[1]}", "content": content, "meta": meta, "root": root}
    RangeHandler.stall_after = None
    srv.shutdown()


@pytest.fixture
def store(tmp_path, remote):
    meta = MetadataCache(tmp_path / "meta.json", fetch=lambda: remote["meta"], head=False)
    return ModelStore(tmp_path / "models", meta, url_for=lambda f: f"{remote['url']}/{f}")


def test_download_verifies_and_installs(store, remote):
    seen = []
    st = store.download("tiny", progress=lambda d, t, r: seen.append(d))
    assert st.state == "verified" and seen[-1] == len(remote["content"])
    assert store.path("tiny").read_bytes() == remote["content"]


def test_status_is_offline_and_cache_only(tmp_path):
    calls = []
    meta = MetadataCache(tmp_path / "m.json", fetch=lambda: calls.append(1) or {}, head=False)
    st = ModelStore(tmp_path / "models", meta).status("small")
    assert st.state == "not_installed" and calls == []  # never hits the network from status()


def test_interrupted_download_resumes(store, remote):
    RangeHandler.stall_after = 1_000_000
    with pytest.raises(ModelError, match="incomplete"):
        store.download("tiny")
    assert store.status("tiny").state == "partial"
    RangeHandler.stall_after = None
    ranges = []
    orig = RangeHandler.do_GET

    def spy(self):
        ranges.append(self.headers.get("Range"))
        return orig(self)
    RangeHandler.do_GET = spy
    try:
        assert store.download("tiny").state == "verified"
    finally:
        RangeHandler.do_GET = orig
    assert ranges == ["bytes=1000000-"]


def test_cancel_keeps_partial_for_resume(store, remote):
    cancel = threading.Event()

    def prog(done, total, rate):
        if done >= 2_000_000:
            cancel.set()
    with pytest.raises(DownloadCancelled):
        store.download("tiny", progress=prog, cancel=cancel)
    assert store.status("tiny").state == "partial"
    assert store.download("tiny").state == "verified"


def test_checksum_mismatch_rejected(store, remote):
    remote["meta"]["ggml-tiny.bin"]["sha256"] = "0" * 64
    with pytest.raises(ModelError, match="checksum"):
        store.download("tiny")
    assert not store.path("tiny").exists()


def test_corrupt_models_detected(store, remote):
    store.download("tiny")
    p = store.path("tiny")
    p.write_bytes(b"XXXX" + remote["content"][4:])  # same size, wrong magic
    assert store.status("tiny").state == "corrupt"
    p.write_bytes(remote["content"][:-10])  # truncated
    assert "size" in store.status("tiny").detail
    p.write_bytes(remote["content"][:-1] + b"\x01")  # same size + magic, modified content
    st = store.status("tiny")
    assert st.state == "installed"  # mtime changed → no longer marked verified
    with pytest.raises(ModelError, match="mismatch"):
        store.verify("tiny")


def test_import_hardlinks_and_verifies(store, remote, tmp_path):
    src = remote["root"] / "ggml-tiny.bin"
    st = store.import_file(src)
    assert st.state == "verified"
    assert store.path("tiny").stat().st_ino == src.stat().st_ino  # no copy on the same volume


def test_import_rejects_unknown_or_modified(store, remote, tmp_path):
    other = tmp_path / "random.bin"
    other.write_bytes(b"lmgg")
    with pytest.raises(ModelError, match="known"):
        store.import_file(other)
    fake = tmp_path / "ggml-tiny.bin"
    fake.write_bytes(remote["content"][:-1] + b"\x00")
    with pytest.raises(ModelError, match="checksum"):
        store.import_file(fake)


def test_remove(store):
    store.download("tiny")
    store.remove("tiny")
    assert store.status("tiny").state == "not_installed"


def test_network_timeout_mid_download_is_resumable(store, monkeypatch):
    import omonflow.stt.models as models_mod

    class Flaky:
        def __init__(self, resp):
            self.resp, self.n = resp, 0
            self.status, self.headers = resp.status, resp.headers

        def read(self, amt):
            self.n += 1
            if self.n == 3:
                raise TimeoutError("The read operation timed out")
            return self.resp.read(amt)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            self.resp.close()

    real = models_mod.urllib.request.urlopen
    monkeypatch.setattr(models_mod.urllib.request, "urlopen", lambda *a, **k: Flaky(real(*a, **k)))
    with pytest.raises(ModelError, match="retry to resume"):
        store.download("tiny")
    assert store.status("tiny").state == "partial"
    monkeypatch.setattr(models_mod.urllib.request, "urlopen", real)
    assert store.download("tiny").state == "verified"
