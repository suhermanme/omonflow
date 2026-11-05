# OmonFlow — Phased Plan and Acceptance Criteria (v2.1)

Implements [SPEC.md](SPEC.md) §21.

**Every milestone ends with a report containing:**
- Scope delivered.
- Implementation notes.
- Tests and **results**.
- Hardware actually used.
- Known limitations.
- Remaining work.
- Updated docs.

**No milestone starts without explicit owner approval.** A criterion counts as met only when it has been tested; untested items are listed as such.

## Status

| Milestone | Status |
|---|---|
| M01 Repository assessment and architecture | Complete (owner continued to M02 on 2026-11-05) |
| M02 Application foundation | Complete on macOS ([report](M02-report.md)); owner continued to M03 |
| M03 On-demand audio core | Complete on macOS ([report](M03-report.md)); gate decided: Rust primary |
| M04 Whisper process manager and models | Complete on macOS ([report](M04-report.md)) |
| M05 Dictation MVP (macOS) | Complete on macOS ([report](M05-report.md)) |
| M06 Hardware and inference settings | **Implemented and validated on the M4 Pro; other machines pending; owner review requested** ([report](M06-report.md)) |
| M07–M09 | Not started; M07 awaits approval |

## Dependencies

```text
M01 ─► M02 Foundation ─┬─► M03 Audio core (+ comparison gate) ─┐
                       └─► M04 Whisper manager ─────────────────┼─► M05 Dictation MVP (macOS) ─┬─► M06 Hardware/inference
                                                                │                              ├─► M07 Refinement
                                                                │                              └─► M08 Windows/Linux ─► M09 Release readiness
tools/validation runs (owner, any time) ───────────────────────────────────────────────────► inputs to M03, M06, M08
```

- M03 and M04 are independent. M03 goes first because it holds the most uncertain measurements.
- M06 and M07 are independent of each other. M07 can go first if validation machines aren't ready.

---

## M01 — Repository assessment and architecture

**Deliverables:**
- `docs/M01-assessment.md`.
- `docs/SPEC.md` v2.1.
- `docs/ARCHITECTURE.md`, `docs/PLAN.md`, `docs/RISKS.md`.
- `poc/rust_audio_poc/` with measurements.
- `tools/validation/` kit.
- Git repository.

**Acceptance:**
- [x] voice-client assessed (read-only).
- [x] License verified (MIT, © 2026 suhermanme).
- [x] Hybrid architecture evaluated.
- [x] Microphone lifecycle measured on the M4 Pro (2 devices).
- [x] Spec contradictions removed (SPEC §0).
- [x] Signing identities inspected: none present.
- [x] Validation kit prepared: `hwprobe.py` run on M4 Pro.
- [ ] `whisper_bench.py` end-to-end run: blocked in the agent sandbox; first owner run validates it.
- [ ] voice-client test suite: not run (sandbox blocked running downloaded code).
- [x] Owner sign-off (owner instructed "commit, and continue", 2026-11-05).

---

## M02 — Application foundation

**Scope:**
- OmonFlow package and identity:
  - maturin mixed project.
  - Python 3.13 / uv.
  - `rust/` skeleton imported as `omonflow._core`.
  - CI (macOS, Windows, Linux) running lint and tests.
- Lifecycle: single instance with relaunch → activate, tray/menu bar, close-to-tray, Quit with clean shutdown.
- Visualizers copied from voice-client with attribution and refactored into swappable painters:
  - Spectrum Pill default.
  - Orb and Circular Wave selectable.
  - Live switching, persistence, safe fallback.
  - Idle throttling.
- Floating widget with controls (mic / Stop / Cancel / Copy) wired to a **stub** audio backend and stub orchestrator states.
- Settings v1: General, Appearance, Dictation (shortcut section), About. Persisted versioned settings.
- Show/hide global shortcut on macOS: ⌘⇧Space registered backend with status reporting. ⌘⌥ opt-in backend behind Advanced (if time allows; else M05).
- **Spikes (reported with results):**
  1. Non-activating widget on macOS: clicking widget buttons must not change the frontmost app or its focused text field.
  2. Shortcut conflict survey (macOS now; Windows/Linux list prepared for M08).
  3. Python 3.13 compatibility check of PySide6, PyInstaller, pyobjc and maturin for all four targets.
- Packaging smoke test: ad-hoc-signed `.app` with the Rust extension at a fixed location. The mic permission prompt names OmonFlow, not the terminal.

