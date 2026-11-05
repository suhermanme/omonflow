#!/usr/bin/env python3
"""OmonFlow hardware/platform inventory for validation machines.

Collects what OmonFlow's hardware detection must eventually report: OS,
CPU and instruction sets, RAM, GPUs and drivers, GPU runtimes/toolchains,
audio stack, and desktop session (X11/Wayland). Records evidence only;
it does NOT decide whether a backend works. That is whisper_bench.py's job.

Standard library only. Every probe is optional and time-limited: missing
tools are recorded as missing, never treated as errors.

Usage:  python3 hwprobe.py [--out results/]
Output: <out>/<host>-<timestamp>/hwprobe.json (+ printed summary)
"""

import argparse
import ctypes
import datetime as dt
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path

X86_FLAGS = [
    "sse3", "ssse3", "sse4_1", "sse4_2", "avx", "avx2", "fma", "f16c",
    "avx512f", "avx512bw", "avx512vl", "avx512_vnni", "avx512_bf16",
    "avx_vnni", "amx_tile", "amx_int8", "amx_bf16",
]


def run(cmd, timeout=20):
    """Run a command; return dict with rc/stdout/stderr or a 'missing' marker."""
    exe = shutil.which(cmd[0])
    if not exe:
        return {"missing": True}
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        return {"rc": p.returncode, "out": p.stdout.strip(), "err": p.stderr.strip()[-2000:]}
    except subprocess.TimeoutExpired:
        return {"timeout": True}
    except OSError as e:
        return {"error": str(e)}


def first_line(r):
    return r.get("out", "").splitlines()[0] if r.get("out") else None


def powershell_json(command):
    r = run(["powershell", "-NoProfile", "-Command", command + " | ConvertTo-Json -Depth 3"], 60)
    try:
        data = json.loads(r.get("out") or "null")
        return data if isinstance(data, list) else ([data] if data else [])
    except json.JSONDecodeError:
        return []


# ---------------------------------------------------------------- OS / session

def probe_os():
    info = {
        "system": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
        "python": sys.version.split()[0],
        "hostname": socket.gethostname(),
    }
    if sys.platform == "darwin":
        info["macos"] = run(["sw_vers"]).get("out")
        # Rosetta would make an arm64 Mac report x86_64 for this process.
        info["rosetta_translated"] = run(["sysctl", "-n", "sysctl.proc_translated"]).get("out")
    elif sys.platform.startswith("linux"):
        try:
            osr = dict(
                line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines() if "=" in line
            )
            info["distro"] = osr.get("PRETTY_NAME", "").strip('"')
        except OSError:
            pass
        info["session_type"] = os.environ.get("XDG_SESSION_TYPE")
        info["wayland_display"] = os.environ.get("WAYLAND_DISPLAY")
        info["x11_display"] = os.environ.get("DISPLAY")
        info["desktop"] = os.environ.get("XDG_CURRENT_DESKTOP")
        portal = run(["busctl", "--user", "introspect", "org.freedesktop.portal.Desktop",
                      "/org/freedesktop/portal/desktop"], 10)
        info["portal_global_shortcuts"] = (
            "org.freedesktop.portal.GlobalShortcuts" in portal.get("out", "") if "out" in portal else None
        )
    elif sys.platform == "win32":
        info["windows"] = platform.win32_ver()
        info["edition"] = platform.win32_edition() if hasattr(platform, "win32_edition") else None
    return info


# ---------------------------------------------------------------- CPU / RAM

