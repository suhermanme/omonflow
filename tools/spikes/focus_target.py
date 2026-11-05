"""Stand-in for "the app being dictated into": a regular app with a focused
text field. Logs every activation and focus change to the file in argv[1]."""

import sys
import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QVBoxLayout, QWidget

log = open(sys.argv[1], "a", buffering=1)


def note(msg):
    log.write(f"{time.monotonic():.3f} {msg}\n")


app = QApplication(sys.argv[:1])
w = QWidget()
w.setWindowTitle("OmonFlow focus target")
edit = QLineEdit("caret should stay here")
status = QLabel("active")
lay = QVBoxLayout(w)
lay.addWidget(edit)
lay.addWidget(status)
w.resize(420, 120)
w.move(700, 200)
app.applicationStateChanged.connect(lambda s: (note(f"app_state {s.name}"), status.setText(s.name)))
app.focusChanged.connect(lambda old, new: note(f"focus {type(old).__name__}->{type(new).__name__}"))
w.show()
w.raise_()
w.activateWindow()
edit.setFocus(Qt.OtherFocusReason)
if sys.platform == "darwin":
    # Activate ourselves: since macOS 14 another process can't force this.
    from AppKit import NSApplication
    NSApplication.sharedApplication().activateIgnoringOtherApps_(True)
note("ready")
sys.exit(app.exec())
