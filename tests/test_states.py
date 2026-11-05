import pytest

from omonflow.core.states import CANCELLABLE, TRANSITIONS, IllegalTransition, State, StateMachine


def test_happy_path_matches_spec():
    sm = StateMachine()
    for event, expected in [("start", State.INITIALIZING), ("ready", State.CAPTURE_READY),
                            ("activity", State.RECORDING), ("stop", State.FINALIZING),
                            ("audio_ready", State.TRANSCRIBING), ("text_refine", State.REFINING),
                            ("refined", State.INSERTING), ("inserted", State.SUCCESS), ("done", State.IDLE)]:
        assert sm.fire(event) == expected


def test_stop_allowed_before_activity():
    sm = StateMachine()
    sm.fire("start"); sm.fire("ready")
    assert sm.fire("stop") == State.FINALIZING


@pytest.mark.parametrize("state,expected", [
    (State.INITIALIZING, State.IDLE), (State.CAPTURE_READY, State.IDLE), (State.RECORDING, State.IDLE),
    (State.FINALIZING, State.IDLE), (State.TRANSCRIBING, State.IDLE),
    (State.REFINING, State.ERROR), (State.INSERTING, State.ERROR),
])
def test_cancel_targets(state, expected):
    assert TRANSITIONS[(state, "cancel")] == expected


def test_no_cancel_in_terminal_states():
    assert State.IDLE not in CANCELLABLE and State.SUCCESS not in CANCELLABLE


def test_illegal_transitions_rejected():
    sm = StateMachine()
    with pytest.raises(IllegalTransition):
        sm.fire("stop")
    sm.fire("start")
    with pytest.raises(IllegalTransition):
        sm.fire("start")  # duplicate session


def test_refine_failure_still_inserts():
    assert TRANSITIONS[(State.REFINING, "refine_failed")] == State.INSERTING


def test_every_state_reachable():
    reachable = {State.IDLE} | set(TRANSITIONS.values())
    assert reachable == set(State)
