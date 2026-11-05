"""macOS window/app integration via pyobjc.

- Accessory activation policy: no Dock icon, and showing windows does not
  activate OmonFlow (packaged builds also set LSUIElement).
- Non-activating panel for the floating widget, so clicking it does not take
  focus from the app the user is dictating into (M02 focus spike).
"""

from __future__ import annotations

import logging

LOG = logging.getLogger("omonflow.macos")

# AppKit constants (stable values).
NS_NONACTIVATING_PANEL = 1 << 7
CAN_JOIN_ALL_SPACES = 1 << 0
MOVE_TO_ACTIVE_SPACE = 1 << 1
FULLSCREEN_AUXILIARY = 1 << 8
ACTIVATION_POLICY_ACCESSORY = 1


def set_accessory_app() -> bool:
    try:
        from AppKit import NSApplication

        NSApplication.sharedApplication().setActivationPolicy_(ACTIVATION_POLICY_ACCESSORY)
        return True
    except Exception:  # noqa: BLE001
        LOG.warning("could not set accessory activation policy", exc_info=True)
        return False


def make_non_activating(widget) -> dict:
    """Configure the widget's NSWindow. Returns a report for diagnostics."""
    report = {"ok": False}
    from PySide6.QtGui import QGuiApplication

    if QGuiApplication.platformName() != "cocoa":
        # winId() is only an NSView* under the native Cocoa platform plugin;
        # dereferencing it elsewhere (e.g. offscreen tests) crashes.
        report["error"] = f"Qt platform is {QGuiApplication.platformName()!r}, not cocoa"
        return report
    try:
        import objc
        from AppKit import NSPanel

        view = objc.objc_object(c_void_p=int(widget.winId()))
        win = view.window()
        if win is None:
            report["error"] = "no NSWindow yet"
            return report
        report["class"] = win.className()
        report["is_panel"] = bool(win.isKindOfClass_(NSPanel))
        if report["is_panel"]:
            win.setStyleMask_(win.styleMask() | NS_NONACTIVATING_PANEL)
            win.setBecomesKeyOnlyIfNeeded_(True)
            win.setFloatingPanel_(True)
        win.setHidesOnDeactivate_(False)
        # Qt gives tool windows MoveToActiveSpace, which AppKit refuses to combine
        # with CanJoinAllSpaces: clear it first.
        behavior = (win.collectionBehavior() & ~MOVE_TO_ACTIVE_SPACE) | CAN_JOIN_ALL_SPACES | FULLSCREEN_AUXILIARY
        win.setCollectionBehavior_(behavior)
        # A style-mask change after creation does not always reach the window
        # server; this private setter is the documented community workaround.
        # Optional: if it disappears in a future macOS, the public flags remain.
        if win.respondsToSelector_("_setPreventsActivation:"):
            win._setPreventsActivation_(True)
            report["prevents_activation"] = True
        report["style_mask"] = int(win.styleMask())
        report["nonactivating"] = bool(win.styleMask() & NS_NONACTIVATING_PANEL)
        report["ok"] = True
    except Exception as exc:  # noqa: BLE001
        report["error"] = repr(exc)
        LOG.warning("non-activating setup failed", exc_info=True)
    return report


def frontmost_app() -> str | None:
    try:
        from AppKit import NSWorkspace

        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        return app.bundleIdentifier() if app else None
    except Exception:  # noqa: BLE001
        return None


def bring_to_front() -> None:
    """Activates OmonFlow so a window it opens on its own (not after a click) is in front."""
    try:
        from AppKit import NSApplication

        app = NSApplication.sharedApplication()
        if hasattr(app, "activate"):
            app.activate()
        else:
            app.activateIgnoringOtherApps_(True)
    except Exception:  # noqa: BLE001 - cosmetic
        pass
