"""API keys in the OS keychain (macOS Keychain, Windows Credential Manager,
Secret Service on Linux) via `keyring`. Never written to settings or logs.

One key per server address, so switching servers never sends a key to the
wrong place.
"""

from __future__ import annotations

import logging

from omonflow.refinement.ai import normalize_base_url

LOG = logging.getLogger("omonflow.secrets")
SERVICE = "OmonFlow AI refinement"


class SecretError(Exception):
    pass


class KeyStore:
    def __init__(self, backend=None) -> None:
        self._backend = backend  # tests pass an in-memory keyring backend

    def _kr(self):
        import keyring

        return self._backend or keyring.get_keyring()

    def get(self, base_url: str) -> str | None:
        url = normalize_base_url(base_url)
        if not url:
            return None
        try:
            return self._kr().get_password(SERVICE, url)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("keychain read failed: %s", type(exc).__name__)
            return None

    def set(self, base_url: str, key: str | None) -> None:
        url = normalize_base_url(base_url)
        if not url:
            raise SecretError("Set the server address first.")
        try:
            if key:
                self._kr().set_password(SERVICE, url, key)
            else:
                try:
                    self._kr().delete_password(SERVICE, url)
                except Exception:  # noqa: BLE001 - nothing stored
                    pass
        except Exception as exc:  # noqa: BLE001
            raise SecretError(f"Could not save the API key in the keychain ({type(exc).__name__}).") from exc
