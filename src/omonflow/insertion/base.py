"""Insertion interface and results."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

STRATEGIES = ("auto", "paste", "type")


@dataclass
class Target:
    pid: int | None
    bundle_id: str | None
    name: str | None
    element: Any = None        # platform handle of the focused text element, if known
    role: str | None = None    # e.g. AXTextArea, AXTextField
    secure: bool = False       # password field


@dataclass
class InsertResult:
    outcome: str               # inserted | copied | failed | not_started
    method: str | None = None  # accessibility | typing | paste | clipboard
    verified: bool | None = None  # True/False when read-back was possible, None = unknown
    error: str | None = None
    ms: float = 0.0
    steps: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.outcome == "inserted"


class Inserter(Protocol):
    def capture_target(self) -> Target | None: ...
    def insert(self, text: str, target: Target | None, strategy: str = "auto",
               restore_clipboard: bool = True, on_start=None, cancelled=None) -> InsertResult: ...
    def permission(self) -> dict: ...


class ClipboardInserter:
    """Fallback for platforms without an adapter yet (and Wayland): copy only."""

    name = "clipboard"

    def capture_target(self):
        return None

    def insert(self, text, target, strategy="auto", restore_clipboard=True, on_start=None, cancelled=None):
        from PySide6.QtGui import QGuiApplication

        if cancelled and cancelled():
            return InsertResult("not_started", error="cancelled")
        if on_start and on_start() is False:
            return InsertResult("not_started", error="cancelled")
        QGuiApplication.clipboard().setText(text)
        return InsertResult("copied", "clipboard", None,
                            "Automatic insertion is not available on this platform yet; the text was copied — paste it.")

    def permission(self):
        return {"accessibility": None}


def create_inserter():
    import sys

    if sys.platform == "darwin":
        from omonflow.platform.macos.insert import MacInserter

        return MacInserter()
    return ClipboardInserter()
