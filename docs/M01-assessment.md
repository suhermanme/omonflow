# OmonFlow — M01: Repository Assessment and Architecture Decision (rev. 3)

Date: 2026-11-05 · Status: **deliverables complete; awaiting formal M01 sign-off and M02 approval**
Spec: [`SPEC.md`](SPEC.md) v2 (widget-driven, on-demand capture). Its §0 lists every v1 requirement that was replaced.
Assessed: `suhermanme/voice-client` @ `6f509d1`, read-only clone. The original repository was not modified.
Test machine: MacBook Pro M4 Pro, 24 GB, macOS 27.2. Mics: built-in "MacBook Pro Microphone" and USB "iTalenanMax Microphone".

> **Rev. 3:** the owner's decisions are recorded (§13). The authoritative requirements, design, plan and risks now live in [SPEC.md](SPEC.md) v2.1, [ARCHITECTURE.md](ARCHITECTURE.md), [PLAN.md](PLAN.md) and [RISKS.md](RISKS.md). Sections 3–12 below are the rev. 2 analysis that led to them. Where they differ, the companion documents win, e.g. the added CAPTURE_READY state, the ⌘⇧Space default, cancel-during-insertion and ad-hoc signing.
>
> **Rev. 2:** the activation model changed from global push-to-talk with continuous capture to: shortcut shows the widget → click mic → speak → click Stop. Push-to-talk, modifier-chord recording, the rolling pre-buffer and the Always-ready/Privacy modes have been removed from this report and from the spec. Section 2 (the voice-client assessment) is unchanged because it doesn't depend on the activation model.

---

## 1. Summary

- **The hybrid architecture still holds, but Rust has a narrower job.**
  - Rust's main value is now fast, reliable **on-demand** stream start and stop, plus keeping the real-time audio callback away from the Python GIL and Qt painting.
  - The global shortcut only shows/hides the widget, so it's no longer latency-critical.
  - With no rolling buffer to protect, Rust's measurable advantage over a Python `sounddevice` baseline is smaller. M03 should measure it rather than assume it (§9, R7).
- **Measured on-demand lifecycle (M4 Pro, §11):**

  | | Built-in mic | USB mic |
  |---|---|---|
  | Click → ready (first audio), median / max | ~100 ms / 103 ms | ~260 ms / 2.8 s |
  | Device warm-up after the first callback | Up to 130 ms of exact digital silence in some cycles | None |
  | Stop → finalized audio | ~0.1 ms | ~0.1 ms |
  | Device released after | ~15 ms | ~31 ms |

  In one earlier run, opening the USB mic **hung for more than 10 s**.

  So "ready" must mean *real signal is arriving*, not "the stream opened" (§4).
- **The biggest new risk is focus.**
  - Clicking a normal window activates its app, so clicking the mic button would take focus away from the app the text is meant for.
  - The widget must be a *non-activating* panel. Otherwise OmonFlow must track the last-focused app and restore it before inserting.
  - This needs a spike in **M02**, because the whole interaction depends on it.
  - On **Wayland** neither approach is generally possible. Insertion there will likely be clipboard + "press Ctrl+V" (§6).
- **Reuse from voice-client is unchanged:**
  - Visualizers, WAV and HTTP helpers, device selection and config patterns are copied with attribution.
  - The assistant pipeline is excluded.
  - The visualizers now also need clickable mic/Stop/Cancel controls.
- **The continuous pre-buffer** built and verified in the first PoC is kept only as reference code. It is not part of the approved product (SPEC §6.5).

---

## 2. Existing repository assessment

### 2.1 Architecture

| File | Lines | Role |
|---|---:|---|
| `voice_assistant.py` | 2,350 | Everything else: device helpers, Silero VAD, Whisper HTTP, LLM streaming, Supertonic TTS, conversation history, worker loop, `main()` |
| `voice_client/config.py` | 202 | Constants, argparse + env-var config, frozen `AppConfig` dataclass |
| `voice_client/ui.py` | 346 | `AssistantSignals` (Qt signals) and the `Orb` widget, which renders all three styles |
| `tests/test_voice_assistant.py` | 523 | 17 unittest tests with mocked audio/HTTP |

**Runtime flow:**
1. `main()` resolves devices, builds `QApplication`, and shows the widget.
2. It starts a daemon worker thread running `VoiceAssistant._run()`.
3. The worker validates services, then opens a `sounddevice.InputStream` (16 kHz mono, 512-sample blocks).
4. It runs Silero VAD per chunk, posts each utterance as WAV to whisper-server, streams the LLM, and speaks via TTS.
5. UI state crosses threads through Qt signals.

