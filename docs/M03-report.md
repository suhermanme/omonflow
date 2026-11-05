# M03: On-Demand Audio Core, Report

Date: 2026-11-05 · Machine: MacBook Pro M4 Pro, macOS 27.2 · built-in microphone (48 kHz, F32) · Python 3.13.14 · cpal 0.18.2 · sounddevice/PortAudio (fallback)
Status: **implemented and tested on macOS; comparison gate decision requested from the owner.**

## Summary

- **Real on-demand capture works end to end.** The mic button opens the microphone. The widget shows CAPTURE_READY when real signal arrives and RECORDING when speech activity is detected. Stop finalizes 16 kHz mono WAV and the microphone is released.
- **macOS itself confirms the privacy behavior.** CoreAudio's "device running" flag is off while idle, on only during capture, and off again after finalize or cancel.
- **The Rust core passed every check:**
  - 500 consecutive real-microphone cycles: 0 failures, 0 leaked streams.
  - 99.8–99.9 % of the audio arrived in every load scenario, with 0 drops.
  - Stream closed 11 ms after Stop.
- **The Python fallback works but is fragile.**
  - Under ordinary Python load it silently loses ~15 % of the audio, with no overflow reported.
  - Under heavy load it deadlocks inside PortAudio/CoreAudio and keeps the microphone open until the process is killed. This happened once even in a plain lifecycle run.
- **Recommendation for the gate:** keep the Rust core as the primary implementation. Keep the Python fallback only as a labelled degraded mode (details below).

## What was built

