"""Floating dictation widget: the primary dictation control (SPEC §12).

Visibility is independent of dictation state: show/hide never sends intents.
"""

from __future__ import annotations

import logging
import math
import sys
import time

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QToolTip, QWidget

from omonflow.core.states import State
from omonflow.ui import visualizers
from omonflow.ui.visualizers.base import LIVE_STATES, STYLE, Frame

LOG = logging.getLogger("omonflow.widget")

ACTIVE_FRAME_MS = 33    # ~30 fps while capturing or processing
DRAG_THRESHOLD = 4
# No animation at all in these states: repaint only on change (M02 idle-CPU
# measurement: even 10 fps idle cost 3.6-11 % of a core on an M4 Pro).
STATIC_STATES = frozenset({State.IDLE, State.ERROR})


class FloatingWidget(QWidget):
    intent = Signal(str)            # "primary" | "cancel"
    visualizer_error = Signal(str)  # message; saved preference is NOT changed
    moved = Signal(QPoint)

    def __init__(self, can_cancel=lambda: False, always_on_top: bool = True) -> None:
        super().__init__()
        self._can_cancel = can_cancel
        self.state = State.IDLE
        self.phase = 0.0
        self.level = 0.0
        self.has_transcript = False
        self.audio_kept = False
        self.label: str | None = None
        self.visualizer = visualizers.create(visualizers.DEFAULT)
        self.active_key = visualizers.DEFAULT
        self._press: tuple[QPointF, str | None] | None = None
        self._drag_offset: QPoint | None = None
        self._dragging = False
        self._last_tick = time.monotonic()
        self.frames_painted = 0
        self.platform_report: dict = {}

        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_MacAlwaysShowToolWindow, True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setMouseTracking(True)
        self.set_always_on_top(always_on_top)
        self.setFixedSize(self.visualizer.size())

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self._update_timer()

    # ------------------------------------------------------------ configuration
    def set_always_on_top(self, on: bool) -> None:
        flags = Qt.Tool | Qt.FramelessWindowHint | Qt.WindowDoesNotAcceptFocus
        if on:
            flags |= Qt.WindowStaysOnTopHint
        visible = self.isVisible()
        self.setWindowFlags(flags)
        if visible:
            self.show()  # setWindowFlags hides the window

    def set_visualizer(self, key: str) -> bool:
        """Switch immediately. On failure fall back and report; never touches settings."""
        try:
            vis = visualizers.create(key)
            self._probe(vis)
        except Exception as exc:  # noqa: BLE001 - any visualizer failure → safe fallback
            msg = f"Visualizer '{key}' failed ({exc!s}); using {visualizers.REGISTRY[visualizers.DEFAULT].title}."
            LOG.warning(msg, exc_info=True)
            self._use(visualizers.create(visualizers.DEFAULT), visualizers.DEFAULT)
            self.visualizer_error.emit(msg)
            return False
        self._use(vis, key)
        return True

    def _use(self, vis, key: str) -> None:
        self.visualizer, self.active_key = vis, key
        self.setFixedSize(vis.size())
        self.update()

    @staticmethod
    def _probe(vis) -> None:
        img = QImage(vis.size(), QImage.Format_ARGB32_Premultiplied)
        img.fill(0)
        p = QPainter(img)
        try:
            vis.paint(p, QRectF(img.rect()), Frame(State.RECORDING, 0.0, 0.5))
        finally:
            p.end()

    # ------------------------------------------------------------ state input
    def set_state(self, state: str) -> None:
        self.state = State(state)
        self.setAccessibleName(f"OmonFlow dictation: {self.state.value.replace('_', ' ').lower()}")
        self._update_timer()
        self.update()

    def set_level(self, level: float) -> None:
        level = max(0.0, min(float(level), 1.0))
        self.level = max(level, self.level * 0.65)

    def set_error_payload(self, has_transcript: bool, audio_kept: bool, label: str | None = None) -> None:
        self.has_transcript, self.audio_kept, self.label = has_transcript, audio_kept, label
        self.update()

    # ------------------------------------------------------------ animation
    def _update_timer(self) -> None:
        if not self.isVisible() or self.state in STATIC_STATES:
            self.timer.stop()
            return
        if not self.timer.isActive():
            self._last_tick = time.monotonic()
            self.timer.start(ACTIVE_FRAME_MS)

    def showEvent(self, e) -> None:
        super().showEvent(e)
        self._last_tick = time.monotonic()
        self._update_timer()

    def hideEvent(self, e) -> None:
        super().hideEvent(e)
        self.timer.stop()  # no painting while hidden; dictation is unaffected

    def _tick(self) -> None:
        now = time.monotonic()
        steps = (now - self._last_tick) / 0.016
        self._last_tick = now
        self.phase += STYLE[self.state][2] * steps
        self.level *= 0.92 ** steps
        self.update()

    # ------------------------------------------------------------ painting
    def paintEvent(self, event) -> None:
        del event
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(self.rect())
        try:
            label = self.label if self.state in (State.ERROR, State.SUCCESS) else None
            self.visualizer.paint(p, rect, Frame(self.state, self.phase, self.level, label))
        except Exception as exc:  # noqa: BLE001
            p.end()
            if self.active_key != visualizers.DEFAULT:
                self.set_visualizer_fallback(str(exc))
            else:
                LOG.exception("default visualizer failed to paint")
            return
        self._paint_controls(p, rect)
        p.end()
        self.frames_painted += 1

    def set_visualizer_fallback(self, reason: str) -> None:
        msg = f"Visualizer '{self.active_key}' stopped rendering ({reason}); using Spectrum Pill."
        LOG.warning(msg)
        self._use(visualizers.create(visualizers.DEFAULT), visualizers.DEFAULT)
        self.visualizer_error.emit(msg)

    def primary_action(self) -> str | None:
        s = self.state
        if s == State.IDLE:
            return "mic"
        if s in LIVE_STATES:
            return "stop"
        if s == State.SUCCESS:
            return "copy" if self.has_transcript else "done"
        if s == State.ERROR:
            return "copy" if self.has_transcript else ("retry" if self.audio_kept else "dismiss")
        return "busy"

    def cancel_visible(self) -> bool:
        return self.state == State.ERROR or self._can_cancel()

    def _paint_controls(self, p: QPainter, rect: QRectF) -> None:
        rects = self.visualizer.control_rects(rect)
        r = rects["primary"]
        rgb = STYLE[self.state][0]
        action = self.primary_action()
        if self.active_key == visualizers.DEFAULT:
            p.setPen(QPen(QColor(*rgb, 230), 2))
            p.setBrush(QColor(*rgb, 60 if action != "stop" else 200))
            p.drawEllipse(r)
        draw_glyph(p, r, action, self.phase)
        if self.cancel_visible():
            c = rects["cancel"]
            # Dark backing so the button reads on light and dark desktops alike.
            p.setPen(QPen(QColor(255, 255, 255, 70), 1))
            p.setBrush(QColor(18, 22, 31, 215))
            p.drawEllipse(c)
            pen = QPen(QColor(235, 239, 247, 220), 1.6)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            d = c.width() * 0.28
            cc = c.center()
            p.drawLine(QPointF(cc.x() - d, cc.y() - d), QPointF(cc.x() + d, cc.y() + d))
            p.drawLine(QPointF(cc.x() - d, cc.y() + d), QPointF(cc.x() + d, cc.y() - d))

    # ------------------------------------------------------------ input
    def hit(self, pos: QPointF) -> str | None:
        rects = self.visualizer.control_rects(QRectF(self.rect()))
        if self.cancel_visible() and rects["cancel"].adjusted(-4, -4, 4, 4).contains(pos):
            return "cancel"
        if rects["primary"].contains(pos) and self.primary_action() not in ("busy", "done"):
            return "primary"
        return None

    def mousePressEvent(self, e) -> None:
        LOG.debug("mouse press at %s (state %s)", e.position(), self.state.value)
        if e.button() == Qt.LeftButton:
            self._press = (e.position(), self.hit(e.position()))
            self._dragging = False
            self._drag_offset = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e) -> None:
        if self._press and e.buttons() & Qt.LeftButton:
            if not self._dragging and (e.position() - self._press[0]).manhattanLength() > DRAG_THRESHOLD:
                self._dragging = True
                handle = self.windowHandle()
                if sys.platform.startswith("linux") and handle is not None and handle.startSystemMove():
                    self._press = None  # compositor handles the move (Wayland)
                    return
            if self._dragging:
                self.move(e.globalPosition().toPoint() - self._drag_offset)
        else:
            target = self.hit(e.position())
            self.setCursor(Qt.PointingHandCursor if target else Qt.ArrowCursor)

    def mouseReleaseEvent(self, e) -> None:
        press, self._press = self._press, None
        LOG.debug("mouse release at %s, press target %s", e.position(), press and press[1])
        if press is None:
            return
        if self._dragging:
            self._dragging = False
            self.moved.emit(self.pos())
            return
        target = self.hit(e.position())
        if target and target == press[1]:
            self.intent.emit(target)

    def event(self, e) -> bool:
        if e.type() == e.Type.ToolTip:
            target = self.hit(e.position() if hasattr(e, "position") else QPointF(e.pos()))
            tips = {"cancel": "Dismiss" if self.state == State.ERROR else "Cancel",
                    "primary": {"mic": "Start dictation", "stop": "Stop and transcribe",
                                "copy": "Copy transcript", "retry": "Retry transcription",
                                "dismiss": "Dismiss"}.get(self.primary_action(), "")}
            if target:
                QToolTip.showText(e.globalPos(), tips[target], self)
            else:
                QToolTip.hideText()
            return True
        return super().event(e)