There is no settings file, tray, hotkey, history persistence, or packaging.

### 2.2 Audio capture (`sounddevice` / PortAudio)

- **Callback:** copies channel 0, computes RMS level, emits a Qt signal *from the audio callback*, and puts the chunk into a bounded `queue.Queue` of about 2 s, dropping the oldest on overflow.
- **Capture is gated by `capture_enabled`**, which is cleared during STT/LLM/TTS. The mic stream stays open but audio is discarded. That's right for an assistant (no self-hearing) but wrong for dictation.
- **Pre-roll:** 250 ms (rounded to 256 ms), kept only while the VAD waits for speech. It is a VAD feature, not a key-triggered rolling buffer.
- **Device selection is good and reusable:** match by name, partial name, or index; check direction; report ambiguous matches; fall back to a preferred name, then the system default. It has a hard-coded preference for "MacBook Pro Microphone".
- **Problems for OmonFlow:**
  - The audio callback does Python work: numpy maths and a Qt signal emit. That's fine at 32 ms blocks but not "lightweight".
  - No sample-continuity guarantees.
  - No device-change handling.

### 2.3 Whisper integration

- **HTTP client:** `POST /inference` (multipart WAV, `language=auto`, a bilingual technical `prompt`, `carry_initial_prompt=true`, JSON response) and `GET /health` expecting `{"status":"ok"}`. It uses one shared `httpx.Client` with cancellation checks before and after the request.
- **No process management.** The user builds and starts whisper-server by hand. The README documents pinned commit `60c0be6a`, Metal/CUDA/Vulkan/CPU build commands, and SHA-256 hashes for `ggml-small.bin`. The hashes are reusable as model-manager seed data.
- **whisper-server is launched with `--vad`** (Silero, 120 ms speech pad). For explicit Start/Stop dictation this must be off. Whisper-side VAD can drop low-energy word onsets that were captured correctly.
- **Reusable:** `wav_bytes`, `transcribe`, `sibling_url`, the health-check pattern, and the bilingual initial prompt.

### 2.4 Configuration and storage

- **How config works:** module constants, then env vars (`VOICE_*`), then CLI flags, ending in an immutable `AppConfig`. Nothing is persisted. Conversation history is in memory only.
- **Privacy issue not to carry over:** the default LLM URL is a LAN IP (`192.168.3.243`), so transcripts go to another machine by default. OmonFlow must start with LLM refinement **off** and no endpoint configured.
- **Reusable:** the validation helpers (`positive_int`, `probability`, …) and the dataclass approach. The CLI/env layering becomes a dev/debug override only.

### 2.5 Visualizers (especially Spectrum Pill)

All three live in one `Orb(QWidget)` class:
- The style string is fixed at construction.
- `paintEvent` branches on `self.style`.
- Window size is set once: 280×72 for the pill, 220×220 for the others.
- Selection comes from `--ui-style` / `VOICE_UI_STYLE`. The current default is `orb`; OmonFlow changes it to `spectrum-pill`.

**Spectrum Pill (`_paint_spectrum_pill`):**
- A rounded dark pill with a 1.5 px state-coloured border.
- A 16 px state dot.
- 15 rounded bars, 5 px wide with 4 px gaps, under a sine envelope with per-bar phase motion.
- A state label in a roughly 74 px text area at x=190.
- In `HEARING` the bars follow the live mic level. Other states use fixed levels. `ERROR` freezes motion.

**Findings that matter for OmonFlow:**
1. **No runtime switching.** The spec needs immediate switching, so style must become a swappable painter, and window size must change with it.
2. **Steals focus.**
   - Flags are `Qt.Window | FramelessWindowHint | WindowStaysOnTopHint`.
   - `main()` calls `raise_()` and `activateWindow()`.
   - Needed instead: `Qt.Tool`, `WindowDoesNotAcceptFocus`, `WA_ShowWithoutActivating`, and on macOS running as an accessory app (no Dock icon) so showing the widget doesn't activate OmonFlow.
