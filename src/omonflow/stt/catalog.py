"""Whisper model catalog (SPEC §8.2).

Sizes and SHA-256 come from Hugging Face's LFS metadata for
ggerganov/whisper.cpp (authoritative), fetched at runtime and cached.
Memory figures are approximations from whisper.cpp's published table
(model weights + runtime buffers) and are labelled as such in the UI.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

LOG = logging.getLogger("omonflow.models")

HF_REPO = "ggerganov/whisper.cpp"
HF_TREE = f"https://huggingface.co/api/models/{HF_REPO}/tree/main"
HF_FILE = f"https://huggingface.co/{HF_REPO}/resolve/main/{{name}}"
CACHE_TTL_S = 7 * 24 * 3600

# Approximate runtime memory beyond the file size, per family (MB).
_OVERHEAD_MB = {"tiny": 200, "base": 250, "small": 400, "medium": 600, "large": 1000}


@dataclass(frozen=True)
class ModelSpec:
    id: str              # e.g. "small", "large-v3-turbo-q5_0"
    family: str          # tiny | base | small | medium | large
    english_only: bool
    quant: str | None    # None = full precision (f16)
    approx_mb: int       # used when metadata is unavailable
    note: str = ""

    @property
    def filename(self) -> str:
        return f"ggml-{self.id}.bin"

    def memory_mb(self, size_bytes: int | None) -> int:
        size = size_bytes / 1e6 if size_bytes else self.approx_mb
        return int(size + _OVERHEAD_MB[self.family])

    @property
    def label(self) -> str:
        name = self.id.replace("-", " ")
        tags = []
        if self.english_only:
            tags.append("English only")
        if self.quant:
            tags.append(f"quantized {self.quant}")
        return name + (f" ({', '.join(tags)})" if tags else "")


S = ModelSpec
CATALOG: list[ModelSpec] = [
    S("tiny", "tiny", False, None, 78, "fastest, lowest accuracy"),
    S("tiny.en", "tiny", True, None, 78),
    S("base", "base", False, None, 148),
    S("base.en", "base", True, None, 148),
    S("base-q5_1", "base", False, "q5_1", 60),
    S("small", "small", False, None, 488, "good balance for Indonesian + English"),
    S("small.en", "small", True, None, 488),
    S("small-q5_1", "small", False, "q5_1", 190),
    S("medium", "medium", False, None, 1530),
    S("medium-q5_0", "medium", False, "q5_0", 539),
    S("large-v3", "large", False, None, 3100, "most accurate, slowest"),
    S("large-v3-q5_0", "large", False, "q5_0", 1080),
    S("large-v3-turbo", "large", False, None, 1620, "near large-v3 accuracy, much faster"),
    S("large-v3-turbo-q5_0", "large", False, "q5_0", 574),
    S("large-v3-turbo-q8_0", "large", False, "q8_0", 874),
]
BY_ID = {m.id: m for m in CATALOG}
DEFAULT_MODEL = "small"


@dataclass
class RemoteMeta:
    size: int | None
    sha256: str | None


class MetadataCache:
    """Fetches and caches size/SHA-256 for every catalog file."""

    def __init__(self, path: Path, fetch=None, head=None) -> None:
        self.path = path
        self._fetch = fetch or _fetch_tree
        self._head = head if head is not None else (_head_file if fetch is None else None)
        self._data: dict[str, dict] | None = None
        self.fetched_at: float | None = None
        self.error: str | None = None

    def get(self, filename: str, refresh: bool = False, network: bool = False) -> RemoteMeta:
        """Cache-only unless `network` (never call with network=True on the UI thread)."""
        self._load(refresh and network, network)
        d = (self._data or {}).get(filename)
        if not d and network and self._head is not None:
            # Per-file fallback: Hugging Face returns size and SHA-256 headers.
            try:
                d = self._head(filename)
                self._data[filename] = d
                self._save()
            except Exception as exc:  # noqa: BLE001
                self.error = str(exc)
                d = None
        d = d or {}
        return RemoteMeta(d.get("size"), d.get("sha256"))

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"fetched_at": self.fetched_at or 0, "files": self._data}))
        except OSError:
            pass

    def _load(self, refresh: bool, network: bool = True) -> None:
        if self._data is not None and not refresh:
            return
        cached = None
        if self.path.exists():
            try:
                cached = json.loads(self.path.read_text())
            except (OSError, ValueError):
                cached = None
        fresh = cached and time.time() - cached.get("fetched_at", 0) < CACHE_TTL_S
        if cached and not refresh and (fresh or not network):
            self._data, self.fetched_at = cached["files"], cached["fetched_at"]
            return
        if not network:
            self._data = {}
            return
        try:
            files = self._fetch()
            self._data, self.fetched_at, self.error = files, time.time(), None
            self._save()
        except Exception as exc:  # noqa: BLE001 - offline is normal
            self.error = str(exc)
            LOG.info("model metadata unavailable (%s); using cache if any", exc)
            if cached:
                self._data, self.fetched_at = cached["files"], cached.get("fetched_at")
            else:
                self._data = {}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _head_file(filename: str) -> dict:
    req = urllib.request.Request(HF_FILE.format(name=filename), method="HEAD", headers={"User-Agent": "OmonFlow"})
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        resp = opener.open(req, timeout=30)
        headers = resp.headers
    except urllib.error.HTTPError as exc:  # the 302 itself carries the headers
        headers = exc.headers
    size = headers.get("x-linked-size")
    sha = (headers.get("x-linked-etag") or "").strip('"')
    if not size or len(sha) != 64:
        raise ValueError(f"no checksum headers for {filename}")
    return {"size": int(size), "sha256": sha}


def _fetch_tree() -> dict[str, dict]:
    with urllib.request.urlopen(HF_TREE, timeout=60) as r:
        items = json.load(r)
    wanted = {m.filename for m in CATALOG}
    out = {}
    for it in items:
        if it.get("path") in wanted and it.get("lfs"):
            out[it["path"]] = {"size": it["lfs"].get("size"), "sha256": it["lfs"].get("oid")}
    return out