def draw_glyph(p: QPainter, r: QRectF, action: str | None, phase: float) -> None:
    c = r.center()
    s = min(r.width(), r.height())
    white = QColor(245, 247, 252, 235)
    pen = QPen(white, max(1.6, s * 0.06))
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    if action == "mic":
        w, h = s * 0.20, s * 0.34
        p.setBrush(white)
        p.drawRoundedRect(QRectF(c.x() - w / 2, c.y() - h * 0.75, w, h), w / 2, w / 2)
        p.setBrush(Qt.NoBrush)
        arc = QRectF(c.x() - s * 0.19, c.y() - s * 0.30, s * 0.38, s * 0.38)
        p.drawArc(arc, 200 * 16, 140 * 16)
        p.drawLine(QPointF(c.x(), c.y() + s * 0.08), QPointF(c.x(), c.y() + s * 0.20))
    elif action == "stop":
        d = s * 0.26
        p.setPen(Qt.NoPen)
        p.setBrush(white)
        p.drawRoundedRect(QRectF(c.x() - d / 2, c.y() - d / 2, d, d), 2.5, 2.5)
    elif action == "busy":
        arc = QRectF(c.x() - s * 0.2, c.y() - s * 0.2, s * 0.4, s * 0.4)
        p.drawArc(arc, int(-phase * 900) % 5760, 100 * 16)
    elif action == "done":
        path = QPainterPath(QPointF(c.x() - s * 0.16, c.y()))
        path.lineTo(QPointF(c.x() - s * 0.04, c.y() + s * 0.12))
        path.lineTo(QPointF(c.x() + s * 0.18, c.y() - s * 0.12))
        p.drawPath(path)
    elif action == "copy":
        d = s * 0.22
        p.drawRoundedRect(QRectF(c.x() - d * 0.75, c.y() - d * 0.45, d, d * 1.15), 2, 2)
        p.drawRoundedRect(QRectF(c.x() - d * 0.25, c.y() - d * 0.75, d, d * 1.15), 2, 2)
    elif action == "retry":
        arc = QRectF(c.x() - s * 0.18, c.y() - s * 0.18, s * 0.36, s * 0.36)
        p.drawArc(arc, 60 * 16, 280 * 16)
        tip = QPointF(c.x() + s * 0.18 * math.cos(math.radians(60)), c.y() - s * 0.18 * math.sin(math.radians(60)))
        p.drawLine(tip, tip + QPointF(s * 0.08, 0))
    elif action == "dismiss":
        d = s * 0.14
        p.drawLine(QPointF(c.x() - d, c.y() - d), QPointF(c.x() + d, c.y() + d))
        p.drawLine(QPointF(c.x() - d, c.y() + d), QPointF(c.x() + d, c.y() - d))