def probe_cpu():
    cpu = {"logical_cpus": os.cpu_count()}
    if sys.platform == "darwin":
        g = lambda k: run(["sysctl", "-n", k]).get("out")
        cpu.update(model=g("machdep.cpu.brand_string"), physical_cores=g("hw.physicalcpu"),
                   perf_levels=g("hw.nperflevels"))
        if platform.machine() == "arm64":
            feats = run(["sysctl", "hw.optional"]).get("out", "")
            cpu["arm_features"] = sorted(
                m.group(1) for m in re.finditer(r"hw\.optional\.arm\.(FEAT_\w+): 1", feats)
            )
        else:
            f = ((g("machdep.cpu.features") or "") + " " + (g("machdep.cpu.leaf7_features") or "")).lower()
            cpu["x86_flags"] = [x for x in X86_FLAGS if x.replace("_", ".") in f or x in f.replace(".", "_")]
    elif sys.platform.startswith("linux"):
        try:
            text = Path("/proc/cpuinfo").read_text()
            m = re.search(r"model name\s*:\s*(.+)", text)
            cpu["model"] = m.group(1) if m else None
            flags = set(re.search(r"^flags\s*:\s*(.+)$", text, re.M).group(1).split())
            cpu["x86_flags"] = [x for x in X86_FLAGS if x in flags]
        except (OSError, AttributeError):
            pass
        lscpu = run(["lscpu", "-J"])
        try:
            fields = {d["field"].rstrip(":"): d["data"] for d in json.loads(lscpu["out"])["lscpu"]}
            cpu["physical_cores_per_socket"] = fields.get("Core(s) per socket")
            cpu["sockets"] = fields.get("Socket(s)")
            cpu["vendor"] = fields.get("Vendor ID")
        except (KeyError, TypeError, json.JSONDecodeError):
            pass
    elif sys.platform == "win32":
        procs = powershell_json("Get-CimInstance Win32_Processor | Select-Object Name,NumberOfCores,NumberOfLogicalProcessors,Manufacturer")
        if procs:
            cpu.update(model=procs[0].get("Name"), physical_cores=procs[0].get("NumberOfCores"),
                       vendor=procs[0].get("Manufacturer"))
        k32 = ctypes.windll.kernel32
        # PF_* constants from winnt.h
        pf = {"sse3": 13, "avx": 39, "avx2": 40, "avx512f": 41}
        cpu["x86_flags"] = [n for n, c in pf.items() if k32.IsProcessorFeaturePresent(c)]
    return cpu


def probe_ram():
    try:
        if sys.platform == "darwin":
            return int(run(["sysctl", "-n", "hw.memsize"])["out"])
        if sys.platform.startswith("linux"):
            kb = re.search(r"MemTotal:\s+(\d+)", Path("/proc/meminfo").read_text()).group(1)
            return int(kb) * 1024
        if sys.platform == "win32":
            class MS(ctypes.Structure):
                _fields_ = [("len", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                            ("avail", ctypes.c_ulonglong), ("tp", ctypes.c_ulonglong), ("ap", ctypes.c_ulonglong),
                            ("tv", ctypes.c_ulonglong), ("av", ctypes.c_ulonglong), ("ae", ctypes.c_ulonglong)]
            m = MS(); m.len = ctypes.sizeof(MS)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return m.total
    except Exception:  # noqa: BLE001 - inventory must never crash
        return None
    return None


# ---------------------------------------------------------------- GPU

def probe_gpu():
    gpu = {"adapters": []}
    if sys.platform == "darwin":
        r = run(["system_profiler", "SPDisplaysDataType", "-json"], 60)
        try:
            for d in json.loads(r["out"]).get("SPDisplaysDataType", []):
                gpu["adapters"].append({
                    "name": d.get("sppci_model") or d.get("_name"),
                    "cores": d.get("sppci_cores"),
                    "vram": d.get("spdisplays_vram") or d.get("spdisplays_vram_shared"),
                    "metal": d.get("spdisplays_mtlgpufamilysupport"),
                    "vendor": d.get("spdisplays_vendor"),
                })
        except (KeyError, TypeError, json.JSONDecodeError):
            gpu["raw_error"] = r
    elif sys.platform.startswith("linux"):
        lspci = run(["lspci", "-nn"])
        gpu["lspci"] = [l for l in lspci.get("out", "").splitlines()
                        if re.search(r"VGA|3D controller|Display controller", l)]
        for card in sorted(Path("/sys/class/drm").glob("card[0-9]")):
            dev = card / "device"
            entry = {"card": card.name}
            for f in ("vendor", "device", "mem_info_vram_total"):
                try:
                    entry[f] = (dev / f).read_text().strip()
                except OSError:
                    pass
            try:
                entry["driver"] = os.path.basename(os.readlink(dev / "driver"))
            except OSError:
                pass
            gpu["adapters"].append(entry)
    elif sys.platform == "win32":
        for v in powershell_json("Get-CimInstance Win32_VideoController | Select-Object Name,DriverVersion,AdapterRAM,PNPDeviceID"):
            # AdapterRAM is a 32-bit field: values >= 4 GiB are wrong. Prefer nvidia-smi / vulkaninfo.
            gpu["adapters"].append({"name": v.get("Name"), "driver": v.get("DriverVersion"),
                                    "adapter_ram_unreliable": v.get("AdapterRAM"), "pnp": v.get("PNPDeviceID")})

    smi = run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version,compute_cap",
               "--format=csv,noheader"])
    gpu["nvidia_smi"] = smi.get("out") if "out" in smi else smi
    vk = run(["vulkaninfo", "--summary"], 30)
    if "out" in vk:
        gpu["vulkan_devices"] = [
            {k: v for k, v in zip(("deviceName", "driverName", "apiVersion", "driverInfo"), vals)}
            for vals in zip(*(re.findall(rf"{k}\s*=\s*(.+)", vk["out"])
                              for k in ("deviceName", "driverName", "apiVersion", "driverInfo")))
        ]
        if not gpu["vulkan_devices"]:
            gpu["vulkan_raw_tail"] = vk["out"][-1500:]
    else:
        gpu["vulkan_devices"] = vk
    rocm = run(["rocminfo"], 30)
    gpu["rocm_agents"] = re.findall(r"Marketing Name:\s*(.+)", rocm.get("out", "")) if "out" in rocm else rocm
    return gpu


