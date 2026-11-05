"""Privacy-mode cost: time from 'open mic' to first audio callback (cold start)."""
import sys, time
import numpy as np
import omonflow_audio_poc as poc

def measure(device, rounds=10):
    opens, firsts = [], []
    for _ in range(rounds):
        t0 = time.perf_counter()
        eng = poc.AudioEngine(device=device, history_ms=1000)
        t1 = time.perf_counter()
        while eng.stats()["callbacks"] == 0:
            time.sleep(0.0005)
        t2 = time.perf_counter()
        eng.shutdown()
        opens.append((t1 - t0) * 1000); firsts.append((t2 - t0) * 1000)
        time.sleep(0.3)
    f = lambda xs: f"median {np.median(xs):.0f} ms, max {np.max(xs):.0f} ms"
    print(f"{device}: open+play {f(opens)}; first audio {f(firsts)}")

for d in [x["name"] for x in poc.list_input_devices()]:
    measure(d)
