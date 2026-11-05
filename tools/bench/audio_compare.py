#!/usr/bin/env python3
"""M03 comparison gate: Rust audio core vs Python (sounddevice) fallback.

On a real microphone, for each backend:
  1. Lifecycle: N start → ready → short capture → stop/finalize cycles
     (every 5th cancelled). Latencies, failures, timeouts, leaked streams
     (OS-level check on macOS), memory growth.
  2. Integrity under load: 8 s captures while a Python thread hogs the GIL
     at different switch intervals (emulates heavy UI work such as widget
     painting). Reports lost audio (captured vs expected samples),
     ring drops, overflows/xruns.
  3. CPU while capturing (no load).

Usage: uv run python tools/bench/audio_compare.py [--cycles 500] [--device-id ID] [--out docs/m03]
"""

import argparse
import json
import platform
import resource
import statistics
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from omonflow.audio.rust_backend import RustAudio  # noqa: E402
from omonflow.audio.sounddevice_backend import SoundDeviceAudio  # noqa: E402

if sys.platform == "darwin":
    from omonflow.platform.macos.audio import any_input_running  # noqa: E402
else:
    def any_input_running():
        return None


def pct(values, q):
    if not values:
        return None
    v = sorted(values)
    return round(v[min(len(v) - 1, int(round(q / 100 * (len(v) - 1))))], 1)


def cpu_s():
    r = resource.getrusage(resource.RUSAGE_SELF)
    return r.ru_utime + r.ru_stime


def rss_mb():
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(r / 1e6 if sys.platform == "darwin" else r / 1e3, 1)


def lifecycle(make, cycles):
    a = make()
    click_ready, warm, stop_closed, finalize, fails, timeouts, leaks = [], [], [], [], [], 0, 0
    close_timeouts = 0
    rss0 = rss_mb()
    t_all = time.perf_counter()
    for i in range(cycles):
        t0 = time.perf_counter()
        try:
            a.start_capture()
            info = a.wait_until_ready(5000)
        except Exception as e:  # noqa: BLE001
            fails.append(f"{type(e).__name__}: {e}")
            timeouts += "Timeout" in type(e).__name__
            continue
        click_ready.append((time.perf_counter() - t0) * 1000)
        warm.append(info.get("warmup_ms") or 0.0)
        time.sleep(0.15)
        if i % 5 == 4:
            a.cancel_capture()
        else:
            a.stop_capture()
            try:
                rec = a.finalize_audio()
            except Exception as e:  # noqa: BLE001 - e.g. the stream did not close in time
                fails.append(f"cycle {i}: finalize {type(e).__name__}: {e}")
                close_timeouts += 1
                continue
            stop_closed.append(rec.get("stop_to_closed_ms") or 0.0)
            finalize.append(rec.get("finalize_ms") or 0.0)
        if a.stream_open():
            leaks += 1
        if i % 25 == 24:
            print(f"  cycle {i + 1}/{cycles}", flush=True)
        if i % 25 == 24 and any_input_running():
            time.sleep(0.3)  # allow the OS to update, then re-check
            leaks += bool(any_input_running())
    elapsed = time.perf_counter() - t_all
    retired = a.retired_count()
    a.shutdown()
    time.sleep(0.5)
    return {
        "cycles": cycles, "seconds": round(elapsed, 1), "failures": len(fails), "timeouts": timeouts,
        "close_timeouts": close_timeouts,
        "failure_examples": fails[:3], "stream_leaks": leaks, "retired_workers_left": retired,
        "os_mic_running_after": any_input_running(),
        "click_to_ready_ms": {"p50": pct(click_ready, 50), "p95": pct(click_ready, 95), "max": pct(click_ready, 100)},
        "warmup_ms": {"p50": pct(warm, 50), "max": pct(warm, 100), "nonzero_runs": sum(w > 0 for w in warm)},
        "stop_to_closed_ms": {"p50": pct(stop_closed, 50), "max": pct(stop_closed, 100)},
        "finalize_ms": {"p50": pct(finalize, 50), "max": pct(finalize, 100)},
        "rss_growth_mb": round(rss_mb() - rss0, 1),
    }


def hog(stop, interval):
    old = sys.getswitchinterval()
    sys.setswitchinterval(interval)
    x = 0
    try:
        while not stop.is_set():
            for _ in range(20000):
                x += 1
    finally:
        sys.setswitchinterval(old)


