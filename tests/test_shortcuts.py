from omonflow.shortcuts.service import BackendError, ShortcutService, ShortcutStatus, pretty


class FakeBackend:
    def __init__(self, fail=None):
        self.fail = fail
        self.registered = {}
        self.events = []

    def register(self, spec):
        if self.fail:
            raise BackendError(self.fail, f"{spec}: {self.fail.value}")
        self.registered[1] = spec
        return 1

    def unregister(self, hid):
        self.registered.pop(hid, None)

    def poll(self):
        ev, self.events = self.events, []
        return ev


def test_registered_and_activates(qapp):
    be = FakeBackend()
    svc = ShortcutService(lambda: be)
    assert svc.apply("cmd+shift+space") == ShortcutStatus.REGISTERED
    hits = []
    svc.activated.connect(lambda: hits.append(1))
    be.events = [(1, "pressed"), (1, "released"), (2, "pressed")]
    svc._poll()
    assert hits == [1]  # only our id, only on press


def test_conflict_is_reported_never_substituted(qapp):
    be = FakeBackend(fail=ShortcutStatus.CONFLICT)
    svc = ShortcutService(lambda: be)
    assert svc.apply("cmd+shift+space") == ShortcutStatus.CONFLICT
    assert be.registered == {} and svc.spec == "cmd+shift+space"
    assert "different shortcut" in svc.message


def test_unavailable_backend(qapp):
    def factory():
        raise BackendError(ShortcutStatus.NOT_SUPPORTED, "Wayland")
    svc = ShortcutService(factory)
    assert svc.apply("ctrl+alt+space") == ShortcutStatus.NOT_SUPPORTED


def test_disable_unregisters(qapp):
    be = FakeBackend()
    svc = ShortcutService(lambda: be)
    svc.apply("cmd+shift+space")
    assert svc.apply("cmd+shift+space", enabled=False) == ShortcutStatus.DISABLED
    assert be.registered == {}


def test_pretty_names():
    import sys
    if sys.platform == "darwin":
        assert pretty("cmd+shift+space") == "⇧⌘Space"
    else:
        assert pretty("ctrl+alt+space") == "Ctrl+Alt+Space"


def test_native_parser_accepts_default():
    from omonflow import _core
    from omonflow.storage.settings import default_shortcut
    assert _core.HotkeyManager.parse(default_shortcut())
