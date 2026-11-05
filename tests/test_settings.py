import json
import sys

from omonflow.storage.settings import DEFAULT_VISUALIZER, Settings, SettingsStore, default_shortcut


def test_fresh_install_defaults(config_dir):
    s = SettingsStore().load()
    assert s.visualizer == "spectrum-pill" == DEFAULT_VISUALIZER
    assert s.shortcut == default_shortcut()
    if sys.platform == "darwin":
        assert s.shortcut == "cmd+shift+space"


def test_round_trip(config_dir):
    store = SettingsStore()
    s = store.load()
    s.visualizer = "orb"
    s.widget_pos = [10, 20]
    store.save(s)
    again = SettingsStore().load()
    assert again.visualizer == "orb" and again.widget_pos == [10, 20]


def test_corrupt_file_is_backed_up_and_defaults_used(config_dir):
    (config_dir / "settings.json").write_text("{not json")
    store = SettingsStore()
    s = store.load()
    assert s == Settings()
    assert store.recovered_from is not None and store.recovered_from.exists()
    assert store.recovered_from.read_text() == "{not json"


def test_unknown_keys_ignored_and_known_kept(config_dir):
    (config_dir / "settings.json").write_text(json.dumps({"visualizer": "circular-wave", "future_key": 1}))
    assert SettingsStore().load().visualizer == "circular-wave"


def test_atomic_write_leaves_no_temp_files(config_dir):
    store = SettingsStore()
    store.save(Settings())
    assert [p.name for p in config_dir.iterdir()] == ["settings.json"]
