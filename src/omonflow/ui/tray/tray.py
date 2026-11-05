"""System tray / menu bar integration (SPEC §13)."""

from __future__ import annotations

import sys

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QMenu, QSystemTrayIcon

from omonflow.core.states import CAPTURE_STATES, State

STATE_TEXT = {
    State.IDLE: "Idle",
    State.INITIALIZING: "Starting microphone…",
    State.CAPTURE_READY: "Listening",
    State.RECORDING: "Recording",
    State.FINALIZING: "Processing",
    State.TRANSCRIBING: "Transcribing",
    State.REFINING: "Refining",
    State.INSERTING: "Inserting",
    State.SUCCESS: "Done",
    State.ERROR: "Error",
}


def make_icon(dot: QColor | None = None) -> QIcon:
    """Pill-with-bars glyph. Monochrome template on macOS when no state dot."""
    pm = QPixmap(44, 44)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    ink = QColor(0, 0, 0) if sys.platform == "darwin" else QColor(230, 233, 240)
    p.setPen(Qt.NoPen)
    p.setBrush(ink)
    for i, h in enumerate((10, 18, 26, 18, 10)):
        x = 9 + i * 6
        p.drawRoundedRect(QRectF(x, 22 - h / 2, 4, h), 2, 2)
    if dot is not None:
        p.setBrush(dot)
        p.drawEllipse(QPointF(36, 10), 6, 6)
    p.end()
    icon = QIcon(pm)
    icon.setIsMask(dot is None and sys.platform == "darwin")
    return icon


class TrayController(QObject):
    toggle_widget = Signal()
    open_settings = Signal(str)  # tab name
    start = Signal()
    stop = Signal()
    cancel = Signal()
    quit = Signal()
    open_history = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.available = QSystemTrayIcon.isSystemTrayAvailable()
        self.icons = {
            "idle": make_icon(),
            "capture": make_icon(QColor(255, 72, 72)),
            "busy": make_icon(QColor(155, 95, 255)),
            "error": make_icon(QColor(255, 165, 65)),
        }
        self.tray = QSystemTrayIcon(self.icons["idle"])
        self.menu = QMenu()
        self.status_action = self.menu.addAction("OmonFlow: Idle")
        self.status_action.setEnabled(False)
        self.menu.addSeparator()
        self.widget_action = self._add("Hide Widget", self.toggle_widget.emit)
        self._add("Settings…", lambda: self.open_settings.emit("general"))
        self.menu.addSeparator()
        self.start_action = self._add("Start Dictation", self.start.emit)
        self.stop_action = self._add("Stop Dictation", self.stop.emit)
        self.cancel_action = self._add("Cancel", self.cancel.emit)
        self.menu.addSeparator()
        self._add("History…", self.open_history.emit)
        self._add("Diagnostics…", lambda: self.open_settings.emit("about"))
        self.menu.addSeparator()
        self._add("Quit OmonFlow", self.quit.emit)
        self.tray.setContextMenu(self.menu)
        self.tray.setToolTip("OmonFlow")
        self.set_state(State.IDLE.value, can_cancel=False)

    def _add(self, text: str, slot) -> QAction:
        a = self.menu.addAction(text)
        a.triggered.connect(slot)
        return a

    def show(self) -> None:
        if self.available:
            self.tray.show()

    def set_widget_visible(self, visible: bool) -> None:
        self.widget_action.setText("Hide Widget" if visible else "Show Widget")

    def set_state(self, state: str, can_cancel: bool) -> None:
        s = State(state)
        text = STATE_TEXT[s]
        self.status_action.setText(f"OmonFlow: {text}")
        self.tray.setToolTip(f"OmonFlow — {text}")
        if s in CAPTURE_STATES:
            key = "capture"
        elif s == State.ERROR:
            key = "error"
        elif s in (State.IDLE, State.SUCCESS):
            key = "idle"
        else:
            key = "busy"
        self.tray.setIcon(self.icons[key])
        self.start_action.setEnabled(s == State.IDLE)
        self.stop_action.setEnabled(s in (State.CAPTURE_READY, State.RECORDING))
        self.cancel_action.setEnabled(can_cancel)

    def notify(self, title: str, text: str, error: bool = False) -> None:
        if self.available and self.tray.supportsMessages():
            icon = QSystemTrayIcon.Critical if error else QSystemTrayIcon.Information
            self.tray.showMessage(title, text, icon, 5000)

    def hide(self) -> None:
        self.tray.hide()

    def dispose(self) -> None:
        """Ordered teardown: the tray icon must never outlive its menu."""
        self.tray.hide()
        self.tray.setContextMenu(None)
        self.tray.deleteLater()
        self.menu.deleteLater()
