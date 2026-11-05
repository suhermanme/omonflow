# OmonFlow — Product Specification (v2.1, authoritative)

**Tagline:** Speak naturally. Type everywhere.

**Companion documents:**
- [ARCHITECTURE.md](ARCHITECTURE.md): how it's built.
- [PLAN.md](PLAN.md): milestones, acceptance criteria, decision log.
- [RISKS.md](RISKS.md): risk register.
- [M01-assessment.md](M01-assessment.md): repository assessment and measurements.

If a companion document conflicts with this file, this file wins.

---

## 0. Change log

### v2.1 (2026-11-05): owner decisions on v2

| # | Change | Section |
|---|---|---|
| D1 | macOS default shortcut candidate: **⌘⇧Space** (registered combination, no Input Monitoring). **⌘⌥** is an advanced, opt-in modifier-only shortcut that may need extra permission | §5.2 |
| D2 | Cancellation rules confirmed, including INSERTING: never lose the transcript; report the actual outcome | §5.3 |
| D3 | Microphone states split: **INITIALIZING → CAPTURE_READY → RECORDING → FINALIZING**. RECORDING means audio activity detected (UI only, not VAD) | §6, §8 |
| D4 | Small Python audio fallback behind the same interface; it is also the M03 comparison baseline | §2.2 |
| D5 | voice-client is the owner's own MIT project: reuse authorized with attribution; no runtime dependency | §3 |
| D6 | Python 3.13, subject to dependency and packaging validation on all targets | §2.1 |
| D7 | No paid certificates for development. macOS ad-hoc signing, Windows unsigned. Signing and notarization are release-readiness tasks | §18 |
| D8 | M08 split into **M08** Windows/Linux integration and **M09** packaging/release | §21 |

### v2 (2026-11-05): interaction change. These v1 requirements were removed

| # | Removed (v1) | Replaced by |
|---|---|---|
| C1 | Hold-to-talk push-to-talk as default activation | Shortcut shows/hides the widget; mic button starts; **Stop** ends |
| C2 | Alt+Space default; modifier-only chord for recording | Configurable shortcut, platform candidates validated (§5.2) |
| C3 | Keyboard controls recording boundaries | Explicit clicks (widget or tray) control boundaries |
| C4 | Mic initialized before activation; continuous capture | On-demand capture; mic inactive while idle |
| C5 | Mandatory 300–500 ms rolling pre-buffer | Startup buffer after stream start + readiness states; continuous pre-buffer deferred (§6.5) |
| C6 | Always-ready vs Privacy capture modes | Removed; on-demand is the only approved behavior |
| C7 | Rust low-latency push-to-talk hotkey | Rust focuses on stream start/stop; shortcut only toggles the widget |
| C8 | States without FINALIZING | Full state set in §8 |
| C9 | Settings: push-to-talk/toggle, pre-buffer duration | Settings per §14 |
| C10 | Push-to-talk and rolling-buffer test suites | Widget, lifecycle and privacy tests (§20) |
| C11 | Old milestone order | §21 |

---

## 1. Product

OmonFlow is a standalone, cross-platform, local-first **dictation-only** desktop utility.
- **Not a voice assistant:** no conversational AI, dialogue, TTS, spoken responses or assistant history.
- **LLMs only for optional refinement.** The core workflow works fully offline.

**Priorities:**
- Explicit, private microphone use.
- No audio lost once capture is ready.
- Accurate transcription.
- Reliable insertion.
- Low resource use.
- Genuine cross-platform behavior.
- Simple setup.

## 2. Technology architecture (mandatory)

### 2.1 Python 3.13 + PySide6: application layer

**Covers:**
- Lifecycle and orchestration.
- UI: widget, visualizers, tray, Settings, history.
- Configuration and notifications.
- whisper.cpp process management and HTTP.
- Models and hardware detection.
- Refinement and insertion adapters.
- Diagnostics and packaging orchestration.

**Rules:**
- The UI thread never blocks.
- Python 3.13 is the target. It must be validated (PySide6, PyInstaller, pyobjc, maturin/abi3) for macOS arm64 and x86_64, Windows x64 and Linux x64 before it is final; see PLAN M02.

