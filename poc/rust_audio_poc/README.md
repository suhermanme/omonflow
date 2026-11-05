# M01 proof of concept: Rust capture core + rolling pre-buffer

Isolated throwaway spike. Not the OmonFlow implementation; nothing else depends on it.

After spec v2 (on-demand capture, widget-driven), the continuous rolling pre-buffer here is **reference only and not part of the product**. `cold_start.py` and `on_demand.py` measure the v2 lifecycle: open, ready, warm-up, stop, release. Results are in `docs/M01-assessment.md` §11.

What it shows:

- `cpal` continuous input capture on a dedicated engine thread (the stream is `!Send` on some hosts).
- A real-time callback that only downmixes to mono and pushes into a lock-free SPSC ring (`rtrb`): no locks, allocation, logging, or Python.
- `src/timeline.rs`: a pure, hardware-free rolling history and session state machine. Every sample has an absolute index, so ordering, gaps, and duplicates can be tested exactly.
- A PyO3 (`pyo3` 0.30) extension built by maturin. Blocking calls release the GIL (`Python::detach`). Finished audio crosses to Python as one `bytes` copy (`numpy.frombuffer` gives a zero-copy view).

## Run

```bash
cargo test                                   # 6 buffer-logic tests, no hardware
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python maturin numpy
.venv/bin/maturin develop --release
.venv/bin/python smoke_test.py               # continuous pre-buffer (reference); plays one short beep
.venv/bin/python cold_start.py               # mic open -> first audio, per device
N=15 .venv/bin/python on_demand.py           # full on-demand cycle timing, per device
```

`cargo build --features python` fails at link time on macOS by design; maturin supplies the extension-module linker flags.

## Known gaps (deliberate for a spike)

- f32 input streams only; no i16/i32 conversion.
- Returns the device sample rate (48 kHz here). No resampling to 16 kHz yet.
- No device hot-swap or reconnect; it only detects `DeviceNotAvailable` and returns the partial recording.
- No hotkey; key press and release are simulated by Python calls.
- Only tested on macOS 27.2 / M4 Pro.
