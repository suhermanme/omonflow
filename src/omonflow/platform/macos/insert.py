"""macOS text insertion (SPEC §11, ARCHITECTURE §4).

Layers, in order for strategy "auto":
  1. Accessibility: set the focused element's selected text, then read the
     value back to verify (works in most native Cocoa text views).
  2. Clipboard + ⌘V (Electron apps, browsers, terminals, …), saving and
     restoring the user's clipboard; the temporary entry is marked transient
     so clipboard managers ignore it.
Strategy "type" uses Unicode keyboard events for single-line text (paste for
multi-line, since a typed Return can send a chat message). "paste" skips 1.

Never inserts into password fields or while Secure Input is on. Without
Accessibility permission nothing can be inserted: the text is put on the
clipboard and the user is told to paste.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
import os
import time
import weakref

from omonflow.insertion.base import InsertResult, Target

LOG = logging.getLogger("omonflow.insert")
ACCESSIBILITY_URL = "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
KEY_V, KEY_RETURN = 9, 36
# Focused elements that can never receive text. Only used when the app exposes
# its focused element; unknown roles are always attempted (never block blindly).
NOT_EDITABLE_ROLES = {"AXWebArea", "AXStaticText", "AXWindow", "AXButton", "AXLink", "AXImage", "AXApplication",
                      "AXScrollArea", "AXHeading", "AXMenuBar", "AXToolbar"}
TYPE_CHUNK = 20            # UTF-16 units per keyboard event
PASTE_SETTLE_S = 0.25      # time for the target app to read the pasteboard
FOCUS_TIMEOUT_S = 1.0


def _ax():
    import ApplicationServices as AS

    return AS


_TRACKERS: "weakref.WeakSet[FocusTracker]" = weakref.WeakSet()
_OBS_CLASS = None


def _observer_class():
    """One Objective-C observer class per process (classes cannot be redefined)."""
    global _OBS_CLASS
    if _OBS_CLASS is None:
        from Foundation import NSObject

        class OmonFlowAppActivationObserver(NSObject):
            def appActivated_(self, note):  # noqa: N802
                app = note.userInfo()["NSWorkspaceApplicationKey"]
                for tracker in list(_TRACKERS):
                    tracker.update(app)

        _OBS_CLASS = OmonFlowAppActivationObserver
    return _OBS_CLASS


class FocusTracker:
    """Remembers the last focused app that is not OmonFlow."""

    def __init__(self) -> None:
        from AppKit import NSWorkspace

        self._ws = NSWorkspace.sharedWorkspace()
        self.last: Target | None = None
        self._observer = None
        try:
            self._install()
        except Exception:  # noqa: BLE001
            LOG.warning("focus tracking unavailable", exc_info=True)
        self.update(self._ws.frontmostApplication())

    def _install(self) -> None:
        import objc

        _TRACKERS.add(self)
        self._observer = _observer_class().alloc().init()
        self._ws.notificationCenter().addObserver_selector_name_object_(
            self._observer, objc.selector(_observer_class().appActivated_, signature=b"v@:@"),
            "NSWorkspaceDidActivateApplicationNotification", None)

    def close(self) -> None:
        if self._observer is not None:
            self._ws.notificationCenter().removeObserver_(self._observer)
            self._observer = None
        _TRACKERS.discard(self)

    def update(self, app) -> None:
        if app is None or app.processIdentifier() == os.getpid():
            return
        self.last = Target(app.processIdentifier(), app.bundleIdentifier(), app.localizedName())


class MacInserter:
    name = "macos"

    def __init__(self) -> None:
        from AppKit import NSWorkspace

        self._ws = NSWorkspace.sharedWorkspace()
        self.tracker = FocusTracker()
        self._carbon = None
        self._ax_enabled: set[int] = set()

    # ------------------------------------------------------------ permission / state
    def trusted(self) -> bool:
        try:
            return bool(_ax().AXIsProcessTrusted())
        except Exception:  # noqa: BLE001
            return False

    def request_permission(self) -> None:
        """Shows macOS's own Accessibility prompt (once per app identity)."""
        AS = _ax()
        AS.AXIsProcessTrustedWithOptions({AS.kAXTrustedCheckOptionPrompt: True})

    def secure_input(self) -> bool:
        try:
            if self._carbon is None:
                self._carbon = ctypes.CDLL(ctypes.util.find_library("Carbon"))
                self._carbon.IsSecureEventInputEnabled.restype = ctypes.c_bool
            return bool(self._carbon.IsSecureEventInputEnabled())
        except Exception:  # noqa: BLE001
            return False

    def permission(self) -> dict:
        return {"accessibility": self.trusted(), "secure_input": self.secure_input()}

    # ------------------------------------------------------------ target
    def capture_target(self) -> Target | None:
        from AppKit import NSRunningApplication

        pid = self.front_pid() if self.trusted() else None
        app = (NSRunningApplication.runningApplicationWithProcessIdentifier_(pid) if pid
               else self._ws.frontmostApplication())
        if app is not None and app.processIdentifier() != os.getpid():
            t = Target(app.processIdentifier(), app.bundleIdentifier(), app.localizedName())
        else:
            t = self.tracker.last
            if t is not None:
                t = Target(t.pid, t.bundle_id, t.name)
        if t is not None and self.trusted():
            el = self._focused_element(t.pid)
            if el is not None:
                t.element = el
                t.role = self._attr(el, "AXRole")
                t.secure = self._is_secure(el)
        return t

    def _editable(self, el) -> bool:
        role = self._attr(el, "AXRole")
        if role not in NOT_EDITABLE_ROLES:
            return True
        AS = _ax()
        for attr in ("AXValue", "AXSelectedText"):
            err, settable = AS.AXUIElementIsAttributeSettable(el, attr, None)
            if err == 0 and settable:
                return True
        return False

    def _is_secure(self, el) -> bool:
        """Password field? Native fields use the AXSecureTextField subrole; some
        toolkits (e.g. Qt) only set the role description."""
        if self._attr(el, "AXSubrole") == "AXSecureTextField":
            return True
        desc = self._attr(el, "AXRoleDescription")
        return isinstance(desc, str) and "secure text" in desc.lower()

    def _focused_element(self, pid: int):
        AS = _ax()
        app = AS.AXUIElementCreateApplication(pid)
        err, el = AS.AXUIElementCopyAttributeValue(app, "AXFocusedUIElement", None)
        if err == 0 and el is not None:
            return el
        if pid in self._ax_enabled:
            return None
        # Chromium and Electron apps expose their focused element (and its text)
        # only after an assistive client asks; this switches it on asynchronously.
        self._ax_enabled.add(pid)
        AS.AXUIElementSetAttributeValue(app, "AXManualAccessibility", True)
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline:
            time.sleep(0.05)
            err, el = AS.AXUIElementCopyAttributeValue(app, "AXFocusedUIElement", None)
            if err == 0 and el is not None:
                return el
        return None

    @staticmethod
    def _attr(el, name):
        err, val = _ax().AXUIElementCopyAttributeValue(el, name, None)
        return val if err == 0 else None

    # ------------------------------------------------------------ insert
    def insert(self, text: str, target: Target | None, strategy: str = "auto",
               restore_clipboard: bool = True, on_start=None, cancelled=None) -> InsertResult:
        t0 = time.perf_counter()
        cancelled = cancelled or (lambda: False)
        notify = on_start or (lambda: None)

        def start() -> bool:
            """Last check before anything is injected; True = go ahead.
            Refuses if the target is not the live frontmost app (never type into
            the wrong app); `on_start` may veto atomically with a concurrent cancel."""
            if cancelled():
                return False
            if target is None or self.front_pid() != target.pid or not self._alive(target.pid):
                guard["tripped"] = True
                return False
            return notify() is not False

        guard = {"tripped": False}

        res = self._insert(text, target, strategy, restore_clipboard, start)
        if guard["tripped"] and not res.ok:
            res = InsertResult("not_started", error=f"{getattr(target, 'name', None) or 'The target app'} is no "
                                                    "longer the active app; nothing was inserted.")
        res.ms = round((time.perf_counter() - t0) * 1000, 1)
        LOG.info("insert: %s via %s verified=%s (%s ms) %s", res.outcome, res.method, res.verified, res.ms,
                 res.error or "")
        return res

    def _insert(self, text, target, strategy, restore_clipboard, on_start) -> InsertResult:
        if target is None or target.pid is None:
            return InsertResult("not_started", error="No target app was recorded when dictation started.")
        if target.secure:
            return InsertResult("not_started", error="The target is a password field; OmonFlow never types there.")
        if not self.trusted():
            self._clipboard_set(text, transient=False)
            return InsertResult("copied", "clipboard", None,
                                "Accessibility permission is needed to insert text. The text was copied — press ⌘V. "
                                "Grant access in Settings → Text Insertion.")
        focus = self._ensure_focus(target)
        if focus:
            return InsertResult("not_started", error=focus)
        if self.secure_input():
            return InsertResult("not_started", error="Secure input is active (a password field or a terminal with "
                                                     "Secure Keyboard Entry); text cannot be inserted.")
        steps: list[str] = []
        element = self._focused_element(target.pid) or target.element
        if element is not None and self._is_secure(element):  # focus may have moved since the start
            return InsertResult("not_started", error="The focused field is a password field; OmonFlow never types there.")
        if element is not None and not self._editable(element):
            # e.g. reading a web page in Chrome with no text box selected: a paste
            # would silently go nowhere. Keep the text on the clipboard instead.
            self._clipboard_set(text, transient=False)
            return InsertResult("copied", "clipboard", None,
                                f"No text field is selected in {target.name or 'the app'}. The text was copied — "
                                "click into a text field and press ⌘V.")
        multiline = "\n" in text

        cancelled_result = InsertResult("not_started", error="cancelled before insertion started")
        if strategy == "auto" and element is not None:
            if not on_start():
                return cancelled_result
            ok, why = self._ax_insert(element, text)
            steps.append(f"accessibility: {why}")
            if ok:
                return InsertResult("inserted", "accessibility", True, steps=steps)
        elif strategy == "type" and not multiline:
            if not on_start():
                return cancelled_result
            self._type(text)
            return InsertResult("inserted", "typing", self._verify(element, text), steps=steps + ["typed"])
        if not (steps or on_start()):  # after a failed AX attempt we have already started
            return cancelled_result
        before = self._value(element)
        self._paste(text, restore_clipboard)
        steps.append("paste")
        verified = self._verify(element, text, before)
        return InsertResult("inserted", "paste", verified, steps=steps)

    def _ensure_focus(self, target: Target) -> str | None:
        from AppKit import NSRunningApplication

        app = NSRunningApplication.runningApplicationWithProcessIdentifier_(target.pid)
        if app is None or app.isTerminated() or not self._alive(target.pid):
            return f"{target.name or 'The target app'} is no longer running."
        if self.front_pid() == target.pid:
            return None
        # Since macOS 14 an inactive app cannot simply activate another one; with
        # Accessibility access, setting AXFrontmost on the target works.
        app.activateWithOptions_(0)
        if self._wait_front(target.pid, 0.2):
            return None
        AS = _ax()
        ax_app = AS.AXUIElementCreateApplication(target.pid)
        AS.AXUIElementSetAttributeValue(ax_app, "AXFrontmost", True)
        err, win = AS.AXUIElementCopyAttributeValue(ax_app, "AXFocusedWindow", None)
        if err == 0 and win is not None:
            AS.AXUIElementPerformAction(win, "AXRaise")
        if self._wait_front(target.pid, FOCUS_TIMEOUT_S):
            return None
        return f"Could not return focus to {target.name or 'the target app'}."

    def front_pid(self) -> int | None:
        """Live frontmost app. NSWorkspace's value can be stale on threads without
        a run loop, so ask the accessibility server; apps that don't answer it
        (e.g. Firefox: kAXErrorCannotComplete) fall back to the window server,
        whose on-screen list is ordered front to back."""
        AS = _ax()
        err, app = AS.AXUIElementCopyAttributeValue(AS.AXUIElementCreateSystemWide(), "AXFocusedApplication", None)
        if err == 0 and app is not None:
            err, pid = AS.AXUIElementGetPid(app, None)
            if err == 0:
                return pid
        pid = self._launchservices_front_pid()
        return pid if pid is not None else self._front_window_pid()

    @staticmethod
    def _launchservices_front_pid() -> int | None:
        """LaunchServices' authoritative front app (works from any thread)."""
        import re
        import subprocess

        try:
            asn = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True, timeout=2).stdout.strip()
            if not asn.startswith("ASN:"):
                return None
            info = subprocess.run(["lsappinfo", "info", "-only", "pid", asn], capture_output=True, text=True,
                                  timeout=2).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        m = re.search(r"pid\W*=\s*(\d+)", info)
        return int(m.group(1)) if m else None

    @staticmethod
    def _front_window_pid() -> int | None:
        """Last resort only: on multi-display setups the topmost window may be on
        another display than the active app."""
        import Quartz as Q

        opts = Q.kCGWindowListOptionOnScreenOnly | Q.kCGWindowListExcludeDesktopElements
        for w in Q.CGWindowListCopyWindowInfo(opts, Q.kCGNullWindowID) or []:
            # Layer 0 = normal app windows (menus, the menu bar, panels like our widget are above it)
            if w.get("kCGWindowLayer") == 0 and w.get("kCGWindowOwnerPID") != os.getpid():
                return int(w["kCGWindowOwnerPID"])
        return None

    @staticmethod
    def _alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def _wait_front(self, pid: int, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.front_pid() == pid:
                time.sleep(0.08)  # let the app's key window settle
                return True
            time.sleep(0.02)
        return False

    # ---- layer 1: accessibility
    def _ax_insert(self, el, text) -> tuple[bool, str]:
        AS = _ax()
        err, settable = AS.AXUIElementIsAttributeSettable(el, "AXSelectedText", None)
        if err != 0 or not settable:
            return False, "selected text not settable"
        before = self._value(el)
        if AS.AXUIElementSetAttributeValue(el, "AXSelectedText", text) != 0:
            return False, "set failed"
        after = self._value(el)
        if before is None or after is None:
            return False, "cannot read back (unverified, trying paste)"
        if text in after and len(after) >= len(before) + len(text) - 1:
            return True, "verified"
        # Some views accept the call but change nothing (common in Electron/web views).
        return False, "no visible change"

    def _value(self, el):
        if el is None:
            return None
        v = self._attr(el, "AXValue")
        return str(v) if isinstance(v, str) or (v is not None and hasattr(v, "length")) else None

    def _verify(self, el, text, before=None) -> bool | None:
        if el is None:
            return None
        time.sleep(0.05)
        after = self._value(el)
        if after is None:
            return None
        if before is not None and after == before:
            return False
        return text.strip()[:40] in after

    # ---- layer 2: typing
    def _type(self, text: str) -> None:
        import Quartz as Q

        units = text.encode("utf-16-le")
        for i in range(0, len(units), TYPE_CHUNK * 2):
            chunk = units[i:i + TYPE_CHUNK * 2].decode("utf-16-le", errors="ignore")
            for down in (True, False):
                ev = Q.CGEventCreateKeyboardEvent(None, 0, down)
                Q.CGEventSetFlags(ev, 0)
                Q.CGEventKeyboardSetUnicodeString(ev, len(chunk.encode("utf-16-le")) // 2, chunk)
                Q.CGEventPost(Q.kCGHIDEventTap, ev)
            time.sleep(0.004)

    # ---- layer 3: clipboard
    def _paste(self, text: str, restore: bool) -> None:
        import Quartz as Q

        saved = self._clipboard_save() if restore else None
        count = self._clipboard_set(text, transient=True)
        for down in (True, False):
            ev = Q.CGEventCreateKeyboardEvent(None, KEY_V, down)
            Q.CGEventSetFlags(ev, Q.kCGEventFlagMaskCommand)  # explicit: no stray modifiers
            Q.CGEventPost(Q.kCGHIDEventTap, ev)
        time.sleep(PASTE_SETTLE_S)
        if saved is not None:
            self._clipboard_restore(saved, count)

    @staticmethod
    def _pb():
        from AppKit import NSPasteboard

        return NSPasteboard.generalPasteboard()

    def _clipboard_save(self) -> list[list[tuple[str, bytes]]]:
        items = []
        for item in self._pb().pasteboardItems() or []:
            entry = []
            for t in item.types():
                data = item.dataForType_(t)
                if data is not None:
                    entry.append((str(t), bytes(data)))
            items.append(entry)
        return items

    def _clipboard_set(self, text: str, transient: bool) -> int:
        pb = self._pb()
        pb.clearContents()
        pb.setString_forType_(text, "public.utf8-plain-text")
        if transient:  # nspasteboard.org convention: clipboard managers skip these
            pb.setData_forType_(b"", "org.nspasteboard.TransientType")
            pb.setData_forType_(b"", "org.nspasteboard.AutoGeneratedType")
        return pb.changeCount()

    def _clipboard_restore(self, saved, our_count: int) -> None:
        from AppKit import NSPasteboardItem
        from Foundation import NSData

        pb = self._pb()
        if pb.changeCount() != our_count:
            LOG.info("clipboard changed by someone else meanwhile; not restoring")
            return
        pb.clearContents()
        objs = []
        for entry in saved:
            it = NSPasteboardItem.alloc().init()
            for t, data in entry:
                it.setData_forType_(NSData.dataWithBytes_length_(data, len(data)), t)
            objs.append(it)
        if objs:
            pb.writeObjects_(objs)

    def copy(self, text: str) -> None:
        self._clipboard_set(text, transient=False)