| Area | Delivered | Location |
|---|---|---|
| Session rules | CAPTURE_READY at the first non-zero sample, or after a warm-up cap with a "silent input" warning. Adaptive noise floor (20th percentile of the first 150 ms after ready). Activity = 10 ms RMS above max(min(floor×4, 0.05), 0.01) for 60 ms; UI-only and sticky. Bounded length. Leading-zero trim at finalize | `rust/src/audio/session.rs`, mirrored in `src/omonflow/audio/session.py` |
| Resampler | Hann-windowed sinc (16 zero crossings, tabulated kernel) to 16 kHz; WAV s16 encoder | `rust/src/audio/resample.rs`, Python mirror |
| Engine | One worker thread per capture attempt; the worker owns the cpal stream. Real-time callback converts any input format (f32/i16/i32/u16), downmixes, and pushes into a lock-free ring (no locks, allocation or Python). The stream is closed *before* the final drain. A timed-out worker is retired (never joined on the caller's path), and the next start uses a fresh worker. Device loss finalizes the partial audio | `rust/src/audio/engine.rs` |
| Python API | `omonflow._core.AudioCore`: frozen class, thread-safe, every wait releases the GIL, Rust never calls into Python. Typed errors (`DeviceTimeout`, `DeviceNotFound`, `SessionActive`, `CaptureFailed`). A fake source (open delay, hangs, warm-up zeros, speech onset, device loss) for tests/CI | `rust/src/audio/py.rs` |
| Python fallback | `SoundDeviceAudio` behind the same interface and rules, including retire-on-timeout | `src/omonflow/audio/sounddevice_backend.py` |
| Backend choice | Rust; if the extension cannot load → Python fallback, labelled "degraded" in Settings and diagnostics (`OMONFLOW_AUDIO_BACKEND` overrides) | `audio/factory.py` |
| Orchestrator | Open and finalize run on worker threads; states INITIALIZING → CAPTURE_READY → RECORDING → FINALIZING; timeout → ERROR "Microphone not responding" with **Retry**; device loss → finalize partial + notice; macOS permission check before opening; mic test; startup statistics. **Audio is discarded right after finalize** (transcription is M04/M05) | `app/orchestrator.py` |
| macOS helpers | Microphone permission via AVFoundation; OS-level "any input device running" check via CoreAudio | `platform/macos/audio.py` |
| Settings → Audio Input | Engine status (Native / degraded fallback), permission + "Open Privacy Settings…", microphone choice (persisted), device details, open timeout, 3-second microphone test with level meter, recent startup timings | `ui/settings/window.py` |
| Exit safety | If a capture is still stuck in the audio driver at quit, OmonFlow flushes logs and ends the process so the OS releases the microphone | `app/main.py` |
| Comparison harness | Lifecycle cycles, integrity under GIL load and on-screen UI load, CPU; single-scenario mode with per-process time limits | `tools/bench/audio_compare.py` |

## Acceptance criteria (PLAN.md M03)

| # | Criterion | Result | Evidence |
|---|---|---|---|
| 1 | No stream while idle: engine state **and** OS-level check (macOS) | **Pass** | `test_privacy_hw.py` (real mic): idle app, opening Settings, listing devices → CoreAudio reports no running input; running only between start and finalize; released after cancel. OS check also every 25 cycles in the 500-cycle runs |
| 2 | ≥ 500 consecutive cycles per device; 0 crashes; 0 leaked streams | **Pass (Rust, built-in mic)** | 500 cycles in 120 s: 0 failures, 0 timeouts, 0 leaks, 0 retired workers left, mic off afterwards, RSS +5.2 MB. Python fallback: 500/500 in a second run, but the first run ended in a close timeout + deadlock (see below). **USB and Bluetooth mics not tested** (none connected) |
| 3 | Integrity: no gaps/duplicates from the first callback; bounded memory | **Pass** | Rust unit tests (ordering across irregular blocks, every callback sample reaches the session); `test_no_audio_lost_after_first_callback` (captured samples = output samples × 3 exactly, both backends); `test_bounded_session_length` |
| 4 | Hung open → ERROR within timeout + 100 ms; Retry works; shutdown ≤ 2 s with a retired worker | **Pass** | `test_hung_open_times_out_retires_and_retry_works` (both backends), `test_hung_device_errors_then_retry_succeeds` (orchestrator) |
| 5 | CAPTURE_READY never before real signal; RECORDING never before activity; nothing lost after the first callback | **Pass with fakes; partly on hardware** | Warm-up tests (150 ms zeros → ready at 150 ms, zeros kept until trimmed); `test_states_follow_real_signal`. On the real built-in mic warm-up was 0 ms in all 1,000+ starts today (the 130 ms zeros from M01 did not recur). **Activity on real speech not validated**: nobody spoke during the runs |
| 6 | Metrics table per device | **Partly** | Built-in mic below. USB/Bluetooth not connected; "click → RECORDING with real speech" not measured |
| 7 | Comparison gate report | **Done** | Below and `docs/m03/audio_compare.json`. Decision is the owner's |
| 8 | Unplugging during recording returns partial audio + warning | **Pass with fakes only** | `test_device_loss_keeps_partial_audio` (both backends), `test_device_loss_finalizes_partial`. **A physical unplug test is still needed** (USB mic) |
| M02 carry-over | Packaged app's mic prompt names OmonFlow | **Not done** | Packaged app builds, signs, loads the Rust audio core and keeps the mic off while idle. The prompt needs someone to click the mic button and see the dialog; the screen was locked |

## Measurements: built-in microphone (M4 Pro)

| Metric | Rust core | Python fallback |
|---|---|---|
| Click → CAPTURE_READY, p50 / p95 / max | 68 / 73 / 77 ms | 63 / 74 / 83 ms |
| Warm-up (exact zeros after open) | 0 ms in all runs | 0 ms |
| Stop → stream closed, p50 / max | **11 / 18 ms** | 118 / 129 ms |
| Finalize (trim + resample + WAV), p50 | 0.9 ms | 10.5 ms |
| 500 cycles | 0 failures, 0 leaks | Run 1: aborted (close > 3 s, then deadlock). Run 2: 0 failures |
| CPU while capturing, no load | 1.3 % of a core | 3.4 % |
| Memory growth over 500 cycles | +5.2 MB | +8.3 MB |

**Integrity**, 8 s capture: captured ÷ expected samples, where expected = device rate × time the stream was open.

| Load in the same process | Rust core | Python fallback |
|---|---|---|
| None | 99.81 % | 98.42 % |
| Python thread busy, default 5 ms GIL slices | 99.90 % | **84.55 %**, 0 overflows reported (silent loss) |
| Python thread busy, 50 ms GIL slices | 99.82 % | **hung**, killed after 60 s (a dry run that completed kept 2.2 %) |
| Python thread busy, 200 ms GIL slices | 99.83 % | **hung**, killed after 60 s (dry run: 1.8 %) |
| OmonFlow widget animating (Circular Wave, ~31 fps) | 99.84 % | 98.44 % |

Rust's remaining 0.1–0.2 % is the gap between the stream opening and its first callback (~15 ms), not dropped audio: ring drops and xruns were 0 in every run. The UI-load row ran with the screen locked. The widget painted every frame, but macOS may not have composited it, so real on-screen load could be somewhat higher.

## Comparison gate: recommendation

**Keep Rust as the primary audio core.** The benefit is measurable and large exactly where it matters:
1. **Integrity under load.** Rust lost nothing in any scenario. The Python fallback silently dropped 15 % of the audio as soon as another Python thread was busy. During dictation the app runs widget painting (13–46 % of a core, M02) and, from M04, HTTP and LLM work on the Python side.
2. **Reliability.** The Python fallback deadlocked inside PortAudio/CoreAudio once in 500 plain cycles and under every heavy-load run, and **kept the microphone open until the process was killed**. Rust: 0 failures in 500 cycles plus all load runs.
3. **Responsiveness.** The microphone is released 11 ms after Stop vs 118 ms.
4. **Cost:** ~1,370 lines of Rust in the audio module, tests included. Start latency is equal.

**Keep the Python fallback** only as a labelled degraded mode for platforms where the extension cannot load. It is small, already behind the same interface, and the exit safety net prevents it from holding the mic after quit. If you prefer not to maintain it, removing it is a contained change.

## Defects found and fixed in M03

| Defect | Fix |
|---|---|
| A cancelled open's late result could clear a *newer* capture (both backends) | Clean up only the worker that was waited on |
| `uv sync` did not rebuild the Rust extension after Rust edits | `tool.uv.cache-keys` on `rust/**/*.rs` |
| Python fallback deadlock kept the process (and mic) alive after a crash | Exit safety net at quit; documented as a fallback limitation |
| Benchmark lost results when one backend failed | Save per backend; per-scenario processes with time limits |

## Known limitations and open items

1. **Needs a person at the Mac:**
   - The packaged app's microphone permission prompt (does it name OmonFlow?).
   - Activity detection on real speech in quiet and noisy rooms.
   - A physical USB unplug during recording.
2. **Only the built-in mic was measured.** The USB mic was not connected (M01: it once took >10 s to open and up to 2.8 s normally). No Bluetooth headset was tested; their call-profile switch is expected to be slow.
3. **Permission handling during development:** run from a terminal, permission belongs to the terminal. A packaged ad-hoc build may ask again after each rebuild (SPEC §18).
4. **Activity thresholds are first estimates** (factor 4, cap 0.05 RMS, min 0.01 RMS, 60 ms). Tune with real speech in M05.
5. **Windows/Linux audio paths are unbuilt and untested** (cpal WASAPI/ALSA/PipeWire compile on CI only once a remote exists). M08.
6. **Captured audio is thrown away** after finalize until transcription arrives in M04/M05.

## How to reproduce

```bash
uv sync
uv run pytest -q                                            # 123 tests (fake microphones)
OMONFLOW_HW_TESTS=1 uv run pytest -q tests/test_privacy_hw.py  # real mic + CoreAudio check
uv run python tools/bench/audio_compare.py --backends rust --cycles 500
uv run python tools/bench/audio_compare.py --backends python --scenario 5   # one load scenario
```
