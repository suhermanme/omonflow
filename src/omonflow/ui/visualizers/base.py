# Adapted from voice-client (https://github.com/suhermanme/voice-client),
# Copyright (c) 2026 suhermanme, MIT License.
"""Visualizer base class and the state → style mapping."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QRectF, QSize
from PySide6.QtGui import QPainter

from omonflow.core.states import State

# (rgb, orb base radius, phase speed per 16 ms) — voice-client's ORB_STATES,
# mapped onto dictation states.
_LISTENING = ((70, 175, 255), 41, 0.040)
_HEARING = ((255, 165, 65), 45, 0.085)
_THINKING = ((155, 95, 255), 43, 0.075)
STYLE = {
    State.IDLE: ((110, 120, 145), 36, 0.020),
    State.INITIALIZING: _LISTENING,
    State.CAPTURE_READY: ((70, 175, 255), 43, 0.055),
    State.RECORDING: _HEARING,
    State.FINALIZING: _THINKING,
    State.TRANSCRIBING: _THINKING,
    State.REFINING: _THINKING,
    State.INSERTING: _THINKING,
    State.SUCCESS: ((70, 245, 180), 46, 0.120),
    State.ERROR: ((255, 70, 85), 44, 0.030),
}

LABELS = {
    State.IDLE: "Ready",
    State.INITIALIZING: "Starting…",
    State.CAPTURE_READY: "Speak now",
    State.RECORDING: "Recording",
    State.FINALIZING: "Finishing",
    State.TRANSCRIBING: "Transcribing",
    State.REFINING: "Refining",
    State.INSERTING: "Inserting",
    State.SUCCESS: "Done",
    State.ERROR: "Error",
}

LIVE_STATES = frozenset({State.CAPTURE_READY, State.RECORDING})


@dataclass
class Frame:
    state: State
    phase: float
    level: float  # 0..1, smoothed microphone level
    label: str | None = None  # overrides the state label (e.g. "Copied — ⌘V")


class Visualizer:
    """Paints one style. Controls are painted by the widget at `control_rects`."""

    key = ""
    title = ""

    def size(self) -> QSize:
        raise NotImplementedError

    def paint(self, painter: QPainter, rect: QRectF, frame: Frame) -> None:
        raise NotImplementedError

    def control_rects(self, rect: QRectF) -> dict[str, QRectF]:
        """Hit areas: 'primary' (mic/stop/copy) and 'cancel'."""
        raise NotImplementedError
