"""A file-backed ``keyring`` backend standing in for the OS keychain in CLI tests.

Spawned CLI processes select it with
``PYTHON_KEYRING_BACKEND=tests.e2e._file_keyring_backend.FileKeyring`` (the repo
root on ``PYTHONPATH``); ``OMNIGENT_TEST_KEYRING_FILE`` names the JSON store.
Entries are keyed ``"<service>\\x00<username>"`` so the ``keyring`` CLI and
``omnigent.onboarding.secrets`` share one store.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import keyring.backend
import keyring.errors

STORE_ENV = "OMNIGENT_TEST_KEYRING_FILE"


def store_path() -> Path:
    configured = os.environ.get(STORE_ENV)
    if not configured:
        raise RuntimeError(f"{STORE_ENV} must name the JSON store when FileKeyring is selected")
    return Path(configured)


def _key(service: str, username: str) -> str:
    return f"{service}\x00{username}"


class FileKeyring(keyring.backend.KeyringBackend):
    priority = 10

    def _read(self) -> dict[str, str]:
        path = store_path()
        if not path.exists():
            return {}
        data: dict[str, str] = json.loads(path.read_text(encoding="utf-8"))
        return data

    def _write(self, entries: dict[str, str]) -> None:
        path = store_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entries, indent=2), encoding="utf-8")

    def get_password(self, service: str, username: str) -> str | None:
        return self._read().get(_key(service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        entries = self._read()
        entries[_key(service, username)] = password
        self._write(entries)

    def delete_password(self, service: str, username: str) -> None:
        entries = self._read()
        try:
            del entries[_key(service, username)]
        except KeyError:
            raise keyring.errors.PasswordDeleteError(
                f"no entry for service {service!r}, username {username!r}"
            ) from None
        self._write(entries)
