# OmonFlow

**Speak naturally. Type everywhere.**

Local-first, cross-platform desktop dictation. A global shortcut shows the floating Spectrum Pill; click the microphone, speak, click Stop, and the text is transcribed locally by whisper.cpp and inserted into the app you were using.

**Status: M02 (application foundation) on macOS.** Tray, floating widget with three visualizers, Settings, ⌘⇧Space show/hide and single instance all work. **Microphone, transcription and insertion are stubs:** clicking the mic runs a demo flow, records nothing and types nothing. See [docs/M02-report.md](docs/M02-report.md).

```bash
uv sync            # Python 3.13 env + builds the Rust extension (needs a Rust toolchain)
uv run omonflow    # start
uv run pytest -q   # tests (headless)
```

| Document | Contents |
|---|---|
| [docs/SPEC.md](docs/SPEC.md) | Authoritative product specification (v2.1) |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Components, audio lifecycle, interfaces, platform adapters, signing, licensing |
| [docs/PLAN.md](docs/PLAN.md) | Milestones M01–M09, acceptance criteria, M02 prerequisites, decision log |
| [docs/RISKS.md](docs/RISKS.md) | Risk register |
| [docs/M01-assessment.md](docs/M01-assessment.md) | voice-client assessment and M01 measurements |
| [docs/M02-report.md](docs/M02-report.md) | M02 results, measurements, limitations |
| [tools/validation/](tools/validation/) | Hardware validation kit (inventory + whisper.cpp benchmark) |
| [poc/rust_audio_poc/](poc/rust_audio_poc/) | M01 Rust/PyO3 audio spike (reference only) |

MIT License. Parts of OmonFlow are adapted from [voice-client](https://github.com/suhermanme/voice-client) (MIT, © 2026 suhermanme).
