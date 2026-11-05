# M06: Hardware and Inference Settings, Report

Date: 2026-11-05 · Machine: MacBook Pro M4 Pro, 24 GB, macOS 27.2 · whisper.cpp 1.9.5 (Homebrew, ggml 0.27)
Status: **implemented and tested on the M4 Pro. Intel/AMD and NVIDIA results need your machines. Owner review requested.**

## Summary

- **Settings → Hardware & Acceleration** keeps four things apart, as the spec requires:
  - **Detected:** what the hardware is.
  - **Available:** what whisper.cpp itself reports it can use.
  - **Validated:** a benchmark passed on this computer, with the measured speed.
  - **Active:** what the running engine's own log says it uses.
  - A GPU's presence never counts as acceleration.
- **Measured on the M4 Pro** (bundled 11 s clip, correct transcript every time):

  | Backend | small | large-v3-turbo-q5_0 |
  |---|---|---|
  | Metal | **0.018×** real time (199 ms) | **0.057×** (628 ms) |
  | CPU (12 threads auto) | 0.113× (1,246 ms) | 0.425× (4,677 ms) |

  Metal is 6–7× faster than the CPU here, with both models comfortably faster than real time.
- **Changing model, backend or threads never breaks a working engine.** The new configuration starts next to the old one. It must start, report the requested backend in its own log, and transcribe the test clip correctly. Only then does it take over, and only then are your settings saved. Otherwise you get an explanation, an offer to use the CPU, and the old engine keeps running.
- **Profiles** (Fast / Balanced / Accurate) are recommendations based on this machine's RAM and the devices whisper.cpp reports. They change nothing until you click *Apply profile…*. Choosing a model or backend by hand sets the profile to *Custom*.
- **New option:** unload the model after 5–60 idle minutes. It is reloaded automatically when you click the mic; the reload is hidden behind recording, since start-up takes about 0.3 s. The default is "never" (fastest).

## What was built

| Area | Delivered | Location |
|---|---|---|
| Hardware detection | OS/version, CPU model/vendor/cores/performance cores/features (ARM: BF16, DotProd, FP16, I8MM, SME…; x86: AVX/AVX2/AVX-512/FMA…), RAM, GPUs with VRAM/driver/APIs (Apple: Metal + unified memory; NVIDIA: nvidia-smi; Vulkan: vulkaninfo; AMD VRAM via sysfs), CUDA toolkit/Vulkan runtime. Structured sources only, time-limited, off the UI thread | `hardware/detect.py` |
| Backends | Devices parsed from whisper.cpp's start-up log (GPU/iGPU types only; BLAS is an accelerator, not a choice); `--no-gpu` / `--device N`; validation records per executable + version + device + model | `stt/backends.py` |
| Controller | Start/switch with candidate + checks + swap (promoted server takes over pidfile and log); requested-vs-reported backend check; validation in a separate temporary server; idle unload + automatic reload; last-transcription metrics | `app/whisper_controller.py` |
| Profiles | Recommendation from RAM, unified memory and *reported* devices; CPU-only machines never get GPU recommendations | `stt/profiles.py` |
| Settings | Hardware & Acceleration: hardware summary, backend table with Validate buttons, profile + Apply, backend + threads + Apply, unload when idle, runtime (selected vs running, last transcription speed); failure dialog with "Use CPU instead". Speech Recognition's "Use" now goes through the same commit-after-success path | `ui/settings/window.py`, `app/main.py` |
| Sample | whisper.cpp's `jfk.wav` (MIT) bundled for validation and self-tests | `resources/jfk.wav`, `NOTICE` |
| Validation kit | `omonflow_validate.py`: OmonFlow's own detection + validation for any whisper-server build and model, JSON report | `tools/validation/` |

## Acceptance criteria (PLAN.md M06)