**Acceptance criteria:**
1. A fresh profile shows Spectrum Pill without any configuration. Orb and Circular Wave switch live from Settings, and the choice persists across restart.
2. A forced painter error falls back to a safe visualizer with an error shown, and the saved preference is unchanged.
3. Every SPEC §8 state (including CAPTURE_READY and FINALIZING) renders in all three visualizers. Screenshots are attached.
4. Idle CPU of the widget is measured and reported. Target: under 1% of one core on the M4 Pro.
5. The shortcut toggles visibility. Settings shows its live status. A forced registration failure shows "conflict/unavailable" without substituting another shortcut.
6. Showing or hiding the widget or Settings, and launching the app, never call `start_capture` (asserted with the stub backend).
7. Second launch activates the first instance. Closing Settings or hiding the widget never quits. Quit exits cleanly.
8. **Focus spike result:** with TextEdit, Safari and VS Code in front, clicking the widget's mic button leaves that app frontmost with its caret, or the documented fallback is chosen with evidence.
9. Packaged `.app` launches outside the dev environment and loads `omonflow._core`. The ad-hoc signature verifies (`codesign -v`). The mic prompt is attributed to OmonFlow.
10. Copied files carry voice-client attribution headers; `NOTICE` and the chosen `LICENSE` are present.
11. CI is green on macOS, Windows and Linux for unit tests. Platform UI behavior on Windows/Linux is **not** claimed in M02.

### M02 prerequisites (owner input needed before start)

| # | Item | Proposed default |
|---|---|---|
| P1 | Formal M01 sign-off and M02 go-ahead | — |
| P2 | OmonFlow's own license | MIT (consistent with voice-client) |
| P3 | Bundle identifier, fixed from M02 onward | `io.github.suhermanme.omonflow` |
| P4 | GitHub remote for the new repository (private or public), or local-only for now | Local-only until you create the remote |
| P5 | macOS dev signing: ad-hoc only (as instructed), or additionally a free self-signed local certificate to keep permissions across rebuilds | Ad-hoc; revisit if re-prompting slows M05 testing |
| P6 | Windows/Linux default shortcut direction for the survey: Ctrl+Alt tap vs a registered combination | Survey both; decide in M08 with evidence |
| P7 | Is an **Intel Mac** available for validation? SPEC targets Intel macOS, but no Intel Mac is in the validation list. Which OS runs on the i9 machine? | Needed by M09 at the latest |

---

## M03 — On-demand audio core (Rust + Python baseline)

**Scope:**
- Production `AudioCore` (Rust) and `SoundDeviceAudio` (Python) behind `AudioBackend`.
- Device discovery and selection, format-generic input, warm-up handling, CAPTURE_READY/activity signals, timeout + worker retirement, device loss, finalize (trim, resample, WAV), shutdown.
- Audio Input settings: device, test, level, startup diagnostics, timeout.
- Comparison harness.

**Inputs from M02:** recording animation costs 13–46 % of a core on the Python side, so include it as "UI load" in the comparison. The first packaged run with real capture must confirm the mic permission prompt names OmonFlow (M02 criterion 9).

**Acceptance criteria:**
1. No stream exists while idle, checked by engine state **and** OS-level device-in-use check on macOS. Linux/Windows checks are run in M08.
2. ≥ 500 consecutive start/stop cycles per device with 0 crashes and 0 leaked streams. Failure rate and hang count reported.
3. Integrity: synthetic-source tests show no gaps or duplicates from the first callback; bounded memory at the maximum session length.
4. Timeout path: a simulated hung open reaches ERROR within timeout + 100 ms; Retry works; shutdown completes within 2 s with a retired worker present.
5. CAPTURE_READY never precedes real (non-zero) signal. RECORDING never precedes measured activity. No audio after the first callback is lost (warm-up included).
6. Metrics table per device (built-in, USB, and Bluetooth if available): click → open, → CAPTURE_READY, → RECORDING (spoken test), Stop → finalized, release, CPU, memory.
7. **Comparison gate report:** Rust vs Python on the metrics in ARCHITECTURE §3.3, including under UI/GIL load. The owner decides whether Rust stays primary.
8. Device unplug during recording returns the partial audio with a warning.

**M03 inputs to M05:** tune activity thresholds with real speech; physical unplug test; packaged-app mic prompt; USB/Bluetooth startup timing.

## M04 — Whisper process manager and models

**Scope:**
- Bundled whisper-server discovery, free port, start/health/restart/stop, owned-PID tracking, duplicate prevention, logs.
- Model catalog with download, resume, Hugging Face SHA-256 verification, confirmation for large models, removal, compatibility checks.
- `/load` switching.
- Whisper VAD off.
- Evaluate ggml dynamic backends.

**Acceptance:**
- Killing whisper-server is detected and it restarts within policy.
- No duplicate instances.
- Only OmonFlow-owned PIDs are stopped.
- An interrupted download resumes or restarts cleanly.
- A corrupted model is detected.
- Models are stored in the user data dir.
- Cold start and model-load times reported on the M4 Pro.

**M04 inputs to M05:** wire `WhisperServer.transcribe` into the orchestrator (replace the stub); "No speech recognized" path; transcription timeout; complete a real model download on a normal connection.

## M05 — Dictation MVP (macOS)

**Scope:**
- Orchestrator with the full SPEC §8 state machine and §5.3 cancellation.
- Audio → Whisper → raw insert.
- FocusTracker; layered macOS inserter with clipboard preservation.
- Error states with Copy/Retry.
- History (optional); Text Insertion settings with permission status.

