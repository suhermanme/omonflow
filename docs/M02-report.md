# M02: Application Foundation, Report

Date: 2026-11-05 · Machine: MacBook Pro M4 Pro, macOS 27.2 · Python 3.13.14 · PySide6 6.11.3 · Rust 1.99.1
Status: **implemented and tested on macOS. Owner review requested before M03.**

## Prerequisites used (owner said "continue"; defaults from PLAN.md applied)

| # | Item | Applied |
|---|---|---|
| P2 | OmonFlow license | MIT (`LICENSE`), matching voice-client |
| P3 | Bundle identifier | `io.github.suhermanme.omonflow` |
| P4 | Remote | Local repository only |
| P5 | macOS dev signing | Ad-hoc (`packaging/macos/sign_adhoc.sh`) |
| P6 | Windows/Linux default shortcut | Placeholder `ctrl+alt+space` (registered combination). Decided in M08 |
| P7 | Intel Mac / i9 OS | Still open |

All of these can be changed. Tell me if any default is wrong.

## What was built

| Area | Delivered | Location |
|---|---|---|
| Project | maturin mixed project; `uv sync` builds the Rust extension `omonflow._core` (abi3, Python 3.13); console script `omonflow` | `pyproject.toml`, `rust/` |
| State machine | SPEC §8 states and transitions, including CAPTURE_READY and FINALIZING; the cancel rules of SPEC §5.3 | `core/states.py`, `app/orchestrator.py` |
| Stub pipeline | The orchestrator runs the full flow with a **stub audio backend** (never opens a microphone) and simulated transcription/insertion (**nothing is typed into other apps**) | `audio/stub.py` |
| Visualizers | Spectrum Pill (default), Orb and Circular Wave, copied from voice-client with attribution headers, refactored into swappable painters; live switching; safe fallback that never overwrites the saved choice | `ui/visualizers/` |
| Floating widget | Clickable controls (mic / stop / cancel / copy / retry / dismiss), click vs drag, tooltips, accessible name per state; **static when idle**, 30 fps only while active; no painting while hidden | `ui/widget/floating.py` |
| macOS non-activating panel | Accessory app (no Dock icon); widget is an `NSPanel` with `NonactivatingPanel` and joins all Spaces | `platform/macos/window.py` |
| Tray / menu bar | Status line, Show/Hide, Settings, Start/Stop/Cancel (enabled per state), Diagnostics, Quit (asks during a session). History is shown disabled, labelled "available in M05" | `ui/tray/tray.py` |
| Settings | General (shortcut + live status, launch visibility, notifications), Appearance (style + previews, always-on-top, reset position), About & Diagnostics. Versioned JSON with atomic writes and corruption backup | `ui/settings/`, `storage/settings.py` |
| Global shortcut | ⌘⇧Space registered through the Rust `global-hotkey` crate (Carbon on macOS); statuses registered / disabled / conflict / unavailable / not supported / invalid; never substituted | `rust/src/lib.rs`, `shortcuts/service.py` |
| Single instance | Local socket; a second launch forwards `activate` / `--toggle-widget` / `--settings` | `app/single_instance.py` |
| Packaging smoke | PyInstaller `.app` (80 MB, arm64), `LSUIElement`, macOS 13 minimum, mic usage text; ad-hoc signing script; release entitlements prepared | `packaging/` |
| CI | GitHub Actions matrix (macOS, Windows, Ubuntu): build, pytest, clippy | `.github/workflows/ci.yml` |
| Tools | State renderer, focus / shortcut / idle-CPU spikes, Python 3.13 compatibility check | `tools/` |

## Acceptance criteria (PLAN.md M02)

| # | Criterion | Result | Evidence |
|---|---|---|---|
| 1 | Fresh profile shows Spectrum Pill; live switching; persists across restart | **Pass** | `test_app.py::test_fresh_install_shows_spectrum_pill`, `test_visualizer_choice_persists_across_restart`, `test_widget.py::test_switching_is_immediate_and_resizes` |
| 2 | Failing painter falls back with an error; saved preference unchanged | **Pass** | `test_failing_visualizer_falls_back_and_reports`, `test_failed_visualizer_keeps_saved_preference` |
| 3 | All states render in all three visualizers | **Pass** | 30 parametrized render tests; images `docs/m02/states-*.png` |
| 4 | Idle widget CPU < 1 % of a core (M4 Pro) | **Pass, after a fix** | Before: 3.6 % / 4.5 % / 11.1 % at 10 fps idle. After making idle static: **0.10–0.15 %** visible, 0.07–0.1 % hidden (`tools/spikes/idle_cpu.py`) |
| 5 | Shortcut toggles visibility; live status; forced failure shows conflict without substitution | **Pass, with a macOS limitation** | Real ⌘⇧Space key events hid then re-showed the widget (`tools/spikes/shortcut_spike.py`); `test_shortcuts.py`. **Limitation:** a second process registered the same ⌘⇧Space without error, so macOS does not report conflicts with other apps. Settings now says so |
| 6 | Showing/hiding the widget, Settings or launching never calls `start_capture` | **Pass** | `test_visibility_never_touches_microphone`, `test_hiding_does_not_stop_recording` |
| 7 | Second launch activates the first instance; closing Settings never quits; clean Quit | **Pass** | `test_single_instance_forwards_commands`; `--toggle-widget` used against a live app; SIGTERM shutdown logged in the packaged app |
| 8 | Focus spike: clicking the widget leaves the target app frontmost with its caret | **Pass** | `tools/spikes/focus_spike.py`: target app frontmost before and after, **zero** focus or activation events in the target, and both clicks (start, stop) reached OmonFlow. Control run without the non-activating panel: the target lost focus. *Target was a Qt test app with a text field, not TextEdit/Safari/VS Code: real-app checks move to the M05 insertion matrix* |
| 9 | Packaged `.app` runs outside the dev environment, loads `omonflow._core`, ad-hoc signature verifies; mic prompt attributed to OmonFlow | **Partly** | Signature `adhoc`, identifier `io.github.suhermanme.omonflow`, `codesign --verify --strict` OK; started with an empty environment, loaded `_core` 0.1.0, applied the non-activating panel, shut down cleanly. **Mic prompt: not tested.** M02 has no microphone code, so the prompt moves to the first M03 packaged run |
| 10 | Attribution headers, `NOTICE`, `LICENSE` | **Pass** | Headers in the four visualizer files; `NOTICE`; `LICENSE` (MIT) |
| 11 | CI green on macOS, Windows and Linux | **Not verified** | Workflow written; there is no GitHub remote yet, so it has never run. Locally: 84/84 tests (5 repeated runs) and clippy clean on macOS |

