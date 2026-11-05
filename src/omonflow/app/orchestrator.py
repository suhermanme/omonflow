"""Orchestrator: owns the dictation state (SPEC §8) and turns UI intents into
backend actions.

M02 runs a *stub pipeline*: the audio backend is a stub (no microphone),
transcription and insertion are simulated with timers, and nothing is ever
typed into another application. M03–M05 replace the stubs.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QGuiApplication

from omonflow.core.interfaces import AudioBackend, AudioError
from omonflow.core.states import CAPTURE_STATES, IllegalTransition, State, StateMachine

LOG = logging.getLogger("omonflow.orchestrator")

DEFAULT_DELAYS_MS = {
    "init": 250,          # INITIALIZING → CAPTURE_READY
    "finalize": 150,
    "transcribe": 700,
    "focus_restore": 150,  # INSERTING before insertion "starts"
    "insert": 200,
    "success_hold": 1500,
}
ACTIVITY_THRESHOLD = 0.25
ACTIVITY_POLLS = 3
LEVEL_POLL_MS = 33


class Orchestrator(QObject):
    state_changed = Signal(str)
    level_changed = Signal(float)
    notice = Signal(str, str)  # (severity, text)
    controls_changed = Signal()  # e.g. cancel became unavailable within a state

    def __init__(self, audio: AudioBackend, delays: dict | None = None, parent=None) -> None:
        super().__init__(parent)
        self.audio = audio
        self.delays = {**DEFAULT_DELAYS_MS, **(delays or {})}
        self.sm = StateMachine()
        self.error_text = ""
        self.transcript: str | None = None  # kept on errors for Copy
        self.audio_kept = False  # kept after STT failure for Retry
        self.insertion_started = False
        self._gen = 0  # invalidates pending timers on cancel
        self._activity_hits = 0
        self._level_timer = QTimer(self)
        self._level_timer.setInterval(LEVEL_POLL_MS)
        self._level_timer.timeout.connect(self._poll_level)

    # ------------------------------------------------------------ properties
    @property
    def state(self) -> State:
        return self.sm.state

    def can_cancel(self) -> bool:
        if self.state == State.INSERTING:
            return not self.insertion_started
        return self.sm.can("cancel")

    # ------------------------------------------------------------ intents
    def start(self) -> bool:
        """Mic button / tray Start. Returns False if rejected."""
        if self.state != State.IDLE:
            LOG.info("start rejected: session already in %s", self.state.value)
            return False
        try:
            self.audio.start_capture()
        except AudioError as exc:
            self._fire("start")
            self._fail(f"Microphone error: {exc}")
            return False
        self._fire("start")
        self._later("init", self._on_ready)
        return True

    def stop(self) -> bool:
        if self.state not in (State.CAPTURE_READY, State.RECORDING):
            return False
        self._fire("stop")
        self._level_timer.stop()
        self.audio.stop_capture()
        self._later("finalize", self._on_finalized)
        return True

    def cancel(self) -> bool:
        if not self.can_cancel():
            return False
        prev = self.state
        self._gen += 1  # drop pending timers
        if prev in CAPTURE_STATES:
            self._level_timer.stop()
            self.audio.cancel_capture()
            self.level_changed.emit(0.0)
        elif prev == State.FINALIZING:
            self.audio.cancel_capture()
        if prev == State.REFINING:
            self.error_text = "Refinement cancelled. Raw text kept, not inserted."
        elif prev == State.INSERTING:
            self.error_text = "Insertion cancelled before it started. Text kept, not inserted."
        if prev in (State.FINALIZING, State.TRANSCRIBING) or prev in CAPTURE_STATES:
            self.transcript = None
            self.audio_kept = False
        self._fire("cancel")
        return True

    def primary(self) -> None:
        """The widget's main button: meaning depends on state."""
        s = self.state
        if s == State.IDLE:
            self.start()
        elif s in (State.CAPTURE_READY, State.RECORDING):
            self.stop()
        elif s == State.ERROR:
            if self.transcript:
                self.copy_transcript()
            elif self.audio_kept:
                self.retry()
            else:
                self.dismiss()

    def dismiss(self) -> None:
        if self.state == State.ERROR:
            self._gen += 1
            self.transcript = None
            self.audio_kept = False
            self.error_text = ""
            self._fire("dismiss")

    def retry(self) -> None:
        if self.state == State.ERROR and self.audio_kept:
            self.audio_kept = False
            self._fire("retry")
            self._later("transcribe", self._on_transcribed)

    def copy_transcript(self) -> bool:
        if not self.transcript:
            return False
        QGuiApplication.clipboard().setText(self.transcript)
        self.notice.emit("info", "Transcript copied to the clipboard.")
        return True

    def shutdown(self) -> None:
        self._gen += 1
        self._level_timer.stop()
        try:
            if self.state in CAPTURE_STATES or self.state == State.FINALIZING:
                self.audio.cancel_capture()
        finally:
            self.audio.shutdown()

    # ------------------------------------------------------------ pipeline (stub)
    def _on_ready(self) -> None:
        info = self.audio.wait_until_ready(3000)
        self._fire("ready")
        if info.get("silent_warning"):
            self.notice.emit("warning", "Microphone is delivering silence. Is it muted?")
        self._activity_hits = 0
        self._level_timer.start()

    def _poll_level(self) -> None:
        level = self.audio.get_input_level()
        self.level_changed.emit(level)
        if self.state == State.CAPTURE_READY:
            # UI-only activity indication (SPEC §6.1). Never affects the audio.
            self._activity_hits = self._activity_hits + 1 if level > ACTIVITY_THRESHOLD else 0
            if self._activity_hits >= ACTIVITY_POLLS:
                self._fire("activity")

    def _on_finalized(self) -> None:
        self.audio.finalize_audio()
        self.level_changed.emit(0.0)
        self._fire("audio_ready")
        self._later("transcribe", self._on_transcribed)

    def _on_transcribed(self) -> None:
        self.transcript = "OmonFlow demo transcript (M02 stub: no audio was captured)."
        self.insertion_started = False
        self._fire("text")
        self._later("focus_restore", self._on_insert_begin)

    def _on_insert_begin(self) -> None:
        self.insertion_started = True  # from here on, cancel is not allowed
        self.controls_changed.emit()
        self._later("insert", self._on_inserted)

    def _on_inserted(self) -> None:
        # Stub: nothing is typed anywhere in M02.
        self.insertion_started = False
        self.transcript = None
        self._fire("inserted")
        self._later("success_hold", lambda: self._fire("done"))

    # ------------------------------------------------------------ helpers
    def _fail(self, text: str) -> None:
        self.error_text = text
        self.notice.emit("error", text)
        if self.sm.can("fail"):
            self._fire("fail")

    def _fire(self, event: str) -> None:
        try:
            new = self.sm.fire(event)
        except IllegalTransition:
            LOG.exception("illegal transition")
            return
        LOG.info("state %s (%s)", new.value, event)
        self.state_changed.emit(new.value)

    def _later(self, key: str, fn) -> None:
        gen = self._gen

        def run():
            if gen == self._gen:
                fn()

        QTimer.singleShot(self.delays[key], self, run)
