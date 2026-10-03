"""
Regression test: ``omnigent uninstall --purge`` must delete the OS-keychain secret.

``omnigent setup`` stores a pasted API key in the OS keychain (service
``omnigent``, via ``omnigent.onboarding.secrets``) and references it as
``keychain:<name>`` in ``~/.omnigent/config.yaml``. ``omnigent uninstall state
--purge --yes`` must delete that keychain entry along with ``~/.omnigent`` and
report the removal in its action list.

The OS keychain is stood in for by the file-backed ``keyring`` backend in
``tests/e2e/_file_keyring_backend.py`` (selected via ``PYTHON_KEYRING_BACKEND``)
so the journey runs on headless Linux CI; the macOS Keychain itself is not
exercised.

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
from dataclasses import dataclass
from pathlib import Path

import pytest

pexpect = pytest.importorskip("pexpect")

_REPO_ROOT = Path(__file__).resolve().parents[2]

KEYRING_SERVICE = "omnigent"
SECRET_NAME = "anthropic"
TEST_KEY = "sk-ant-test-purge-keychain-not-a-real-key"
TEST_MODEL = "claude-sonnet-4-5"
KEYRING_BACKEND = "tests.e2e._file_keyring_backend.FileKeyring"

_POINTER = "❯"
_ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[=>]")
# Ambient credentials would be auto-adopted by setup and change the menus.
_AMBIENT_ENV_RE = re.compile(
    r"API_KEY|TOKEN|^DATABRICKS_|^OMNIGENT_(CONFIG_HOME|DATA_DIR|DISABLE_KEYRING)$"
)


def cli_env(home: Path) -> dict[str, str]:
    """Environment for CLI processes: fresh HOME, repo code, stand-in keychain."""
    home.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not _AMBIENT_ENV_RE.search(k)}
    env.update(
        {
            "HOME": str(home),
            "PYTHONPATH": str(_REPO_ROOT),
            "PYTHON_KEYRING_BACKEND": KEYRING_BACKEND,
            "OMNIGENT_TEST_KEYRING_FILE": str(home / ".test-keychain.json"),
            "NO_COLOR": "1",
            "TERM": "xterm",
        }
    )
    return env


def _read_quiet(child: pexpect.spawn, settle: float = 1.5) -> bytes:
    """Return everything pexpect already buffered plus PTY output until *settle* seconds pass."""
    buf = bytes(child.buffer)
    child.buffer = b""
    deadline = time.time() + settle
    while time.time() < deadline:
        try:
            buf += child.read_nonblocking(4096, timeout=0.3)
        except pexpect.TIMEOUT:
            continue
        except pexpect.EOF:
            break
    return buf


def _plain(buf: bytes) -> str:
    return _ANSI_RE.sub(b"", buf).decode("utf-8", "replace")


def _pointer_row(buf: bytes) -> str:
    rows = [line.strip() for line in _plain(buf).splitlines() if _POINTER in line]
    return rows[-1] if rows else ""


def _choose(child: pexpect.spawn, label: str, transcript: list[bytes]) -> None:
    """Move the menu pointer with ``j`` until it sits on *label*, then press Enter."""
    frame = _read_quiet(child)
    transcript.append(frame)
    row = _pointer_row(frame)
    for _ in range(16):
        if label in row:
            child.send(b"\r")
            return
        child.send(b"j")
        frame = _read_quiet(child, settle=0.8)
        transcript.append(frame)
        row = _pointer_row(frame) or row
    raise AssertionError(f"menu row {label!r} not reached; last pointer row: {row!r}")


def store_anthropic_key_via_setup(
    env: dict[str, str], key: str = TEST_KEY, transcript: list[bytes] | None = None
) -> bytes:
    """Drive the real ``omnigent setup`` TUI to add an Anthropic API key; return the transcript."""
    child = pexpect.spawn(
        sys.executable,
        ["-m", "omnigent", "setup"],
        env=env,
        encoding=None,
        dimensions=(40, 120),
        timeout=90,
    )
    if transcript is None:
        transcript = []
    try:
        child.expect(re.compile(rb"Configure harnesses"), timeout=90)
        transcript.append(child.before + child.after)
        _choose(child, "Claude", transcript)
        child.expect(re.compile(rb"select or add a credential"), timeout=30)
        transcript.append(child.before + child.after)
        _choose(child, "+ Add a credential", transcript)
        child.expect(re.compile(rb"What do you want to add"), timeout=30)
        transcript.append(child.before + child.after)
        _choose(child, "Anthropic", transcript)
        child.expect(re.compile(rb"paste your key, then press Enter"), timeout=30)
        transcript.append(child.before + child.after)
        child.sendline(key)
        child.expect(re.compile(rb"Default model"), timeout=30)
        transcript.append(child.before + child.after)
        child.sendline(TEST_MODEL)
        child.expect(re.compile(rb"select or add a credential"), timeout=30)
        transcript.append(child.before + child.after)
        transcript.append(_read_quiet(child))
        child.send(b"\x1b")
        child.expect(re.compile(rb"Configure harnesses"), timeout=30)
        transcript.append(child.before + child.after)
        transcript.append(_read_quiet(child))
        child.send(b"\x1b")
        child.expect(pexpect.EOF, timeout=30)
        transcript.append(child.before)
    finally:
        if child.isalive():
            child.terminate(force=True)
    return b"".join(transcript)


def keyring_get(
    env: dict[str, str], service: str, username: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "keyring", "get", service, username],
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )


def run_uninstall(env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "omnigent", "uninstall", *args],
        env=env,
        check=False,
        text=True,
        capture_output=True,
    )


@dataclass
class PurgeOutcome:
    home: Path
    purge: subprocess.CompletedProcess[str]
    payload: dict[str, object]
    secret_after_purge: str | None
    lookup_after_purge: subprocess.CompletedProcess[str]


@pytest.fixture(scope="module")
def purge_outcome(tmp_path_factory: pytest.TempPathFactory) -> PurgeOutcome:
    home = tmp_path_factory.mktemp("purge") / "home"
    env = cli_env(home)
    store_anthropic_key_via_setup(env)

    config = (home / ".omnigent" / "config.yaml").read_text(encoding="utf-8")
    assert f"keychain:{SECRET_NAME}" in config, config
    assert not (home / ".omnigent" / "secrets.json").exists(), "setup fell back to the file store"
    before = keyring_get(env, KEYRING_SERVICE, SECRET_NAME)
    assert before.stdout.strip() == TEST_KEY, (before.stdout, before.stderr)

    purge = run_uninstall(env, "state", "--purge", "--yes", "--json")
    assert purge.returncode == 0, (purge.stdout, purge.stderr)
    assert not (home / ".omnigent").exists(), "purge did not remove the state directory"
    payload = json.loads(purge.stdout)

    after = keyring_get(env, KEYRING_SERVICE, SECRET_NAME)
    secret = after.stdout.strip() or None
    return PurgeOutcome(
        home=home,
        purge=purge,
        payload=payload,
        secret_after_purge=secret,
        lookup_after_purge=after,
    )


def test_purge_deletes_keychain_secret(purge_outcome: PurgeOutcome) -> None:
    assert purge_outcome.secret_after_purge is None, (
        f"keychain entry {KEYRING_SERVICE}/{SECRET_NAME} survived `uninstall state --purge`"
    )
    lookup = purge_outcome.lookup_after_purge
    # `keyring get` exits 1 silently for a clean miss; a broken backend prints a traceback.
    assert lookup.returncode == 1 and "Traceback" not in lookup.stderr, (
        lookup.returncode,
        lookup.stderr,
    )


def test_purge_output_reports_keychain_secret_removal(purge_outcome: PurgeOutcome) -> None:
    actions = purge_outcome.payload["actions"]
    assert isinstance(actions, list)
    keychain_actions = [
        action for action in actions if action.get("artifact") == "keychain_secret"
    ]
    assert keychain_actions, f"purge actions never mention the keychain secret: {actions}"
    assert [(action["path"], action["status"]) for action in keychain_actions] == [
        (SECRET_NAME, "done")
    ], keychain_actions