**Python 3.13 compatibility (spike 3, `tools/compat_check.py`):** every required package publishes wheels for macOS arm64, macOS x86_64, Windows x64 and Linux x86_64. Constraints that follow from it:
- **macOS ≥ 13** (PySide6 6.11 universal2 wheels).
- **glibc ≥ 2.34 on Linux** (Ubuntu 22.04+).

## Measurements (M4 Pro, real screen)

| Measurement | Spectrum Pill | Orb | Circular Wave |
|---|---|---|---|
| Idle, widget visible (CPU % of one core) | 0.10 | 0.10 | 0.10–0.15 |
| Hidden | 0.1 | 0.1 | 0.07 |
| Recording animation at 30 fps | **13.2** | **14.8** | **45.6** (before optimization) |
| Paint time per frame, offscreen @2x | 0.20 ms | 0.30 ms | 3.75 → 3.23 ms |

**Where the recording-animation cost goes** (same machine, in-process):
- About **7 % of a core is the window update itself.** Redrawing a translucent frameless Qt window 30 times a second costs this even when the painter draws nothing.
- Spectrum Pill drawing adds ~3.5 %.
- Circular Wave adds ~23 %. Its cost is Qt stroking the wide blended glow lines; optimizing the Python math saved only ~15 %.
- Circular Wave's recording CPU after the optimization **could not be re-measured**: the screen was locked when this report was written.

These costs fall on the Python interpreter while dictating. That is a direct input for the M03 comparison of Rust vs Python audio under UI load.

## Defects found and fixed during M02

| Defect | Fix |
|---|---|
| Segfault when configuring the macOS window under a non-Cocoa Qt platform (tests) | Platform check before touching the native view |
| AppKit exception: "CanJoinAllSpaces" combined with Qt's "MoveToActiveSpace" | Clear MoveToActiveSpace first |
| Tray icon could outlive its menu at teardown → crash | Ordered `dispose()` |
| Single-instance socket lambdas → crash after server teardown | Slot methods + explicit socket cleanup |
| Ctrl-C/SIGTERM during a session opened a modal dialog and hung | Signals discard and quit without a dialog; the dialog stays for the menu's Quit |
| Idle animation cost 3.6–11 % CPU | Idle and error states are static |
| Cancel ✕ invisible on light desktops (Orb, Circular Wave) | Dark backing |

## Known limitations and open items

1. **macOS shortcut conflicts are undetectable** through registration. The status says "registered" even if another app also uses ⌘⇧Space. Mitigation: hint in Settings; conflict survey on more machines.
2. **⌘⌥ modifier-only opt-in shortcut not implemented** (PLAN allowed deferral). Moved to M05.
3. **Recording animation CPU** (13–46 %) is high; the window-update part (~7 %) needs a different rendering approach. To be measured as UI load in M03, then optimized (frame rate, partial updates or layer-backed rendering). Circular Wave is the heaviest.
4. **Widget appears 20 pt above its requested position** on macOS (requested y=300, on screen y=280). Positions round-trip through Qt consistently, so saved positions don't drift; cause not yet investigated.
5. **Settings has no separate Dictation tab**: the shortcut lives under General (SPEC §14.1). A Dictation tab arrives with real dictation options in M05, so there are no empty controls before then.
6. **Windows and Linux:** code paths exist (registered shortcut via `RegisterHotKey`/`XGrabKey`, tray, single instance) but are **untested on real machines** until M08. Wayland reports "not supported" with the `omonflow --toggle-widget` workaround.
7. **Packaged build is arm64 only.** Intel macOS needs an x86_64 or universal2 build (M09).
8. **The repaint added after macOS window setup** (main.py) is defensive. It was added while diagnosing click failures that turned out to be caused by the locked screen.
9. voice-client's own tests were still not run (sandbox rule for downloaded code).

## How to try it

```bash
uv sync                       # builds the Rust extension too
uv run omonflow               # menu bar icon + Spectrum Pill; ⌘⇧Space shows/hides it
uv run pytest -q              # 84 tests (headless)
uv run --group package pyinstaller --noconfirm packaging/macos/omonflow.spec && packaging/macos/sign_adhoc.sh
```

Clicking the microphone runs the **demo flow only**. No microphone is opened and no text is inserted until M03 and M05.