# ---------------------------------------------------------------- audio

def probe_audio():
    a = {}
    if sys.platform == "darwin":
        r = run(["system_profiler", "SPAudioDataType", "-json"], 60)
        try:
            items = json.loads(r["out"])["SPAudioDataType"][0]["_items"]
            a["inputs"] = [{"name": i.get("_name"), "default": i.get("coreaudio_default_audio_input_device"),
                            "rate": i.get("coreaudio_device_srate"), "transport": i.get("coreaudio_device_transport")}
                           for i in items if i.get("coreaudio_device_input")]
        except (KeyError, IndexError, TypeError, json.JSONDecodeError):
            a["raw_error"] = r
    elif sys.platform.startswith("linux"):
        a["pactl_server"] = first_line(run(["pactl", "info"])) and [
            l for l in run(["pactl", "info"])["out"].splitlines() if l.startswith(("Server Name", "Server Version"))]
        a["pipewire"] = first_line(run(["pipewire", "--version"]))
        a["arecord"] = run(["arecord", "-l"]).get("out")
        src = run(["pactl", "list", "short", "sources"])
        a["sources"] = src.get("out", "").splitlines() if "out" in src else src
    elif sys.platform == "win32":
        a["devices"] = [d.get("Name") for d in powershell_json("Get-CimInstance Win32_SoundDevice | Select-Object Name")]
    return a


# ---------------------------------------------------------------- toolchains

def probe_tools():
    tools = {}
    for name, cmd in {
        "git": ["git", "--version"], "cmake": ["cmake", "--version"],
        "cc": ["cc", "--version"], "gcc": ["gcc", "--version"], "clang": ["clang", "--version"],
        "cl": ["cl"], "cargo": ["cargo", "--version"], "rustc": ["rustc", "--version"],
        "uv": ["uv", "--version"], "nvcc": ["nvcc", "--version"], "glslc": ["glslc", "--version"],
        "ffmpeg": ["ffmpeg", "-version"], "xcrun": ["xcrun", "--version"],
    }.items():
        r = run(cmd)
        if r.get("missing"):
            tools[name] = None
        else:
            text = (r.get("out") or r.get("err") or "").strip()
            tools[name] = text.splitlines()[-1] if name == "nvcc" and text else (text.splitlines()[0] if text else "present")
    tools["VULKAN_SDK"] = os.environ.get("VULKAN_SDK")
    tools["CUDA_PATH"] = os.environ.get("CUDA_PATH") or os.environ.get("CUDA_HOME")
    return tools


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(Path(__file__).with_name("results")))
    args = ap.parse_args()

    report = {
        "schema": "omonflow-hwprobe/1",
        "collected_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "os": probe_os(), "cpu": probe_cpu(), "ram_bytes": probe_ram(),
        "gpu": probe_gpu(), "audio": probe_audio(), "tools": probe_tools(),
        "note": "Inventory only. Detected hardware != available backend != validated backend.",
    }
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out) / f"{socket.gethostname()}-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "hwprobe.json").write_text(json.dumps(report, indent=2, default=str))

    c, g = report["cpu"], report["gpu"]
    print(f"OS:    {report['os']['system']} {report['os'].get('macos') or report['os'].get('distro') or report['os']['release']}".replace("\n", " "))
    print(f"CPU:   {c.get('model')} | logical {c.get('logical_cpus')} | flags {c.get('x86_flags') or c.get('arm_features', [])[:6]}")
    print(f"RAM:   {round((report['ram_bytes'] or 0) / 2**30, 1)} GiB")
    print(f"GPU:   {[a.get('name') or a.get('card') for a in g['adapters']]} {g.get('lspci') or ''}")
    print(f"NVIDIA:{g.get('nvidia_smi')}  Vulkan: {g.get('vulkan_devices')}")
    print(f"Tools: { {k: bool(v) for k, v in report['tools'].items()} }")
    print(f"\nSaved {out_dir / 'hwprobe.json'}")


if __name__ == "__main__":
    main()
