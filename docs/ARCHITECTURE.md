# OmonFlow — Architecture (v2.1)

How OmonFlow implements [SPEC.md](SPEC.md). Evidence and measurements are in [M01-assessment.md](M01-assessment.md); risks in [RISKS.md](RISKS.md).

## 1. Components and threads

```text
┌───────────────────────────── OmonFlow process (Python 3.13 + PySide6) ─────────────────────────────┐
│ Qt main thread: widget + visualizers · tray · Settings · history · notifications                   │
│      ▲ Qt signals (state, level, errors)          │ intents (start/stop/cancel/retry/copy/dismiss)  │
│ Orchestrator (QObject, worker thread): state machine of SPEC §8, single-session guard              │
│   ├─ AudioBackend ── omonflow._core.AudioCore (Rust)  |  SoundDeviceAudio (Python fallback)        │
│   ├─ Event pump thread: get_input_level() ~30 Hz, poll_events() → Qt signals                       │
│   ├─ WhisperManager ── HTTP ──► whisper-server (child process, OmonFlow-owned)                     │
│   ├─ Refiner (raw / clean / OpenAI-compatible)                                                     │
│   ├─ FocusTracker + Inserter (platform adapters)                                                   │
│   ├─ ShortcutService (platform backends; show/hide widget only)                                    │
│   └─ Storage: settings (versioned JSON, atomic write) · history (SQLite) · keyring (API keys)      │
│                                                                                                    │
│ omonflow._core (Rust/PyO3): engine worker owns the cpal stream; RT callback → SPSC ring → session  │
└────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

**Rules:**
- UI components only send **intents** and render **state**. Widget visibility (`WidgetController.visible`) is separate from dictation state.
- Rust never calls into Python. Events and levels are pulled by the pump thread.
- Every blocking call (open, finalize, HTTP, insertion) runs off the Qt main thread.

**M02 implementation notes:**
- The widget is static in IDLE and ERROR, animates at 30 fps otherwise, and never paints while hidden.
- Signals (Ctrl-C, SIGTERM) discard an active session and quit; the menu's Quit asks first.
- Platform minimums from the dependency check: macOS 13, glibc 2.34.

## 2. Repository layout

A maturin mixed project: one `pyproject.toml` builds the Rust extension and the Python package, and `uv sync` builds everything.

```text
OmonFlow/
├── pyproject.toml            # maturin backend; Python 3.13; abi3 extension
├── rust/                     # crate omonflow_core → module omonflow._core
├── src/omonflow/
│   ├── app/                  # main, lifecycle, single instance (QLocalServer), orchestrator
│   ├── core/                 # states, events, interfaces (AudioBackend, Inserter, ShortcutBackend, …)
│   ├── audio/                # facade, Rust adapter, sounddevice fallback
│   ├── stt/                  # whisper process manager, client, model manager
│   ├── hardware/             # detection, backend availability/validation, benchmark
│   ├── refinement/
│   ├── insertion/            # focus tracking, layered inserters
│   ├── shortcuts/            # registered-combo and monitoring backends, status model
│   ├── platform/{macos,windows,linux}/
│   ├── ui/{widget,visualizers,settings,tray,history}/
│   └── storage/
├── tests/                    # pytest (Python); Rust tests live in rust/
├── tools/validation/         # hardware validation kit
├── packaging/{macos,windows,linux}/
├── docs/
├── poc/                      # M01 spike, reference only
├── LICENSE · NOTICE
```

## 3. Audio core

### 3.1 Lifecycle (SPEC §6)

```text
idle (no stream)
  └─start_capture()─► opening ─first callback─► warming ─non-zero block / cap─► CAPTURE_READY ─activity─► RECORDING
                        │ timeout → retire worker, DeviceTimeout                            stop_capture() ─► finalize ─► release ─► idle
