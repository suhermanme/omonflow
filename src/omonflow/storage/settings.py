"""Persistent settings: versioned JSON, atomic writes, corruption recovery."""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

import platformdirs

from omonflow import APP_NAME, ORG_NAME

LOG = logging.getLogger("omonflow.settings")
SCHEMA_VERSION = 1
DEFAULT_VISUALIZER = "spectrum-pill"


def default_shortcut() -> str:
    # SPEC §5.2. macOS default decided by the owner; Windows/Linux defaults are
    # candidates until the M08 survey. Registered combinations only (no
    # keyboard monitoring) in M02.
    if sys.platform == "darwin":
        return "cmd+shift+space"
    return "ctrl+alt+space"


def config_dir() -> Path:
    override = os.environ.get("OMONFLOW_CONFIG_DIR")  # tests and diagnostics
    return Path(override) if override else Path(platformdirs.user_config_dir(APP_NAME, ORG_NAME))


def data_dir() -> Path:
    override = os.environ.get("OMONFLOW_CONFIG_DIR")
    return Path(override) / "data" if override else Path(platformdirs.user_data_dir(APP_NAME, ORG_NAME))


def log_dir() -> Path:
    override = os.environ.get("OMONFLOW_CONFIG_DIR")
    return Path(override) / "logs" if override else Path(platformdirs.user_log_dir(APP_NAME, ORG_NAME))


@dataclass
class Settings:
    schema_version: int = SCHEMA_VERSION
    visualizer: str = DEFAULT_VISUALIZER
    shortcut: str = field(default_factory=default_shortcut)
    shortcut_enabled: bool = True
    widget_visible_on_launch: bool = True
    always_on_top: bool = True
    widget_pos: list[int] | None = None
    notifications: bool = True
    audio_device_id: str | None = None    # Rust core device id (None = system default)
    audio_device_name: str | None = None  # also used by the Python fallback
    mic_timeout_ms: int = 3000
    whisper_model: str = "small"

    @classmethod
    def from_dict(cls, data: dict) -> "Settings":
        known = {f.name for f in fields(cls)}
        clean = {k: v for k, v in data.items() if k in known}
        s = cls(**clean)
        s.schema_version = SCHEMA_VERSION
        return s


class SettingsStore:
    """Loads and saves Settings. Never raises on bad files: backs them up."""

    def __init__(self, directory: Path | None = None) -> None:
        self.dir = directory or config_dir()
        self.path = self.dir / "settings.json"
        self.recovered_from: Path | None = None  # set when a corrupt file was backed up

    def load(self) -> Settings:
        if not self.path.exists():
            return Settings()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("settings root is not an object")
            return Settings.from_dict(data)
        except (OSError, ValueError, TypeError) as exc:
            backup = self.path.with_name(f"settings.corrupt-{int(time.time())}.json")
            try:
                self.path.replace(backup)
                self.recovered_from = backup
            except OSError:
                pass
            LOG.warning("settings file unreadable (%s); using defaults, backup at %s", exc, backup)
            return Settings()

    def save(self, settings: Settings) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(asdict(settings), indent=2, sort_keys=True)
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".settings-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
