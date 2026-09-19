"""
Regression test: ``omnigent uninstall --purge`` leaves the OS-keychain
secret (keyring service ``omnigent``) behind.

Journey: the user stores a provider API key through the real
``omnigent setup`` wizard (Claude -> Add a credential -> Anthropic API
key), which writes it to the OS keychain via
``omnigent.onboarding.secrets.store_secret`` under service ``omnigent``.
They then run ``omnigent uninstall state --purge --yes``. The purge
removes ``~/.omnigent`` and exits 0, but the keychain secret survives
(``security find-generic-password -s omnigent`` still succeeds on
macOS) and nothing in the purge output says a credential was left
behind.

The wizard is driven under a pseudo-TTY (pexpect) so the real
raw-termios menus and hidden-input key paste run. The OS keychain is a
file-backed ``keyring`` backend selected via ``PYTHON_KEYRING_BACKEND``;
like the real macOS Keychain / GNOME Keyring it persists outside the
omnigent state home, so only the purge path's explicit
``keyring.delete_password`` can clear it.

Usage::

    python -m pytest tests/e2e/test_uninstall_purge_keychain_e2e.py -v --timeout=300
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

pexpect = pytest.importorskip("pexpect")

_REPO_ROOT = Path(__file__).resolve().parents[2]

_SECRET = "test-anthropic-key-e2e-uninstall-purge"

# Strip ANSI escape sequences (CSI, OSC, and keypad-mode toggles) so menu
# rows can be matched as plain text.
_ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[=>]")

# The accent pointer select() prefixes the highlighted row with.
_POINTER = "❯"

# A minimal file-backed keyring backend standing in for the OS keychain:
# real ``keyring`` API, storage outside the omnigent state home.
_FAKE_KEYCHAIN_BACKEND = '''\
"""File-backed keyring backend standing in for the OS keychain."""
import json
import os

import keyring.backend
import keyring.errors


class FakeKeychain(keyring.backend.KeyringBackend):
    priority = 100

    def _read(self):
        try:
            with open(os.environ["FAKE_KEYCHAIN_PATH"], encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return {}

    def _write(self, data):
        with open(os.environ["FAKE_KEYCHAIN_PATH"], "w", encoding="utf-8") as f:
            json.dump(data, f)

    def get_password(self, service, username):
        return self._read().get(service, {}).get(username)

    def set_password(self, service, username, password):
        data = self._read()
        data.setdefault(service, {})[username] = password
        self._write(data)

    def delete_password(self, service, username):
        data = self._read()
        try:
            del data[service][username]
        except KeyError:
            raise keyring.errors.PasswordDeleteError(username)
        self._write(data)
'''


def _keychain_entries(keychain_path: Path) -> dict[str, dict[str, str]]:
    """Return the fake keychain's ``{service: {name: secret}}`` mapping."""
    if not keychain_path.exists():
        return {}
    with open(keychain_path, encoding="utf-8") as f:
        data: dict[str, dict[str, str]] = json.load(f)
    return data


def _build_env(tmp_path: Path) -> dict[str, str]:
    """Build the spawned CLI's environment around an isolated ``$HOME``.

    Provider API keys are stripped so the wizard's "Detected ... in the
    environment - use it?" shortcut never replaces the keychain paste, and
    omnigent home/keyring overrides are stripped so the run is hermetic.
    """
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    dropped = ("OMNIGENT_CONFIG_HOME", "OMNIGENT_DATA_DIR", "OMNIGENT_DISABLE_KEYRING")
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.endswith("_API_KEY") and k not in dropped
    }
    backend_dir = tmp_path / "keyring-backend"
    backend_dir.mkdir(exist_ok=True)
    (backend_dir / "fake_keychain_backend.py").write_text(_FAKE_KEYCHAIN_BACKEND)
    # A stub `claude` satisfies harness_cli_installed("anthropic") so the
    # drill-in goes to credential setup instead of the install prompt.
    stub_bin = tmp_path / "stub-bin"
    stub_bin.mkdir(exist_ok=True)
    stub = stub_bin / "claude"
    stub.write_text('#!/bin/sh\necho "2.999.0 (Claude Code)"\n')
    stub.chmod(0o755)
    env.update(
        {
            "HOME": str(home),
            "PATH": f"{stub_bin}{os.pathsep}{env.get('PATH', '')}",
            "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{backend_dir}",
            "NO_COLOR": "1",
            "TERM": "xterm",
            "PYTHON_KEYRING_BACKEND": "fake_keychain_backend.FakeKeychain",
            "FAKE_KEYCHAIN_PATH": str(tmp_path / "fake-keychain.json"),
        }
    )
    return env


