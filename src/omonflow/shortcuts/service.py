"""Show/hide-widget global shortcut (SPEC §5.2, ARCHITECTURE §5).

Only *registered* combinations in M02 (no keyboard monitoring, no extra
permissions). The user's shortcut is never silently replaced: failures are
reported as a status, and the tray/menu keeps working.
"""

from __future__ import annotations

import logging
import os
import sys
from enum import Enum

from PySide6.QtCore import QObject, QTimer, Signal

LOG = logging.getLogger("omonflow.shortcuts")
POLL_MS = 30


class ShortcutStatus(str, Enum):
    REGISTERED = "registered"
    DISABLED = "disabled"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"
    PERMISSION_DENIED = "permission denied"
    NOT_SUPPORTED = "not supported by platform"
    INVALID = "invalid shortcut"


class BackendError(Exception):
    def __init__(self, status: ShortcutStatus, message: str) -> None:
        super().__init__(message)
        self.status = status


class RustRegisteredBackend:
    """Wraps omonflow._core.HotkeyManager (Carbon / RegisterHotKey / XGrabKey)."""

    def __init__(self) -> None:
        if sys.platform.startswith("linux") and os.environ.get("XDG_SESSION_TYPE") == "wayland":
            raise BackendError(
                ShortcutStatus.NOT_SUPPORTED,
                "Wayland does not allow apps to register global shortcuts directly. "
                "Bind a desktop custom shortcut to: omonflow --toggle-widget",
            )
        try:
            from omonflow import _core
        except ImportError as exc:
            raise BackendError(ShortcutStatus.UNAVAILABLE, f"Native core not loaded: {exc}") from exc
        self._core = _core
        try:
            self._mgr = _core.HotkeyManager()
        except _core.ShortcutUnavailable as exc:
            raise BackendError(ShortcutStatus.UNAVAILABLE, str(exc)) from exc

    def register(self, spec: str) -> int:
        try:
            return self._mgr.register(spec)
        except ValueError as exc:
            raise BackendError(ShortcutStatus.INVALID, str(exc)) from exc
        except self._core.ShortcutConflict as exc:
            raise BackendError(ShortcutStatus.CONFLICT, str(exc)) from exc
        except self._core.ShortcutUnavailable as exc:
            raise BackendError(ShortcutStatus.UNAVAILABLE, str(exc)) from exc

    def unregister(self, hid: int) -> None:
        self._mgr.unregister(hid)

    def poll(self) -> list[tuple[int, str]]:
        return self._core.HotkeyManager.poll()


class ShortcutService(QObject):
    activated = Signal()
    status_changed = Signal(str, str)  # (ShortcutStatus value, message)

    def __init__(self, backend_factory=RustRegisteredBackend, parent=None) -> None:
        super().__init__(parent)
        self.status = ShortcutStatus.DISABLED
        self.message = ""
        self.spec: str | None = None
        self._id: int | None = None
        self._backend = None
        self._backend_error: BackendError | None = None
        try:
            self._backend = backend_factory()
        except BackendError as exc:
            self._backend_error = exc
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._poll)

    def apply(self, spec: str, enabled: bool = True) -> ShortcutStatus:
        """Register exactly `spec`. On failure nothing else is registered."""
        self._release()
        self.spec = spec
        if not enabled:
            return self._set(ShortcutStatus.DISABLED, "Shortcut turned off.")
        if self._backend is None:
            err = self._backend_error
            return self._set(err.status, str(err))
        try:
            self._id = self._backend.register(spec)
        except BackendError as exc:
            hint = " Choose a different shortcut in Settings." if exc.status == ShortcutStatus.CONFLICT else ""
            return self._set(exc.status, f"{exc}{hint}")
        self._timer.start()
        msg = f"{pretty(spec)} shows or hides the widget."
        if sys.platform == "darwin":
            # M02 finding: macOS accepts the same combination from several apps
            # without reporting a conflict, so it cannot be detected here.
            msg += " If pressing it does nothing, another app may be using the same shortcut."
        return self._set(ShortcutStatus.REGISTERED, msg)

    def shutdown(self) -> None:
        self._release()

    def _release(self) -> None:
        self._timer.stop()
        if self._id is not None and self._backend is not None:
            try:
                self._backend.unregister(self._id)
            except Exception:  # noqa: BLE001 - best effort on teardown
                LOG.debug("unregister failed", exc_info=True)
        self._id = None

    def _poll(self) -> None:
        for hid, state in self._backend.poll():
            if hid == self._id and state == "pressed":
                self.activated.emit()

    def _set(self, status: ShortcutStatus, message: str) -> ShortcutStatus:
        self.status, self.message = status, message
        LOG.info("shortcut %s: %s", status.value, message)
        self.status_changed.emit(status.value, message)
        return status


# ---------------------------------------------------------------- formatting

_MAC = {"cmd": "⌘", "shift": "⇧", "alt": "⌥", "ctrl": "⌃"}
_ORDER = ["ctrl", "alt", "shift", "cmd"]


def pretty(spec: str) -> str:
    parts = [p.strip().lower() for p in spec.split("+")]
    mods = [p for p in parts if p in _MAC or p in ("option", "command", "control", "meta", "super")]
    norm = {"option": "alt", "command": "cmd", "control": "ctrl", "meta": "cmd", "super": "cmd"}
    mods = sorted({norm.get(m, m) for m in mods}, key=_ORDER.index)
    key = next((p for p in parts if p not in _MAC and p not in norm), "")
    key = key.capitalize() if len(key) > 1 else key.upper()
    if sys.platform == "darwin":
        return "".join(_MAC[m] for m in mods) + key
    names = {"cmd": "Super", "ctrl": "Ctrl", "alt": "Alt", "shift": "Shift"}
    return "+".join([names[m] for m in mods] + [key])


def from_qt(key_combination) -> str | None:
    """QKeyCombination → spec string, or None if the key is unsupported."""
    from PySide6.QtCore import Qt

    mods = key_combination.keyboardModifiers()
    key = key_combination.key()
    parts = []
    # Qt maps ⌘ to ControlModifier and ⌃ to MetaModifier on macOS.
    if sys.platform == "darwin":
        if mods & Qt.MetaModifier:
            parts.append("ctrl")
        if mods & Qt.AltModifier:
            parts.append("alt")
        if mods & Qt.ShiftModifier:
            parts.append("shift")
        if mods & Qt.ControlModifier:
            parts.append("cmd")
    else:
        if mods & Qt.ControlModifier:
            parts.append("ctrl")
        if mods & Qt.AltModifier:
            parts.append("alt")
        if mods & Qt.ShiftModifier:
            parts.append("shift")
        if mods & Qt.MetaModifier:
            parts.append("super")
    k = int(key)
    if Qt.Key_A <= k <= Qt.Key_Z or Qt.Key_0 <= k <= Qt.Key_9:
        name = chr(k)
    elif k == Qt.Key_Space:
        name = "space"
    elif Qt.Key_F1 <= k <= Qt.Key_F24:
        name = f"F{k - int(Qt.Key_F1) + 1}"
    else:
        punct = {Qt.Key_Comma: ",", Qt.Key_Period: ".", Qt.Key_Slash: "/", Qt.Key_Semicolon: ";",
                 Qt.Key_Minus: "-", Qt.Key_Equal: "=", Qt.Key_BracketLeft: "[",
                 Qt.Key_BracketRight: "]", Qt.Key_Backslash: "\\", Qt.Key_QuoteLeft: "`"}
        name = punct.get(Qt.Key(k))
    if not name or not parts:
        return None  # require at least one modifier for a global shortcut
    return "+".join(parts + [name])
