"""Dictation state machine (SPEC §8). Pure Python, no Qt."""

from __future__ import annotations

from enum import Enum


class State(str, Enum):
    IDLE = "IDLE"
    INITIALIZING = "INITIALIZING"
    CAPTURE_READY = "CAPTURE_READY"
    RECORDING = "RECORDING"
    FINALIZING = "FINALIZING"
    TRANSCRIBING = "TRANSCRIBING"
    REFINING = "REFINING"
    INSERTING = "INSERTING"
    SUCCESS = "SUCCESS"
    ERROR = "ERROR"


S = State

# (state, event) -> next state. Anything not listed is an illegal transition.
TRANSITIONS: dict[tuple[State, str], State] = {
    (S.IDLE, "start"): S.INITIALIZING,
    (S.INITIALIZING, "ready"): S.CAPTURE_READY,
    (S.INITIALIZING, "fail"): S.ERROR,
    (S.INITIALIZING, "cancel"): S.IDLE,
    (S.CAPTURE_READY, "activity"): S.RECORDING,
    (S.CAPTURE_READY, "stop"): S.FINALIZING,
    (S.CAPTURE_READY, "cancel"): S.IDLE,
    (S.CAPTURE_READY, "device_lost"): S.FINALIZING,
    (S.RECORDING, "stop"): S.FINALIZING,
    (S.RECORDING, "cancel"): S.IDLE,
    (S.RECORDING, "device_lost"): S.FINALIZING,
    (S.FINALIZING, "audio_ready"): S.TRANSCRIBING,
    (S.FINALIZING, "cancel"): S.IDLE,
    (S.FINALIZING, "fail"): S.ERROR,
    (S.TRANSCRIBING, "text_refine"): S.REFINING,
    (S.TRANSCRIBING, "text"): S.INSERTING,
    (S.TRANSCRIBING, "no_speech"): S.IDLE,
    (S.TRANSCRIBING, "fail"): S.ERROR,
    (S.TRANSCRIBING, "cancel"): S.IDLE,
    (S.REFINING, "refined"): S.INSERTING,
    (S.REFINING, "refine_failed"): S.INSERTING,  # raw/clean text used
    (S.REFINING, "cancel"): S.ERROR,  # raw transcript kept, not inserted
    (S.INSERTING, "inserted"): S.SUCCESS,
    (S.INSERTING, "fail"): S.ERROR,  # transcript kept, Copy offered
    (S.INSERTING, "cancel"): S.ERROR,  # only before insertion starts (guarded)
    (S.SUCCESS, "done"): S.IDLE,
    (S.ERROR, "dismiss"): S.IDLE,
    (S.ERROR, "retry"): S.TRANSCRIBING,  # only when audio is kept (guarded)
}

# States in which a capture stream may be open.
CAPTURE_STATES = frozenset({S.INITIALIZING, S.CAPTURE_READY, S.RECORDING})
# States in which a dictation session is in progress.
ACTIVE_STATES = frozenset(set(State) - {S.IDLE, S.SUCCESS, S.ERROR})
CANCELLABLE = frozenset(s for (s, e) in TRANSITIONS if e == "cancel")


class IllegalTransition(Exception):
    pass


class StateMachine:
    def __init__(self) -> None:
        self.state = State.IDLE
        self.history: list[tuple[State, str, State]] = []

    def can(self, event: str) -> bool:
        return (self.state, event) in TRANSITIONS

    def fire(self, event: str) -> State:
        key = (self.state, event)
        if key not in TRANSITIONS:
            raise IllegalTransition(f"{event!r} not allowed in {self.state.value}")
        new = TRANSITIONS[key]
        self.history.append((self.state, event, new))
        self.state = new
        return new