### 2.2 Rust: native audio core

**On-demand microphone capture:**
- Device discovery and selection.
- Stream open, readiness reporting and activity level.
- Bounded recording buffers.
- Finalize: resample to 16 kHz mono and encode.
- Cancel and release.
- Device-loss, timeout and error reporting.
- Repeated start/stop reliability.
- Deterministic shutdown.

**Integration:** PyO3 extension module (`omonflow._core`). No blocking, filesystem, network or Python work in real-time callbacks. Bounded queues, no shared mutable buffers, minimal copies.

**Python fallback (approved):**
- A small `sounddevice`-based implementation of the **same interface**.
- Used when the Rust module can't load; shown as "degraded" in diagnostics.
- Also the **comparison baseline in M03**.
- Rust stays the primary implementation only if M03 shows a measurable benefit (PLAN M03 gate).

### 2.3 whisper.cpp

Local STT as a supervised child process (whisper-server, HTTP). No Whisper inference in Python.

### 2.4 Principle

Rust only where it measurably improves reliability or latency. Python where development speed and maintainability matter more.

## 3. Existing repository and reuse

- `suhermanme/voice-client` is the owner's own project, MIT License, © 2026 suhermanme (license verified in M01).
- **Reuse is authorized:** copy, adapt, refactor and improve. That includes the visualizers (Spectrum Pill, Orb, Circular Wave), audio and Whisper helpers, and config infrastructure.
- **Obligations:**
  - Keep copyright notices and attribution: per-file origin headers plus a `NOTICE` entry.
  - voice-client is not modified, restructured or deleted.
  - **No runtime or build dependency** on voice-client.
  - OmonFlow has its own Git repository and history, and builds, tests and ships independently.
  - Assess existing components before writing replacements.
- **Excluded:** assistant features (VAD endpointing loop, LLM conversation, prompts, TTS, conversation history).

## 4. Scope

Dictation only. Raw transcription never requires an LLM or cloud service.

## 5. Activation model

### 5.1 Primary workflow

1. Global shortcut → show/hide the floating Spectrum Pill.
2. Click **microphone** → INITIALIZING → CAPTURE_READY → RECORDING (§6). The user never holds a key.
3. Click **Stop** → FINALIZING → TRANSCRIBING → REFINING (optional) → INSERTING into the previously focused app → SUCCESS → IDLE. The mic is released at finalize.

**Rules:**
- The global shortcut **never** starts, stops or cancels capture.
- Showing or hiding the widget, opening or closing Settings, and launching OmonFlow never start or stop capture.
- The tray's *Start Dictation* equals the mic button and goes through the same readiness checks.
- No mandatory VAD; no silence-based auto-stop.

### 5.2 Global shortcut (show/hide widget)

- Configurable, persisted, and shown in Settings with live status: **registered / unavailable / permission denied / conflict / not supported by platform**.
- Registration failures and known conflicts are detected and explained. The user's shortcut is never silently substituted.
- **Defaults** (candidates until the M02 conflict survey confirms them):

  | Platform | Default | Mechanism | Permission |
  |---|---|---|---|
  | macOS | **⌘⇧Space** | Registered hot key | None |
  | macOS (advanced, opt-in) | ⌘⌥ tap (modifier-only) | Keyboard event monitoring | Input Monitoring (explained before enabling) |
  | Windows | Ctrl+Alt tap, or a registered combination (decided by M02 survey) | Low-level hook (tap: press and release with no other key; AltGr-safe) or `RegisterHotKey` | None |
  | Linux X11 | As Windows | XInput2 monitoring or `XGrabKey` | None |
  | Linux Wayland | User-bound via xdg-desktop-portal GlobalShortcuts, or a desktop-environment custom shortcut running `omonflow --toggle-widget` | Compositor-mediated | Portal consent |

- Arbitrary global key monitoring on Wayland is never promised.
- If no shortcut is available, the tray/menu bar and app UI remain fully usable.

### 5.3 Cancellation