```

| Concern | Design | Evidence (M01, M4 Pro) |
|---|---|---|
| Open | Runs on an engine worker. Python call is non-blocking; `wait_until_ready(timeout)` releases the GIL | Built-in ready ~100 ms; USB median ~260 ms, max 2.8 s |
| Warm-up | Samples kept from the first callback. CAPTURE_READY at the first block that isn't exact zeros, or at a cap (~300 ms) with a "silent input" warning | Built-in mic: up to 130 ms of exact zeros |
| Activity → RECORDING | Noise floor = median peak over the first ~150 ms of real signal; activity = level > floor × k (and > absolute minimum) for ≥ ~60 ms. UI only; sticky | To tune in M03 on several mics |
| Recording | Bounded session buffer (max length setting, default 10 min) | |
| Stop/finalize | Close the stream first, then trim leading exact zeros, resample to 16 kHz (`rubato` or polyphase), encode s16 WAV → one copy into Python `bytes` | Stop → samples ~0.1 ms; release 15–31 ms |
| Hung open | A worker that misses the timeout is *retired*: never joined on the UI path, cleaned up if it returns. The next start uses a fresh worker. Shutdown doesn't wait on retired workers beyond a short grace period | One >10 s USB open hang observed |
| Device loss | cpal `DeviceNotAvailable` → finalize the partial audio + warning | |
| Idle | No stream object exists | Verified by OS-level test |

### 3.2 Interface (implemented by Rust and by the Python fallback)

```python
class AudioBackend(Protocol):            # omonflow.core.interfaces
    name: str                            # "rust" | "sounddevice"
    def enumerate_devices(self) -> list[DeviceInfo]: ...        # id, name, is_default, rate, channels, transport
    def select_device(self, device_id: str | None) -> None: ... # idle only; None = system default
    def start_capture(self) -> None: ...                        # non-blocking; raises SessionActive
    def wait_until_ready(self, timeout_ms: int) -> ReadyInfo: ...  # open_ms, ready_ms, warmup_ms, silent_warning
    def get_input_level(self) -> LevelInfo: ...                 # peak since last call, noise floor, activity flag
    def stop_capture(self) -> None: ...                         # marks end, closes stream; fast
    def finalize_audio(self) -> Recording: ...                  # wav bytes (16 kHz mono s16), duration, trimmed_ms, device_lost, overflowed
    def cancel_capture(self) -> None: ...                       # close + drop
    def state(self) -> CaptureState: ...                        # idle|opening|warming|ready|active|stopping
    def poll_events(self) -> list[AudioEvent]: ...              # ready, activity, device_lost, xrun, error, open_timeout
    def shutdown(self) -> None: ...                             # idempotent
