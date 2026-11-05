"""Settings window (M02: General, Appearance, About). Every control is live.

Closing the window only hides it; OmonFlow keeps running.
"""

from __future__ import annotations

import sys

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QImage, QKeySequence, QPainter, QPixmap
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QGridLayout, QGroupBox, QHBoxLayout, QKeySequenceEdit, QLabel,
    QPushButton, QRadioButton, QTabWidget, QTextBrowser, QVBoxLayout, QWidget,
)

import omonflow
from omonflow.core.states import State
from omonflow.shortcuts.service import ShortcutStatus, from_qt, pretty
from omonflow.storage.settings import default_shortcut
from omonflow.ui import visualizers
from omonflow.ui.visualizers.base import Frame

STATUS_COLORS = {
    ShortcutStatus.REGISTERED.value: "#2e9d5b",
    ShortcutStatus.DISABLED.value: "#888888",
}


def render_preview(key: str, states=(State.IDLE, State.RECORDING, State.TRANSCRIBING)) -> QPixmap:
    vis = visualizers.create(key)
    w, h = vis.size().width(), vis.size().height()
    gap = 12
    img = QImage(w * len(states) + gap * (len(states) - 1), h, QImage.Format_ARGB32_Premultiplied)
    img.fill(0)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    for i, st in enumerate(states):
        vis.paint(p, QRectF(i * (w + gap), 0, w, h), Frame(st, 1.3 + i, 0.6))
    p.end()
    pm = QPixmap.fromImage(img)
    return pm.scaled(QSize(400, 90), Qt.KeepAspectRatio, Qt.SmoothTransformation)