3. **Always redraws at about 60 fps,** even in IDLE, through a 16 ms `QTimer`. Circular Wave also builds 3×241-point paths in Python every frame. Animation should drop to a low rate or stop when idle.
4. **The state model is assistant-specific** (IDLE/LISTENING/HEARING/THINKING/SPEAKING/ERROR). It needs remapping to IDLE/INITIALIZING/RECORDING/TRANSCRIBING/REFINING/INSERTING/SUCCESS/ERROR. RECORDING takes over HEARING's live-level behaviour.
5. **Labels won't fit.** "Transcribing" may not fit the roughly 74 px label area at default font sizes. Layout needs a small adjustment, keeping the visual design.
6. **Closing the widget quits the app** (`closeEvent` sets stop and emits shutdown). In OmonFlow, hiding the widget must not quit; the tray owns lifecycle.
7. **Dragging** uses native `startSystemMove()` with a manual fallback. Good, keep it; it works on Wayland.

### 2.6 Assistant-only components to exclude

- Silero ONNX VAD (`SileroVAD`, onnxruntime).
- `ConversationHistory`.
- `stream_llm` and SSE chat streaming *as a conversation*. Its HTTP/SSE handling may inform the refinement client, but refinement should use a non-streaming single request.
- `SYSTEM_PROMPT` and the `<id>/<en>` tag parsing (`parse_language_spans`, `StreamingLanguageParser`, `normalize_tts_lang`, `clean_response_for_display`).
- `LocalTTS` / Supertonic.
- Speaker output selection.
- The capture-gating-during-playback logic.

### 2.7 Platform-specific functionality

There is very little:
- macOS-oriented defaults (preferred device names, Metal docs).
- The Wayland-friendly drag.
- README notes for Linux packages.

No Windows code or testing, no tray, hotkeys, insertion, or permissions handling.

### 2.8 Tests and technical debt

- **Tests:** 17 behaviour tests covering duration rounding, history recovery, audio queue overflow, device selection, HTTP client reuse, startup validation, SSE parsing, TTS worker states and language parsing. They mock audio and HTTP well.
  - Relevant to OmonFlow: device selection, audio-queue bounds and Whisper client tests, adapted.
  - **I did not run them.** The tool sandbox blocked running code from the downloaded repository.
- **Technical debt:**
  - A 2,350-line monolith with heavy top-level imports, so you can't import one helper without onnxruntime and Supertonic.
  - A one-argument-per-line formatting style that inflates line counts.
  - Signal emission from the audio callback.
  - Hard-coded device and LAN defaults.
  - A daemon worker thread with a 5 s join.
  - No persisted settings.
  - No packaging.

### 2.9 Dependencies and licensing

| Component | License | OmonFlow use | Notes |
|---|---|---|---|
| voice-client | MIT, © 2026 suhermanme | Copy + adapt visualizers and helpers | Same owner. Keep the copyright notice in OmonFlow's LICENSE/NOTICE |
| PySide6 / Qt | LGPLv3 (commercial alternative) | UI | Bundle as separate shared libraries (PyInstaller does), ship license texts and a source offer, keep libraries replaceable |
| whisper.cpp / ggml | MIT | Bundled executables | Ship the license text |
| Whisper model weights | MIT (OpenAI) | User-downloaded | Not bundled |
| cpal | Apache-2.0 | Rust audio | Ship notice |
| pyo3, rtrb | MIT / Apache-2.0 | Rust ↔ Python, ring buffer | |
| httpx, numpy | BSD-3 | HTTP, audio arrays | Already used |
| CUDA runtime / cuBLAS | NVIDIA EULA (redistributable subset) | Optional CUDA build | Large (hundreds of MB). Decide in M05/M08: bundle, separate download, or Vulkan-only on Linux |
| onnxruntime, supertonic, sounddevice | — | **Dropped** | sounddevice is kept only for the optional Python fallback, if approved (§13, decision 4) |

### 2.10 Packaging

None. The app is run with `uv run python voice_assistant.py`, and whisper-server is built from source by the user.

---

---

## 3. Updated interaction model

```text
[global shortcut] ──► show/hide widget            (never touches the microphone)
[mic button] or tray "Start Dictation"
      └─► INITIALIZING ─(real signal arriving)─► RECORDING ─[Stop]─► FINALIZING ─► TRANSCRIBING
                                                         ─► (REFINING) ─► INSERTING ─► SUCCESS ─► IDLE
```

- **Three ways to act, all explicit:**
  - Widget buttons (primary).
  - Tray menu items (full alternative).
  - A *session-scoped* cancel shortcut, registered only while a session is active.