```

**Typed errors:**
- `PermissionDenied`
- `DeviceNotFound`
- `DeviceTimeout`
- `SessionActive`
- `EngineStopped`
- `BackendUnavailable`

**Rust side:**
- The cpal callback does downmix + SPSC push + atomic peak only: no locks, allocation, I/O or Python.
- One engine worker per capture attempt owns the `Stream`. It is `!Send` on some hosts, which is why the worker owns it.
- Format-generic input (f32/i16/i32).
- **abi3 wheels** (`abi3-py313`).

**Python fallback:**
- `sounddevice` with the same interface and the same Python-side warm-up/activity logic.
- Its callback only copies into a bounded queue (no Qt emits).
- Needs PortAudio: bundled in the macOS and Windows wheels; Linux needs `libportaudio2`.

**M03 implementation notes:**
- Capture is implemented as specified in §3.1–3.2.
- Every wait releases the GIL.
- Cancel runs off the Qt thread.
- Device ids are cpal `DeviceId` strings for Rust and names for the fallback; both are persisted.
- At quit, a capture stuck in the driver triggers an immediate process exit so the OS releases the microphone.

### 3.3 M03 comparison (decides Rust's future)

Result (M03 report): Rust lost no audio under any load; the Python fallback lost 15 % under ordinary GIL load and deadlocked under heavy load. Recommendation: Rust primary.

Both backends are measured on the same machines and devices:
- Click → CAPTURE_READY: p50, p95 and max.
- Gaps or dropouts while the UI is under load: heavy widget animation plus a CPU-bound Python thread holding the GIL.
- CPU use while recording.
- Failure and hang rate over ≥ 500 start/stop cycles.
- Shutdown time.
- Memory.

The owner decides at the M03 gate whether Rust stays primary.

## 4. Focus and insertion

### 4.1 Focus

- **FocusTracker** records the last focused non-OmonFlow app/window continuously:
  - macOS: `NSWorkspace` activation notifications + AX focused element.
  - Windows: `SetWinEventHook(EVENT_SYSTEM_FOREGROUND)`.
  - X11: `_NET_ACTIVE_WINDOW`.
- It is frozen as the target at the mic click.

| Platform | Widget click must not steal focus | Fallback |
|---|---|---|
| macOS | Accessory app (`LSUIElement`); widget = Qt `Tool` window (NSPanel) with `NonactivatingPanel` set via pyobjc; first-mouse handling | Re-activate the target app before insertion |
| Windows | `WS_EX_NOACTIVATE` | `SetForegroundWindow(target)` (allowed: OmonFlow received the last input) |
| X11 | `WM_HINTS.input = False`, utility window type | `_NET_ACTIVE_WINDOW` request |
| Wayland | Not controllable by the app | Clipboard mode + "Press Ctrl+V" notice |

M02 spike decides the macOS and Windows mechanisms (PLAN M02).

### 4.2 Insertion layers (per platform adapter)

1. **Accessibility:**
   - macOS: `kAXSelectedTextAttribute` + read-back verification.
   - Windows: UI Automation where supported.
   - Linux: AT-SPI (experimental).
2. **Simulated typing:**
   - macOS: `CGEventKeyboardSetUnicodeString`.
   - Windows: `SendInput` + `KEYEVENTF_UNICODE`.
   - X11: XTest.
   - Wayland: portal/`wtype` only where allowed.
3. **Clipboard + paste:** save the clipboard, set the text, send ⌘V/Ctrl+V, restore the clipboard.
4. **Failure:** transcript kept, Copy offered, history if enabled. The outcome is reported accurately (inserted / partial / failed).

**Detected blockers:** macOS Secure Input, missing Accessibility permission, Windows UIPI (elevated target), Wayland injection limits.

Long or multiline text prefers paste.

## 5. Global shortcut (show/hide only)

`ShortcutService` uses pluggable backends. Each one reports **registered / unavailable / permission denied / conflict / not supported**.

| Backend | Platforms | Notes |
|---|---|---|
| Registered combination | macOS (Carbon `RegisterEventHotKey`), Windows (`RegisterHotKey`), X11 (`XGrabKey`) | No permission. Conflicts surface as registration errors where the OS reports them (Windows, X11). On macOS some conflicts are invisible to the API, which is why there is a conflict survey |
| Modifier-only tap | macOS (listen-only `CGEventTap`, **Input Monitoring**), Windows (`WH_KEYBOARD_LL`), X11 (XInput2) | Tap = chord down + up with no other key; AltGr-safe. Implemented in Rust (hook callbacks must never stall on the GIL) |
| Portal | Wayland (xdg-desktop-portal GlobalShortcuts) | User binds the shortcut in the desktop's dialog |
| CLI toggle | All (`omonflow --toggle-widget` via single-instance IPC) | For desktop-environment custom shortcuts, especially Wayland |

macOS default: **⌘⇧Space** (registered). ⌘⌥ tap is opt-in under Settings → Dictation → Advanced, with the permission explained first.

## 6. Whisper

- whisper-server is a supervised child process: bundled executable, free localhost port, owned-PID tracking, health check (`/health`), restart policy, captured logs.
- Model switching via `/load`; backend switching restarts the process.
- **whisper-side VAD off.** A short or near-silent clip is rejected before transcription (an energy check, not a boundary decision).
- Backend truth comes from whisper/ggml's own initialization log and a benchmark (`tools/validation/whisper_bench.py` uses the same rule).

## 7. Storage, privacy and config

- **Locations:** `platformdirs` user data/config dirs.
- **Settings:** versioned JSON with atomic writes. A corrupt file is backed up and defaults are used, with a notice.
- **History:** SQLite.
- **API keys:** OS keychain (`keyring`).
- **Logs:** structured; never contain audio or transcript content unless the user enables debug content logging.

## 8. Packaging and signing

- **Bundling:** PyInstaller onedir. macOS `.app` → DMG (`dmgbuild`); Windows → Inno Setup installer; Linux → AppImage.
- **whisper-server:** shipped as a resource. Evaluate ggml dynamic backends (CPU variants + CUDA/Vulkan as loadable libraries) in M04.
- **macOS development:**
  - Bundle ID `io.github.suhermanme.omonflow` (to be confirmed in M02).
  - **Ad-hoc signature** (`codesign --sign - --force --deep`) applied by the packaging script.
  - Fixed install location for permission tests (e.g. `~/Applications/OmonFlow.app`).
  - Hardened runtime and the `audio-input` entitlement are prepared but only meaningful with a real identity.
  - `tccutil reset Microphone|Accessibility io.github.suhermanme.omonflow` gives clean-state tests.
- **Ad-hoc caveat:** permissions are tied to the code hash, so each rebuild may need permissions granted again. An *optional* free self-signed local certificate keeps permissions across rebuilds without an Apple account. That is owner's choice; it's not required.
- **Windows:** unsigned; SmartScreen behavior documented.
- **Release (M09):** the signing step is a single switchable script stage (identity, entitlements, notarytool, Authenticode).

## 9. Licensing and attribution

- **voice-client:** MIT, © 2026 suhermanme. It's the same owner, so the license has no compatibility issue.
  - Copied or adapted files carry a header: `Adapted from voice-client (https://github.com/suhermanme/voice-client), Copyright (c) 2026 suhermanme, MIT License.`
  - `NOTICE` lists the origin.
- **OmonFlow's own license:** to be chosen by the owner before M02 (PLAN prerequisites).
- **Third-party:**
  - PySide6/Qt: LGPLv3. Dynamic libraries, license texts, source offer.
  - whisper.cpp/ggml: MIT.
  - cpal: Apache-2.0.
  - PyO3, rtrb: MIT/Apache-2.0.
  - httpx, numpy: BSD.
  - sounddevice: MIT; PortAudio: MIT-style.
  - Whisper weights: MIT (downloaded, not bundled).
  - CUDA redistributables: NVIDIA EULA (decided in M06/M09).
- The About dialog and `THIRD_PARTY_LICENSES` are generated at packaging time.
