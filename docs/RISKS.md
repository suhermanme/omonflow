# OmonFlow — Risk Register (v2.1)

**Scale:** Impact and Likelihood are rated H/M/L. **Owner** = the milestone that must retire or measure the risk. Review the register at every milestone report.

| # | Risk | Impact | Likelihood | Mitigation | Owner | Status |
|---|---|---|---|---|---|---|
| R1 | **Widget click steals focus** from the target app, so text goes to the wrong place | H | H until spike | Non-activating panel (macOS/Windows/X11); FocusTracker + restore fallback; verify before paste | M02 spike, M05, M08 | **macOS: mitigated.** M02 spike passed (non-activating panel; control run confirmed it is needed). Open for Windows/Linux and real apps |
| R2 | **Wayland** blocks arbitrary shortcuts, focus control and input injection | M | Certain | Portal / desktop-environment shortcut, CLI toggle, clipboard mode, honest docs | M08 | Accepted (documented) |
| R3 | **Insertion compatibility** varies (Electron, terminals, RDP/VNC, Secure Input, elevated apps) | H | H | Layered adapters, per-app matrix, blocker detection, Copy fallback, history | M05, M08 | Open |
| R4 | **Mic startup**: 0.1–2.8 s measured, warm-up zeros, one >10 s hang, so users may start speaking too early or the widget gets stuck | H | M–H | CAPTURE_READY only on real signal; capture from first callback; timeout + worker retirement + Retry/switch device; per-device stats | M03 | Measured in M01; open |
| R5 | **Activity detection misjudges** (noisy room shows RECORDING; very quiet speaker stays CAPTURE_READY) | L (UI only) | M | Adaptive noise floor, sticky RECORDING, never affects the audio itself; tune on several mics | M03 | Open |
| R6 | **Bluetooth headset** switches to call profile on mic open: slow start, lower quality, playback interrupted | M | H for BT users | Document; show startup timing; recommend built-in/USB | M03, M09 docs | Open |
| R7 | **Ad-hoc signing changes identity every rebuild**, so macOS keeps asking for Mic/Accessibility again; tests slow down or are misread as bugs | M | H | Fixed bundle ID and location; rebuild the permission-test app sparingly; `tccutil reset` for clean tests; optional free self-signed cert (owner choice) | M02, M05 | Open |
| R8 | **Unsigned distribution**: Gatekeeper quarantine on other Macs, SmartScreen on Windows | M | Certain | Document workarounds; signing and notarization as an M09 release task | M09 | Accepted for dev |
| R9 | **Rust benefit not measurable** in on-demand mode; two-language cost remains | M | M | M03 comparison gate; Python fallback ready; keep the crate small; abi3 | M03 | Open |
| R10 | **Shortcut conflicts**: ⌘⇧Space may be taken by third-party apps (e.g. some password managers), and macOS doesn't always report Carbon conflicts; modifier-only needs Input Monitoring | M | M | Conflict survey; status reporting; never substitute; tray always works; ⌘⌥ opt-in only | M02, M08 | Open. **M02 finding: macOS accepted the same combination from a second process without error, so conflicts with other apps cannot be detected on macOS**; Settings tells the user. No conflict on the M4 Pro (no system binding, no 1Password/Raycast/Alfred) |
| R11 | **Python 3.13 compatibility** gaps (PySide6/pyobjc/PyInstaller wheels, especially macOS x86_64) | M | L | M02 check: all wheels exist for the four targets. Constraints: macOS ≥ 13, glibc ≥ 2.34 | M02 | Closed (constraints recorded) |
| R12 | **No Intel Mac** in the validation set, although Intel macOS is a target | M | H | Owner to confirm a machine (P7), or document Intel macOS as unvalidated | M09 | Open |
| R13 | **whisper.cpp behavior**: VAD trimming, hallucinated text on short/silent clips | M | M | VAD off; energy check rejects silent clips; prompt tuning; "No speech recognized" path | M04, M05 | Open |
| R14 | **GPU packaging**: CUDA size and EULA, Vulkan driver variance, RX580 Vulkan-only (no ROCm for Polaris), no Metal on Intel Macs | M | M | Dynamic ggml backends, validation kit evidence, CPU always available | M04, M06 | Open |
| R15 | **Validation coverage**: only the M4 Pro is reachable by the agent; `whisper_bench.py` not yet run end to end | M | Certain | Owner runs the kit; first runs also validate the scripts; no claims without results | M01→M08 | Open |
| R16 | **PySide6 LGPL** obligations in frozen bundles | L | L | Dynamic libraries, notices, source offer | M09 | Open |
| R18 | **Widget animation CPU while dictating**: 13–15 % (Pill/Orb), up to 46 % (Circular Wave) at 30 fps; ~7 % is the translucent window update itself | M | Measured | Static idle (done: 0.1 %); M03 measures it as UI load; options: lower frame rate, partial updates, layer-backed rendering; Circular Wave glow cost | M03, M05 | Open |
| R17 | **Retired (hung) audio workers** accumulate or block shutdown | M | L | One worker per attempt, bounded retired-worker count, shutdown grace period then exit | M03 | Open |

**Retired in v2:**
- Alt+Space and modifier-chord collisions during recording.
- Paste sent while activation modifiers are held.
- Always-ready mic indicator perception.
- Rolling pre-buffer correctness as a product risk (the PoC remains for reference).
