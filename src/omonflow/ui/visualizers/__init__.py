"""Visualizer registry. Spectrum Pill is the default (SPEC §12)."""

from __future__ import annotations

from .base import Frame, Visualizer
from .circular_wave import CircularWave
from .orb import Orb
from .spectrum_pill import SpectrumPill

DEFAULT = SpectrumPill.key
REGISTRY: dict[str, type[Visualizer]] = {
    SpectrumPill.key: SpectrumPill,
    Orb.key: Orb,
    CircularWave.key: CircularWave,
}


def create(key: str) -> Visualizer:
    """Create a visualizer; raises KeyError for unknown keys (caller falls back)."""
    return REGISTRY[key]()


__all__ = ["DEFAULT", "REGISTRY", "Frame", "Visualizer", "create"]