- **The global shortcut is a visibility toggle.** Pressing it during RECORDING just hides or shows the widget; recording continues.
- **The target app is recorded *before* the mic click.** OmonFlow continuously tracks the last focused app/window that isn't OmonFlow (macOS `NSWorkspace` activation notifications, Windows `SetWinEventHook(EVENT_SYSTEM_FOREGROUND)`, X11 `_NET_ACTIVE_WINDOW`). When the mic is clicked, that value is frozen as the insertion target.
- **The widget needs new controls.** voice-client's widget is paint-only (§2.5). OmonFlow adds a mic button (IDLE), Stop + Cancel (RECORDING), Cancel (INITIALIZING/processing), and Copy/Retry (ERROR).
  - The Spectrum Pill's 280×72 layout has room for a 32–36 px button on the left, in place of the state dot, and one on the right, in place of part of the label area.
  - The bars and the look are kept.
  - The other two visualizers get the same control row as an overlay.
- **Cancel semantics** are defined in SPEC §5.3: discard audio during capture or transcription; keep the raw transcript if cancelled during refinement; no cancel during insertion.

## 4. Updated audio capture lifecycle

```text
idle (no stream) ─start_capture()─► opening ─first callback─► warming ─first non-zero block─► ready/recording
      ▲                                │ timeout/error           │ cap reached (e.g. 300 ms)
      │                                ▼                         ▼
      └── release ◄── finalize ◄── stop_capture()          ready + "mic may be muted" warning
```

| Phase | Rule | Basis |
|---|---|---|
| Open | Runs on the engine thread. Python gets a non-blocking call and a timeout (default ~3 s; slow USB/Bluetooth devices need it) | USB median ~250 ms, max 2.75 s, one >10 s hang |
| Warm-up | Samples are recorded from the first callback, but the UI says RECORDING only after the first block that isn't exact digital zeros, or after a cap | The built-in mic sometimes delivers up to 130 ms of exact zeros first |
| Startup buffer | Everything from the first callback onwards is kept in the session, including warm-up. It is trimmed of leading exact-zero samples at finalize. **Not** presented as pre-activation capture | SPEC §6.3 |
| Recording | Bounded session buffer (max length configurable, e.g. 10 min); level via atomic peak | |
| Stop | Mark the end, close the stream immediately, then resample to 16 kHz + encode WAV off the callback | Stop → audio ~0.1 ms at 48 kHz without resampling; release 15–31 ms |
| Hang | If open doesn't finish before the timeout, report "device not responding", leave INITIALIZING, and **retire** that engine worker (a stuck CoreAudio call can't be cancelled). The next start uses a fresh worker; the stuck one is cleaned up if it ever returns | The observed >10 s USB hang |
| Device loss mid-recording | Finalize with partial audio and warn | cpal `DeviceNotAvailable` |
| Idle | No stream object exists. The OS mic indicator is off | Privacy rule, verified by test (§8) |

**M03 investigation (not assumed):** can a stream be *prepared* (device and format resolved, audio unit initialized) without being *started*, to cut click-to-ready time?
- Only acceptable if the device is provably not capturing: the OS indicator stays off and macOS reports the device as not "running somewhere".
- Otherwise drop the idea.
- The open step currently takes ~90 of the ~100 ms on the built-in mic.

## 5. Revised Rust/Python interface

Python module `omonflow._core` (PyO3, abi3). All blocking calls release the GIL. Rust never calls into Python.

```python
class AudioCore:
    def __init__(self, *, max_session_s: int = 600, ready_timeout_ms: int = 3000,
                 warmup_cap_ms: int = 300): ...          # initialize(); no device opened
    @staticmethod
    def enumerate_devices() -> list[dict]: ...            # id (cpal DeviceId), name, default, rate, channels
    def select_device(self, device_id: str | None) -> None: ...   # None = system default; idle only
    def start_capture(self) -> None: ...                  # non-blocking; opens stream on engine thread
    def wait_until_ready(self, timeout_ms: int) -> dict: ...  # {open_ms, ready_ms, warmup_ms} or raises
    def get_input_level(self) -> float: ...               # peak since last call, 0..1
    def stop_capture(self) -> None: ...                   # marks end + closes stream; fast
    def finalize_audio(self) -> dict: ...                 # {wav: bytes (16 kHz mono s16), duration_s,
                                                          #  trimmed_ms, device_lost, overflowed}
    def cancel_capture(self) -> None: ...                 # close stream, drop samples
    def state(self) -> str: ...                           # idle|opening|warming|recording|stopping
    def poll_events(self) -> list[dict]: ...              # ready, device_lost, xrun, error, open_timeout
    def shutdown(self) -> None: ...                       # idempotent; closes any stream
```

**Errors** are raised as typed exceptions:
- `PermissionDenied`
- `DeviceNotFound`
- `DeviceTimeout`
- `SessionActive`
- `EngineStopped`

