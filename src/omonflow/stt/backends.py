"""Inference backends (SPEC §9.2): detected / available / validated / active.

- Available: devices whisper.cpp itself lists at start-up
  ("whisper_backend_init_gpu: device N: NAME (type: T)"), plus CPU (always).
- Validated: a benchmark on this executable + device + model succeeded and
  whisper.cpp's log shows that backend in use. Stored on disk.
- Active: the backend the running server reports (parse_backend).
GPU presence alone never makes a backend "available" or "validated".
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

GPU_TYPE, IGPU_TYPE = 1, 2  # ggml_backend_dev_type
FAMILIES = {"MTL": "metal", "METAL": "metal", "CUDA": "cuda", "VULKAN": "vulkan", "SYCL": "sycl", "OPENCL": "opencl"}
LABELS = {"cpu": "CPU", "metal": "Apple Metal", "cuda": "NVIDIA CUDA", "vulkan": "Vulkan", "sycl": "SYCL",
          "opencl": "OpenCL", "auto": "Automatic"}


@dataclass(frozen=True)
class Device:
    family: str          # cpu | metal | cuda | vulkan | …
    gpu_index: int | None  # value for --device (index among GPUs); None for CPU
    name: str            # ggml device name, e.g. MTL0, CUDA0, Vulkan1

    @property
    def key(self) -> str:
        return "cpu" if self.family == "cpu" else f"{self.family}:{self.gpu_index}"

    @property
    def label(self) -> str:
        return LABELS.get(self.family, self.family) + ("" if self.family == "cpu" else f" ({self.name})")


CPU = Device("cpu", None, "CPU")


def parse_devices(log_lines: list[str]) -> list[Device]:
    """Devices whisper.cpp can use, from its own start-up log. CPU is always first."""
    out = [CPU]
    gpu_count = 0
    for line in log_lines:
        m = re.search(r"whisper_backend_init_gpu: device \d+: (\S+) \(type: (\d+)\)", line)
        if not m:
            continue
        name, dtype = m.group(1), int(m.group(2))
        if dtype not in (GPU_TYPE, IGPU_TYPE):
            continue
        fam = FAMILIES.get(re.sub(r"\d+$", "", name).upper(), name.lower())
        out.append(Device(fam, gpu_count, name))
        gpu_count += 1
    return out


def server_args(device: Device | None) -> list[str]:
    """whisper-server arguments for a device; None = whisper.cpp's default (first GPU, else CPU)."""
    if device is None:
        return []
    if device.family == "cpu":
        return ["--no-gpu"]
    return ["--device", str(device.gpu_index)]


@dataclass
class Validation:
    key: str
    executable: str
    version: str | None
    model: str
    ok: bool
    active_backend: str | None
    start_ms: float | None
    transcribe_ms: float | None
    audio_s: float | None
    rtf: float | None
    error: str | None
    at: float

    @property
    def summary(self) -> str:
        if not self.ok:
            return f"failed: {self.error}"
        return f"{self.rtf:.3f}× real time ({self.transcribe_ms:.0f} ms for {self.audio_s:.1f} s, {self.model})"


class ValidationStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _all(self) -> list[dict]:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return []

    def add(self, v: Validation) -> None:
        rows = [r for r in self._all() if not (r["key"] == v.key and r["executable"] == v.executable
                                               and r["version"] == v.version and r["model"] == v.model)]
        rows.append(asdict(v))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(rows[-200:], indent=1))

    def latest(self, key: str, executable: str, version: str | None) -> Validation | None:
        rows = [r for r in self._all() if r["key"] == key and r["executable"] == executable and r["version"] == version]
        return Validation(**max(rows, key=lambda r: r["at"])) if rows else None


def now() -> float:
    return time.time()
