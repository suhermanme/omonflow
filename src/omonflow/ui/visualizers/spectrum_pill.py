# Adapted from voice-client (https://github.com/suhermanme/voice-client),
# Copyright (c) 2026 suhermanme, MIT License.
"""Spectrum Pill: OmonFlow's default visualizer.

Visual design kept from voice-client (dark pill, state-coloured border,
15 rounded bars under a sine envelope, state label). Changes: the 16 px state
dot became the primary control button, the pill is wider so labels like
"Transcribing" fit, and a small cancel button sits at the top right.
"""

from __future__ import annotations

import math

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen

from omonflow.core.states import State
from .base import LABELS, STYLE, Frame, Visualizer

PILL_WIDTH = 312
PILL_HEIGHT = 72
BAR_COUNT = 15
BAR_WIDTH = 5
BAR_GAP = 4
BARS_X = 60
LABEL_X = BARS_X + BAR_COUNT * (BAR_WIDTH + BAR_GAP) + 8


class SpectrumPill(Visualizer):
    key = "spectrum-pill"
    title = "Spectrum Pill"

    def size(self) -> QSize:
        return QSize(PILL_WIDTH, PILL_HEIGHT)

    def control_rects(self, rect: QRectF) -> dict[str, QRectF]:
        cy = rect.center().y()
        return {
            "primary": QRectF(rect.left() + 14, cy - 18, 36, 36),
            "cancel": QRectF(rect.right() - 26, rect.top() + 8, 18, 18),
        }

    def paint(self, painter: QPainter, rect: QRectF, frame: Frame) -> None:
        rgb = STYLE[frame.state][0]
        painter.setPen(QPen(QColor(*rgb, 90), 1.5))
        painter.setBrush(QColor(18, 22, 31, 225))
        painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 22, 22)

        state_levels = {
            State.IDLE: 0.10,
            State.INITIALIZING: 0.22,
            State.CAPTURE_READY: max(0.15, frame.level),
            State.RECORDING: max(0.12, frame.level),
            State.SUCCESS: 0.72,
            State.ERROR: 0.12,
        }
        level = state_levels.get(frame.state, 0.48)  # processing states: THINKING level
        center_y = rect.center().y()
        painter.setPen(Qt.NoPen)
        for index in range(BAR_COUNT):
            position = index / max(1, BAR_COUNT - 1)
            envelope = math.sin(position * math.pi) * 0.65 + 0.35
            motion = (math.sin(frame.phase * 2.6 + index * 0.78) + 1.0) / 2.0
            if frame.state == State.ERROR:
                motion = 0.08
            elif frame.state in (State.CAPTURE_READY, State.RECORDING):
                motion = max(motion * 0.65, frame.level)
            height = 4 + level * envelope * (12 + motion * 24)
            x = rect.left() + BARS_X + index * (BAR_WIDTH + BAR_GAP)
            painter.setBrush(QColor(*rgb, 155 + int(motion * 90)))
            painter.drawRoundedRect(QRectF(x, center_y - height / 2, BAR_WIDTH, height), 2.5, 2.5)

        font = QFont(painter.font())
        font.setPixelSize(13)
        painter.setFont(font)
        painter.setPen(QColor(235, 239, 247, 230))
        painter.drawText(
            QRectF(rect.left() + LABEL_X, rect.top(), rect.width() - LABEL_X - 28, rect.height()),
            Qt.AlignVCenter | Qt.AlignLeft,
            frame.label or LABELS[frame.state],
        )