| State | Cancel does |
|---|---|
| INITIALIZING, CAPTURE_READY, RECORDING | Close the stream; **discard** captured audio |
| FINALIZING, TRANSCRIBING | Abort processing where possible; **discard** audio and any result |
| REFINING | Stop refinement; **keep the raw transcript**. Not inserted; offered with Copy and saved to history if enabled |
| INSERTING (not yet started, e.g. restoring focus) | Abort insertion; **keep the transcript** with Copy |
| INSERTING (already started) | Not interruptible. Finish, then report the **actual** outcome (inserted / partial / failed); the transcript is always kept |

- A cancel shortcut may exist, but it is registered **only while a session is active**.

### 5.4 Deferred advanced modes (not approved)

Push-to-talk, toggle-by-shortcut and continuous pre-buffer capture each need a justification and separate approval.

## 6. Microphone capture (on-demand)

### 6.1 States and readiness

| State | Meaning | Entered when |
|---|---|---|
| INITIALIZING | Device/stream opening | Mic button or tray Start |
| CAPTURE_READY | Stream delivers real signal; capture is running and buffered; UI invites speech | First audio block that is not exact digital silence (device warm-up over), or the warm-up cap is reached (then with a "mic delivering silence, muted?" warning) |
| RECORDING | Audio activity detected | Input level rises clearly above the measured noise floor for a short period |
| FINALIZING | User pressed Stop; audio being finalized | Stop |

**Rules:**
- **Capture starts at the first audio callback and nothing after that is ever dropped.** That covers warm-up, CAPTURE_READY and RECORDING.
- Activity detection drives **UI state only**. It never starts, stops or trims a recording, and is not VAD.
- RECORDING is sticky until Stop; it doesn't flicker back to CAPTURE_READY during pauses.
- Stop is allowed from CAPTURE_READY. If no speech is recognized, the user gets "No speech recognized" and nothing is inserted.

### 6.2 Initialization timeout and recovery

- **Timeout:** default ~3 s, configurable. On timeout: ERROR "Microphone not responding", with **Retry**, **Choose another microphone** and **Cancel**.
- **Stuck opens:** a hung device-open call is abandoned to a retired worker and never blocks the next attempt or shutdown.
- **Repeated failures:** two consecutive timeouts on the same device trigger a suggestion to switch devices.
- **Logging:** per-device startup statistics (open time, ready time, warm-up length, failures) are recorded and shown in Audio Input diagnostics.

### 6.3 First-word protection

Audio before the stream runs can't be recovered. The UI must not invite speech before CAPTURE_READY. The startup buffer is never described as equivalent to a pre-activation buffer.

### 6.4 Correctness

- Order kept; no duplicates or gaps from the first callback onward.
- Bounded memory (maximum session length).
- Device loss returns the partial audio with a warning.
- Safe shutdown.
- **No stream left open after a session or while idle.**

### 6.5 Continuous pre-buffer (deferred)

The M01 PoC implementation is reference code only, not part of the product.

## 7. Audio engine features

- Device discovery, information and selection.
- Level metering and noise-floor estimate for UI state.
- Native rate/channel handling; resample to 16 kHz at finalize.
- Lifecycle of §6.
- Disconnect/reconnect handling.
- Permission-denied detection.
- Graceful shutdown.
- Lightweight real-time callbacks.

## 8. Application state machine

States: **IDLE, INITIALIZING, CAPTURE_READY, RECORDING, FINALIZING, TRANSCRIBING, REFINING, INSERTING, SUCCESS, ERROR.**