**Threads:**
- The cpal callback pushes into an SPSC ring (no locks, allocation or I/O), the same pattern as the PoC.
- One engine worker owns the stream and drains the ring.
- A Python orchestrator worker calls `start_capture` / `wait_until_ready` / `finalize_audio`.
- A small Python pump thread polls `get_input_level` (~30 Hz) and `poll_events`, and emits Qt signals.

**Copies:**
- Callback → ring → session `Vec` (needed for the lock-free handoff).
- At finalize: resample + encode into one WAV `Vec<u8>` → one copy into Python `bytes` → posted by httpx.

**Optional Python fallback** (decision §13): a `sounddevice` class with the same interface, labelled "degraded" in diagnostics.

## 6. Platform-specific constraints

### 6.1 Clicking the widget without stealing focus (spike in M02)

| Platform | Preferred | Fallback | Notes |
|---|---|---|---|
| macOS | App runs as accessory (`LSUIElement`); widget is a Qt `Tool` window (an NSPanel) whose style mask gets `NonactivatingPanel` via pyobjc, plus first-mouse click handling | Track the last frontmost app; re-activate it before insertion (macOS restores its key window and caret in most apps) | Must be verified with Qt 6.11 / PySide6. Qt may reset the style mask, and widgets inside a non-key panel must still receive clicks |
| Windows | `WS_EX_NOACTIVATE` (Qt `WindowDoesNotAcceptFocus`, or set via ctypes) | `SetForegroundWindow(target)` before insertion. Allowed because OmonFlow received the last user input | Elevated targets: insertion is blocked by UIPI. Document it |
| Linux X11 | `WM_HINTS.input = False`, utility/notification window type | `_NET_ACTIVE_WINDOW` client message to restore the target | Depends on the window manager |
| Linux Wayland | The compositor decides focus; clicking usually focuses the widget, and apps can't re-focus another app | **Clipboard mode:** put the text on the clipboard and notify "Press Ctrl+V". Layer-shell surfaces that never take keyboard focus exist on KDE/wlroots but not GNOME, and aren't exposed by PySide6 | Documented limitation, not hidden |

### 6.2 Global shortcut (show/hide widget)

The candidates ⌘+⌥ / Ctrl+Alt are **modifier-only**. OS hotkey *registration* APIs can't express them, so they need keyboard *monitoring*:

| Platform | Registered key combo (e.g. modifiers + letter) | Modifier-only tap (⌘⌥ / Ctrl+Alt) |
|---|---|---|
| macOS | Carbon `RegisterEventHotKey`: **no permission**; fails if already taken by another app using the same API | Listen-only `CGEventTap`: needs **Input Monitoring** permission just to show a widget |
| Windows | `RegisterHotKey`: no permission; **reports conflicts** | `WH_KEYBOARD_LL` hook: no permission. Must use *tap* semantics (press and release with no other key in between), or AltGr typing (= Ctrl+Alt) on European layouts would toggle the widget |
| Linux X11 | `XGrabKey`: `BadAccess` on conflict | XInput2 raw events, listen-only |
| Linux Wayland | xdg-desktop-portal GlobalShortcuts where supported (KDE, recent GNOME, Hyprland), or a desktop-environment custom shortcut running `omonflow --toggle-widget` through single-instance IPC | **Not available.** Not promised |

- **Recommendation:**
  - Support both kinds behind one `ShortcutBackend` interface, with status: registered / unavailable / permission denied / conflict / not supported.
  - Choose per-platform defaults after an **M02 conflict survey**. The likely result: Windows and X11 use the Ctrl+Alt *tap*, with no permission and AltGr-safe; macOS uses a registered combination, so OmonFlow doesn't need Input Monitoring by default, with ⌘⌥ tap as an opt-in.
  - Note that ⌥⌘Space is already macOS's Finder search shortcut, and ⌃⌥Space may switch input sources, so macOS candidates need care.
- **Where to implement it:**
  - Registration can be Python (ctypes on Windows) or Rust (the `global-hotkey` crate).
  - Monitoring hooks should be in **Rust**: a Windows low-level hook is silently removed if its callback is slow, and a Python callback can stall on the GIL.
  - Events reach Python through `poll_events()`, the same as audio events.

### 6.3 Microphone, permissions, identity

