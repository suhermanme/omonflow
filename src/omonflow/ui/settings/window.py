"""Settings window (M02: General, Appearance, About). Every control is live.

Closing the window only hides it; OmonFlow keeps running.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt, Signal
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

    def __init__(self, settings, diagnostics: dict, audio_info=None, whisper=None) -> None:
        super().__init__(None, Qt.Window)
        self.settings = settings
        self.setWindowTitle("OmonFlow Settings")
        self.setMinimumWidth(620)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._general(), "General")
        self.tabs.addTab(self._appearance(), "Appearance")
        self.audio_info = audio_info
        self.tabs.addTab(self._audio(), "Audio Input")
        self.whisper = whisper
        self.tabs.addTab(self._speech(), "Speech Recognition")
        self.about = QTextBrowser()
        self.about.setOpenExternalLinks(True)
        self.tabs.addTab(self.about, "About && Diagnostics")
        lay = QVBoxLayout(self)
        lay.addWidget(self.tabs)
        self.set_diagnostics(diagnostics)

    def show_tab(self, name: str) -> None:
        self.tabs.setCurrentIndex({"general": 0, "appearance": 1, "audio": 2, "speech": 3, "about": 4}.get(name, 0))
        if name == "audio":
            self.refresh_audio()
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