| From | Event | To | Notes |
|---|---|---|---|
| IDLE | Start (mic button / tray) | INITIALIZING | Rejected if a session exists (single session; enforced in the orchestrator and the audio core) |
| INITIALIZING | Real signal / warm-up cap | CAPTURE_READY | Cap → warning shown |
| INITIALIZING | Permission denied / no device / timeout | ERROR | Stream closed; recovery actions §6.2 |
| INITIALIZING / CAPTURE_READY / RECORDING | Cancel | IDLE | Audio discarded |
| CAPTURE_READY | Activity detected | RECORDING | UI only |
| CAPTURE_READY / RECORDING | Stop | FINALIZING | |
| CAPTURE_READY / RECORDING | Device lost or max length | FINALIZING | Partial audio kept, warning |
| FINALIZING | Audio ready | TRANSCRIBING | Stream released |
| FINALIZING / TRANSCRIBING | Cancel | IDLE | Audio and result discarded |
| TRANSCRIBING | Text | REFINING or INSERTING | |
| TRANSCRIBING | Empty or no speech | IDLE | Notice; nothing inserted |
| TRANSCRIBING | Error / timeout | ERROR | Audio kept in memory for **Retry** until dismissed |
| REFINING | Error / timeout | INSERTING | Raw/clean text used; user notified |
| REFINING | Cancel | ERROR ("Not inserted") | Raw transcript kept, Copy |
| INSERTING | Cancel before insertion starts | ERROR ("Not inserted") | Transcript kept, Copy |
| INSERTING | OK | SUCCESS | |
| INSERTING | Fail / focus lost / partial | ERROR | Transcript kept, Copy, accurate outcome; saved to history if enabled |
| SUCCESS | ~1.5 s | IDLE | |
| ERROR | Dismiss | IDLE | |

**Widget visibility is not a state:**
- Hiding never stops or cancels; showing never starts.
- While the widget is hidden, the tray icon mirrors the state.
- Settings has no effect on the session.
- **Quit** during a session asks: finish and insert / discard / keep running.

## 9. Hardware detection and inference settings

- **Detect:** OS, CPU (vendor, model, architecture, cores/threads, instruction sets), RAM, GPU (model, memory, driver), runtimes and compatible whisper.cpp backends, through reliable APIs.
- **Backends:** offer Automatic, CPU, Metal, CUDA, Vulkan and others only when supported. Distinguish **detected / available / validated / active**; never claim GPU acceleration from detection alone.
- **Settings:** CPU threads, GPU device, profiles Fast / Balanced / Accurate (never overwrite manual choices).
- **Benchmark:** optional, reporting RTF and failures.
- **Config changes:** validated first, rolled back if they fail. Runtime diagnostics.
- **Platform notes:** Apple Silicon (Metal + CPU fallback, unified memory); Intel macOS (Metal not assumed); NVIDIA (CUDA/Vulkan validated); AMD (Vulkan only when it validates).

## 10. Text refinement

- **Modes:** Raw, Clean, AI. Independent of STT.
- **Providers:** OpenAI-compatible (Ollama, llama.cpp server, others). Settings: base URL, API key (OS keychain), model, timeout, parameters, instructions, languages, connection test.
- **Must preserve:** intent, technical terms, product and model names, commands, identifiers, paths, URLs, numbers, measurements and Indonesian–English mixing. No invention; no translation unless asked.
- **Failure:** fall back to raw/clean, notify, never discard.
- **Remote endpoints:** explicit configuration only, with disclosure.

## 11. Text insertion

Insert into the application focused **before the mic click**. Platform adapters, layered:

1. Accessibility / app-supported insertion where reliable.
2. Native simulated keyboard input where appropriate.
3. Clipboard + paste, with the clipboard preserved where practical.
4. On failure: keep the transcript and offer **Copy**.

**Requirements:**
- Track the last focused non-OmonFlow app/window continuously; freeze it as the target at the mic click.
- The widget must not take focus (non-activating), or the target is restored before insertion.
- Detect focus loss, permission denial, macOS Secure Input and Windows UIPI.
- Handle Unicode, multiline, Indonesian/English and long text.
- **Wayland:** clipboard mode ("press Ctrl+V") where focus can't be controlled; documented.
- Compatibility is per application and documented; never claimed as universal.

## 12. Floating widget and visualizers

- **Spectrum Pill is the default** on every fresh install. Orb and Circular Wave are selectable and switch immediately.
- The selection persists. A failing visualizer falls back safely with an error and **does not overwrite the saved preference**.
- **Controls by state:**
  - IDLE: mic button (+ settings access).
  - INITIALIZING: progress + Cancel.
  - CAPTURE_READY: "speak now" cue + level + Stop/Cancel.
  - RECORDING: live animation + recording indicator + Stop/Cancel.
  - Processing: progress, Cancel where §5.3 allows.
  - SUCCESS: confirmation.
  - ERROR: message + Copy/Retry/device actions.
