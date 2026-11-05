# Adapted from voice-client (https://github.com/suhermanme/voice-client),
# Copyright (c) 2026 suhermanme, MIT License.
"""Orb: softly pulsing sphere. The orb itself is the primary button."""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QRadialGradient

from .base import STYLE, Frame, Visualizer

WINDOW_SIZE = 220


class Orb(Visualizer):
    key = "orb"
    title = "Orb"

    def size(self) -> QSize:
        return QSize(WINDOW_SIZE, WINDOW_SIZE)

    def control_rects(self, rect: QRectF) -> dict[str, QRectF]:
        c = rect.center()
        return {
            "primary": QRectF(c.x() - 30, c.y() - 30, 60, 60),
            "cancel": QRectF(rect.right() - 34, rect.top() + 16, 18, 18),
        }

    def paint(self, painter: QPainter, rect: QRectF, frame: Frame) -> None:
        rgb, base_radius, _ = STYLE[frame.state]
        color = QColor(*rgb)
        cx, cy = rect.center().x(), rect.center().y()
        pulse = (math.sin(frame.phase) + 1.0) / 2.0
        radius = base_radius + pulse * 8 + frame.level * 6

        glow_radius = radius * 2.0
        glow = QRadialGradient(cx, cy, glow_radius)
        glow.setColorAt(0.0, QColor(*rgb, 185))
        glow.setColorAt(0.35, QColor(*rgb, 105))
        glow.setColorAt(1.0, QColor(*rgb, 0))
        painter.setPen(Qt.NoPen)
        painter.setBrush(glow)
        painter.drawEllipse(QPointF(cx, cy), glow_radius, glow_radius)

        inner = QRadialGradient(cx - radius * 0.25, cy - radius * 0.30, radius * 1.35)
        inner.setColorAt(0.0, QColor(255, 255, 255, 245))
        inner.setColorAt(0.20, QColor(min(color.red() + 70, 255), min(color.green() + 70, 255),
                                      min(color.blue() + 70, 255), 245))
        inner.setColorAt(0.70, color)
        inner.setColorAt(1.0, QColor(color.red() // 2, color.green() // 2, color.blue() // 2, 245))
        painter.setBrush(inner)
        painter.drawEllipse(QPointF(cx, cy), radius, radius)
