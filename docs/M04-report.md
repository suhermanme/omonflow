# M04: Whisper Process Manager and Models, Report

Date: 2026-11-05 · Machine: MacBook Pro M4 Pro, macOS 27.2 · whisper.cpp 1.9.5 (Homebrew `whisper-server`, ggml 0.27) · Python 3.13.14
Status: **implemented and tested on macOS. Owner review requested before M05.**

## Summary

- **The managed engine works:**
  - OmonFlow starts its own whisper-server on a free local port at launch, waits until the model is loaded, reads which backend whisper.cpp actually chose, and stops the server when OmonFlow quits.
  - On the M4 Pro: ready in 0.2–0.3 s with Metal. The 11 s test clip was transcribed in 0.20 s with `small` and 0.64 s with `large-v3-turbo-q5_0`.
- **Crash recovery:** a killed server is restarted automatically (1.5–2.6 s on real hardware), at most 3 times in 5 minutes, then reported as failed.
- **Safety:** only the server OmonFlow started is ever stopped. Another whisper-server you run yourself is left alone; tested with the real binary.
- **Models:**
  - A catalog of 15 whisper.cpp models, stored in your user data folder.
  - Downloads ask first, show size and memory, resume after interruption, and are checked against the SHA-256 published by whisper.cpp on Hugging Face.
  - Existing model files can be imported (hard-linked, no extra disk space) after the same checksum check.
- **A real-world finding:** the real whisper-server **crashes** when asked to load a damaged model file. OmonFlow now refuses damaged files up front. If a crash happens anyway, it waits for the automatic restart on the previous model and reports "could not be loaded; restored …".
- **Still not inserting text:** dictation still captures audio and discards it. Wiring transcription into dictation and inserting the text is M05.

## What was built

| Area | Delivered | Location |
|---|---|---|
| Model catalog | 15 models (tiny … large-v3-turbo, English-only and quantized variants) with family, quantization, notes and approximate memory. Sizes and SHA-256 from Hugging Face metadata, cached for a week, with a per-file fallback (`x-linked-size` / `x-linked-etag` headers). **Never touches the network from the UI thread** | `stt/catalog.py` |
| Model store | Resumable download (HTTP Range, `.part` file), cancel, SHA-256 verification before install, verified-state cache keyed on size + nanosecond mtime, damaged-file detection (ggml header, size, checksum), import with hard link + checksum, remove. Interrupted connections become a "retry to resume" error | `stt/models.py` |
| Server manager | Executable discovery (env → bundled → PATH → Homebrew/`/usr/local`), free port, start → `/health` ok, logs to `whisper-server.log`, backend parsed from whisper.cpp's own decision line, pidfile with stale-server cleanup that only stops a process proven to be OmonFlow's, crash monitor with backoff and restart limit, `/load` switching with rollback (including the crash case), transcription client (bilingual prompt as in voice-client). **No `--vad`** | `stt/server.py` |
| Controller | Runs downloads, checks, imports, start/switch/stop on worker threads; signals to the UI; refuses damaged models; refreshes the catalog in the background at launch | `app/whisper_controller.py` |
| Settings → Speech Recognition | Engine status (state, model, backend reported by whisper.cpp, start-up time, executable + version, pid/port/restarts, errors) + Restart. Model table: download size, approximate memory, status, Download (with confirmation) / Cancel / Resume / Verify / Use / Remove / Download again; Import; Refresh sizes and checksums | `ui/settings/window.py` |
| App wiring | Engine starts at launch with the chosen model (default `small`), switches when you pick another, stops on quit | `app/main.py` |
| Tests | Fake whisper-server with the same CLI and HTTP API (slow loads, crashes, rejected models, crash-on-bad-model); local HTTP server with Range support for downloads | `tests/fake_whisper_server.py`, `test_whisper_server.py`, `test_models.py`, `test_whisper_hw.py` |
| Measurements | Real-binary lifecycle, import, switch, rollback, crash recovery, foreign-process safety, download | `tools/bench/whisper_m04.py` → `docs/m04/whisper_m04.json` |

## Acceptance criteria (PLAN.md M04)

