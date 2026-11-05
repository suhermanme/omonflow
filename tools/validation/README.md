# OmonFlow hardware validation kit

Repeatable inventory and benchmark scripts for the three validation machines (SPEC §17). Results are evidence for OmonFlow's hardware and backend decisions. **Nothing is "supported" until a run on the real machine says so.**

| Script | What it does | Needs |
|---|---|---|
| `hwprobe.py` | Inventory: OS, desktop session (X11/Wayland, portal), CPU + instruction sets, RAM, GPUs/drivers, NVIDIA/Vulkan/ROCm runtimes, audio stack, toolchains | Python 3.9+ only |
| `whisper_bench.py` | Builds whisper.cpp per backend (cpu/metal/cuda/vulkan), downloads models with Hugging Face SHA-256 verification, runs a warm-up + N timed transcriptions, and marks a backend **validated** only if it built, its init appears in whisper's own log, and transcription succeeded | git, cmake, C/C++ compiler; CUDA toolkit or Vulkan SDK for those backends |
| `../../poc/rust_audio_poc/on_demand.py` | Mic open → ready → record → stop → release timing per input device (the Rust capture PoC) | Rust toolchain, uv; Linux: `libasound2-dev pkg-config` |

All output goes to `results/<hostname>-<timestamp>/`. Please send back the whole folder.

## Per-machine steps

### Apple M4 Pro (macOS)
```bash
python3 tools/validation/hwprobe.py
python3 tools/validation/whisper_bench.py --backends cpu,metal --models base,small,large-v3-turbo
cd poc/rust_audio_poc && uv venv --python 3.13 .venv && uv pip install --python .venv/bin/python maturin numpy \
  && .venv/bin/maturin develop --release && N=15 .venv/bin/python on_demand.py
```

### Ubuntu + NVIDIA RTX 3060 12 GB
```bash
sudo apt install build-essential cmake git python3 libasound2-dev pkg-config
# CUDA: NVIDIA driver + CUDA toolkit (nvcc on PATH). Vulkan: libvulkan-dev glslc vulkan-tools
python3 tools/validation/hwprobe.py
python3 tools/validation/whisper_bench.py --backends cpu,cuda,vulkan --models base,small,medium,large-v3-turbo
# mic lifecycle: same as macOS, using .venv/bin/... paths
```
Also note whether the session is X11 or Wayland and which desktop. `hwprobe` records it, but please confirm.

### Intel i9-9900K + AMD RX580
Run on whichever OS is installed (please tell us which).
- **Linux:** as Ubuntu above, with `--backends cpu,vulkan`. RX580 (Polaris) uses Mesa RADV for Vulkan; ROCm does not officially support Polaris, so there is no ROCm backend.
- **Windows:** install Git, CMake, Visual Studio Build Tools (C++), Python 3.13, and the Vulkan SDK for the Vulkan backend. Then from a "Developer PowerShell":
  ```powershell
  py tools\validation\hwprobe.py
  py tools\validation\whisper_bench.py --backends cpu,vulkan --models base,small
  ```
- The i9-9900K has AVX2/FMA but **no AVX-512**. The CPU results show what a portable CPU build must at least match.

## Comparable runs

- Use the same `--ref`, `--models`, `--runs` and `--threads` across machines. The defaults are whisper.cpp commit `60c0be6a` (voice-client's proven build), models `base,small`, 5 runs.
- Add Indonesian/English clips with `--audio clip.wav` (16 kHz mono WAV). Use the same clips on every machine.
- Close heavy apps; plug laptops into power.

## Status of these scripts

- `hwprobe.py`: run on the M4 Pro (macOS 27.2). Not yet run on Linux or Windows.
- `whisper_bench.py`: compile-checked, and its log parsing tested on sample lines. **Not yet run end to end:** the coding agent's sandbox blocked building third-party code. The first real run on each machine is also the script's own test, so please report any crash with its output.
