"""Insertion target app for tests: one field focused, contents mirrored to a file.

usage: insert_target.py OUT_JSON FIELD [TITLE]   FIELD = line | text | password
"""

import json
import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLineEdit, QTextEdit, QVBoxLayout, QWidget

out, field = sys.argv[1], sys.argv[2]
title = sys.argv[3] if len(sys.argv) > 3 else "OmonFlow insertion target"
app = QApplication(sys.argv[:1])
w = QWidget()
w.setWindowTitle(title)
line, text, pw = QLineEdit(), QTextEdit(), QLineEdit()
pw.setEchoMode(QLineEdit.Password)
lay = QVBoxLayout(w)
for x in (line, text, pw):
    lay.addWidget(x)


def dump(*_):
    with open(out, "w") as f:
        json.dump({"line": line.text(), "text": text.toPlainText(), "password": pw.text()}, f)


line.textChanged.connect(dump)
text.textChanged.connect(dump)
pw.textChanged.connect(dump)
dump()
w.resize(480, 260)
w.move(720 if field != "password" else 760, 180)
w.show()
w.raise_()
w.activateWindow()
{"line": line, "text": text, "password": pw}[field].setFocus(Qt.OtherFocusReason)
from AppKit import NSApplication  # noqa: E402
NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
sys.exit(app.exec())