- Compact, draggable, optional always-on-top, independently hideable, non-activating.
- Accessible: names, tooltips, contrast.
- Idle animation throttled.

## 13. System tray / menu bar

- **Actions:** Show/Hide widget, Settings, Start Dictation, Stop Dictation, Cancel, History, Diagnostics, Quit. The tray shows the state.
- **Platform integration:**
  - macOS: menu bar extra, `.app`, login item, permissions.
  - Windows: notification-area icon, taskbar identity, startup.
  - Linux: desktop entry, icon, tray where available (GNOME needs AppIndicator).
- **Lifecycle:**
  - Closing Settings or hiding the widget never quits; explicit Quit.
  - Single instance; relaunching activates the running instance.
  - Clean shutdown of OmonFlow-owned processes.

## 14. Settings (every control must change behavior)

1. **General:** show/hide shortcut + status, start with OS, start minimized, widget visibility, notifications, close behavior.
2. **Appearance:** visualizer (Spectrum Pill default / Orb / Circular Wave), live preview, position, always-on-top.
3. **Dictation:**
   - Activation: widget button (default).
   - Advanced: ⌘⌥ modifier-only shortcut (macOS, opt-in, with a permission explanation).
   - Start/Stop behavior, cancel shortcut, default transcription mode.
4. **Audio Input:** device, info, mic test, level, capture status, startup diagnostics, initialization timeout.
5. **Speech Recognition:** models, download/progress/remove, status, size, memory estimate.
6. **Hardware & Acceleration:** §9.
7. **Text Refinement:** §10.
8. **Text Insertion:** strategy, clipboard fallback and preservation, permission status, diagnostics, limitations.
9. **Privacy & History:** mic behavior explanation, audio retention, history controls, remote-refinement disclosure.
10. **Diagnostics:** version, OS/CPU/GPU, whisper status, model and backend, durations, RTF, audio backend (Rust / fallback), errors, logs.
11. **About:** version, licenses and attribution, project info, updates where implemented.

Restart-requiring settings are labelled.

## 15. Privacy

- No capture while idle; no background recording. Explicit activation only; capture ends at finalize.
- OS mic indicators are never hidden or bypassed. Permission ≠ activity, and the UI shows both truthfully.
- No uploads, no mandatory cloud, no telemetry, no automatic transmission.
- No raw audio retention by default. Audio and transcript content are not logged by default.
- History is optional: search, copy, delete one, clear all.
- Remote LLM use is disclosed.

## 16. Cross-platform architecture

**Targets:**
- macOS Intel and Apple Silicon.
- Windows 10/11.
- Linux X11, and Wayland within compositor limits.

Shared core plus platform adapters. Layout in ARCHITECTURE.md.

## 17. Hardware validation targets

| Machine | Validate |
|---|---|
| i9-9900K + AMD RX580 | CPU and instruction sets (AVX2, no AVX-512), Vulkan where it validates, audio, resources |
| MacBook Pro M4 Pro, 24 GB | Metal, CPU fallback, memory, audio, packaging, permissions |
| Ubuntu + RTX 3060 12 GB | CUDA, Vulkan, CPU, model loading, VRAM, speed |

Repeatable kit: `tools/validation/`. No claims without recorded runs on the real machine. Comparable settings across machines.

## 18. Packaging, signing and distribution

- **End users install nothing else:** no Python, uv, Git, CMake, compilers, Homebrew or Rust.
- **Bundled:** runtimes and whisper executables. **Not bundled:** models (downloaded) and vendor drivers.
- **Outputs:** macOS `.app` + DMG; Windows installer or standalone package; Linux AppImage (or equivalent).

**Development signing (no paid certificates):**

- **macOS:**
  - Ad-hoc code signing.
  - A stable bundle ID.
  - A consistent build location and signing method.
  - Permissions tested with the packaged `.app`.
  - Documented that ad-hoc signing gives no Developer ID trust or notarization.
  - **Permission resets:** macOS ties permissions to the code's identity. With ad-hoc signing that identity is the code hash, so **every rebuild can trigger new Microphone/Accessibility prompts.** The same applies when the bundle ID, signing identity or location changes.
  - Builds copied to other Macs are quarantined by Gatekeeper.
