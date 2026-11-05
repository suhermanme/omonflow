from PySide6.QtCore import QPointF, QRectF

from omonflow.core.states import State
from omonflow.ui import visualizers
from omonflow.ui.visualizers.base import Visualizer
from omonflow.ui.widget.floating import FloatingWidget


class Broken(Visualizer):
    key = "broken"
    title = "Broken"

    def size(self):
        return visualizers.create("orb").size()

    def paint(self, painter, rect, frame):
        raise RuntimeError("boom")

    def control_rects(self, rect):
        return {"primary": QRectF(0, 0, 1, 1), "cancel": QRectF(0, 0, 1, 1)}


def test_starts_with_spectrum_pill(qapp):
    w = FloatingWidget()
    assert w.active_key == "spectrum-pill"
    assert (w.width(), w.height()) == (312, 72)


def test_switching_is_immediate_and_resizes(qapp):
    w = FloatingWidget()
    assert w.set_visualizer("orb")
    assert w.active_key == "orb" and w.width() == 220
    assert w.set_visualizer("circular-wave")
    assert w.active_key == "circular-wave"


def test_failing_visualizer_falls_back_and_reports(qapp, monkeypatch):
    monkeypatch.setitem(visualizers.REGISTRY, "broken", Broken)
    w = FloatingWidget()
    errors = []
    w.visualizer_error.connect(errors.append)
    assert not w.set_visualizer("broken")
    assert w.active_key == "spectrum-pill"
    assert errors and "broken" in errors[0]


def test_unknown_visualizer_falls_back(qapp):
    w = FloatingWidget()
    assert not w.set_visualizer("does-not-exist")
    assert w.active_key == "spectrum-pill"


def test_primary_action_per_state(qapp):
    w = FloatingWidget()
    expect = {State.IDLE: "mic", State.CAPTURE_READY: "stop", State.RECORDING: "stop",
              State.TRANSCRIBING: "busy", State.SUCCESS: "done", State.ERROR: "dismiss"}
    for state, action in expect.items():
        w.set_state(state.value)
        assert w.primary_action() == action
    w.set_error_payload(has_transcript=True, audio_kept=False)
    assert w.primary_action() == "copy"


def test_hit_testing_mic_button(qapp):
    w = FloatingWidget()
    assert w.hit(QPointF(32, 36)) == "primary"
    assert w.hit(QPointF(150, 36)) is None
    w.set_state(State.TRANSCRIBING.value)
    assert w.hit(QPointF(32, 36)) is None  # busy: no action


def test_hidden_widget_does_not_animate(qapp):
    w = FloatingWidget()
    w.show()
    w.set_state(State.RECORDING.value)
    assert w.timer.isActive()
    w.hide()
    assert not w.timer.isActive()


def test_idle_is_static_and_active_animates(qapp):
    from omonflow.ui.widget.floating import ACTIVE_FRAME_MS

    w = FloatingWidget()
    w.show()
    assert not w.timer.isActive()  # IDLE: no animation timer at all
    w.set_state(State.RECORDING.value)
    assert w.timer.isActive() and w.timer.interval() == ACTIVE_FRAME_MS
    w.set_state(State.ERROR.value)
    assert not w.timer.isActive()
    w.hide()
