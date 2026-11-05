#!/usr/bin/env python3
"""Build whisper.cpp per backend and benchmark transcription on this machine.

A backend counts as VALIDATED only when all of these hold:
  1. it builds,
  2. whisper-cli's own log shows that backend initialising a device, and
  3. a transcription completes with non-empty text.
GPU presence alone proves nothing (OmonFlow spec §9).

Standard library only. Needs git, cmake and a C/C++ compiler. Also needs:
  CUDA:   NVIDIA driver + CUDA toolkit (nvcc)
  Vulkan: Vulkan loader/driver + headers + glslc (e.g. Vulkan SDK or
          libvulkan-dev + glslc packages)
  Metal:  macOS (Apple Silicon expected; Intel Macs are tested, not assumed)

Usage examples:
  python3 whisper_bench.py                          # auto: cpu + whatever GPU toolchains exist
  python3 whisper_bench.py --backends cpu,vulkan --models small,base --runs 5
  python3 whisper_bench.py --audio my_indonesian.wav  # add your own 16 kHz WAV clips

Output: <out>/<host>-<timestamp>/whisper_bench.json and summary.md
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import statistics
import subprocess
import sys
import time
import urllib.request
import wave
from pathlib import Path

WHISPER_REPO = "https://github.com/ggml-org/whisper.cpp.git"
# Commit proven with voice-client on the M4 Pro. Override with --ref.
DEFAULT_REF = "60c0be6a"
HF_TREE = "https://huggingface.co/api/models/ggerganov/whisper.cpp/tree/main"
HF_FILE = "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/{name}"

BACKEND_FLAGS = {
    "cpu": ["-DGGML_METAL=OFF", "-DGGML_CUDA=OFF", "-DGGML_VULKAN=OFF"],
    "metal": ["-DGGML_METAL=ON", "-DGGML_CUDA=OFF", "-DGGML_VULKAN=OFF"],
    "cuda": ["-DGGML_METAL=OFF", "-DGGML_CUDA=ON", "-DGGML_VULKAN=OFF"],
    "vulkan": ["-DGGML_METAL=OFF", "-DGGML_CUDA=OFF", "-DGGML_VULKAN=ON"],
}
# Log lines that prove which backend actually initialised (patterns cover
# several whisper.cpp/ggml versions; matching lines are stored verbatim).
EVIDENCE = {
    "metal": re.compile(r"ggml_metal_init|using Metal backend", re.I),
    "cuda": re.compile(r"ggml_cuda_init: found \d+ CUDA|using CUDA\d* backend", re.I),
    "vulkan": re.compile(r"ggml_vulkan: Found \d+ Vulkan|using Vulkan\d* backend", re.I),
    "cpu": re.compile(r"system_info|using CPU backend", re.I),
}
NO_GPU = re.compile(r"no GPU found|use gpu\s*=\s*0|failed to initialize .*(Metal|CUDA|Vulkan)", re.I)
TIMING = re.compile(r"whisper_print_timings:\s+(load|mel|encode|decode|batchd|prompt|total) time\s*=\s*([\d.]+) ms")


def log(msg):
    print(msg, flush=True)


def sh(cmd, cwd=None, timeout=None):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout,
                       encoding="utf-8", errors="replace")
    return p.returncode, p.stdout, p.stderr


def detect_backends():
    found = ["cpu"]
    if sys.platform == "darwin":
        found.append("metal")
    if shutil.which("nvcc") or os.environ.get("CUDA_PATH"):
        found.append("cuda")
    if shutil.which("glslc") and (shutil.which("vulkaninfo") or os.environ.get("VULKAN_SDK")):
        found.append("vulkan")
    return found


def checkout(work: Path, ref: str):
    src = work / "whisper.cpp"
    if not src.exists():
        log(f"cloning whisper.cpp into {src}")
        rc, _, err = sh(["git", "clone", "--filter=blob:none", WHISPER_REPO, str(src)])
        if rc:
            raise SystemExit(f"git clone failed: {err}")
    sh(["git", "fetch", "--tags", "origin"], cwd=src)
    rc, _, err = sh(["git", "checkout", "--quiet", ref], cwd=src)
    if rc:
        raise SystemExit(f"git checkout {ref} failed: {err}")
    _, commit, _ = sh(["git", "rev-parse", "HEAD"], cwd=src)
    return src, commit.strip()


def build(src: Path, backend: str, jobs: int):
    bdir = src / f"build-{backend}"
    cfg = ["cmake", "-S", str(src), "-B", str(bdir), "-DCMAKE_BUILD_TYPE=Release",
           "-DWHISPER_BUILD_TESTS=OFF", "-DWHISPER_SDL2=OFF", *BACKEND_FLAGS[backend]]
    t0 = time.time()
    rc, out, err = sh(cfg)
    if rc:
        return None, {"stage": "configure", "log_tail": (out + err)[-3000:]}
    rc, out, err = sh(["cmake", "--build", str(bdir), "--config", "Release", "-j", str(jobs),
                       "--target", "whisper-cli"])
    if rc:
        return None, {"stage": "build", "log_tail": (out + err)[-3000:]}
    exe_name = "whisper-cli.exe" if sys.platform == "win32" else "whisper-cli"
    hits = sorted(bdir.rglob(exe_name))
    if not hits:
        return None, {"stage": "locate", "log_tail": "whisper-cli not found after build"}
    return hits[0], {"build_seconds": round(time.time() - t0, 1)}


def hf_sha256(filename):
    """Authoritative SHA-256 from the Hugging Face LFS metadata, if reachable."""
    try:
        with urllib.request.urlopen(HF_TREE, timeout=30) as r:
            for item in json.load(r):
                if item.get("path") == filename and item.get("lfs"):
                    return item["lfs"].get("oid"), item["lfs"].get("size")
    except Exception as e:  # noqa: BLE001
        log(f"  could not fetch checksum metadata: {e}")
    return None, None


def get_model(models_dir: Path, model: str):
    name = f"ggml-{model}.bin"
    path = models_dir / name
    expected, size = hf_sha256(name)
    if not path.exists():
        models_dir.mkdir(parents=True, exist_ok=True)
        log(f"  downloading {name}" + (f" ({size / 1e6:.0f} MB)" if size else ""))
        tmp = path.with_suffix(".part")
        urllib.request.urlretrieve(HF_FILE.format(name=name), tmp)
        tmp.rename(path)
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    actual = h.hexdigest()
    status = "verified" if expected and actual == expected else ("MISMATCH" if expected else "unverified")
    if status == "MISMATCH":
        raise SystemExit(f"checksum mismatch for {name}: expected {expected}, got {actual}. Delete it and retry.")
    return path, {"sha256": actual, "checksum": status}


def wav_seconds(path: Path):
    with wave.open(str(path)) as w:
        return w.getnframes() / w.getframerate()


def run_cli(exe, model, audio, threads, gpu: bool, lang):
    cmd = [str(exe), "-m", str(model), "-f", str(audio), "-t", str(threads), "-l", lang, "-nt"]
    if not gpu:
        cmd.append("-ng")
    t0 = time.perf_counter()
    rc, out, err = sh(cmd, timeout=1800)
    wall = (time.perf_counter() - t0) * 1000
    timings = {k: float(v) for k, v in TIMING.findall(err)}
    return rc, out.strip(), err, wall, timings


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ref", default=DEFAULT_REF, help="whisper.cpp git ref (default: voice-client proven commit)")
    ap.add_argument("--backends", default="auto", help="comma list of cpu,metal,cuda,vulkan or 'auto'")
    ap.add_argument("--models", default="base,small", help="comma list, e.g. tiny,base,small,medium,large-v3-turbo")
    ap.add_argument("--runs", type=int, default=5, help="timed runs per case (after 1 warm-up)")
    ap.add_argument("--threads", type=int, default=min(8, os.cpu_count() or 4))
    ap.add_argument("--lang", default="auto", help="whisper language (OmonFlow default is auto)")
    ap.add_argument("--audio", action="append", default=[], help="extra WAV file(s); jfk.wav is always included")
    ap.add_argument("--work", default=str(Path(__file__).with_name("work")))
    ap.add_argument("--out", default=str(Path(__file__).with_name("results")))
    ap.add_argument("--jobs", type=int, default=os.cpu_count() or 4)
    args = ap.parse_args()

    work = Path(args.work)
    work.mkdir(parents=True, exist_ok=True)
    src, commit = checkout(work, args.ref)
    backends = detect_backends() if args.backends == "auto" else [b.strip() for b in args.backends.split(",")]
    audios = [src / "samples" / "jfk.wav", *map(Path, args.audio)]
    report = {
        "schema": "omonflow-whisper-bench/1",
        "host": socket.gethostname(), "platform": platform.platform(), "machine": platform.machine(),
        "whisper_ref": args.ref, "whisper_commit": commit, "threads": args.threads, "runs": args.runs,
        "lang": args.lang, "started": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "builds": {}, "models": {}, "cases": [],
    }

    exes = {}
    for b in backends:
        if b not in BACKEND_FLAGS:
            log(f"unknown backend {b}, skipped")
            continue
        log(f"building {b} ...")
        exe, info = build(src, b, args.jobs)
        report["builds"][b] = {"ok": exe is not None, **info}
        if exe:
            exes[b] = exe
        else:
            log(f"  {b} build FAILED at {info['stage']} (see JSON for log tail)")

    for m in [x.strip() for x in args.models.split(",") if x.strip()]:
        log(f"model {m}")
        path, meta = get_model(work / "models", m)
        report["models"][m] = meta
        for audio in audios:
            secs = wav_seconds(audio)
            for b, exe in exes.items():
                gpu = b != "cpu"
                log(f"  {b:6s} {audio.name} ({secs:.1f}s): warm-up + {args.runs} runs")
                rc, text, err, _, _ = run_cli(exe, path, audio, args.threads, gpu, args.lang)
                evidence = [l.strip() for l in err.splitlines() if EVIDENCE[b].search(l)][:6]
                no_gpu = [l.strip() for l in err.splitlines() if NO_GPU.search(l)][:4]
                walls, totals = [], []
                for _ in range(args.runs if rc == 0 else 0):
                    rc, text, err, wall, t = run_cli(exe, path, audio, args.threads, gpu, args.lang)
                    if rc:
                        break
                    walls.append(wall)
                    totals.append(t.get("total", wall))
                ok = rc == 0 and bool(text) and len(walls) == args.runs
                validated = ok and bool(evidence) and (b == "cpu" or not no_gpu)
                case = {
                    "backend": b, "model": m, "audio": audio.name, "audio_seconds": round(secs, 2),
                    "exit_code": rc, "completed": ok, "validated": validated,
                    "backend_evidence": evidence, "gpu_fallback_evidence": no_gpu,
                    "median_total_ms": round(statistics.median(totals), 1) if totals else None,
                    "median_wall_ms": round(statistics.median(walls), 1) if walls else None,
                    "rtf": round(statistics.median(totals) / 1000 / secs, 3) if totals else None,
                    "last_timings_ms": t if walls else None, "transcript": text[:400],
                    "stderr_tail": None if ok else err[-2000:],
                }
                report["cases"].append(case)
                log(f"    -> {'VALIDATED' if validated else ('completed, NOT validated' if ok else 'FAILED')}"
                    f"  median {case['median_total_ms']} ms  RTF {case['rtf']}")

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out) / f"{socket.gethostname()}-{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "whisper_bench.json").write_text(json.dumps(report, indent=2))
    lines = [f"# whisper.cpp benchmark: {report['host']}", "",
             f"{report['platform']} · whisper.cpp `{commit[:10]}` · threads {args.threads} · runs {args.runs} · lang {args.lang}", "",
             "| Backend | Model | Audio | Validated | Median total (ms) | RTF | Evidence |",
             "|---|---|---|---|---:|---:|---|"]
    for c in report["cases"]:
        ev = (c["backend_evidence"] or ["none"])[0][:70].replace("|", "/")
        lines.append(f"| {c['backend']} | {c['model']} | {c['audio']} | {'yes' if c['validated'] else 'NO'} "
                     f"| {c['median_total_ms']} | {c['rtf']} | `{ev}` |")
    for b, info in report["builds"].items():
        if not info["ok"]:
            lines.append(f"\nBuild failed: **{b}** at {info['stage']}.")
    lines.append("\nRTF = processing time / audio duration (lower is faster; < 1 is faster than real time).")
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n")
    log(f"\nSaved {out_dir}")


if __name__ == "__main__":
    main()