def _drain(child: pexpect.spawn, settle: float = 2.0) -> bytes:
    """Drain PTY output until *settle* seconds elapse."""
    buf = b""
    deadline = time.time() + settle
    while time.time() < deadline:
        try:
            buf += child.read_nonblocking(4096, timeout=0.3)
        except pexpect.TIMEOUT:
            continue
        except pexpect.EOF:
            break
    return buf


def _pointer_rows(buf: bytes) -> list[str]:
    """Extract the menu rows carrying the selection pointer from raw output."""
    text = _ANSI_RE.sub(b"", buf).decode("utf-8", "replace")
    return [line.strip() for line in text.splitlines() if _POINTER in line]


def _store_key_via_setup_wizard(env: dict[str, str]) -> None:
    """Drive the real ``omnigent setup`` wizard to store an Anthropic key."""
    child = pexpect.spawn(
        sys.executable,
        ["-m", "omnigent", "setup"],
        env=env,
        encoding=None,
        dimensions=(40, 120),
        timeout=180,
    )
    try:
        child.expect(rb"Quit", timeout=180)
        buf = (child.before or b"") + (child.after or b"") + _drain(child)
        rows = _pointer_rows(buf)
        assert rows and "Claude" in rows[-1], (
            f"setup menu no longer opens with the pointer on Claude "
            f"(pointer rows: {rows}); update this driver"
        )
        child.send(b"\r")  # Claude
        child.expect(rb"add a credential", timeout=60)
        _drain(child, 1.0)
        child.send(b"\r")  # + Add a credential
        child.expect(rb"What do you want to add\?", timeout=60)
        _drain(child, 1.0)
        child.send(b"\r")  # Anthropic - API key
        child.expect(rb"Anthropic API key", timeout=60)
        _drain(child, 1.0)
        child.send(_SECRET.encode() + b"\r")
        child.expect(rb"Default model", timeout=60)
        _drain(child, 1.0)
        child.send(b"claude-sonnet-4-5\r")
        child.expect(rb"Added anthropic", timeout=60)
        _drain(child, 1.0)
        child.send(b"\x1b")  # Esc: back to the harness picker
        _drain(child, 1.0)
        child.send(b"\x1b")  # Esc: quit setup
        child.expect(pexpect.EOF, timeout=60)
    finally:
        child.close(force=True)


def test_uninstall_purge_removes_or_reports_keychain_secret(tmp_path: Path) -> None:
    env = _build_env(tmp_path)
    keychain_path = Path(env["FAKE_KEYCHAIN_PATH"])
    home = Path(env["HOME"])
    state_dir = home / ".omnigent"

    _store_key_via_setup_wizard(env)

    config = (state_dir / "config.yaml").read_text()
    assert "keychain:anthropic" in config, (
        f"setup did not record a keychain api_key_ref; config was:\n{config}"
    )
    stored = _keychain_entries(keychain_path).get("omnigent", {}).get("anthropic")
    assert stored == _SECRET, (
        f"setup did not store the key in the OS keychain (service 'omnigent'); "
        f"keychain held: {_keychain_entries(keychain_path)}"
    )

    # `--purge` implies the state target; naming it explicitly keeps the run
    # off the cli target (which would uninstall this environment's wheel).
    result = subprocess.run(
        [sys.executable, "-m", "omnigent", "uninstall", "state", "--purge", "--yes", "--json"],
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, f"uninstall --purge failed:\n{output}"
    assert not state_dir.exists(), f"purge did not remove the state dir:\n{output}"

    leftover = _keychain_entries(keychain_path).get("omnigent", {}).get("anthropic")
    reported = re.search(r"keychain|secret", output, re.IGNORECASE)
    assert leftover is None or reported, (
        "uninstall --purge left the OS-keychain secret (service 'omnigent', "
        f"name 'anthropic') behind and never reported it; purge output was:\n{output}"
    )
