"""Hardware smoke test for the M01 audio proof of concept.

1. Session test: simulated key press/release cycles. Checks lengths,
   sample continuity across sessions, ring drops, and call latency.
2. Loopback first-word test: plays a short beep through the speakers and
   triggers the "key press" only after the mic has already heard it, then
   checks that the beep onset is inside the recorded pre-buffer.

Run:  .venv/bin/python smoke_test.py
"""

import os
import random
import resource
import subprocess
import sys
import tempfile
import time
import wave

import numpy as np

import omonflow_audio_poc as poc

PRE_MS = 400
POST_MS = 100


def rss_mb():
    # macOS reports bytes, Linux kilobytes.
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return r / 1e6 if sys.platform == "darwin" else r / 1e3


def session_test(eng, rounds=20):
    rate = eng.sample_rate
    print(f"\n[session test] {rounds} press/release cycles, pre={PRE_MS} ms post={POST_MS} ms")
    start_lat, stop_lat, ages, errors = [], [], [], []
    for i in range(rounds):
        hold = random.uniform(0.2, 1.2)
        t0 = time.perf_counter()
        info = eng.start_session(PRE_MS)
        start_lat.append((time.perf_counter() - t0) * 1000)
        ages.append(info["last_callback_age_us"] / 1000)
        time.sleep(hold)
        t1 = time.perf_counter()
        rec = eng.stop_session(POST_MS)
        stop_lat.append((time.perf_counter() - t1) * 1000)

        samples = np.frombuffer(rec["samples"], dtype=np.float32)
        expected = (PRE_MS + POST_MS) / 1000 * rate + hold * rate
        # Held duration is wall-clock; allow one callback (~10-20 ms) slack.
        if rec["pre_samples"] != PRE_MS * rate // 1000:
            errors.append(f"round {i}: pre_samples={rec['pre_samples']}")
        if abs(len(samples) - expected) > 0.03 * rate:
            errors.append(f"round {i}: len={len(samples)} expected~{expected:.0f}")
        if rec["dropped_total"]:
            errors.append(f"round {i}: dropped={rec['dropped_total']}")
        time.sleep(random.uniform(0.05, 0.6))

    p = lambda xs: f"median {np.median(xs):.2f} / max {np.max(xs):.2f} ms"
    print(f"  start_session call latency: {p(start_lat)}")
    print(f"  stop_session call latency (includes {POST_MS} ms padding): {p(stop_lat)}")
    print(f"  newest-audio age at key press: {p(ages)}")
    print(f"  errors: {errors or 'none'}")
    return not errors


def write_beep(path, rate=48000, ms=300, freq=1000, amp=0.25):
    t = np.arange(int(rate * ms / 1000)) / rate
    tone = (np.sin(2 * np.pi * freq * t) * amp * 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(tone.tobytes())


def loopback_test(eng):
    rate = eng.sample_rate
    print("\n[loopback first-word test] playing a 300 ms beep via afplay")
    if sys.platform != "darwin":
        print("  skipped (uses afplay; macOS only)")
        return None
    # Measure ambient level first.
    eng.level()
    time.sleep(0.5)
    ambient = eng.level()
    threshold = max(0.03, ambient * 2.5)
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "beep.wav")
        write_beep(path)
        proc = subprocess.Popen(["afplay", path])
        heard = False
        deadline = time.perf_counter() + 2.0
        while time.perf_counter() < deadline:
            if eng.level() > threshold:
                heard = True
                break
            time.sleep(0.005)
        if not heard:
            proc.wait()
            print(f"  mic never heard the beep (ambient peak {ambient:.4f}).")
            print("  Likely cause: microphone permission not granted to this terminal,")
            print("  or speaker volume muted. Inconclusive.")
            return None
        # Simulate a user who starts speaking 250 ms before pressing the key.
        time.sleep(0.25)
        eng.start_session(PRE_MS)
        time.sleep(0.6)
        rec = eng.stop_session(POST_MS)
        proc.wait()

    s = np.frombuffer(rec["samples"], dtype=np.float32)
    pre = rec["pre_samples"]
    win = rate // 200  # 5 ms windows
    env = np.array([np.abs(s[i:i + win]).max() for i in range(0, len(s) - win, win)])
    onset_win = int(np.argmax(env > threshold))
    onset = onset_win * win
    lead_ms = (pre - onset) / rate * 1000
    print(f"  ambient peak {ambient:.4f}, threshold {threshold:.3f}")
    print(f"  beep onset at sample {onset}; key press at sample {pre} (end of pre-buffer)")
    ok = 0 <= onset < pre and env[:max(1, onset_win)].max() < threshold
    # Onset should be ~250 ms (+ detection delay) before the key press.
    ok = ok and 200 <= lead_ms < PRE_MS
    if ok:
        print(f"  PASS: onset preserved {lead_ms:.0f} ms before the key press, preceded by quiet audio")
    else:
        print("  FAIL: onset not found inside the pre-buffer")
    return ok


def main():
    print("devices:", poc.list_input_devices())
    rss0 = rss_mb()
    eng = poc.AudioEngine(history_ms=1000)
    print(f"engine: {eng.device_name} @ {eng.sample_rate} Hz")
    time.sleep(1.2)  # let the rolling buffer fill
    st = eng.stats()
    print(f"after 1.2 s: callbacks={st['callbacks']} dropped={st['dropped']} error={st['last_error']}")

    ok1 = session_test(eng)
    ok2 = loopback_test(eng)

    t = time.perf_counter()
    eng.shutdown()
    print(f"\nshutdown took {(time.perf_counter() - t) * 1000:.1f} ms; peak RSS {rss_mb():.1f} MB (was {rss0:.1f} MB before engine)")
    print("RESULT:", "session OK" if ok1 else "session FAILED", "/",
          {True: "loopback PASS", False: "loopback FAIL", None: "loopback inconclusive"}[ok2])


if __name__ == "__main__":
    main()
