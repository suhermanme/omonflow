# Adapted from voice-client (https://github.com/suhermanme/voice-client),
# Copyright (c) 2026 suhermanme, MIT License.
"""Circular Wave: three overlapping waveforms around a dark disk.

The disk is the primary button. Performance changes vs voice-client (same
look): trigonometry tables are precomputed once, and each ring uses 160
points instead of 240 (M02 measurement: drawing cost ~23 % of a core at 30 fps
before this change).
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPolygonF, QRadialGradient

from omonflow.core.states import State
from .base import Frame, Visualizer

WINDOW_SIZE = 220
POINTS = 160
_ANGLES = [i / POINTS * math.tau for i in range(POINTS)]
_COS = [math.cos(a) for a in _ANGLES]
_SIN = [math.sin(a) for a in _ANGLES]


class CircularWave(Visualizer):
    key = "circular-wave"
    title = "Circular Wave"

    def size(self) -> QSize:
        return QSize(WINDOW_SIZE, WINDOW_SIZE)

    def control_rects(self, rect: QRectF) -> dict[str, QRectF]:
        c = rect.center()
        return {
            "primary": QRectF(c.x() - 30, c.y() - 30, 60, 60),
            "cancel": QRectF(rect.right() - 34, rect.top() + 16, 18, 18),
        }

    def paint(self, painter: QPainter, rect: QRectF, frame: Frame) -> None:
        live = 6.0 + frame.level * 11.0
        amplitude = {
            State.IDLE: 2.5,
            State.INITIALIZING: 5.0,
            State.CAPTURE_READY: live,
            State.RECORDING: live,
            State.SUCCESS: 12.0,
            State.ERROR: 3.5,
        }.get(frame.state, 8.0)
        cx, cy = rect.center().x(), rect.center().y()
        pulse = (math.sin(frame.phase * 1.6) + 1.0) / 2.0
        core_radius = 65 + pulse * 2

        core = QRadialGradient(cx - core_radius * 0.22, cy - core_radius * 0.25, core_radius * 1.3)
        core.setColorAt(0.0, QColor(70, 17, 105, 235))
        core.setColorAt(0.55, QColor(36, 7, 67, 238))
        core.setColorAt(1.0, QColor(8, 3, 22, 245))
        painter.setPen(Qt.NoPen)
        painter.setBrush(core)
        painter.drawEllipse(QPointF(cx, cy), core_radius, core_radius)

        if frame.state == State.ERROR:
            palette = (QColor(255, 58, 92), QColor(255, 126, 48), QColor(255, 54, 180))
        else:
            palette = (QColor(20, 245, 232), QColor(220, 38, 255), QColor(91, 104, 255))

        base_radius = 69
        painter.setBrush(Qt.NoBrush)
        painter.setCompositionMode(QPainter.CompositionMode_Plus)
        sin = math.sin
        for layer, wave_color in enumerate(palette):
            lobes = 5 + layer
            layer_phase = frame.phase * (1.05 + layer * 0.14)
            back = layer_phase * 0.63
            pts = []
            for a, ca, sa in zip(_ANGLES, _COS, _SIN):
                radius = base_radius + amplitude * (sin(lobes * a + layer_phase) * 0.72
                                                    + sin((lobes + 3) * a - back) * 0.28)
                pts.append(QPointF(cx + ca * radius, cy + sa * radius))
            path = QPainterPath()
            path.addPolygon(QPolygonF(pts))
            path.closeSubpath()

            glow_color = QColor(wave_color)
            glow_color.setAlpha(38)
            glow_pen = QPen(glow_color, 9)
            glow_pen.setJoinStyle(Qt.RoundJoin)
            painter.setPen(glow_pen)
            painter.drawPath(path)
            line_color = QColor(wave_color)
            line_color.setAlpha(220)
            line_pen = QPen(line_color, 1.8)
            line_pen.setJoinStyle(Qt.RoundJoin)
            painter.setPen(line_pen)
            painter.drawPath(path)
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)