class SettingsWindow(QWidget):
    visualizer_selected = Signal(str)
    always_on_top_changed = Signal(bool)
    reset_position = Signal()
    shortcut_requested = Signal(str, bool)  # spec, enabled
    behavior_changed = Signal()             # simple boolean settings changed

    def __init__(self, settings, diagnostics: dict) -> None:
        super().__init__(None, Qt.Window)
        self.settings = settings
        self.setWindowTitle("OmonFlow Settings")
        self.setMinimumWidth(620)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._general(), "General")
        self.tabs.addTab(self._appearance(), "Appearance")
        self.about = QTextBrowser()
        self.about.setOpenExternalLinks(True)
        self.tabs.addTab(self.about, "About && Diagnostics")
        lay = QVBoxLayout(self)
        lay.addWidget(self.tabs)
        self.set_diagnostics(diagnostics)

    def show_tab(self, name: str) -> None:
        self.tabs.setCurrentIndex({"general": 0, "appearance": 1, "about": 2}.get(name, 0))
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, e) -> None:
        e.ignore()
        self.hide()

    # ------------------------------------------------------------ General
    def _general(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)

        box = QGroupBox("Show/hide widget shortcut")
        g = QGridLayout(box)
        self.sc_enabled = QCheckBox("Enable global shortcut")
        self.sc_enabled.setChecked(self.settings.shortcut_enabled)
        self.sc_current = QLabel()
        self.sc_edit = QKeySequenceEdit()
        self.sc_edit.setMaximumSequenceLength(1)
        self.sc_apply = QPushButton("Use this shortcut")
        self.sc_reset = QPushButton(f"Reset to default ({pretty(default_shortcut())})")
        self.sc_status = QLabel()
        self.sc_status.setWordWrap(True)
        g.addWidget(self.sc_enabled, 0, 0, 1, 3)
        g.addWidget(QLabel("Current:"), 1, 0)
        g.addWidget(self.sc_current, 1, 1, 1, 2)
        g.addWidget(QLabel("New:"), 2, 0)
        g.addWidget(self.sc_edit, 2, 1)
        g.addWidget(self.sc_apply, 2, 2)
        g.addWidget(self.sc_reset, 3, 1, 1, 2)
        g.addWidget(self.sc_status, 4, 0, 1, 3)
        note = QLabel("The shortcut only shows or hides the widget. It never starts recording; "
                      "click the microphone on the widget (or use the menu) to dictate.")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray")
        g.addWidget(note, 5, 0, 1, 3)
        self.sc_apply.clicked.connect(self._apply_shortcut)
        self.sc_reset.clicked.connect(lambda: self.shortcut_requested.emit(default_shortcut(), self.sc_enabled.isChecked()))
        self.sc_enabled.toggled.connect(lambda on: self.shortcut_requested.emit(self.settings.shortcut, on))
        lay.addWidget(box)

        beh = QGroupBox("Behavior")
        v = QVBoxLayout(beh)
        self.show_on_launch = QCheckBox("Show the floating widget when OmonFlow starts")
        self.show_on_launch.setChecked(self.settings.widget_visible_on_launch)
        self.notifications = QCheckBox("Show notifications for errors and copied text")
        self.notifications.setChecked(self.settings.notifications)
        tray_name = "menu bar" if sys.platform == "darwin" else "system tray"
        close_note = QLabel(f"Closing this window keeps OmonFlow running in the {tray_name}. "
                            "Use Quit in its menu to exit.")
        close_note.setWordWrap(True)
        close_note.setStyleSheet("color: gray")
        for w in (self.show_on_launch, self.notifications, close_note):
            v.addWidget(w)
        self.show_on_launch.toggled.connect(self._behavior)
        self.notifications.toggled.connect(self._behavior)
        lay.addWidget(beh)
        lay.addStretch(1)
        return page

    def _apply_shortcut(self) -> None:
        seq: QKeySequence = self.sc_edit.keySequence()
        if seq.isEmpty():
            self.show_shortcut_status("invalid shortcut", "Press a key combination first.")
            return
        spec = from_qt(seq[0])
        if spec is None:
            self.show_shortcut_status(
                "invalid shortcut",
                "Use at least one modifier plus a letter, digit, Space, F-key or punctuation key.")
            return
        self.shortcut_requested.emit(spec, self.sc_enabled.isChecked())

    def show_shortcut_status(self, status: str, message: str) -> None:
        self.sc_current.setText(f"<b>{pretty(self.settings.shortcut)}</b>")
        color = STATUS_COLORS.get(status, "#c0392b")
        self.sc_status.setText(f"<span style='color:{color}'><b>Status: {status}</b></span> — {message}")

    def _behavior(self) -> None:
        self.settings.widget_visible_on_launch = self.show_on_launch.isChecked()
        self.settings.notifications = self.notifications.isChecked()
        self.behavior_changed.emit()

    # ------------------------------------------------------------ Appearance
    def _appearance(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        box = QGroupBox("Floating widget style")
        v = QVBoxLayout(box)
        self.vis_group = QButtonGroup(self)
        self.vis_buttons: dict[str, QRadioButton] = {}
        for key, cls in visualizers.REGISTRY.items():
            row = QHBoxLayout()
            title = cls.title + (" (default)" if key == visualizers.DEFAULT else "")
            rb = QRadioButton(title)
            self.vis_group.addButton(rb)
            self.vis_buttons[key] = rb
            prev = QLabel()
            try:
                prev.setPixmap(render_preview(key))
            except Exception as exc:  # noqa: BLE001
                prev.setText(f"(preview unavailable: {exc})")
            row.addWidget(rb, 0)
            row.addWidget(prev, 1)
            v.addLayout(row)
            rb.toggled.connect(lambda on, k=key: on and self.visualizer_selected.emit(k))
        self.vis_error = QLabel()
        self.vis_error.setStyleSheet("color: #c0392b")
        self.vis_error.setWordWrap(True)
        v.addWidget(self.vis_error)
        lay.addWidget(box)
        self.select_visualizer(self.settings.visualizer)

        pos = QGroupBox("Position and stacking")
        h = QVBoxLayout(pos)
        self.on_top = QCheckBox("Keep the widget above other windows")
        self.on_top.setChecked(self.settings.always_on_top)
        self.on_top.toggled.connect(self.always_on_top_changed.emit)
        reset = QPushButton("Move widget back to the default position")
        reset.clicked.connect(self.reset_position.emit)
        h.addWidget(self.on_top)
        h.addWidget(reset)
        lay.addWidget(pos)
        lay.addStretch(1)
        return page

    def select_visualizer(self, key: str) -> None:
        rb = self.vis_buttons.get(key) or self.vis_buttons[visualizers.DEFAULT]
        rb.blockSignals(True)
        rb.setChecked(True)
        rb.blockSignals(False)

    def show_visualizer_error(self, text: str) -> None:
        self.vis_error.setText(text)

    # ------------------------------------------------------------ About
    def set_diagnostics(self, d: dict) -> None:
        rows = "".join(f"<tr><td><b>{k}</b></td><td>{v}</td></tr>" for k, v in d.items())
        self.about.setHtml(f"""
<h2>OmonFlow {omonflow.__version__}</h2>
<p><i>Speak naturally. Type everywhere.</i></p>
<p><b>Development build (milestone M02).</b> The microphone, transcription and text
insertion are stubs: the widget shows the full dictation flow, but no audio is
captured and no text is typed into other apps.</p>
<h3>Diagnostics</h3><table cellspacing=4>{rows}</table>
<h3>Licenses and attribution</h3>
<p>OmonFlow is released under the MIT License.<br>
The Spectrum Pill, Orb and Circular Wave visualizers are adapted from
<a href="https://github.com/suhermanme/voice-client">voice-client</a>,
Copyright (c) 2026 suhermanme, MIT License.<br>
Qt for Python (PySide6): LGPLv3. global-hotkey: MIT/Apache-2.0. PyO3: MIT/Apache-2.0.
platformdirs: MIT. PyObjC: MIT.</p>
""")
