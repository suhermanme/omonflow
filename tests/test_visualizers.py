import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QImage, QPainter

from omonflow.core.states import State
from omonflow.ui import visualizers
from omonflow.ui.visualizers.base import Frame


def render(key, state, level=0.5):
    vis = visualizers.create(key)
    img = QImage(vis.size(), QImage.Format_ARGB32_Premultiplied)
    img.fill(0)
    p = QPainter(img)
    vis.paint(p, QRectF(img.rect()), Frame(state, 1.0, level))
    p.end()
    return img


def opaque_pixels(img):
    return sum(1 for y in range(0, img.height(), 3) for x in range(0, img.width(), 3)
               if img.pixelColor(x, y).alpha() > 0)


def test_default_is_spectrum_pill():
    assert visualizers.DEFAULT == "spectrum-pill"
    assert set(visualizers.REGISTRY) == {"spectrum-pill", "orb", "circular-wave"}


@pytest.mark.parametrize("key", list(visualizers.REGISTRY))
@pytest.mark.parametrize("state", list(State))
def test_every_state_renders_in_every_visualizer(qapp, key, state):
    assert opaque_pixels(render(key, state)) > 50


def test_live_level_changes_spectrum_bars(qapp):
    assert render("spectrum-pill", State.RECORDING, 0.05) != render("spectrum-pill", State.RECORDING, 1.0)


@pytest.mark.parametrize("key", list(visualizers.REGISTRY))
def test_controls_inside_widget(key):
    vis = visualizers.create(key)
    rect = QRectF(0, 0, vis.size().width(), vis.size().height())
    for r in vis.control_rects(rect).values():
        assert rect.contains(r)
