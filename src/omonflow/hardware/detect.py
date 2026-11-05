"""Hardware detection (SPEC §9.1). Detection only: whether a backend works is
decided by whisper.cpp itself (stt/backends.py), never by GPU presence.

Uses structured sources (sysctl, system_profiler -json, /proc, lspci,
nvidia-smi CSV, PowerShell CIM JSON) with timeouts; never raises.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

X86_FLAGS = ["sse4_2", "avx", "avx2", "fma", "f16c", "avx512f", "avx512bw", "avx512_vnni", "avx_vnni", "amx_tile"]


@dataclass
class GPUInfo:
    name: str
    vendor: str | None = None
    memory_mb: int | None = None       # dedicated VRAM; None if unknown/shared
    unified_memory: bool = False       # Apple Silicon: shares system RAM
    driver: str | None = None
    apis: list[str] = field(default_factory=list)  # e.g. Metal, CUDA 12.4, Vulkan 1.3


@dataclass
class Hardware:
    os: str
    os_version: str
    arch: str
    cpu_model: str | None
    cpu_vendor: str | None
    physical_cores: int | None
    logical_cpus: int | None
    perf_cores: int | None
    cpu_features: list[str]
    ram_mb: int | None
    gpus: list[GPUInfo]
    runtimes: dict[str, str | None]
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def apple_silicon(self) -> bool:
        return self.os == "macOS" and self.arch == "arm64"


def _run(cmd: list[str], timeout: float = 15) -> str | None:
    if not shutil.which(cmd[0]):
        return None
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
        return p.stdout if p.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def detect() -> Hardware:
    if sys.platform == "darwin":
        return _macos()
    if sys.platform.startswith("linux"):
        return _linux()
    if sys.platform == "win32":
        return _windows()
    return Hardware(platform.system(), platform.release(), platform.machine(), None, None, None, os.cpu_count(),
                    None, [], None, [], {}, ["unsupported platform for detailed detection"])


# ---------------------------------------------------------------- macOS

def _sysctl(key: str) -> str | None:
    out = _run(["sysctl", "-n", key], 5)
    return out.strip() if out else None


def _macos() -> Hardware:
    arch = platform.machine()
    if _sysctl("sysctl.proc_translated") == "1":
        arch = "arm64"  # running under Rosetta on Apple Silicon
    feats: list[str] = []
    if arch == "arm64":
        flags = _run(["sysctl", "hw.optional"], 5) or ""
        feats = sorted(m.group(1) for m in re.finditer(r"hw\.optional\.arm\.(FEAT_\w+): 1", flags))
        feats = [f for f in feats if f in ("FEAT_BF16", "FEAT_I8MM", "FEAT_DotProd", "FEAT_FP16", "FEAT_SME", "FEAT_SME2")]
    else:
        f = ((_sysctl("machdep.cpu.features") or "") + " " + (_sysctl("machdep.cpu.leaf7_features") or "")).lower()
        feats = [x for x in X86_FLAGS if x.replace("_", ".") in f or x.replace("_", "") in f.replace(".", "")]
    ram = _sysctl("hw.memsize")
    gpus = []
    sp = _run(["system_profiler", "SPDisplaysDataType", "-json"], 30)
    try:
        for d in json.loads(sp or "{}").get("SPDisplaysDataType", []):
            name = d.get("sppci_model") or d.get("_name") or "GPU"
            vram = d.get("spdisplays_vram") or d.get("spdisplays_vram_shared")
            mb = None
            m = re.match(r"(\d+)\s*(GB|MB)", str(vram or ""))
            if m:
                mb = int(m.group(1)) * (1024 if m.group(2) == "GB" else 1)
            metal = d.get("spdisplays_mtlgpufamilysupport")
            gpus.append(GPUInfo(name, (d.get("spdisplays_vendor") or "").replace("sppci_vendor_", "") or None, mb,
                                unified_memory=arch == "arm64", apis=["Metal"] if metal else []))
    except ValueError:
        pass
    ver = platform.mac_ver()[0]
    perf = _sysctl("hw.perflevel0.physicalcpu")
    return Hardware("macOS", ver, arch, _sysctl("machdep.cpu.brand_string"),
                    "Apple" if arch == "arm64" else ("Intel" if "Intel" in (_sysctl("machdep.cpu.brand_string") or "") else None),
                    int(_sysctl("hw.physicalcpu") or 0) or None, os.cpu_count(), int(perf) if perf else None,
                    feats, int(ram) // (1024 * 1024) if ram else None, gpus, _runtimes(),
                    ["Apple Silicon GPU uses unified memory (shared with system RAM)"] if arch == "arm64" else [])


# ---------------------------------------------------------------- Linux

def _linux() -> Hardware:
    text = ""
    try:
        text = Path("/proc/cpuinfo").read_text()
    except OSError:
        pass
    m = re.search(r"model name\s*:\s*(.+)", text)
    v = re.search(r"vendor_id\s*:\s*(\S+)", text)
    flags = set((re.search(r"^flags\s*:\s*(.+)$", text, re.M) or [None, ""])[1].split()) if text else set()
    cores = None
    lscpu = _run(["lscpu", "-J"])
    try:
        f = {d["field"].rstrip(":"): d["data"] for d in json.loads(lscpu)["lscpu"]}
        cores = int(f.get("Core(s) per socket", 0)) * int(f.get("Socket(s)", 1)) or None
    except (TypeError, ValueError, KeyError):
        pass
    ram = None
    try:
        ram = int(re.search(r"MemTotal:\s+(\d+)", Path("/proc/meminfo").read_text()).group(1)) // 1024
    except (OSError, AttributeError):
        pass
    gpus = _nvidia_gpus()
    lspci = _run(["lspci", "-mm"]) or ""
    for line in lspci.splitlines():
        if re.search(r'"(VGA compatible controller|3D controller|Display controller)"', line):
            parts = re.findall(r'"([^"]*)"', line)
            name = f"{parts[1]} {parts[2]}" if len(parts) > 2 else line
            if not any(g.vendor == "NVIDIA" for g in gpus) or "NVIDIA" not in name:
                vram = None
                gpus.append(GPUInfo(name, parts[1] if len(parts) > 1 else None, vram))
    for card in sorted(Path("/sys/class/drm").glob("card[0-9]")):
        try:
            vram = int((card / "device/mem_info_vram_total").read_text()) // (1024 * 1024)
            for g in gpus:
                if g.vendor and "AMD" in g.vendor.upper() and g.memory_mb is None:
                    g.memory_mb = vram
                    break
        except (OSError, ValueError):
            pass
    _add_vulkan(gpus)
    notes = []
    sess = os.environ.get("XDG_SESSION_TYPE")
    if sess:
        notes.append(f"desktop session: {sess}")
    return Hardware("Linux", _distro(), platform.machine(), m.group(1) if m else None, v.group(1) if v else None,
                    cores, os.cpu_count(), None, [x for x in X86_FLAGS if x in flags], ram, gpus, _runtimes(), notes)


def _distro() -> str:
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip('"')
    except OSError:
        pass
    return platform.release()


# ---------------------------------------------------------------- Windows

def _windows() -> Hardware:
    def cim(q):
        out = _run(["powershell", "-NoProfile", "-Command", q + " | ConvertTo-Json -Depth 3"], 60)
        try:
            d = json.loads(out or "null")
            return d if isinstance(d, list) else ([d] if d else [])
        except ValueError:
            return []

    cpu = (cim("Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores,Manufacturer") or [{}])[0]
    import ctypes

    k32 = ctypes.windll.kernel32
    feats = [n for n, c in {"avx": 39, "avx2": 40, "avx512f": 41}.items() if k32.IsProcessorFeaturePresent(c)]

    class MS(ctypes.Structure):
        _fields_ = [("len", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong)] + \
                   [(f"x{i}", ctypes.c_ulonglong) for i in range(6)]
    ms = MS()
    ms.len = ctypes.sizeof(MS)
    k32.GlobalMemoryStatusEx(ctypes.byref(ms))
    gpus = _nvidia_gpus()
    for v in cim("Get-CimInstance Win32_VideoController | Select-Object Name,DriverVersion,AdapterCompatibility"):
        if "NVIDIA" in (v.get("Name") or "") and gpus:
            continue
        gpus.append(GPUInfo(v.get("Name") or "GPU", v.get("AdapterCompatibility"), None, driver=v.get("DriverVersion")))
    _add_vulkan(gpus)
    return Hardware("Windows", platform.version(), platform.machine(), cpu.get("Name"), cpu.get("Manufacturer"),
                    cpu.get("NumberOfCores"), os.cpu_count(), None, feats, ms.total // (1024 * 1024), gpus,
                    _runtimes(), [])


# ---------------------------------------------------------------- shared

def _nvidia_gpus() -> list[GPUInfo]:
    out = _run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader,nounits"])
    gpus = []
    cuda = None
    hdr = _run(["nvidia-smi"])
    if hdr:
        m = re.search(r"CUDA Version:\s*([\d.]+)", hdr)
        cuda = m.group(1) if m else None
    for line in (out or "").splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3:
            gpus.append(GPUInfo(parts[0], "NVIDIA", int(float(parts[1])) if parts[1] else None, driver=parts[2],
                                apis=[f"CUDA {cuda}"] if cuda else ["CUDA"]))
    return gpus


def _add_vulkan(gpus: list[GPUInfo]) -> None:
    out = _run(["vulkaninfo", "--summary"], 30)
    if not out:
        return
    names = re.findall(r"deviceName\s*=\s*(.+)", out)
    apis = re.findall(r"apiVersion\s*=\s*(\S+)", out)
    for i, n in enumerate(names):
        n = n.strip()
        api = f"Vulkan {apis[i]}" if i < len(apis) else "Vulkan"
        match = next((g for g in gpus if n.split("(")[0].strip().lower() in g.name.lower()
                      or g.name.lower() in n.lower()), None)
        if match:
            match.apis.append(api)
        elif "llvmpipe" not in n.lower():  # software rasterizer is not a GPU
            gpus.append(GPUInfo(n, None, None, apis=[api]))


def _runtimes() -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    nvcc = _run(["nvcc", "--version"])
    out["cuda_toolkit"] = (re.search(r"release ([\d.]+)", nvcc) or [None, None])[1] if nvcc else None
    vk = _run(["vulkaninfo", "--summary"], 30)
    out["vulkan"] = (re.search(r"Vulkan Instance Version:\s*(\S+)", vk) or [None, None])[1] if vk else None
    return out
