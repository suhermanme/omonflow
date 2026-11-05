"""Orchestrator: owns the dictation state (SPEC §8) and turns UI intents into
backend actions.

M03: real on-demand capture through an AudioBackend (Rust core or Python
fallback). Opening and finalizing run on worker threads; results come back
to the Qt thread through signals. Transcription and insertion are still
stubs (M04/M05): nothing is typed into other apps, and captured audio is
discarded right after finalize (no retention, SPEC §15).
"""

from __future__ import annotations

import collections
import logging
import statistics
import threading
import time

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtGui import QGuiApplication

from omonflow.core import interfaces as I
from omonflow.core.states import CAPTURE_STATES, IllegalTransition, State, StateMachine

LOG = logging.getLogger("omonflow.orchestrator")

DEFAULT_DELAYS_MS = {
    "transcribe": 600,     # stub until M04/M05
    "focus_restore": 150,  # INSERTING before insertion "starts"
    "insert": 200,
    "success_hold": 1500,
    "success_copy_hold": 4000,  # unverified insertion: time to click "copy"
}
POLL_MS = 33


class Orchestrator(QObject):
    state_changed = Signal(str)
    level_changed = Signal(float)
    notice = Signal(str, str)  # (severity, text)
    controls_changed = Signal()
    startup_recorded = Signal(dict)
    mic_test_progress = Signal(float)  # level during a mic test
    mic_test_done = Signal(dict)
    _opened = Signal(int, object)     # (generation, info dict | exception)
    _finalized = Signal(int, object)  # (generation, recording dict | exception)
    _tested = Signal(object)
    _transcribed = Signal(int, object)  # (generation, text | exception)
    _inserted = Signal(int, object)     # (generation, InsertResult | exception)
    _insert_started = Signal(int)

    def __init__(self, audio, delays: dict | None = None, ready_timeout_ms: int = 3000,
                 permission_check=None, transcriber=None, inserter=None, history=None,
                 options=None, parent=None) -> None:
        super().__init__(parent)
        self.audio = audio
        # transcriber(wav: bytes, timeout_s: float) -> str     (None = M03-style stub)
        # inserter: omonflow.insertion.base.Inserter            (None = stub, types nothing)
        # history: callable() -> History | None (None when history is off)
        # options: callable() -> dict(strategy, restore_clipboard, model)
        self.transcriber = transcriber
        self.inserter = inserter
        self.history = history or (lambda: None)
        self.options = options or (lambda: {})
        self.target = None
        self.last_insert = None
        self._wav: bytes | None = None  # kept only for Retry after a transcription failure
        self._insert_lock = threading.Lock()  # cancel vs. "insertion has started" decide atomically
        self._history_id: int | None = None
        self.delays = {**DEFAULT_DELAYS_MS, **(delays or {})}
        self.ready_timeout_ms = ready_timeout_ms
        self.permission_check = permission_check  # () -> "authorized" | "denied" | ...
        self.sm = StateMachine()
        self.error_text = ""
        self.error_label: str | None = None  # short widget label for the error, e.g. "Copied — ⌘V"
        self.widget_label: str | None = None  # label shown in SUCCESS, e.g. "Done · click to copy"
        self.transcript: str | None = None  # kept on errors for Copy
        self.retry_action: str | None = None  # "start" after a mic failure
        self.insertion_started = False
        self.last_recording: dict | None = None  # metadata only, never audio
        self.startup_stats: collections.deque = collections.deque(maxlen=50)
        self.testing = False
        self._gen = 0
        self._t_click = 0.0
        self._poll = QTimer(self)
        self._poll.setInterval(POLL_MS)
        self._poll.timeout.connect(self._on_poll)
        self._opened.connect(self._on_opened)
        self._finalized.connect(self._on_finalized)
        self._tested.connect(self._on_tested)
        self._transcribed.connect(self._on_transcribed)
        self._inserted.connect(self._on_inserted)
        self._insert_started.connect(self._on_insert_started)

    # ------------------------------------------------------------ properties
    @property
    def state(self) -> State:
        return self.sm.state

    @property
    def audio_kept(self) -> bool:  # widget compatibility: "retry" glyph
        return self.retry_action is not None

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
        if self.testing:
            self.notice.emit("info", "A microphone test is running.")
            return False
        self.retry_action = None
        perm = self.permission_check() if self.permission_check else "authorized"
        if perm in ("denied", "restricted"):
            self._fire("start")
            self._fail("Microphone access is turned off for OmonFlow. Turn it on in System Settings → "
                       "Privacy & Security → Microphone.", retry=False)
            return False
        try:
            self.audio.start_capture()
        except I.AudioError as exc:
            self._fire("start")
            self._fail(f"Microphone error: {exc}", retry=True)
            return False
        self._t_click = time.perf_counter()
        self.target = None
        if self.inserter is not None:
            try:
                self.target = self.inserter.capture_target()  # the app being dictated into
            except Exception:  # noqa: BLE001
                LOG.warning("could not capture the target app", exc_info=True)
        self._fire("start")
        gen = self._gen

        def work():
            try:
                info = self.audio.wait_until_ready(self.ready_timeout_ms)
            except Exception as exc:  # noqa: BLE001 - delivered to the Qt thread
                info = exc
            self._opened.emit(gen, info)

        threading.Thread(target=work, name="omonflow-open", daemon=True).start()
        return True

    def stop(self) -> bool:
        if self.state not in (State.CAPTURE_READY, State.RECORDING):
            return False
        self._fire("stop")
        self._finalize()
        return True

    def cancel(self) -> bool:
        with self._insert_lock:
            if not self.can_cancel():
                return False
            prev = self.state
            self._gen += 1  # results of pending work are ignored from now on
        if prev in CAPTURE_STATES:
            self._poll.stop()
            self.level_changed.emit(0.0)
            threading.Thread(target=self.audio.cancel_capture, daemon=True).start()
        if prev == State.REFINING:
            self.error_text = "Refinement cancelled. Raw text kept, not inserted."
        elif prev == State.INSERTING:
            self.error_text = "Insertion cancelled before it started. Text kept, not inserted."
        if prev in (State.FINALIZING, State.TRANSCRIBING) or prev in CAPTURE_STATES:
            self.transcript = None
            self._wav = None
        self._fire("cancel")
        return True

    def primary(self) -> None:
        s = self.state
        if s == State.IDLE:
            self.start()
        elif s in (State.CAPTURE_READY, State.RECORDING):
            self.stop()
        elif s == State.SUCCESS and self.transcript:
            self.copy_transcript()
        elif s == State.ERROR:
            if self.transcript:
                self.copy_transcript()
            elif self.retry_action:
                self.retry()
            else:
                self.dismiss()

    def dismiss(self) -> None:
        if self.state == State.ERROR:
            self._gen += 1
            self.transcript = None
            self._wav = None  # kept audio is released
            self.retry_action = None
            self.error_label = None
            self.error_text = ""
            self._fire("dismiss")

    def retry(self) -> None:
        if self.state != State.ERROR:
            return
        if self.retry_action == "start":
            self.dismiss()
            self.start()
        elif self.retry_action == "transcribe" and self._wav is not None:
            self.retry_action = None
            self.error_text = ""
            self._fire("retry")
            self._transcribe()

    def copy_transcript(self) -> bool:
        if not self.transcript:
            return False
        QGuiApplication.clipboard().setText(self.transcript)
        self.notice.emit("info", "Transcript copied to the clipboard.")
        return True

    def shutdown(self) -> None:
        self._gen += 1
        self._poll.stop()
        try:
            if self.state in CAPTURE_STATES or self.state == State.FINALIZING:
                self.audio.cancel_capture()
        finally:
            self.audio.shutdown()

    # ------------------------------------------------------------ microphone test (Settings)
    def mic_test(self, seconds: float = 3.0) -> bool:
        if self.state != State.IDLE or self.testing:
            return False
        try:
            self.audio.start_capture()
        except I.AudioError as exc:
            self.mic_test_done.emit({"ok": False, "error": str(exc)})
            return False
        self.testing = True
        t0 = time.perf_counter()

        def work():
            result: dict = {"ok": True}
            try:
                info = self.audio.wait_until_ready(self.ready_timeout_ms)
                result.update(info, click_to_ready_ms=round((time.perf_counter() - t0) * 1000, 1))
                peak, end = 0.0, time.monotonic() + seconds
                while time.monotonic() < end:
                    lvl = self.audio.get_input_level()
                    peak = max(peak, lvl)
                    self.mic_test_progress.emit(lvl)
                    time.sleep(0.03)
                self.audio.stop_capture()
                rec = self.audio.finalize_audio()
                rec.pop("wav", None)  # discard audio immediately
                result.update(peak_level=round(peak, 3), recording=rec)
            except Exception as exc:  # noqa: BLE001
                self.audio.cancel_capture()
                result = {"ok": False, "error": str(exc)}
            self._tested.emit(result)

        threading.Thread(target=work, name="omonflow-mic-test", daemon=True).start()
        return True

    def _on_tested(self, result: dict) -> None:
        self.testing = False
        self.mic_test_progress.emit(0.0)
        self._record_startup(result if result.get("ok") else None, None if result.get("ok") else result["error"])
        self.mic_test_done.emit(result)

    # ------------------------------------------------------------ pipeline
    def _on_opened(self, gen: int, info) -> None:
        if gen != self._gen or self.state != State.INITIALIZING:
            return
        if isinstance(info, Exception):
            self._record_startup(None, str(info))
            if isinstance(info, I.DeviceTimeout):
                msg = (f"Microphone not responding ({info}). Retry, or choose another microphone in "
                       "Settings → Audio Input.")
            elif isinstance(info, I.DeviceNotFound):
                msg = "The selected microphone was not found. Choose another one in Settings → Audio Input."
            else:
                msg = f"Could not start the microphone: {info}"
            self._fail(msg, retry=True)
            return
        info = dict(info, click_to_ready_ms=round((time.perf_counter() - self._t_click) * 1000, 1))
        self._record_startup(info, None)
        self._fire("ready")
        if info.get("silent_warning"):
            self.notice.emit("warning", "The microphone is delivering silence. Is it muted?")
        self._poll.start()

    def _on_poll(self) -> None:
        self.level_changed.emit(self.audio.get_input_level())
        for ev in self.audio.poll_events():
            kind = ev.get("type")
            if kind == "activity" and self.state == State.CAPTURE_READY:
                self._fire("activity")  # UI only; never changes the audio
            elif kind == "device_lost" and self.state in (State.CAPTURE_READY, State.RECORDING):
                self.notice.emit("warning", "The microphone was disconnected. Keeping what was recorded.")
                self._fire("device_lost")
                self._finalize()
                return

    def _finalize(self) -> None:
        self._poll.stop()
        self.level_changed.emit(0.0)
        self.audio.stop_capture()
        gen = self._gen

        def work():
            try:
                rec = self.audio.finalize_audio()
            except Exception as exc:  # noqa: BLE001
                rec = exc
            self._finalized.emit(gen, rec)

        threading.Thread(target=work, name="omonflow-finalize", daemon=True).start()

    def _on_finalized(self, gen: int, rec) -> None:
        if gen != self._gen or self.state != State.FINALIZING:
            return
        if isinstance(rec, Exception):
            self._fail(f"Could not finalize the recording: {rec}", retry=False)
            return
        wav = rec.pop("wav", b"")
        self.last_recording = dict(rec, wav_bytes=len(wav))
        LOG.info("recording finalized: %.2f s, trimmed %.0f ms, dropped %s, xruns %s",
                 rec.get("duration_s", 0), rec.get("trimmed_ms", 0), rec.get("dropped"), rec.get("xruns"))
        self._fire("audio_ready")
        if self.transcriber is None:  # stub (tests / no engine wiring)
            del wav
            self._later("transcribe", lambda: self._on_transcribed(self._gen, (
                f"OmonFlow: captured {rec.get('duration_s', 0):.1f} s of audio (no transcriber configured).")))
            return
        self._wav = wav
        self._transcribe()

    def _transcribe(self) -> None:
        gen = self._gen
        wav = self._wav
        dur = (self.last_recording or {}).get("duration_s", 0.0) or 0.0
        timeout_s = max(30.0, 3.0 * dur)

        def work():
            try:
                text = self.transcriber(wav, timeout_s)
            except Exception as exc:  # noqa: BLE001
                text = exc
            self._transcribed.emit(gen, text)

        threading.Thread(target=work, name="omonflow-transcribe", daemon=True).start()

    def _on_transcribed(self, gen: int, text) -> None:
        if gen != self._gen or self.state != State.TRANSCRIBING:
            return
        if isinstance(text, Exception):
            # Audio stays in memory so the user can Retry; released on dismiss.
            self._fail(f"Transcription failed: {text}", retry=True, retry_action="transcribe")
            return
        text = clean_transcript(text)
        heard = (self.last_recording or {}).get("activity", True)
        if not text or (not heard and is_silence_phrase(text)):
            self._wav = None
            self.notice.emit("info", "No speech was recognized; nothing was inserted.")
            self._fire("no_speech")
            return
        self._wav = None  # transcript exists: the audio is no longer needed
        self.transcript = text
        opts = self.options()
        self._history_id = None
        hist = self.history()
        if hist is not None:  # saved before insertion, so it survives any insertion failure
            try:
                self._history_id = hist.add(text, getattr(self.target, "name", None), "pending",
                                            (self.last_recording or {}).get("duration_s"), opts.get("model"))
            except Exception:  # noqa: BLE001
                LOG.warning("could not save history", exc_info=True)
        self.insertion_started = False
        self._fire("text")
        self._insert(text, opts)

    def _insert(self, text: str, opts: dict) -> None:
        gen = self._gen
        if self.inserter is None:  # stub: nothing is typed anywhere
            self._later("focus_restore", lambda: (self._on_insert_started(gen), self._later(
                "insert", lambda: self._on_inserted(gen, None))))
            return

        def begin() -> bool:  # called on the insertion thread right before text is injected
            with self._insert_lock:
                if gen != self._gen:
                    return False
                self.insertion_started = True
            self._insert_started.emit(gen)
            return True

        def work():
            try:
                res = self.inserter.insert(text, self.target, opts.get("strategy", "auto"),
                                           opts.get("restore_clipboard", True),
                                           on_start=begin, cancelled=lambda: gen != self._gen)
            except Exception as exc:  # noqa: BLE001
                res = exc
            self._inserted.emit(gen, res)

        threading.Thread(target=work, name="omonflow-insert", daemon=True).start()

    def _on_insert_started(self, gen: int) -> None:
        if gen == self._gen and self.state == State.INSERTING:
            self.insertion_started = True  # from here on, cancel is not allowed
            self.controls_changed.emit()

    def _on_inserted(self, gen: int, res) -> None:
        if gen != self._gen or self.state != State.INSERTING:
            return  # cancelled before insertion started: the transcript is already in ERROR
        self.insertion_started = False
        self.last_insert = res
        outcome = "inserted" if res is None or getattr(res, "ok", False) else (
            "copied" if getattr(res, "outcome", "") == "copied" else "failed")
        hist = self.history()
        if hist is not None and self._history_id is not None:
            try:
                hist.set_outcome(self._history_id, outcome)
            except Exception:  # noqa: BLE001
                pass
        if outcome == "inserted":
            verified = None if res is None else res.verified
            if verified is False:
                self.notice.emit("warning", "Inserted, but the app did not show the text. Click the widget to copy "
                                            "it, or find it in History.")
            if verified is True or res is None:
                self.transcript = None
                self.widget_label = None
                hold = "success_hold"
            else:
                # The app can't confirm the text arrived (e.g. Chrome with no text box
                # selected silently drops a paste). Keep it a moment so one click copies it.
                self.widget_label = "Done · click to copy"
                hold = "success_copy_hold"
            self._fire("inserted")
            self._later(hold, self._success_done)
            return
        if isinstance(res, Exception):
            msg = f"Insertion failed: {res}"
        else:
            msg = res.error or "The text could not be inserted."
        if outcome == "copied":
            # Not a transcription problem: the text is on the clipboard already.
            self._fail(msg, retry=False, label="Copied — ⌘V")
            return
        self._fail(msg + " Click to copy the text.", retry=False, label="Not inserted")  # transcript kept

    def _success_done(self) -> None:
        self.transcript = None
        self.widget_label = None
        if self.state == State.SUCCESS:
            self._fire("done")

    # ------------------------------------------------------------ helpers
    def _record_startup(self, info: dict | None, error: str | None) -> None:
        entry = {"time": time.strftime("%H:%M:%S"), "ok": error is None, "error": error}
        if info:
            entry.update({k: info.get(k) for k in ("device_name", "open_ms", "ready_ms", "warmup_ms",
                                                   "click_to_ready_ms", "silent_warning")})
        self.startup_stats.append(entry)
        LOG.info("mic startup: %s", entry)
        self.startup_recorded.emit(entry)

    def startup_summary(self) -> dict:
        ok = [e for e in self.startup_stats if e["ok"] and e.get("click_to_ready_ms") is not None]
        vals = [e["click_to_ready_ms"] for e in ok]
        return {"runs": len(self.startup_stats), "failures": sum(not e["ok"] for e in self.startup_stats),
                "median_ready_ms": round(statistics.median(vals), 1) if vals else None,
                "max_ready_ms": max(vals) if vals else None}

    def _fail(self, text: str, retry: bool, retry_action: str = "start", label: str | None = None) -> None:
        self.error_text = text
        self.error_label = label
        self.retry_action = retry_action if retry else None
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


SILENCE_PHRASES = {"thank you.", "thank you", "thanks for watching!", "thanks for watching.", "you", "bye.",
                   "terima kasih.", "terima kasih", "[blank_audio]", "(silence)", "[silence]", "(music)", "[music]"}


def clean_transcript(text: str) -> str:
    """whisper.cpp markers removed; whitespace normalised."""
    import re

    text = re.sub(r"\[(BLANK_AUDIO|MUSIC|NOISE|SOUND)\]|\((music|silence|noise)\)", " ", text or "", flags=re.I)
    return re.sub(r"[ \t]+", " ", text).strip()


def is_silence_phrase(text: str) -> bool:
    """Phrases Whisper tends to produce for silence (only applied when no speech activity was detected)."""
    return text.strip().lower() in SILENCE_PHRASES