| Criterion | Result | Evidence |
|---|---|---|
| Killing whisper-server is detected and it restarts within policy | **Pass** | Fake: crash, SIGKILL, restart limit tests. Real: SIGKILL → ready again in 1.5 s and 2.6 s (two runs); transcription works afterwards |
| No duplicate instances | **Pass** | Second `start` on a running manager is rejected; stale server from a crashed run is cleaned up; single-instance app guard (M02) |
| Only OmonFlow-owned PIDs are stopped | **Pass** | Fake: a foreign server and an unrelated process named in a forged pidfile both survive. Real: a separately started whisper-server survives OmonFlow start/stop |
| Interrupted download resumes or restarts cleanly | **Pass locally; real download not completed** | Local Range server: dropped connection → resumes from the exact byte; cancel → resume; read timeout → resumable error; checksum mismatch → partial removed. Real Hugging Face download: the link ran at ~1 KB/s today; 16.8 MB of `tiny` was kept as `.part`, the full download was not finished |
| Corrupted model detected | **Pass** | Wrong header, wrong size, modified content (checksum) all detected; modified files lose "verified"; damaged models are refused before loading; real server crash on a bad model → previous model restored |
| Models stored in the user data dir | **Pass** | `~/Library/Application Support/OmonFlow/models` (platformdirs); never next to the app |
| Cold start and model-load times on the M4 Pro | **Pass** | Below |
| Evaluate ggml dynamic backends | **Done** | Homebrew ggml 0.27 loads `libggml-cpu-apple_m1/m2_m3/m4`, `libggml-metal` and `libggml-blas` as plug-ins at runtime. A single bundled whisper-server can therefore choose CPU variants and GPU backends on the user's machine, as planned for M09 |
| `/load` model switching | **Pass** | 142–180 ms on the real server |
| whisper.cpp VAD off | **Pass** | Never passed `--vad` |

## Measurements (M4 Pro, Metal, `jfk.wav` 11.0 s, warm runs)

| | small | large-v3-turbo-q5_0 |
|---|---|---|
| Server start → ready (model loaded), 3 runs | 214 / 225 / 319 ms | 318 / 321 / 327 ms |
| Transcription, median of 3 | 202 ms | 641 ms |
| Real-time factor | 0.018 (55× faster than real time) | 0.058 (17×) |
| Backend reported by whisper.cpp | Metal | Metal |
| Import of your existing file (hard link + SHA-256) | verified | verified |

| Other | Result |
|---|---|
| Model switch via `/load` | 142–180 ms |
| SIGKILL → ready again | 1.5–2.6 s |
| Bad model via `/load` | Server crashed; restarted on previous model; reported clearly |

## Defects found and fixed in M04

| Defect | Fix |
|---|---|
| Server start failed when the data folder did not exist yet (first run) | Create it before launching |
| Backend misreported as "BLAS" (library *loaded* vs backend *chosen*), and log lines read before the server flushed them | Parse only the decision line; re-read the log after start |
| A stalled connection mid-download raised an unhandled `TimeoutError` | Becomes a resumable error |
| A same-size file modified within one second still counted as verified | Verification record uses nanosecond mtime |
| Real whisper-server crashes on a damaged model during `/load`; rollback then hit a dead server | Refuse damaged models; wait for the automatic restart on the previous model |
| Model table showed stale buttons and a clipped Remove button; Remove offered for the model in use | Hide replaced cell widgets; taller rows; "in use" instead of Remove |
| Packaged app started from Finder could not find Homebrew's whisper-server (minimal PATH) | Also check `/opt/homebrew/bin` and `/usr/local/bin` until the server is bundled |

## Known limitations and open items

1. **whisper-server is not bundled yet.** Development uses Homebrew's (or `OMONFLOW_WHISPER_SERVER`); bundling the executable and its ggml plug-ins is M09. Without one, Settings explains that it's missing.
2. **A real model download was not completed** because of today's ~1 KB/s connection. Please try "Download…" for `tiny` on a normal connection.
3. **Your OmonFlow data folder has no models yet.** The measurements used a temporary folder. To use your existing files, open Settings → Speech Recognition → *Import a model file…* and pick `~/local-ai/whisper.cpp/models/ggml-small.bin` (hard-linked, no copy).
4. **The engine stays loaded while OmonFlow runs** (~0.9 GB for `small`, ~1.6 GB for `large-v3-turbo-q5_0`, approximate). That trades memory for instant transcription. An "unload when idle" option can come with the hardware settings in M06.
5. **Catalog names are partly unverified:** the Hugging Face listing timed out today. `small`, `large-v3-turbo-q5_0` and `tiny` were confirmed by per-file checks; other names follow whisper.cpp's naming and are verified on the first "Refresh sizes and checksums". A missing file would show as a download error, never a silent failure.
6. **Windows/Linux and other backends untested** (CUDA/Vulkan: M06/M08 with the validation kit).

## How to reproduce

```bash
uv run pytest -q                                                   # 145 tests (fake server, local downloads)
OMONFLOW_HW_TESTS=1 uv run pytest -q tests/test_whisper_hw.py      # real whisper-server + ggml-small.bin
uv run python tools/bench/whisper_m04.py --skip-download           # measurements → docs/m04/whisper_m04.json
```
