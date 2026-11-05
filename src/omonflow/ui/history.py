"""History window: search, copy, delete one, clear all (SPEC §15)."""

from __future__ import annotations

import time

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)


class HistoryWindow(QWidget):
    def __init__(self, history_provider, settings) -> None:
        super().__init__(None, Qt.Window)
        self.history_provider = history_provider
        self.settings = settings
        self.setWindowTitle("OmonFlow History")
        self.resize(720, 480)
        lay = QVBoxLayout(self)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search…")
        self.search.textChanged.connect(self.refresh)
        self.status = QLabel()
        self.status.setStyleSheet("color: gray")
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["When", "Text", "App", "Result"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setWordWrap(True)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        for c in (0, 2, 3):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.table.doubleClicked.connect(self.copy_selected)
        row = QHBoxLayout()
        for text, slot in (("Copy", self.copy_selected), ("Delete", self.delete_selected), ("Delete all…", self.clear)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            row.addWidget(b)
        row.addStretch(1)
        lay.addWidget(self.search)
        lay.addWidget(self.status)
        lay.addWidget(self.table, 1)
        lay.addLayout(row)
        self._ids: list[int] = []

    def refresh(self) -> None:
        h = self.history_provider()
        if h is None:
            self.status.setText("History is turned off (Settings → Privacy & History).")
            self.table.setRowCount(0)
            return
        entries = h.list(self.search.text())
        self.status.setText(f"{len(entries)} entr{'y' if len(entries) == 1 else 'ies'} · stored only on this computer")
        self.table.setRowCount(len(entries))
        self._ids = [e.id for e in entries]
        labels = {"inserted": "inserted", "copied": "copied", "failed": "not inserted", "pending": "—"}
        for r, e in enumerate(entries):
            for c, val in enumerate([time.strftime("%Y-%m-%d %H:%M", time.localtime(e.created)), e.text,
                                     e.app or "", labels.get(e.outcome, e.outcome)]):
                self.table.setItem(r, c, QTableWidgetItem(val))
        self.table.resizeRowsToContents()

    def _selected(self) -> list[int]:
        return sorted({i.row() for i in self.table.selectedIndexes()})

    def copy_selected(self, *_):
        rows = self._selected()
        if rows:
            QGuiApplication.clipboard().setText("\n".join(self.table.item(r, 1).text() for r in rows))

    def delete_selected(self):
        h = self.history_provider()
        if h is None:
            return
        for r in self._selected():
            h.delete(self._ids[r])
        self.refresh()

    def clear(self):
        h = self.history_provider()
        if h is not None and QMessageBox.question(self, "Delete history", "Delete all dictation history?",
                                                  QMessageBox.Yes | QMessageBox.Cancel) == QMessageBox.Yes:
            h.clear()
            self.refresh()
