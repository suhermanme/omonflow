# OmonFlow

**Speak naturally. Type everywhere.**

Local-first, cross-platform desktop dictation. A global shortcut shows the floating Spectrum Pill; click the microphone, speak, click Stop, and the text is transcribed locally by whisper.cpp and inserted into the app you were using.

**Status: M07 (text refinement) on macOS.** Click the microphone on the floating widget, speak, click Stop, and the text is transcribed locally by whisper.cpp and inserted into the app you were using (Accessibility or paste, with clipboard restore, Copy fallback and History). Settings → Hardware & Acceleration shows detected hardware, which backends whisper.cpp offers, benchmark-validated speed and the backend in use. Settings → Text Refinement chooses Raw, Clean (default, local) or AI via an OpenAI-compatible server you configure (Ollama, llama.cpp, …). Windows/Linux (M08) is next. Development needs a `whisper-server` on PATH (e.g. `brew install whisper-cpp`) and a model (Settings → Speech Recognition). See [docs/M07-report.md](docs/M07-report.md).

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
| [docs/M03-report.md](docs/M03-report.md) | M03 audio core results and Rust vs Python comparison |
| [docs/M04-report.md](docs/M04-report.md) | M04 whisper engine, models, measurements |
| [docs/M05-report.md](docs/M05-report.md) | M05 dictation MVP: insertion matrix, spoken corpus, safety |
| [docs/M06-report.md](docs/M06-report.md) | M06 hardware detection, backends, validation, profiles |
| [docs/M07-report.md](docs/M07-report.md) | M07 text refinement: modes, preservation guard, LLM results |
| [tools/validation/](tools/validation/) | Hardware validation kit (inventory + whisper.cpp benchmark) |
| [poc/rust_audio_poc/](poc/rust_audio_poc/) | M01 Rust/PyO3 audio spike (reference only) |

MIT License. Parts of OmonFlow are adapted from [voice-client](https://github.com/suhermanme/voice-client) (MIT, © 2026 suhermanme).