- **macOS:**
  - Microphone permission is tied to code identity, so a stable bundle ID and one signing identity are needed from M02.
  - Dev and test builds use one consistent certificate (Developer ID if available, otherwise one local self-signed identity).
  - Permissions are tested only with the packaged `.app`. When run from a terminal, the *terminal* is the app that holds the permission, which is why the PoC worked here.
  - Detection: `AVCaptureDevice.authorizationStatus` (mic) and `AXIsProcessTrusted` (insertion; needed for all synthetic input).
  - Settings deep-links to the matching System Settings pane.
  - The orange mic indicator is on exactly while a stream exists.
- **Windows:** the privacy setting "Let desktop apps access your microphone" shows up as an open-stream failure; map it to `PermissionDenied` with guidance. The notification-area mic icon appears during capture.
- **Linux:** PipeWire/PulseAudio via cpal (backend choice to be validated). Flatpak/portal mic permissions are only relevant if a Flatpak build is made later.
- **Bluetooth headsets (all OSes):** opening the mic switches the headset to a call profile. Expect latency of a second or more on open, lower playback quality while recording, and interrupted music. This is inherent to on-demand capture; document it and show it in INITIALIZING timing.

### 6.4 Insertion

The layered strategy is unchanged (SPEC §11): accessibility → simulated typing → clipboard paste → Copy.

Changes from rev. 1:
- No activation modifiers are held anymore, so the "paste while ⌘⌥ held" hazard is gone.
- Instead, the widget click must not have moved focus (§6.1).
- Wayland defaults to clipboard mode.
- Secure Input (macOS) and UIPI (Windows) blockers are detected and reported.

## 7. Revised application state machine

The full transition table is in **SPEC §8**. Implementation notes:

- **One orchestrator** (a Python QObject on a worker thread) owns the state. The widget, tray and Settings only *send intents* (`start`, `stop`, `cancel`, `retry`, `dismiss`, `copy`) and *subscribe* to state.
- Duplicate-session prevention lives in the orchestrator **and** in the Rust core (`SessionActive`), so a double click or tray + widget racing can't start two streams.
- **Visibility is not a state:**
  - `WidgetController.visible` is separate.
  - Hiding never emits an intent.
  - The tray icon mirrors the dictation state so a hidden widget never hides an active recording.
- **Timeouts:**
  - Open/ready ~3 s.
  - Transcription scales with audio length (e.g. max(30 s, 3× duration) for CPU).
  - LLM uses its configured timeout.
  - SUCCESS → IDLE after ~1.5 s.
- **ERROR keeps payloads:** audio for Retry after an STT failure; the transcript for Copy after an insertion failure. Both live in memory until dismissed, and the transcript also goes to history if enabled.
- **Quit during a session** shows a three-way prompt (finish and insert / discard / keep running).

## 8. Updated testing strategy

| Area | Automated (CI, mocked) | Real hardware (recorded per machine) |
|---|---|---|
| State machine | Every transition in SPEC §8, illegal transitions rejected, duplicate start rejected, visibility independence (hide/show never emits intents), quit prompts | — |
| Widget and visualizers | Spectrum Pill default on a fresh profile; live switching; persistence; failing painter → fallback without changing the saved value; every state renders in all three; idle animation throttled (frame count) | Click-through without focus loss (§6.1) on each OS |
| Shortcut | Status reporting for each backend outcome (registered / conflict / denied / unsupported) using fake backends | Registration and conflict on each OS; Windows AltGr layouts; Wayland portal |
| Rust core | `cargo test` for session buffer logic (contiguity, bounds, trimming, cancel); engine with a fake audio source for start/stop storms (1,000 cycles), timeout and retired-worker path, device loss | `on_demand.py`-style runs on each mic: open/ready/warm-up/stop/release timing, failure rate over 100+ cycles, USB and Bluetooth devices, unplug during recording |
| Privacy | Engine reports no stream while idle; showing the widget / opening Settings / launching never calls `start_capture` | **OS-level check that the mic is not in use while idle and after each session:** macOS `kAudioDevicePropertyDeviceIsRunningSomewhere`; Linux `pactl list source-outputs`; Windows CapabilityAccessManager usage data |
| First words | — | Spoken-phrase corpus (Indonesian + English) started the moment RECORDING shows; compare the first word with a reference transcript; record warm-up trimming |
| Whisper / hardware / refinement | Per SPEC §20 | Per SPEC §17 machines |
| Insertion | Adapter logic with fake OS APIs; transcript kept on every failure path | App matrix: browsers, editors, Office/LibreOffice, terminals, Electron, RDP/VNC clients, custom controls, password fields |

