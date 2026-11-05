"""Performance profiles (SPEC §9.6): recommendations, never silent changes.

A profile proposes a model and backend for this machine. Nothing changes
until the user applies it; changing model or backend by hand makes the
profile "custom". Recommendations only use devices whisper.cpp reports.
"""

from __future__ import annotations

from dataclasses import dataclass

PROFILES = {
    "fast": "Fast: quickest response, smaller model",
    "balanced": "Balanced: good accuracy for Indonesian + English",
    "accurate": "Accurate: largest model this machine runs comfortably",
    "custom": "Custom: your own model and backend",
}


@dataclass
class Recommendation:
    profile: str
    model: str
    device_key: str   # "auto" or a backends.Device key
    reason: str


def recommend(profile: str, ram_mb: int | None, devices: list, unified_memory: bool = False) -> Recommendation | None:
    """devices: backends.Device list from whisper.cpp (CPU first)."""
    if profile == "custom":
        return None
    gpus = [d for d in devices if d.family != "cpu"]
    ram = ram_mb or 8192
    has_gpu = bool(gpus)
    if profile == "fast":
        model = "base" if not has_gpu else "small-q5_1"
        why = "small, quantized model" if has_gpu else "CPU only: the base model keeps latency low"
    elif profile == "balanced":
        model = "small"
        why = "the proven bilingual model (voice-client profile)"
    else:  # accurate
        if has_gpu and (ram >= 12000 or not unified_memory):
            model, why = "large-v3-turbo-q5_0", "near large-v3 accuracy; fast on this GPU"
        elif ram >= 12000:
            model, why = "medium-q5_0", "CPU only: medium is the largest practical model"
        else:
            model, why = "small", f"only {ram // 1024} GB RAM: larger models would not fit comfortably"
    device = "auto" if has_gpu else "cpu"
    where = f"on {gpus[0].label}" if has_gpu else "on the CPU (no GPU backend reported by whisper.cpp)"
    return Recommendation(profile, model, device, f"{why}, {where}")
