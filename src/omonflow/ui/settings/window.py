"""Settings window (M02: General, Appearance, About). Every control is live.

Closing the window only hides it; OmonFlow keeps running.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt, Signal, Slot
from PySide6.QtGui import QImage, QKeySequence, QPainter, QPixmap
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QAbstractItemView, QButtonGroup, QCheckBox, QComboBox, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QKeySequenceEdit, QLabel, QMessageBox, QProgressBar, QPushButton, QRadioButton, QSpinBox,
    QTableWidget, QTableWidgetItem, QTabWidget, QTextBrowser, QVBoxLayout, QWidget,
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


class SidebarTabs(QWidget):
    """Sidebar navigation (eight sections do not fit as tabs). Same API subset as QTabWidget."""

    currentChanged = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        from PySide6.QtWidgets import QListWidget, QStackedWidget

        self.list = QListWidget()
        self.list.setFixedWidth(190)
        self.stack = QStackedWidget()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.list)
        lay.addWidget(self.stack, 1)
        self.list.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.list.currentRowChanged.connect(self.currentChanged.emit)

    def addTab(self, widget: QWidget, title: str) -> int:  # noqa: N802 (Qt-style API)
        self.stack.addWidget(widget)
        self.list.addItem(title.replace("&&", "&"))
        if self.list.count() == 1:
            self.list.setCurrentRow(0)
        return self.list.count() - 1

    def setCurrentIndex(self, i: int) -> None:  # noqa: N802
        self.list.setCurrentRow(i)

    def currentIndex(self) -> int:  # noqa: N802
        return self.list.currentRow()


class SettingsWindow(QWidget):
    visualizer_selected = Signal(str)
    always_on_top_changed = Signal(bool)
    reset_position = Signal()
    shortcut_requested = Signal(str, bool)  # spec, enabled
    behavior_changed = Signal()             # simple boolean settings changed
    device_selected = Signal(object, object)  # (device id or None, name or None)
    timeout_changed = Signal(int)
    mic_test_requested = Signal()
    model_selected = Signal(str)
    engine_config_requested = Signal(str, str, int, str)  # (model, device key, threads, profile)
    unload_changed = Signal(int)
    request_accessibility = Signal()
    open_history = Signal()
    clear_history = Signal()

    def __init__(self, settings, diagnostics: dict, audio_info=None, whisper=None, insertion_info=None) -> None:
        super().__init__(None, Qt.Window)
        self.settings = settings
        self.setWindowTitle("OmonFlow Settings")
        self.setMinimumWidth(820)
        self.resize(900, 640)
        self.tabs = SidebarTabs()
        self.audio_info = audio_info
        self.whisper = whisper
        self.insertion_info = insertion_info
        self.about = QTextBrowser()
        self.about.setOpenExternalLinks(True)
        self._tab_index: dict[str, int] = {}
        for key, title, build in [
            ("general", "General", self._general), ("appearance", "Appearance", self._appearance),
            ("dictation", "Dictation", self._dictation), ("audio", "Audio Input", self._audio),
            ("speech", "Speech Recognition", self._speech), ("hardware", "Hardware & Acceleration", self._hardware),
            ("insertion", "Text Insertion", self._insertion),
            ("privacy", "Privacy && History", self._privacy), ("about", "About && Diagnostics", lambda: self.about),
        ]:
            self._tab_index[key] = self.tabs.addTab(build(), title)
        self.tabs.currentChanged.connect(self._tab_changed)
        lay = QVBoxLayout(self)
        lay.addWidget(self.tabs)
        self.set_diagnostics(diagnostics)

    def show_tab(self, name: str) -> None:
        self.tabs.setCurrentIndex(self._tab_index.get(name, 0))
        self._tab_changed(self.tabs.currentIndex())
        self.show()
        self.raise_()
        self.activateWindow()

    def _tab_changed(self, idx: int) -> None:
        key = next((k for k, i in self._tab_index.items() if i == idx), None)
        if key == "audio":
            self.refresh_audio()
        elif key == "hardware":
            self.refresh_hardware()
        elif key == "insertion":
            self.refresh_insertion()

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

    # ------------------------------------------------------------ Audio Input
    def _audio(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        status = QGroupBox("Status")
        g = QGridLayout(status)
        self.backend_label = QLabel()
        self.backend_label.setWordWrap(True)
        self.perm_label = QLabel()
        self.perm_button = QPushButton("Open Privacy Settings…")
        self.perm_button.clicked.connect(self._open_privacy)
        g.addWidget(QLabel("Audio engine:"), 0, 0)
        g.addWidget(self.backend_label, 0, 1, 1, 2)
        g.addWidget(QLabel("Permission:"), 1, 0)
        g.addWidget(self.perm_label, 1, 1)
        g.addWidget(self.perm_button, 1, 2)
        lay.addWidget(status)

        dev = QGroupBox("Microphone")
        d = QGridLayout(dev)
        self.device_combo = QComboBox()
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh_audio)
        self.device_details = QLabel()
        self.device_details.setStyleSheet("color: gray")
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(1000, 15000)
        self.timeout_spin.setSingleStep(500)
        self.timeout_spin.setSuffix(" ms")
        self.timeout_spin.setValue(self.settings.mic_timeout_ms)
        self.timeout_spin.valueChanged.connect(self.timeout_changed.emit)
        d.addWidget(self.device_combo, 0, 0, 1, 2)
        d.addWidget(refresh, 0, 2)
        d.addWidget(self.device_details, 1, 0, 1, 3)
        d.addWidget(QLabel("Give up opening the microphone after:"), 2, 0, 1, 2)
        d.addWidget(self.timeout_spin, 2, 2)
        note = QLabel("OmonFlow opens the microphone only when you click the microphone button, and closes it "
                      "as soon as you stop. Your system's microphone indicator is on exactly while it is open.")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray")
        d.addWidget(note, 3, 0, 1, 3)
        self.device_combo.activated.connect(self._device_chosen)
        lay.addWidget(dev)

        test = QGroupBox("Microphone test")
        t = QGridLayout(test)
        self.test_button = QPushButton("Test for 3 seconds")
        self.test_button.clicked.connect(self._start_test)
        self.test_level = QProgressBar()
        self.test_level.setRange(0, 100)
        self.test_level.setTextVisible(False)
        self.test_result = QLabel("Speak while the test runs. The audio is discarded immediately.")
        self.test_result.setWordWrap(True)
        self.startup_label = QLabel("No microphone starts yet in this session.")
        self.startup_label.setWordWrap(True)
        self.startup_label.setStyleSheet("color: gray")
        t.addWidget(self.test_button, 0, 0)
        t.addWidget(self.test_level, 0, 1)
        t.addWidget(self.test_result, 1, 0, 1, 2)
        t.addWidget(QLabel("<b>Recent microphone starts</b>"), 2, 0, 1, 2)
        t.addWidget(self.startup_label, 3, 0, 1, 2)
        lay.addWidget(test)
        lay.addStretch(1)
        self.refresh_audio()
        return page

    def refresh_audio(self) -> None:
        if self.audio_info is None:
            return
        info = self.audio_info()
        engine = "Native (Rust)" if info["backend"] == "rust" else info["backend"]
        if info["degraded"]:
            engine = f"<span style='color:#c0392b'>Python fallback (degraded)</span> — {info['fallback_reason'] or ''}"
        self.backend_label.setText(engine)
        perm = info["permission"]
        texts = {"authorized": "<span style='color:#2e9d5b'>allowed</span>",
                 "denied": "<span style='color:#c0392b'>denied</span> — turn it on in System Settings",
                 "restricted": "<span style='color:#c0392b'>restricted by policy</span>",
                 "not_determined": "not asked yet — macOS asks the first time you dictate"}
        self.perm_label.setText(texts.get(perm, "managed by the operating system") if perm else
                                "managed by the operating system")
        self.perm_button.setVisible(sys.platform == "darwin")
        self.device_combo.clear()
        self._devices = info["devices"]
        self.device_combo.addItem("System default", (None, None))
        current = 0
        for i, dev in enumerate(self._devices, start=1):
            label = dev["name"] + ("  (system default)" if dev.get("is_default") else "")
            self.device_combo.addItem(label, (dev["id"], dev["name"]))
            if (dev["id"] == info["selected_id"]) or (info["selected_id"] is None and dev["name"] == info["selected_name"]):
                current = i
        if info["selected_id"] and current == 0:
            self.device_combo.addItem(f"{info['selected_name'] or info['selected_id']} (not connected)",
                                      (info["selected_id"], info["selected_name"]))
            current = self.device_combo.count() - 1
        self.device_combo.setCurrentIndex(current)
        self._show_device_details()
        if info["error"]:
            self.device_details.setText(f"Could not list microphones: {info['error']}")

    def _show_device_details(self) -> None:
        idx = self.device_combo.currentIndex()
        if 1 <= idx <= len(self._devices):
            d = self._devices[idx - 1]
            self.device_details.setText(f"{d['sample_rate']} Hz · {d['channels']} channel(s) · {d['format']}")
        else:
            self.device_details.setText("Follows the system's default input device.")

    def _device_chosen(self, idx: int) -> None:
        dev_id, name = self.device_combo.itemData(idx)
        self._show_device_details()
        self.device_selected.emit(dev_id, name)

    def _open_privacy(self) -> None:
        from omonflow.platform.macos.audio import PRIVACY_URL

        QDesktopServices.openUrl(QUrl(PRIVACY_URL))

    def _start_test(self) -> None:
        self.test_button.setEnabled(False)
        self.test_result.setText("Opening the microphone… speak now.")
        self.mic_test_requested.emit()

    def show_test_level(self, level: float) -> None:
        self.test_level.setValue(int(level * 100))

    def show_test_result(self, r: dict) -> None:
        self.test_button.setEnabled(True)
        if not r.get("ok"):
            self.test_result.setText(f"<span style='color:#c0392b'>Test failed:</span> {r.get('error')}")
            return
        rec = r.get("recording", {})
        warn = " <b>Silent input: is the microphone muted?</b>" if r.get("silent_warning") else ""
        heard = "speech detected" if rec.get("activity") else "no speech detected"
        self.test_result.setText(
            f"OK — {r.get('device_name', '')}: ready in {r.get('click_to_ready_ms')} ms "
            f"(warm-up {r.get('warmup_ms')} ms), peak level {int(r.get('peak_level', 0) * 100)} %, {heard}, "
            f"{rec.get('duration_s', 0):.1f} s captured, {rec.get('dropped', 0)} samples dropped.{warn}")

    def show_startup_stats(self, entries: list, summary: dict) -> None:
        lines = []
        for e in entries[-5:]:
            if e["ok"]:
                lines.append(f"{e['time']}  {e.get('device_name', '')}: ready in {e.get('click_to_ready_ms')} ms "
                             f"(warm-up {e.get('warmup_ms')} ms)")
            else:
                lines.append(f"{e['time']}  failed: {e['error']}")
        lines.append(f"Median {summary['median_ready_ms']} ms, slowest {summary['max_ready_ms']} ms, "
                     f"{summary['failures']} failure(s) in {summary['runs']} start(s).")
        self.startup_label.setText("<br>".join(lines))

    # ------------------------------------------------------------ Speech Recognition
    def _speech(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        eng = QGroupBox("Engine (whisper.cpp)")
        g = QGridLayout(eng)
        self.engine_label = QLabel("Starting…")
        self.engine_label.setWordWrap(True)
        self.engine_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        restart = QPushButton("Restart engine")
        restart.clicked.connect(lambda: self.whisper and self.whisper.restart(self.settings.whisper_model))
        g.addWidget(self.engine_label, 0, 0)
        g.addWidget(restart, 0, 1, Qt.AlignTop)
        lay.addWidget(eng)

        box = QGroupBox("Models")
        v = QVBoxLayout(box)
        self.model_table = QTableWidget(0, 5)
        self.model_table.setHorizontalHeaderLabels(["Model", "Download", "Memory (approx.)", "Status", ""])
        self.model_table.verticalHeader().setVisible(False)
        self.model_table.setSelectionMode(QAbstractItemView.NoSelection)
        self.model_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        hh = self.model_table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in (1, 2, 3, 4):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        v.addWidget(self.model_table)
        row = QHBoxLayout()
        imp = QPushButton("Import a model file…")
        imp.clicked.connect(self._import_model)
        refresh = QPushButton("Refresh sizes and checksums")
        refresh.clicked.connect(lambda: self.whisper and self.whisper.refresh_catalog())
        row.addWidget(imp)
        row.addWidget(refresh)
        row.addStretch(1)
        v.addLayout(row)
        note = QLabel("Models are stored in your user data folder and checked against the checksums published "
                      "with whisper.cpp. Nothing is downloaded without your confirmation.")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray")
        v.addWidget(note)
        lay.addWidget(box, 1)
        self._progress_text: dict[str, str] = {}
        if self.whisper:
            self.whisper.status_changed.connect(self.show_engine_status)
            self.whisper.models_changed.connect(self.refresh_models)
            self.whisper.progress.connect(self._model_progress)
            self.whisper.task_done.connect(self._model_task_done)
            self.show_engine_status(self.whisper.snapshot())
            self.refresh_models()
        return page

    def show_engine_status(self, st: dict) -> None:
        state = st.get("state", "stopped")
        colors = {"ready": "#2e9d5b", "failed": "#c0392b", "unavailable": "#c0392b", "no_model": "#c0392b"}
        names = {"ready": "ready", "loading": "loading model…", "starting": "starting…", "restarting": "restarting…",
                 "stopped": "stopped", "failed": "failed", "unavailable": "not available", "no_model": "no model"}
        lines = [f"<b>Status:</b> <span style='color:{colors.get(state, '#888')}'>{names.get(state, state)}</span>"]
        if st.get("model"):
            lines.append(f"<b>Model:</b> {Path(st['model']).name}")
        if st.get("backend"):
            lines.append(f"<b>Backend in use:</b> {st['backend']} (reported by whisper.cpp)")
        if st.get("start_ms"):
            extra = f", last model switch {st['load_ms']:.0f} ms" if st.get("load_ms") else ""
            lines.append(f"<b>Start-up:</b> {st['start_ms']:.0f} ms{extra}")
        if st.get("executable"):
            ver = f" {st['version']}" if st.get("version") else ""
            lines.append(f"<b>Executable:</b> {st['executable']}{ver} ({st['source']})")
        if st.get("pid"):
            lines.append(f"<b>Process:</b> pid {st['pid']}, port {st['port']}, restarts {st.get('restarts', 0)}")
        if st.get("error"):
            lines.append(f"<span style='color:#c0392b'>{st['error']}</span>")
        self.engine_label.setText("<br>".join(lines))

    def refresh_models(self) -> None:
        if not self.whisper:
            return
        statuses = self.whisper.store.all_status()
        self.model_table.setRowCount(len(statuses))
        for r, st in enumerate(statuses):
            active = st.spec.id == self.settings.whisper_model
            name = st.spec.label + (" — in use" if active else "")
            if st.spec.note:
                name += f"\n{st.spec.note}"
            size = st.size or st.installed_size
            size_txt = f"{size / 1e6:,.0f} MB" if size else f"~{st.spec.approx_mb:,} MB"
            state_txt = {"not_installed": "not installed", "partial": f"partial ({st.partial_bytes / 1e6:,.0f} MB)",
                         "installed": "installed", "verified": "installed ✓ verified",
                         "corrupt": f"damaged: {st.detail}"}[st.state]
            state_txt = self._progress_text.get(st.spec.id, state_txt)
            for c, text in enumerate([name, size_txt, f"~{st.memory_mb:,} MB", state_txt]):
                item = QTableWidgetItem(text)
                if active:
                    f = item.font()
                    f.setBold(True)
                    item.setFont(f)
                self.model_table.setItem(r, c, item)
            stale = self.model_table.cellWidget(r, 4)
            if stale is not None:  # hide now; Qt deletes replaced cell widgets only later
                stale.hide()
            self.model_table.setCellWidget(r, 4, self._model_actions(st, active))
        self.model_table.resizeRowsToContents()
        for r in range(self.model_table.rowCount()):  # room for the buttons
            self.model_table.setRowHeight(r, max(self.model_table.rowHeight(r), 34))

    def _model_actions(self, st, active: bool) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(2, 0, 2, 0)
        mid = st.spec.id

        def btn(text, slot):
            b = QPushButton(text)
            b.clicked.connect(slot)
            h.addWidget(b)

        if self.whisper.busy(mid):
            btn("Cancel", lambda: self.whisper.cancel(mid))
        elif not st.installed:
            btn("Resume download" if st.state == "partial" else "Download…", lambda: self._confirm_download(st))
        else:
            if not active and st.state != "corrupt":
                btn("Use", lambda: self._use_model(mid))
            if st.state == "installed" and st.sha256:
                btn("Verify", lambda: self.whisper.verify(mid))
            if st.state == "corrupt":
                btn("Download again…", lambda: self._redownload(st))
            if active:
                h.addWidget(QLabel("in use"))
            else:
                btn("Remove", lambda: self._remove_model(mid))
        return w

    def _confirm_download(self, st) -> None:
        size = st.size or st.spec.approx_mb * 1e6
        exact = "" if st.size else " (approximate; exact size unknown while offline)"
        ok = QMessageBox.question(
            self, "Download model",
            f"Download {st.spec.label}?\n\nDownload size: {size / 1e6:,.0f} MB{exact}\n"
            f"Memory while in use: about {st.memory_mb:,} MB\n\nSource: huggingface.co/ggerganov/whisper.cpp",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
        if ok == QMessageBox.Yes:
            self.whisper.download(st.spec.id)

    def _redownload(self, st) -> None:
        self.whisper.store.remove(st.spec.id)
        self._confirm_download(st)

    def _use_model(self, model_id: str) -> None:
        self.model_selected.emit(model_id)
        self.refresh_models()

    def _remove_model(self, model_id: str) -> None:
        if QMessageBox.question(self, "Remove model", f"Delete the {model_id} model file?",
                                QMessageBox.Yes | QMessageBox.Cancel) != QMessageBox.Yes:
            return
        err = self.whisper.remove(model_id, self.settings.whisper_model)
        if err:
            QMessageBox.information(self, "Remove model", err)

    def _import_model(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import a whisper.cpp model", str(Path.home()),
                                              "whisper.cpp models (ggml-*.bin)")
        if path:
            self.whisper.import_file(Path(path))

    def _model_progress(self, model_id: str, task: str, fraction: float, rate: float) -> None:
        verb = {"download": "Downloading", "verify": "Checking", "import": "Importing"}[task]
        pct = f" {fraction * 100:.0f} %" if fraction >= 0 else ""
        speed = f" ({rate / 1e6:.1f} MB/s)" if rate > 0 else ""
        text = f"{verb}{pct}{speed}"
        if self._progress_text.get(model_id, "")[:len(verb) + 4] != text[:len(verb) + 4]:
            self._progress_text[model_id] = text
            self.refresh_models()

    def _model_task_done(self, model_id: str, task: str, ok: bool, message: str) -> None:
        self._progress_text.pop(model_id, None)
        if task != "engine" and not ok:
            QMessageBox.warning(self, "Model", message)
        self.refresh_models()

    # ------------------------------------------------------------ Hardware & Acceleration
    def _hardware(self) -> QWidget:
        from PySide6.QtWidgets import QScrollArea

        page = QWidget()
        lay = QVBoxLayout(page)
        hw = QGroupBox("This computer")
        hv = QVBoxLayout(hw)
        self.hw_label = QLabel("Detecting…")
        self.hw_label.setWordWrap(True)
        self.hw_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        hv.addWidget(self.hw_label)
        lay.addWidget(hw)

        be = QGroupBox("Speech recognition backends")
        bv = QVBoxLayout(be)
        self.backend_table = QTableWidget(0, 6)
        self.backend_table.setHorizontalHeaderLabels(["Backend", "Detected", "Available", "Validated", "Active", ""])
        self.backend_table.verticalHeader().setVisible(False)
        self.backend_table.setSelectionMode(QAbstractItemView.NoSelection)
        self.backend_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.backend_table.setWordWrap(True)
        self.backend_table.setMinimumHeight(150)
        hh = self.backend_table.horizontalHeader()
        for c in (0, 1, 2, 4, 5):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.Stretch)
        bv.addWidget(self.backend_table)
        note = QLabel("A GPU being present does not mean it is used. “Available” comes from whisper.cpp itself, "
                      "“Validated” from a benchmark on this computer, “Active” from the running engine’s own log.")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray")
        bv.addWidget(note)
        lay.addWidget(be)

        cfg = QGroupBox("Settings")
        g = QGridLayout(cfg)
        self.profile_combo = QComboBox()
        from omonflow.stt.profiles import PROFILES

        for key, text in PROFILES.items():
            self.profile_combo.addItem(text.split(":")[0], key)  # short name; description in the hint
        self.profile_combo.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.profile_combo.setCurrentIndex(max(0, self.profile_combo.findData(self.settings.profile)))
        self.profile_combo.currentIndexChanged.connect(self._show_recommendation)
        self.profile_hint = QLabel()
        self.profile_hint.setWordWrap(True)
        self.profile_apply = QPushButton("Apply profile…")
        self.profile_apply.clicked.connect(self._apply_profile)
        self.backend_combo = QComboBox()
        self.threads_spin = QSpinBox()
        self.threads_spin.setRange(0, os.cpu_count() or 64)
        self.threads_spin.setSpecialValueText("Automatic")
        self.threads_spin.setValue(self.settings.whisper_threads)
        apply_btn = QPushButton("Apply")
        apply_btn.clicked.connect(self._apply_backend)
        self.unload_combo = QComboBox()
        for minutes, text in ((0, "Never (fastest response)"), (5, "After 5 minutes"), (15, "After 15 minutes"),
                              (30, "After 30 minutes"), (60, "After 1 hour")):
            self.unload_combo.addItem(text, minutes)
        self.unload_combo.setCurrentIndex(max(0, self.unload_combo.findData(self.settings.unload_idle_min)))
        self.unload_combo.currentIndexChanged.connect(lambda: self.unload_changed.emit(self.unload_combo.currentData()))
        g.addWidget(QLabel("Profile:"), 0, 0)
        g.addWidget(self.profile_combo, 0, 1)
        g.addWidget(self.profile_apply, 0, 2)
        g.addWidget(self.profile_hint, 1, 1, 1, 2)
        g.addWidget(QLabel("Backend:"), 2, 0)
        g.addWidget(self.backend_combo, 2, 1)
        g.addWidget(QLabel("CPU threads:"), 3, 0)
        g.addWidget(self.threads_spin, 3, 1)
        g.addWidget(apply_btn, 3, 2)
        g.addWidget(QLabel("Unload the model when idle:"), 4, 0)
        g.addWidget(self.unload_combo, 4, 1)
        lay.addWidget(cfg)

        diag = QGroupBox("Runtime")
        dv = QVBoxLayout(diag)
        self.runtime_label = QLabel()
        self.runtime_label.setWordWrap(True)
        self.runtime_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        dv.addWidget(self.runtime_label)
        lay.addWidget(diag)
        lay.addStretch(1)
        from PySide6.QtWidgets import QSizePolicy

        for lbl in (self.hw_label, self.profile_hint, self.runtime_label, note):
            # Let long text wrap instead of widening the page past the window.
            lbl.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
            lbl.setMinimumWidth(200)
        self._hardware_info = None
        if self.whisper:
            self.whisper.backends_changed.connect(self.refresh_hardware)
            self.whisper.status_changed.connect(lambda _s: self._show_runtime())
            self.whisper.task_done.connect(self._backend_task_done)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)  # content wraps to the window width
        scroll.setWidget(page)
        return scroll

    def refresh_hardware(self) -> None:
        if self._hardware_info is None:
            import threading

            from omonflow.hardware.detect import detect

            def work():
                self._hardware_info = detect()
                from PySide6.QtCore import QMetaObject
                QMetaObject.invokeMethod(self, "_hardware_ready", Qt.QueuedConnection)

            threading.Thread(target=work, daemon=True).start()
            return
        self._render_hardware()

    @Slot()
    def _hardware_ready(self) -> None:
        self._render_hardware()

    def _render_hardware(self) -> None:
        h = self._hardware_info
        if h is None or not self.whisper:
            return
        gpus = "; ".join(f"{g.name}" + (f", {g.memory_mb // 1024} GB" if g.memory_mb else
                                         (", unified memory" if g.unified_memory else ""))
                         + (f" ({', '.join(g.apis)})" if g.apis else "") for g in h.gpus) or "none detected"
        perf = f", {h.perf_cores} performance" if h.perf_cores else ""
        self.hw_label.setText(
            f"<b>{h.os} {h.os_version}</b> ({h.arch})<br>"
            f"<b>CPU:</b> {h.cpu_model or 'unknown'} — {h.physical_cores or '?'} cores{perf}, "
            f"{h.logical_cpus} threads<br><b>CPU features:</b> {', '.join(h.cpu_features) or '—'}<br>"
            f"<b>Memory:</b> {round((h.ram_mb or 0) / 1024)} GB<br><b>GPU:</b> {gpus}"
            + (f"<br><span style='color:gray'>{'; '.join(h.notes)}</span>" if h.notes else ""))
        self._render_backends()
        self._show_recommendation()
        self._show_runtime()

    def _detected_for(self, family: str) -> str:
        h = self._hardware_info
        if family == "cpu":
            return h.cpu_model or "yes" if h else "yes"
        if not h:
            return "?"
        api = {"metal": "Metal", "cuda": "CUDA", "vulkan": "Vulkan"}.get(family, family)
        match = [g.name for g in h.gpus if any(a.startswith(api) for a in g.apis)]
        return ", ".join(match) if match else "no"

    def _render_backends(self) -> None:
        from omonflow.stt.backends import LABELS

        w = self.whisper
        snap = w.snapshot()
        active = (snap.get("backend") or "").lower() if snap.get("state") == "ready" else ""
        rows = [(d.key, d.family, d.label, True) for d in w.devices]
        families = {d.family for d in w.devices}
        for fam in ("metal", "cuda", "vulkan"):  # detected hardware this whisper.cpp build cannot use
            if fam not in families and self._detected_for(fam) not in ("no", "?"):
                rows.append((None, fam, LABELS[fam], False))
        self.backend_table.setRowCount(len(rows))
        for r, (key, fam, label, available) in enumerate(rows):
            v = w.validation(key) if key else None
            val = "—" if v is None else (("✓ " if v.ok else "✗ ") + v.summary)
            is_active = bool(active) and (active.startswith("cpu") if fam == "cpu" else
                                          LABELS.get(fam, fam).split()[-1].lower() in active)
            cells = [label, self._detected_for(fam),
                     "yes" if available else "no (not in this build)",
                     val, "● in use" if is_active else ""]
            for c, text in enumerate(cells):
                self.backend_table.setItem(r, c, QTableWidgetItem(text))
            stale = self.backend_table.cellWidget(r, 5)
            if stale is not None:  # Qt deletes replaced cell widgets only later
                stale.hide()
                self.backend_table.removeCellWidget(r, 5)
            if key:
                b = QPushButton("Validate")
                b.setToolTip("Benchmark with the bundled 11 s sample in a separate process")
                b.clicked.connect(lambda _=False, k=key: self._validate(k))
                self.backend_table.setCellWidget(r, 5, b)
        self.backend_table.resizeRowsToContents()
        self.backend_combo.blockSignals(True)
        self.backend_combo.clear()
        self.backend_combo.addItem("Automatic (recommended)", "auto")
        for d in w.devices:
            self.backend_combo.addItem(d.label, d.key)
        self.backend_combo.setCurrentIndex(max(0, self.backend_combo.findData(self.settings.whisper_backend)))
        self.backend_combo.blockSignals(False)

    def _validate(self, key: str) -> None:
        self.runtime_label.setText(self.runtime_label.text() + f"<br><i>Validating {key}… (separate process)</i>")
        self.whisper.validate(key, self.settings.whisper_model)

    def _recommendation(self):
        from omonflow.stt.profiles import recommend

        h = self._hardware_info
        return recommend(self.profile_combo.currentData(), h.ram_mb if h else None, self.whisper.devices,
                         bool(h and any(g.unified_memory for g in h.gpus)))

    def _show_recommendation(self) -> None:
        if not self.whisper:
            return
        rec = self._recommendation()
        from omonflow.stt.profiles import PROFILES

        desc = PROFILES[self.profile_combo.currentData()].split(": ", 1)[1]
        if rec is None:
            self.profile_hint.setText(f"{desc}. Your own choices below are kept as they are.")
            self.profile_apply.setEnabled(False)
            return
        st = self.whisper.store.status(rec.model)
        dl = "" if st.installed else f" — needs a download of about {(st.size or st.spec.approx_mb * 1e6) / 1e6:,.0f} MB"
        cur = (self.settings.whisper_model == rec.model and self.settings.whisper_backend == rec.device_key)
        self.profile_hint.setText(f"<i>{desc}.</i><br>Recommends <b>{rec.model}</b> with backend <b>{rec.device_key}</b>: "
                                  f"{rec.reason}{dl}." + (" <i>(current settings)</i>" if cur else ""))
        self.profile_apply.setEnabled(not cur)

    def _apply_profile(self) -> None:
        rec = self._recommendation()
        if rec is None:
            return
        st = self.whisper.store.status(rec.model)
        if not st.installed:
            self._confirm_download(st)  # the profile can be applied once the download has finished
            return
        if QMessageBox.question(self, "Apply profile",
                                f"Switch to {rec.model} on {rec.device_key}?\n\nThe current engine keeps running "
                                "until the new one has started and passed a quick test.",
                                QMessageBox.Yes | QMessageBox.Cancel) == QMessageBox.Yes:
            self.engine_config_requested.emit(rec.model, rec.device_key, self.settings.whisper_threads, rec.profile)

    def _apply_backend(self) -> None:
        self.engine_config_requested.emit(self.settings.whisper_model, self.backend_combo.currentData(),
                                          self.threads_spin.value(), "custom")

    def _backend_task_done(self, subject: str, task: str, ok: bool, message: str) -> None:
        if task == "validate":
            if not ok:
                QMessageBox.warning(self, "Backend validation", f"{subject}: {message}")
            self._render_backends()
        elif task == "engine" and not ok and self.isVisible():
            box = QMessageBox(QMessageBox.Warning, "Speech recognition", message + "\n\nThe previous engine "
                              "is still running with your previous settings.", QMessageBox.Ok, self)
            cpu = box.addButton("Use CPU instead", QMessageBox.ActionRole)
            box.exec()
            if box.clickedButton() is cpu:
                self.engine_config_requested.emit(self.settings.whisper_model, "cpu", self.settings.whisper_threads,
                                                  "custom")
        self._show_runtime()

    def _show_runtime(self) -> None:
        if not self.whisper:
            return
        s = self.whisper.snapshot()
        lt = s.get("last_transcription")
        sel = s.get("selected") or {}
        lines = [
            f"<b>Selected:</b> model {self.settings.whisper_model}, backend {self.settings.whisper_backend}, "
            f"threads {self.settings.whisper_threads or 'automatic'}, profile {self.settings.profile}",
            f"<b>Running:</b> {s.get('state')} — model {Path(s['model']).name if s.get('model') else '—'}, "
            f"backend {s.get('backend') or '—'} (device setting {sel.get('device', '—')})",
        ]
        if lt:
            lines.append(f"<b>Last transcription ({lt['at']}):</b> {lt['ms']:.0f} ms for {lt['audio_s']:.1f} s of audio"
                         + (f" = {lt['rtf']:.3f}× real time" if lt.get("rtf") else "")
                         + f" ({lt['model']}, {lt['backend']})")
        if s.get("error"):
            lines.append(f"<span style='color:#c0392b'>{s['error']}</span>")
        self.runtime_label.setText("<br>".join(lines))

    # ------------------------------------------------------------ Dictation
    def _dictation(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        how = QGroupBox("How to dictate")
        hv = QVBoxLayout(how)
        t = QLabel("1. Show the widget (shortcut or menu).  2. Click the microphone.  3. Speak when it shows "
                   "<i>Speak now</i>.  4. Click Stop. The text is typed into the app you were using.<br>"
                   "Cancel (✕) discards the recording. Nothing is recorded until you click the microphone.")
        t.setWordWrap(True)
        hv.addWidget(t)
        lay.addWidget(how)
        rec = QGroupBox("Recognition")
        g = QGridLayout(rec)
        self.lang_combo = QComboBox()
        for code, label in (("auto", "Detect automatically (Indonesian or English)"), ("en", "English"),
                            ("id", "Indonesian (Bahasa Indonesia)")):
            self.lang_combo.addItem(label, code)
        self.lang_combo.setCurrentIndex(max(0, self.lang_combo.findData(self.settings.language)))
        self.lang_combo.currentIndexChanged.connect(self._lang_changed)
        self.prompt_check = QCheckBox("Help recognition of mixed Indonesian–English and technical words "
                                      "(GPU, Docker, Linux, …)")
        self.prompt_check.setChecked(self.settings.bilingual_prompt)
        self.prompt_check.toggled.connect(self._prompt_changed)
        g.addWidget(QLabel("Language:"), 0, 0)
        g.addWidget(self.lang_combo, 0, 1)
        g.addWidget(self.prompt_check, 1, 0, 1, 2)
        lay.addWidget(rec)
        lay.addStretch(1)
        return page

    def _lang_changed(self) -> None:
        self.settings.language = self.lang_combo.currentData()
        self.behavior_changed.emit()

    def _prompt_changed(self, on: bool) -> None:
        self.settings.bilingual_prompt = on
        self.behavior_changed.emit()

    # ------------------------------------------------------------ Text Insertion
    def _insertion(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        perm = QGroupBox("Permission")
        g = QGridLayout(perm)
        self.ax_label = QLabel()
        self.ax_label.setWordWrap(True)
        ask = QPushButton("Ask macOS for access…")
        ask.clicked.connect(self.request_accessibility.emit)
        open_btn = QPushButton("Open Privacy Settings…")
        open_btn.clicked.connect(self._open_accessibility)
        g.addWidget(self.ax_label, 0, 0, 1, 3)
        g.addWidget(ask, 1, 0)
        g.addWidget(open_btn, 1, 1)
        for w in (ask, open_btn):
            w.setVisible(sys.platform == "darwin")
        lay.addWidget(perm)
        how = QGroupBox("Method")
        v = QVBoxLayout(how)
        self.strategy_group = QButtonGroup(self)
        options = [("auto", "Automatic (recommended): insert directly where the app supports it, otherwise paste"),
                   ("paste", "Always paste (via the clipboard)"),
                   ("type", "Type the text (single lines; multi-line text is pasted)")]
        for key, text in options:
            rb = QRadioButton(text)
            rb.setChecked(self.settings.insertion_strategy == key)
            rb.toggled.connect(lambda on, k=key: on and self._strategy(k))
            self.strategy_group.addButton(rb)
            v.addWidget(rb)
        self.restore_check = QCheckBox("Restore my clipboard after pasting")
        self.restore_check.setChecked(self.settings.restore_clipboard)
        self.restore_check.toggled.connect(self._restore_changed)
        v.addWidget(self.restore_check)
        lay.addWidget(how)
        diag = QGroupBox("Last insertion")
        dv = QVBoxLayout(diag)
        self.insert_diag = QLabel("No dictation yet in this session.")
        self.insert_diag.setWordWrap(True)
        dv.addWidget(self.insert_diag)
        note = QLabel("Some apps don't accept typed or pasted text (password fields, apps running as administrator, "
                      "terminals with Secure Keyboard Entry). When insertion fails, the text is kept: click the "
                      "widget to copy it, or find it in History.")
        note.setWordWrap(True)
        note.setStyleSheet("color: gray")
        dv.addWidget(note)
        lay.addWidget(diag)
        lay.addStretch(1)
        return page

    def refresh_insertion(self) -> None:
        if self.insertion_info is None:
            return
        info = self.insertion_info()
        ax = info.get("accessibility")
        if ax is True:
            txt = "<span style='color:#2e9d5b'>Accessibility access granted</span> — OmonFlow can insert text."
        elif ax is False:
            txt = ("<span style='color:#c0392b'>Accessibility access is off.</span> Without it OmonFlow can only copy "
                   "the text for you to paste. Turn on OmonFlow in System Settings → Privacy & Security → "
                   "Accessibility.")
        else:
            txt = "Automatic insertion is not available on this platform yet; text is copied for you to paste."
        if info.get("secure_input"):
            txt += "<br><b>Secure input is active right now</b> (a password field or secure terminal)."
        self.ax_label.setText(txt)
        last = info.get("last")
        if last:
            verified = {True: "verified", False: "not visible in the app", None: "not verifiable"}[last["verified"]]
            self.insert_diag.setText(
                f"{last['outcome']} via {last['method'] or '—'} ({verified}), {last['ms']:.0f} ms"
                + (f"<br><span style='color:#c0392b'>{last['error']}</span>" if last.get("error") else ""))

    def _strategy(self, key: str) -> None:
        self.settings.insertion_strategy = key
        self.behavior_changed.emit()

    def _restore_changed(self, on: bool) -> None:
        self.settings.restore_clipboard = on
        self.behavior_changed.emit()

    def _open_accessibility(self) -> None:
        from omonflow.platform.macos.insert import ACCESSIBILITY_URL

        QDesktopServices.openUrl(QUrl(ACCESSIBILITY_URL))

    # ------------------------------------------------------------ Privacy & History
    def _privacy(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        hist = QGroupBox("History")
        v = QVBoxLayout(hist)
        self.history_check = QCheckBox("Keep a history of my dictations (text only, on this Mac)")
        self.history_check.setChecked(self.settings.history_enabled)
        self.history_check.toggled.connect(self._history_toggled)
        row = QHBoxLayout()
        open_btn = QPushButton("Open History…")
        open_btn.clicked.connect(self.open_history.emit)
        clear = QPushButton("Delete all history…")
        clear.clicked.connect(self._confirm_clear)
        row.addWidget(open_btn)
        row.addWidget(clear)
        row.addStretch(1)
        v.addWidget(self.history_check)
        v.addLayout(row)
        lay.addWidget(hist)
        info = QGroupBox("What OmonFlow stores and sends")
        iv = QVBoxLayout(info)
        text = QLabel(
            "• The microphone is used only between your click on the microphone and Stop.<br>"
            "• Audio is processed on this computer by whisper.cpp and discarded right after transcription. "
            "It is kept in memory only while a failed transcription waits for Retry.<br>"
            "• Nothing is uploaded. There is no telemetry.<br>"
            "• History stores text only, in your user data folder; you can delete entries or everything.<br>"
            "• Logs never contain audio or what you said.")
        text.setWordWrap(True)
        iv.addWidget(text)
        lay.addWidget(info)
        lay.addStretch(1)
        return page

    def _history_toggled(self, on: bool) -> None:
        self.settings.history_enabled = on
        self.behavior_changed.emit()

    def _confirm_clear(self) -> None:
        if QMessageBox.question(self, "Delete history", "Delete all dictation history? This cannot be undone.",
                                QMessageBox.Yes | QMessageBox.Cancel) == QMessageBox.Yes:
            self.clear_history.emit()

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