**Removed:** the global push-to-talk suite, rolling pre-buffer correctness as a product requirement (the tests remain in `poc/`), and modifier-chord tests.

## 9. Milestones: dependencies and order

```text
M01 ─► M02 Foundation ─┬─► M03 On-demand Rust audio ─┐
                       └─► M04 Whisper manager ───────┼─► M05 Dictation MVP ─┬─► M06 Hardware/inference
                                                       │                      ├─► M07 Refinement
                                                       │                      └─► M08 Platforms ─► M09 Packaging/release
```

The spec's order is kept, with these recommendations:

1. **Add to M02:**
   - (a) The **non-activating widget spike** (§6.1). If it fails, M05's focus design changes, so we should know early.
   - (b) The shortcut **conflict survey** and `ShortcutBackend` statuses.
   - (c) The **stable bundle ID + signing identity + packaged `.app` smoke test**, so M03 permission tests run against the real app identity.
   - (d) The `rust/` crate skeleton imported by Python.
2. **M03 and M04 are independent.** Either order works. M03 goes first because it contains the most uncertain measurements (device latency and hangs).
3. **M03 includes a `sounddevice` baseline comparison** (same lifecycle, same devices) to show Rust's benefit in numbers, as the spec's "measurable benefit" rule requires.
4. **M05 depends on M02 + M03 + M04.** It covers macOS end-to-end only: insertion matrix, focus restore and clipboard fallback on the machine available here.
5. **M06 and M07** depend only on M05 and could swap order. M07 (refinement) is smaller and independent of hardware access, so it could go first if the Intel/NVIDIA machines aren't available yet.
6. **Split the spec's M08** into **M08 Platform integration** (Windows and Linux adapters: shortcut, focus, insertion, tray, autostart, permissions; runs on the i9/RX580 and RTX 3060 machines) and **M09 Reliability and packaging** (signed and notarized DMG, Windows installer, AppImage, clean-machine tests, docs, release readiness). Each half is already a large milestone.

## 10. Remaining architectural risks

| # | Risk | Impact | Likelihood | Mitigation |
|---|---|---|---|---|
| R1 | **Widget click steals focus** from the target app | Insertion goes to the wrong place or nowhere; core flow broken | High until the spike | M02 spike; target tracking + focus restore fallback; verification before paste |
| R2 | **Wayland:** no arbitrary shortcuts, no focus control, restricted input injection | Degraded flow on GNOME/KDE Wayland | Certain | Portal / desktop-environment shortcut, clipboard mode, honest docs |
| R3 | **Insertion compatibility** across apps (Electron, terminals, RDP, Secure Input, elevated apps) | Text not inserted | High | Layered adapters, app matrix, Copy fallback, history |
| R4 | **Mic startup:** latency (0.1–2.8 s measured), warm-up zeros, open hangs (>10 s seen once) | Clipped first words; stuck INITIALIZING | Medium–High | Signal-based readiness, startup buffer, timeouts with worker retirement, per-device stats, possible "prepared stream" (privacy-verified) |
| R5 | **Bluetooth call-profile switch** on mic open | Slow start, worse audio, playback interruption | High for Bluetooth users | Document; show timing; suggest the built-in/USB mic |
| R6 | **macOS identity and permissions** | Repeat prompts, silent denial | High in dev | Stable bundle ID and signing from M02; test only packaged builds |
| R7 | **Rust's benefit shrinks** with on-demand capture; two-language cost remains | Complexity without payoff | Medium | M03 baseline comparison; keep the crate small; abi3; optional Python fallback |
| R8 | **Shortcut defaults:** modifier-only needs monitoring (and Input Monitoring on macOS); combos may conflict | Shortcut unavailable or annoying | Medium | Two backend kinds, conflict survey, clear statuses, tray always works |
| R9 | **whisper.cpp VAD trimming / hallucinated text on short or silent clips** | Lost words or junk insertions | Medium | VAD off; reject near-silent clips with an energy check (not a boundary decision); prompt tuning |
| R10 | **GPU backend packaging** (CUDA size and EULA, Vulkan driver variance, no Metal on Intel Macs) | Large installers, false GPU claims | Medium | Dynamic ggml backends, validate by benchmark, CPU always available |
| R11 | **PySide6 LGPL** compliance in frozen bundles | Licensing | Low | Dynamic libraries, notices, source offer |
| R12 | **Validation coverage:** only the M4 Pro is reachable here | Unverified platform claims | Certain | Test scripts + checklists the user runs; no claims without recorded runs |

