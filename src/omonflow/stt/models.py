"""Model storage: download (resumable, verified), import, verify, remove.

Models live in the user data directory, never in the installation
directory (SPEC §8.2).
"""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import os
import shutil
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .catalog import BY_ID, CATALOG, HF_FILE, MetadataCache, ModelSpec

LOG = logging.getLogger("omonflow.models")
GGML_MAGIC = b"lmgg"  # 0x67676d6c little-endian at file start
CHUNK = 1 << 20


class ModelError(Exception):
    pass


class DownloadCancelled(ModelError):
    pass


@dataclass
class ModelStatus:
    spec: ModelSpec
    size: int | None          # expected size (metadata) or None
    sha256: str | None
    installed: bool
    installed_size: int | None
    state: str                # not_installed | partial | installed | verified | corrupt
    detail: str = ""
    partial_bytes: int = 0

    @property
    def memory_mb(self) -> int:
        return self.spec.memory_mb(self.size or self.installed_size)


class ModelStore:
    def __init__(self, directory: Path, meta: MetadataCache | None = None, url_for=None) -> None:
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self.meta = meta or MetadataCache(directory / ".catalog.json")
        self._url_for = url_for or (lambda filename: HF_FILE.format(name=filename))
        self._verified_path = directory / ".verified.json"
        self._lock = threading.Lock()

    # ------------------------------------------------------------ status
    def path(self, model_id: str) -> Path:
        return self.dir / BY_ID[model_id].filename

    def status(self, model_id: str) -> ModelStatus:
        spec = BY_ID[model_id]
        meta = self.meta.get(spec.filename)
        p = self.dir / spec.filename
        part = p.with_suffix(".bin.part")
        partial = part.stat().st_size if part.exists() else 0
        if not p.exists():
            return ModelStatus(spec, meta.size, meta.sha256, False, None,
                               "partial" if partial else "not_installed", partial_bytes=partial)
        size = p.stat().st_size
        problem = self._quick_problem(p, meta.size)
        if problem:
            return ModelStatus(spec, meta.size, meta.sha256, True, size, "corrupt", problem)
        if self._is_verified(p, meta.sha256):
            return ModelStatus(spec, meta.size, meta.sha256, True, size, "verified")
        return ModelStatus(spec, meta.size, meta.sha256, True, size, "installed",
                           "checksum not verified" if meta.sha256 else "no authoritative checksum available")

    def all_status(self) -> list[ModelStatus]:
        return [self.status(m.id) for m in CATALOG]

    def installed_ids(self) -> list[str]:
        return [m.id for m in CATALOG if (self.dir / m.filename).exists()]

    @staticmethod
    def _quick_problem(p: Path, expected_size: int | None) -> str:
        try:
            with open(p, "rb") as f:
                magic = f.read(4)
        except OSError as exc:
            return f"unreadable: {exc}"
        if magic != GGML_MAGIC:
            return "not a whisper.cpp (ggml) model file"
        if expected_size and p.stat().st_size != expected_size:
            return f"size {p.stat().st_size} differs from expected {expected_size}"
        return ""

    # ------------------------------------------------------------ verification
    def _verified_db(self) -> dict:
        try:
            return json.loads(self._verified_path.read_text())
        except (OSError, ValueError):
            return {}

    def _is_verified(self, p: Path, sha: str | None) -> bool:
        if not sha:
            return False
        st = p.stat()
        rec = self._verified_db().get(p.name)
        return bool(rec) and rec == {"sha256": sha, "size": st.st_size, "mtime_ns": st.st_mtime_ns}

    def _mark_verified(self, p: Path, sha: str) -> None:
        with self._lock:
            db = self._verified_db()
            st = p.stat()
            db[p.name] = {"sha256": sha, "size": st.st_size, "mtime_ns": st.st_mtime_ns}
            self._verified_path.write_text(json.dumps(db, indent=1))

    def verify(self, model_id: str, progress=None) -> ModelStatus:
        """Full SHA-256 check against the authoritative checksum."""
        self.meta.get(BY_ID[model_id].filename, network=True)
        st = self.status(model_id)
        if not st.installed:
            raise ModelError("model is not installed")
        if not st.sha256:
            raise ModelError("no authoritative checksum available (offline?)")
        digest = sha256_file(self.path(model_id), progress)
        if digest != st.sha256:
            raise ModelError(f"checksum mismatch: file is corrupt or modified ({digest[:12]}… ≠ {st.sha256[:12]}…)")
        self._mark_verified(self.path(model_id), digest)
        return self.status(model_id)

    # ------------------------------------------------------------ download
    def download(self, model_id: str, progress=None, cancel: threading.Event | None = None) -> ModelStatus:
        """Resumable download into <name>.part, verified before it becomes the model."""
        spec = BY_ID[model_id]
        meta = self.meta.get(spec.filename, refresh=True, network=True)
        final = self.dir / spec.filename
        part = final.with_suffix(".bin.part")
        url = self._url_for(spec.filename)
        have = part.stat().st_size if part.exists() else 0
        req = urllib.request.Request(url, headers={"User-Agent": "OmonFlow"})
        if have:
            req.add_header("Range", f"bytes={have}-")
        try:
            resp = urllib.request.urlopen(req, timeout=30)
        except urllib.error.HTTPError as exc:
            if exc.code == 416 and meta.size and have == meta.size:
                resp = None  # already complete
            else:
                raise ModelError(f"download failed: HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise ModelError(f"download failed: {exc.reason}") from exc
        if resp is not None:
            with resp:
                if have and resp.status != 206:
                    have = 0  # server ignored the range: start over
                total = meta.size or (have + int(resp.headers.get("Content-Length") or 0)) or None
                t0, done0 = time.monotonic(), have
                with open(part, "ab" if have else "wb") as f:
                    while True:
                        if cancel is not None and cancel.is_set():
                            raise DownloadCancelled("download cancelled; it will resume next time")
                        try:
                            chunk = resp.read(CHUNK)
                        except (TimeoutError, OSError, http.client.HTTPException) as exc:
                            raise ModelError(f"connection interrupted ({exc or type(exc).__name__}) after "
                                             f"{have / 1e6:,.1f} MB; retry to resume") from exc
                        if not chunk:
                            break
                        f.write(chunk)
                        have += len(chunk)
                        if progress:
                            rate = (have - done0) / max(1e-3, time.monotonic() - t0)
                            progress(have, total, rate)
        if meta.size and part.stat().st_size != meta.size:
            raise ModelError(f"incomplete download ({part.stat().st_size} of {meta.size} bytes); retry to resume")
        if meta.sha256:
            digest = sha256_file(part)
            if digest != meta.sha256:
                part.unlink(missing_ok=True)
                raise ModelError("checksum mismatch after download; the partial file was removed, please retry")
        os.replace(part, final)
        if meta.sha256:
            self._mark_verified(final, meta.sha256)
        LOG.info("model %s installed (%s bytes, checksum %s)", model_id, final.stat().st_size,
                 "verified" if meta.sha256 else "not available")
        return self.status(model_id)

    # ------------------------------------------------------------ import / remove
    def import_file(self, src: Path, progress=None) -> ModelStatus:
        """Import an existing model file. It must match a catalog entry's checksum."""
        src = Path(src)
        spec = next((m for m in CATALOG if m.filename == src.name), None)
        if spec is None:
            raise ModelError(f"{src.name} is not a known whisper.cpp model file name")
        meta = self.meta.get(spec.filename, network=True)
        problem = self._quick_problem(src, meta.size)
        if problem:
            raise ModelError(problem)
        if meta.sha256:
            digest = sha256_file(src, progress)
            if digest != meta.sha256:
                raise ModelError("checksum does not match the published model; not imported")
        dst = self.dir / spec.filename
        tmp = dst.with_suffix(".bin.importing")
        tmp.unlink(missing_ok=True)
        try:
            os.link(src, tmp)  # same volume: no copy, no extra disk space
        except OSError:
            shutil.copyfile(src, tmp)
        os.replace(tmp, dst)
        if meta.sha256:
            self._mark_verified(dst, meta.sha256)
        return self.status(spec.id)

    def remove(self, model_id: str) -> None:
        p = self.path(model_id)
        p.unlink(missing_ok=True)
        p.with_suffix(".bin.part").unlink(missing_ok=True)

    @staticmethod
    def needs_confirmation(status: ModelStatus) -> bool:
        return True  # every download shows its size first (SPEC §8.2)


def sha256_file(path: Path, progress=None) -> str:
    h = hashlib.sha256()
    total = path.stat().st_size
    done = 0
    with open(path, "rb") as f:
        while chunk := f.read(4 * CHUNK):
            h.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, total, 0.0)
    return h.hexdigest()