def integrity(make, seconds, switch_interval):
    a = make()
    stop = threading.Event()
    th = None
    if switch_interval is not None:
        th = threading.Thread(target=hog, args=(stop, switch_interval), daemon=True)
        th.start()
    c0 = cpu_s()
    a.start_capture()
    a.wait_until_ready(5000)
    t0 = time.perf_counter()
    time.sleep(seconds)
    a.stop_capture()
    wall = time.perf_counter() - t0
    rec = a.finalize_audio(10000)
    cpu = (cpu_s() - c0) / wall * 100
    stop.set()
    if th:
        th.join()
    a.shutdown()
    expected = rec["device_rate"] * (rec["stream_ms"] or 0) / 1000
    return {
        "load": "none" if switch_interval is None else f"GIL hog, switch interval {int(switch_interval * 1000)} ms",
        "seconds": seconds, "captured_samples": rec["captured_samples"], "expected_samples": int(expected),
        "completeness": round(rec["captured_samples"] / expected, 4) if expected else None,
        "ring_dropped": rec["dropped"], "overflows_or_xruns": rec["xruns"],
        "cpu_percent_one_core": round(cpu, 1) if switch_interval is None else None,
    }


def integrity_ui(make, seconds, visualizer="circular-wave"):
    """Capture while OmonFlow's own widget animates on screen (real UI load)."""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from omonflow.core.states import State
    from omonflow.ui.widget.floating import FloatingWidget

    app = QApplication.instance() or QApplication([])
    w = FloatingWidget()
    w.set_visualizer(visualizer)
    w.move(200, 200)
    w.show()
    w.set_state(State.RECORDING.value)
    level = QTimer()
    level.timeout.connect(lambda: w.set_level(0.6))
    level.start(33)
    a = make()
    box = {}

    def work():
        a.start_capture()
        a.wait_until_ready(5000)
        time.sleep(seconds)
        a.stop_capture()
        box["rec"] = a.finalize_audio(10000)

    th = threading.Thread(target=work, daemon=True)
    th.start()
    poll = QTimer()
    poll.timeout.connect(lambda: (not th.is_alive()) and app.quit())
    poll.start(50)
    app.exec()
    level.stop()
    poll.stop()
    frames = w.frames_painted
    w.hide()
    w.deleteLater()
    a.shutdown()
    rec = box["rec"]
    expected = rec["device_rate"] * (rec["stream_ms"] or 0) / 1000
    return {"load": f"OmonFlow widget animating ({visualizer}, {frames} frames)", "seconds": seconds,
            "captured_samples": rec["captured_samples"], "expected_samples": int(expected),
            "completeness": round(rec["captured_samples"] / expected, 4) if expected else None,
            "ring_dropped": rec["dropped"], "overflows_or_xruns": rec["xruns"], "cpu_percent_one_core": None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cycles", type=int, default=500)
    ap.add_argument("--device-id", default=None)
    ap.add_argument("--seconds", type=float, default=8.0)
    ap.add_argument("--out", default=str(ROOT / "docs" / "m03"))
    ap.add_argument("--no-ui", action="store_true", help="skip the on-screen widget load scenario")
    ap.add_argument("--backends", default="rust,python")
    ap.add_argument("--skip-integrity", action="store_true")
    ap.add_argument("--scenario", default=None,
                    help="run one integrity scenario (none|5|50|200|ui) for --backends and print RESULT json")
    args = ap.parse_args()
    if any_input_running():
        sys.exit("Another app is using a microphone; close it for a clean measurement.")
    if args.scenario:
        name = args.backends.split(",")[0]
        make = (lambda: RustAudio(device_id=args.device_id)) if name == "rust" else (lambda: SoundDeviceAudio())
        if args.scenario == "ui":
            r = integrity_ui(make, args.seconds)
        else:
            r = integrity(make, args.seconds, None if args.scenario == "none" else int(args.scenario) / 1000)
        print("RESULT " + json.dumps(r), flush=True)
        return

    rust_dev = args.device_id
    makers = {
        "rust": lambda: RustAudio(device_id=rust_dev),
        "python": lambda: SoundDeviceAudio(),
    }
    report = {"machine": platform.platform(), "python": sys.version.split()[0],
              "date": time.strftime("%Y-%m-%d %H:%M"), "backends": {}}
    devices = RustAudio().enumerate_devices()
    report["device"] = next((d for d in devices if (d["id"] == rust_dev) or (rust_dev is None and d["is_default"])), None)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    path = out / "audio_compare.json"
    if path.exists():  # merge with earlier runs of other backends
        report["backends"] = json.loads(path.read_text()).get("backends", {})
    for name in args.backends.split(","):
        make = makers[name]
        print(f"[{name}] lifecycle x{args.cycles} ...", flush=True)
        res = {"lifecycle": lifecycle(make, args.cycles), "integrity": []}
        for interval in (() if args.skip_integrity else (None, 0.005, 0.05, 0.2)):
            print(f"[{name}] integrity, load={interval} ...", flush=True)
            res["integrity"].append(integrity(make, args.seconds, interval))
        if not args.no_ui and not args.skip_integrity:
            print(f"[{name}] integrity, UI load ...", flush=True)
            res["integrity"].append(integrity_ui(make, args.seconds))
        report["backends"][name] = res
        print(json.dumps(res, indent=1), flush=True)
        path.write_text(json.dumps(report, indent=2))  # save after each backend
    print(f"saved {path}")


if __name__ == "__main__":
    main()