**Acceptance:**
- End-to-end dictation into the insertion matrix apps on macOS, with results per app and layer.
- Every cancel row of SPEC §5.3 is tested.
- No transcript is lost in any failure path (forced failures).
- Clipboard restored.
- Indonesian/English spoken corpus first-word check reported.
- Ad-hoc packaged app tested with real permission prompts.

**M05 inputs:** packaged-app permission prompts (owner); real-voice activity tuning; Electron/Chrome/Firefox insertion checks; ⌘⌥ opt-in moved to M08.

## M06 — Hardware and inference settings

**Acceptance:**
- Detected / available / validated / active are shown separately on every validation machine actually run.
- No GPU claim without benchmark evidence.
- A failed backend rolls back to the previous working config with an explanation.
- Profiles never overwrite manual choices.

**M06 inputs:** owner runs `tools/validation` on the i9/RX580 and RTX 3060; Windows swap needs per-process log names (M08).

## M07 — Text refinement

**Acceptance:**
- Raw/Clean/AI work.
- The preservation test set (identifiers, paths, URLs, numbers, Indonesian–English) passes.
- LLM down or timeout → raw/clean is inserted with a notice.
- Remote-endpoint disclosure is shown.
- API key stored in the keychain.

## M08 — Windows and Linux integration

**Acceptance (per OS, on real machines):**
- Shortcut backends and statuses.
- Non-activating widget or focus restore.
- Insertion matrix.
- Tray and autostart.
- Mic lifecycle metrics.
- OS-level idle-mic check.
- Documented limitations: Windows UIPI and SmartScreen; Linux X11 vs Wayland (portal, clipboard mode), PipeWire/PulseAudio.

## M09 — Reliability, packaging and release readiness

**Acceptance:**
- SPEC §19 error list covered by tests.
- DMG (ad-hoc), unsigned Windows installer and AppImage install and run on clean machines.
- Release checklist for Developer ID + notarization and Authenticode (purchase and signing are separate owner tasks).
- User docs.
- Release-readiness report.

---

## Decision log

| Date | Decision | By |
|---|---|---|
| 2026-11-05 | Spec v2: widget-driven, on-demand capture; shortcut shows/hides widget | Owner |
| 2026-11-05 | macOS default ⌘⇧Space (registered); ⌘⌥ opt-in advanced | Owner |
| 2026-11-05 | Cancellation rules (SPEC §5.3) | Owner |
| 2026-11-05 | Python fallback + M03 comparison gate for Rust | Owner |
| 2026-11-05 | Reuse voice-client code with attribution; no runtime dependency | Owner |
| 2026-11-05 | Python 3.13 (subject to validation) | Owner |
| 2026-11-05 | Independent Git repository for OmonFlow | Owner |
| 2026-11-05 | No paid certificates in development; macOS ad-hoc, Windows unsigned | Owner |
| 2026-11-05 | Mic states INITIALIZING / CAPTURE_READY / RECORDING / FINALIZING; activity detection is UI-only | Owner |
| 2026-11-05 | M08 split into M08 (Windows/Linux) and M09 (packaging/release) | Owner |
| 2026-11-05 | Proceed to M02 with default prerequisites (MIT, `io.github.suhermanme.omonflow`, local repo, ad-hoc signing) | Owner ("continue") |
| 2026-11-05 | Idle widget is static (no animation) to meet the < 1 % idle CPU target | M02 measurement |
| 2026-11-05 | ⌘⌥ opt-in shortcut deferred to M05 | M02 scope |
| 2026-11-05 | Proceed to M03 | Owner ("continue") |
| 2026-11-05 | Rust stays the primary audio core; Python fallback kept as labelled degraded mode | Owner ("yes agree") |
| 2026-11-05 | Proceed to M04 | Owner |
| 2026-11-05 | Engine starts at launch with the chosen model (default `small`) and stays loaded | M04 (memory vs latency; revisit in M06) |
| 2026-11-05 | Proceed to M05 | Owner |
| 2026-11-05 | Insertion "auto" = Accessibility (verified) → paste; typing only on request and only for single lines | M05 design |
| 2026-11-05 | History on by default (text only, local), with delete-one/delete-all and an off switch | M05 (SPEC §15 lists "disable history") |
| 2026-11-05 | ⌘⌥ opt-in shortcut moved to M08 (with keyboard hooks for Windows/Linux) | M05 scope |
| 2026-11-05 | Proceed to M06 | Owner |
| 2026-11-05 | Engine configuration changes are committed only after the new engine passes start, backend and self-test checks | M06 (SPEC §9.8) |
| 2026-11-05 | Owner's LLM for M07 testing: OpenAI-compatible server at 192.168.3.243:8080 (model read from /v1/models) | Owner |
| 2026-11-05 | Auto language = English or Indonesian, chosen per sentence; never translate. Owner confirmed with own voice | Owner testing |