**Retired risks from rev. 1:**
- Alt+Space and modifier chords colliding *during recording*.
- Paste sent with modifiers held.
- The Always-ready mic indicator.
- Privacy-mode trade-off messaging.

## 11. Proof of concept (`poc/rust_audio_poc/`)

The same PoC crate (cpal 0.18.2, rtrb 0.4, PyO3 0.30.1, maturin 1.16, CPython 3.13). It is reference only, not production code.

**A. Continuous pre-buffer (rev. 1, now deferred, kept for reference):**
- `cargo test` 6/6.
- 20 simulated sessions with 0 drops.
- Session start ~0.15 ms.
- Acoustic loopback: speech onset preserved 255–270 ms before activation (3/3).

**B. On-demand lifecycle (rev. 2, `on_demand.py`):** a full open → ready → record 0.6 s → stop → release cycle each time.

| Metric | Built-in (15 cycles) | USB iTalenanMax (6 cycles) |
|---|---|---|
| Click → stream open | median 91 ms, max 93 | median 253 ms, **max 2,752** |
| Click → first callback (ready) | median 101 ms, max 103 | median 262 ms, **max 2,759** |
| Leading exact-zero samples after the first callback | median 0, **max 130 ms** | 0 |
| Stop → finalized samples (48 kHz, no resample yet) | 0.1 ms | 0.1 ms |
| Finalize → device released | median 15 ms | median 31 ms |
| Dropped samples / failures | 0 / 0 | 0 / 0 |
| Earlier separate runs | `cold_start.py`: 10/10 OK, ~98 ms | One run: **open did not complete within 10 s** (engine timeout); one run: max 2.6 s |

Limits of these numbers:
- One machine, two mics, short runs.
- No Bluetooth device.
- No unplug test.
- Resampling not implemented yet.
- "Ready" here is the first callback, not the signal-based readiness proposed in §4.

## 12. Earlier evaluation points, revised

- **CPAL:** still appropriate.
  - It reports device loss and xruns.
  - Its `DeviceId` is stable across restarts on most hosts.
  - The `Stream` must stay on its owning thread.
  - Production needs i16/i32 formats and resampling (`rubato` or a small polyphase filter).
- **PyO3 + maturin mixed project:** still appropriate. abi3 wheels; `uv sync` builds the extension.
- **Copies:** one Rust → Python copy of the finished WAV. A zero-copy buffer object isn't worth it.
- **Packaging:** unchanged from rev. 1.
  - PyInstaller onedir plus `dmgbuild` (macOS), Inno Setup (Windows) and AppImage (Linux).
  - whisper-server is shipped as a resource; evaluate ggml dynamic backends.
  - Signing matters for macOS permissions.
- **Unnecessary complexity to avoid:**
  - Rust for insertion, hardware detection, settings or LLM calls.
  - Streaming transcription in the MVP.
  - The deferred continuous capture mode, unless separately approved.
  - Keyboard monitoring where a registered combo is enough.

## 13. M01 outcome and recorded decisions

**Done:**
- voice-client assessed (read-only).
- Spec v2.1 and companion docs.
- PoC with continuous-buffer and on-demand measurements.
- Hardware validation kit (`tools/validation/`).
- Independent Git repository initialised.

**Not done:**
- voice-client tests and `whisper_bench.py` end-to-end: both blocked by the agent sandbox for downloaded code.
- Non-Mac testing.
- Any M02 work.

**Checks requested by the owner:**

- **voice-client license:** verified. `LICENSE` is the standard MIT text, "Copyright (c) 2026 suhermanme". There are no per-file license headers and no third-party code in the files to be copied (`voice_client/ui.py`, helpers in `voice_assistant.py`). OmonFlow keeps the notice through per-file origin headers and a `NOTICE` entry (ARCHITECTURE §9).
- **Signing identities on this Mac** (`security find-identity -v -p codesigning`): **0 identities**, so there is no Developer ID Application certificate. Xcode is installed (`codesign`, `notarytool` available). Development proceeds with ad-hoc signing (SPEC §18).
- **⌘⇧Space on this Mac:** no system symbolic hotkey uses it (only the disabled ⌃Space and ⌃⌥Space entries reference Space), and none of the apps that commonly claim it (1Password, Raycast, Alfred) are installed. The cross-machine conflict survey is in M02.

**Decisions:** all owner decisions of 2026-11-05 are in the [PLAN.md decision log](PLAN.md#decision-log).

**Open items before M02:** prerequisites P1–P7 in [PLAN.md](PLAN.md#m02-prerequisites-owner-input-needed-before-start).
