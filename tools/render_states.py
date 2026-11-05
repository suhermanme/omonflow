#!/usr/bin/env python3
"""Render every dictation state in every visualizer (with controls) to PNGs.

Usage: QT_QPA_PLATFORM=offscreen uv run python tools/render_states.py [out_dir]
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from omonflow.core.states import CANCELLABLE, State  # noqa: E402
from omonflow.ui import visualizers  # noqa: E402
from omonflow.ui.widget.floating import FloatingWidget  # noqa: E402


def main():
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/m02")
    out.mkdir(parents=True, exist_ok=True)
    app = QApplication.instance() or QApplication([])
    for key in visualizers.REGISTRY:
        w = FloatingWidget(can_cancel=lambda: w.state in CANCELLABLE)
        w.set_visualizer(key)
        cw, ch = w.width(), w.height()
        cols = 5 if key == "spectrum-pill" else 5
        rows = (len(State) + cols - 1) // cols
        pad, label_h = 16, 22
        img = QImage(cols * (cw + pad) + pad, rows * (ch + label_h + pad) + pad, QImage.Format_ARGB32)
        img.fill(QColor(236, 238, 242))
        p = QPainter(img)
        f = QFont()
        f.setPixelSize(12)
        p.setFont(f)
        for i, state in enumerate(State):
            w.set_state(state.value)
            w.phase, w.level = 1.2, 0.6
            w.set_error_payload(has_transcript=state == State.ERROR, audio_kept=False)
            r, c = divmod(i, cols)
            x, y = pad + c * (cw + pad), pad + r * (ch + label_h + pad)
            p.setPen(QColor(60, 60, 70))
            p.drawText(QRect(x, y, cw, label_h), Qt.AlignLeft | Qt.AlignVCenter, state.value)
            p.drawImage(x, y + label_h, w.grab().toImage())
        p.end()
        path = out / f"states-{key}.png"
        img.save(str(path))
        print(path)
        w.deleteLater()
    app.processEvents()


if __name__ == "__main__":
    main()