- **Windows:** unsigned builds and installers. SmartScreen "Windows protected your PC" and other security prompts are documented.

**Release:**
- Signing (Developer ID + notarization, Windows Authenticode) and certificate purchase are separate **release-readiness tasks** (M09).
- The packaging scripts keep a signing step that can be switched to a real identity later.

**Validation:** clean-machine tests of resources, Rust extension, whisper executable, model directory, permissions, audio, backends and shutdown.

## 19. Reliability (explicitly handled)

- **Microphone:** permission denied, missing, disconnected, initialization failure, **initialization timeout / hung device**.
- **Shortcut:** registration failure or conflict.
- **Audio core:** Rust extension missing or incompatible (→ Python fallback); Rust/Python interface errors.
- **Whisper:** executable missing, startup failure or crash; corrupt or missing model.
- **Resources:** insufficient memory, unsupported backend, GPU initialization failure.
- **Timeouts:** transcription timeout, LLM unavailable or timeout.
- **Insertion:** insertion failure or partial insertion, focus loss.
- **Lifecycle:** quit during a session, configuration corruption, interrupted downloads, visualizer failure.

Never silently discard audio or transcripts. Structured logs without content by default.

## 20. Testing

Acceptance criteria per milestone are in PLAN.md.

- **Widget and activation:**
  - The shortcut and the tray toggle visibility only.
  - Showing never opens the mic; hiding never stops a session.
  - Mic → INITIALIZING → CAPTURE_READY → RECORDING. RECORDING never shown before real signal and activity.
  - Stop finalizes; cancel follows §5.3 in every state.
  - No duplicate sessions.
  - All states render in all three visualizers. Spectrum Pill is the fresh-install default; selection persists and switches live; a failing visualizer falls back without changing the saved choice.
  - Animation never blocks UI or audio.
- **Audio:**
  - No stream while idle.
  - Initialization, warm-up handling and readiness.
  - Timeout and hung-device recovery.
  - Hundreds of repeated cycles.
  - Integrity from the first callback.
  - Bounded memory, disconnect, permission denial, finalize, shutdown.
  - No stream left open.
  - Latency metrics per §6.2.
  - **Rust vs Python fallback comparison** (M03).
- **Privacy:** OS-level checks that the mic is idle when expected (macOS device "running somewhere", Linux `pactl` source outputs, Windows capability usage); no uploads; no retention.
- **Whisper, hardware, refinement, insertion, platform, packaging:** as described in §§9–13 and 18. Insertion is tested across browsers, editors, office apps, terminals, remote desktop clients and custom controls.
- **Rules:** CI mocks hardware. Real-hardware results are recorded per machine. Platform support is never claimed from unit tests on one OS.

## 21. Milestones

Each milestone delivers scope, implementation notes, tests, results, limitations, remaining work and updated docs. **No milestone starts without explicit approval.** Details and acceptance criteria: PLAN.md.

| ID | Milestone |
|---|---|
| M01 | Repository assessment and architecture |
| M02 | Application foundation |
| M03 | On-demand audio core (Rust + Python baseline, comparison gate) |
| M04 | Whisper process manager and models |
| M05 | Dictation MVP (macOS) |
| M06 | Hardware and inference settings |
| M07 | Text refinement |
| M08 | Windows and Linux integration |
| M09 | Reliability, packaging and release readiness |

## 22. Non-negotiables

- Dictation-only. Python/PySide6, Rust audio core (justified by measurement), whisper.cpp.
- Spectrum Pill default; Orb and Circular Wave selectable.
- The shortcut shows/hides the widget and never records.
- Explicit mic activation; Stop finalizes. No mandatory VAD or continuous capture.
- No hidden recording or uploads.
- Transcripts survive insertion failures.
- No promises of unsupported OS capabilities.
- voice-client preserved; no runtime dependency on it.
- Incremental, tested, approved milestones; nothing claimed as working before it is tested.