| Criterion | Result | Evidence |
|---|---|---|
| Detected / available / validated / active shown separately on every validation machine run | **Pass on the M4 Pro; others pending** | `docs/m06/settings-hardware.png`; `omonflow_validate.py` run here. i9/RX580 and RTX 3060 need your runs |
| No GPU claim without benchmark evidence | **Pass** | "Validated" only from a benchmark whose log shows the backend; a listed GPU that silently falls back to CPU is rejected (`test_backend_that_silently_falls_back_is_rejected`) |
| A failed backend rolls back to the previous working config with an explanation | **Pass** | Real: requesting CUDA on this Mac was refused, engine kept running on CPU. Tests: unavailable backend, silent fallback, settings unchanged on failure (`test_engine_settings_saved_only_after_success`) |
| Profiles never overwrite manual choices | **Pass** | Profiles only recommend; applying asks first; manual changes set "custom" |
| CPU threads, GPU device selection, runtime diagnostics | **Pass** | Threads field (Automatic or 1–N), device choice per reported GPU, runtime panel |

## Defects found and fixed in M06

| Defect | Fix |
|---|---|
| **A backend switch could kill the running engine.** The promoted candidate kept its candidate pidfile, so the next candidate's stale-process cleanup stopped it (hidden by the crash monitor's restart) | The promoted server takes over the main pidfile and log names; four real swaps now show 0 restarts and one pidfile |
| Settings page wider than the window; stale "Validate" buttons; long profile names | Wrapping labels, no horizontal scrolling, hide replaced cell widgets, short profile names with the description below |

## Known limitations and open items

1. **Only the M4 Pro has been validated.** Steps for the i9-9900K/RX580 and the RTX 3060 (Ubuntu): `tools/validation/README.md`, `whisper_bench.py` then `omonflow_validate.py`. Hardware detection on Linux and Windows is written but untested.
2. **Windows:** a backend swap renames the running server's log file, which Windows doesn't allow for open files. M08 must use a different approach there (e.g. per-process log names).
3. **No fabricated numbers.** Apple Silicon shows "unified memory" instead of a VRAM figure, and GPU utilisation isn't shown (no reliable source).
4. **Thread count isn't benchmarked separately.** "Automatic" uses min(8, cores − 2) as before; the Validate button measures whatever is configured.
5. **"Automatic" uses whisper.cpp's own choice** (first GPU, else CPU). It doesn't run a validation by itself; the Active column shows what's really in use.
6. **The Intel Mac question is still open** (SPEC targets Intel macOS; Metal is not assumed there).

## How to reproduce

```bash
uv run pytest -q                                         # 171 tests (fake server with devices)
uv run python tools/validation/omonflow_validate.py --whisper-server $(which whisper-server) \
    --model ~/local-ai/whisper.cpp/models/ggml-small.bin
```

## Fixes from owner testing (2026-11-05, after the M06 report)

| Owner report | Cause | Fix | Verified |
|---|---|---|---|
| "Error transcribing" in the packaged app | Not transcription: the ad-hoc build lacked Accessibility, so text was copied; the widget only said "Error" | Widget says "Copied — ⌘V" / "Not inserted"; docs explain that ad-hoc rebuilds need permissions granted again | Owner |
| Long dictation into Chrome inserted nothing | Chrome had no text box focused (owner was reading the page); Chrome silently drops the paste and exposes no signal for it | Unverifiable insertions show "Done · click to copy" for 4 s; apps that expose a non-editable focus get the text copied with a clear message | Reproduced and fixed with an isolated Chrome instance |
| English inserted as (badly translated) Indonesian | Whisper translates when it picks the wrong language; the old hint named Indonesian; on the owner's accented voice the translation scored almost as high as the real text | Neutral vocabulary hint; confidence-based choice between English and Indonesian; optional main language that wins unless the other is clearly better | Owner |
| Indonesian parts of a mixed dictation translated to English (and, reproduced, dropped) | Whisper decodes a whole window in one language | Auto splits the finished recording at pauses and chooses the language per part (contiguous parts, nothing dropped) | Real-mic test 2/2 and **owner confirmed** |

Cost of per-sentence language choice: two transcription passes per part (Stop → inserted ≈ 1.8 s for 12 s of mixed speech, ≈ 0.3 s for one sentence). Optimising this (e.g. a fast single pass when the language is unambiguous) is left for later.
