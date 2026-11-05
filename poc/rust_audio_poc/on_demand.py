"""Usage: [N=15] python on_demand.py ["device name" ...]

On-demand capture lifecycle measurements (spec change §4).

Per cycle: open device (click) -> first callback (ready) -> record 0.6 s ->
stop/finalize -> release. Also checks for device warm-up: leading samples
that are digital silence or far below the room's noise floor.
"""
import time
import numpy as np
import omonflow_audio_poc as poc

def cycle(device):
    t_click = time.perf_counter()
    eng = poc.AudioEngine(device=device, history_ms=2000)
    t_open = time.perf_counter()
    while eng.stats()["callbacks"] == 0:
        time.sleep(0.0005)
    t_ready = time.perf_counter()
    # Keep everything captured since the stream started (startup buffer).
    eng.start_session(2000)
    time.sleep(0.6)
    t_stop = time.perf_counter()
    rec = eng.stop_session(0)
    t_final = time.perf_counter()
    eng.shutdown()
    t_released = time.perf_counter()
    s = np.abs(np.frombuffer(rec["samples"], dtype=np.float32))
    rate = rec["sample_rate"]
    floor = np.median(s[-rate // 5:])            # room noise, last 200 ms
    zeros = int(np.argmax(s > 0)) if s.any() else len(s)
    warm = int(np.argmax(s > floor * 0.25)) if floor > 0 else zeros
    return dict(
        open=(t_open - t_click) * 1e3, ready=(t_ready - t_click) * 1e3,
        stop_to_final=(t_final - t_stop) * 1e3, release=(t_released - t_final) * 1e3,
        zeros_ms=zeros / rate * 1e3, warm_ms=warm / rate * 1e3,
        dropped=rec["dropped_total"], pre=rec["pre_samples"] / rate * 1e3,
    )

import sys
for dev in (sys.argv[1:] or [d["name"] for d in poc.list_input_devices()]):
    rows, fails = [], []
    for _ in range(int(__import__("os").environ.get("N", "15"))):
        try:
            rows.append(cycle(dev))
        except RuntimeError as e:
            fails.append(str(e))
        time.sleep(0.4)
    print(f"\n{dev} ({len(rows)+len(fails)} cycles, {len(fails)} failed: {set(fails) or ''})")
    if not rows:
        continue
    for k in ["open", "ready", "pre", "zeros_ms", "warm_ms", "stop_to_final", "release", "dropped"]:
        v = np.array([r[k] for r in rows])
        print(f"  {k:14s} median {np.median(v):8.1f}  max {v.max():8.1f}  min {v.min():8.1f}")
