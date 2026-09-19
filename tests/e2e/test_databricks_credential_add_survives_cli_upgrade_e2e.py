"""Regression test: a failed Databricks CLI auto-upgrade must not kill `omnigent setup`.

Reported journey:

1. The machine already has a Databricks CLI installed at
   ``/usr/local/bin/databricks`` and is already logged into a workspace
   (a profile exists in ``~/.databrickscfg``).
2. The user opens the credential picker (``omnigent setup``), drills into a
   harness, chooses "+ Add a credential" -> "Databricks - workspace", and
   pastes the workspace URL.
3. The flow shells out to ``ucode configure``, whose preflight
   (``install_databricks_cli`` -> ``ensure_databricks_cli_version``) decides
   the Databricks CLI is too old and auto-upgrades it via the setup-cli
   ``install.sh`` -- which always refuses to overwrite an existing
   ``/usr/local/bin/databricks``:
   "Databricks CLI is too old. Upgrading... If you have an existing Databricks
   CLI installation, please first remove it using 'sudo rm
   /usr/local/bin/databricks'. Failed to install/upgrade Databricks CLI
   automatically."
4. ``ucode configure`` exits non-zero, and before the fix the resulting
   ``ClickException`` propagated out of the menu loop and killed the whole
   setup TUI ("Error: `ucode configure` exited with code 1"), leaving the user
   with only a manual ``sudo rm`` + curl-install escape they had to find
   elsewhere.

The upgrade failure itself happens inside the external, pinned ucode CLI and
cannot be prevented from this repo (``--skip-upgrade`` only skips *agent CLI*
update prompts; the Databricks CLI preflight is unconditional). What omnigent
owns is how its setup TUI reacts: the fixed behavior is that the failed add
aborts back to the credential menu with a "✗ Databricks credential not added"
status and the manual upgrade commands printed in-product, no half-configured
provider is persisted, and the user can quit setup cleanly.

The test drives the real ``omnigent setup`` TUI under a pseudo-TTY through
that exact journey. The one stand-in is ucode itself: the pinned
``git+https://github.com/databricks/ucode`` source is unreachable from CI, so
a local ``ucode`` stub reproduces its verbatim, deterministic configure-time
CLI-upgrade failure. (``uvx`` is kept off PATH so
:func:`omnigent.onboarding.ucode_setup.find_ucode_command` resolves the local
binary - its documented fallback.)

It FAILS on the buggy build (the TUI dies with "Error: `ucode configure`
exited with code 1") and passes once the add degrades gracefully.

Usage::

    python -m pytest -v --timeout=300 \\
        tests/e2e/test_databricks_credential_add_survives_cli_upgrade_e2e.py
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import sys
import time
from pathlib import Path

import pytest

pexpect = pytest.importorskip("pexpect")

_REPO_ROOT = Path(__file__).resolve().parents[2]

_WORKSPACE_URL = "https://myws.cloud.databricks.com"

# The accent pointer select() prefixes the highlighted row with.
_POINTER = "❯"

# Strip ANSI escape sequences (CSI, OSC, and keypad-mode toggles) so menu
# rows can be matched as plain text.
_ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[=>]")

# The reported user-visible failure, emitted by ucode's
# Databricks CLI auto-upgrade when /usr/local/bin/databricks already exists.
_UPGRADE_FAILURE = b"Failed to install/upgrade Databricks CLI automatically"

# Stand-in for the pinned databricks/ucode CLI (the git source is unreachable
# from this environment). `configure` models the reported machine state: the
# unconditional Databricks CLI version preflight decides the CLI is too old,
# and its setup-cli auto-upgrade refuses to overwrite the existing
# /usr/local/bin/databricks, so the command always exits 1.
_STUB_UCODE = '''#!/usr/bin/env python3
"""Test stand-in for the pinned databricks/ucode CLI."""
import sys


def main() -> int:
    argv = sys.argv[1:]
    if not argv or argv[0] != "configure":
        sys.stderr.write("stub ucode: only `configure` is modelled\\n")
        return 2
    # Verbatim reported user-visible failure: the auto-upgrade (setup-cli
    # install.sh) refuses to overwrite an existing /usr/local/bin/databricks.
    print("Databricks CLI is too old. Upgrading...")
    print(
        "If you have an existing Databricks CLI installation, please "
        "first remove it using 'sudo rm /usr/local/bin/databricks'."
    )
    print("Failed to install/upgrade Databricks CLI automatically.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
'''


def _which(cmd: str, path: str) -> str | None:
    """Resolve *cmd* against an explicit PATH string."""
    return shutil.which(cmd, path=path)


def _build_env(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """Build the reported machine state and the env for the spawned TUI.

    - a fresh ``$HOME`` whose ``~/.databrickscfg`` already has a profile for
      the workspace (the user logged in previously, so the add flow reuses it
      without a browser OAuth);
    - a local ``ucode`` stub on PATH, with ``uvx`` removed so
      ``find_ucode_command`` falls back to it;
    - ambient credential env stripped so the picker renders the deterministic
      first-run menu.

    :param tmp_path: Per-test temp dir.
    :returns: ``(env, home)`` for ``pexpect.spawn``.
    """
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / ".databrickscfg").write_text(f"[myws]\nhost = {_WORKSPACE_URL}\n")

    stub_bin = tmp_path / "bin"
    stub_bin.mkdir(parents=True, exist_ok=True)
    stub = stub_bin / "ucode"
    stub.write_text(_STUB_UCODE)
    stub.chmod(0o755)

    path_entries = [str(stub_bin)]
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        # Drop dirs providing uvx so find_ucode_command picks the local stub.
        if entry and not (Path(entry) / "uvx").exists():
            path_entries.append(entry)

    env = dict(os.environ)
    for key in list(env):
        if key.startswith(
            ("ANTHROPIC_", "OPENAI_", "DATABRICKS_", "GEMINI_", "OPENROUTER_", "OMNIGENT_")
        ):
            env.pop(key)
    env.update(
        {
            "HOME": str(home),
            "PATH": os.pathsep.join(path_entries),
            "PYTHONPATH": str(_REPO_ROOT),
            "NO_COLOR": "1",
            "TERM": "xterm",
        }
    )
    return env, home


def _read_quiet(child: pexpect.spawn, settle: float = 1.0) -> bytes:
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


def _select_row(child: pexpect.spawn, fragment: str, max_steps: int = 40) -> None:
    """Move the menu pointer to the row containing *fragment* and press Enter.

    Menus place the pointer on the first row when they open; the targets in
    this journey all sit at or below it, so stepping down with ``j`` (the
    single-byte fallback arrow) always reaches them.
    """
    buf = _read_quiet(child, settle=1.0)
    for _ in range(max_steps):
        rows = _pointer_rows(buf)
        if rows and fragment in rows[-1]:
            child.send("\r")
            return
        child.send("j")
        buf = _read_quiet(child, settle=0.4)
    pytest.fail(f"never reached a menu row containing {fragment!r}; last frame rows: {rows!r}")


def test_add_databricks_credential_survives_cli_upgrade_failure(tmp_path: Path) -> None:
    """A failed Databricks CLI auto-upgrade aborts the add, not the whole setup TUI."""
    env, home = _build_env(tmp_path)
    if _which("databricks", env["PATH"]) is None:
        pytest.skip("databricks CLI not on PATH - the add flow needs it for profile discovery")
    if _which("claude", env["PATH"]) is None:
        pytest.skip("claude CLI not on PATH - needed to drill into the Claude harness menu")

    child = pexpect.spawn(
        sys.executable,
        ["-m", "omnigent", "setup"],
        env=env,
        encoding=None,
        dimensions=(40, 120),
        timeout=120,
    )
    try:
        # Level 1: the harness overview. Claude is the first row.
        child.expect(re.compile(rb"Configure harnesses"), timeout=120)
        _select_row(child, "Claude")

        # Level 2: Claude's credential menu.
        child.expect(re.compile(rb"select or add a credential"), timeout=60)
        _select_row(child, "Add a credential")

        # The add menu, scoped to Claude's surface.
        child.expect(re.compile(rb"What do you want to add\?"), timeout=60)
        _select_row(child, "Databricks")

        # The workspace URL prompt; the profile already exists, so no browser
        # OAuth follows - the flow goes straight to `ucode configure`.
        child.expect(re.compile(rb"Databricks workspace URL"), timeout=60)
        child.sendline(_WORKSPACE_URL)
        child.expect(re.compile(rb"using existing Databricks profile"), timeout=60)

        # The stub ucode always dies on its Databricks CLI self-upgrade -- the
        # scenario's trigger, present on buggy and fixed builds alike.
        child.expect(re.compile(_UPGRADE_FAILURE), timeout=120)

        # Fixed: the add aborts back into the TUI with a "✗ ... not added"
        # status. Buggy: the ClickException kills the whole setup process
        # ("Error: `ucode configure` exited with code 1", then EOF).
        outcome = child.expect(
            [
                re.compile(rb"Databricks credential not added"),
                re.compile(rb"Error: `ucode configure` exited with code"),
                pexpect.EOF,
            ],
            timeout=120,
        )
        if outcome != 0:
            pytest.fail(
                "Bug reproduced: `ucode configure`'s Databricks CLI auto-upgrade "
                "failure killed the whole setup TUI instead of aborting just the "
                "credential add."
            )

        # The manual-upgrade escape printed in-product, before the ✗ status.
        before = _ANSI_RE.sub(b"", child.before or b"").decode("utf-8", "replace")
        assert "sudo rm /usr/local/bin/databricks" in before, (
            f"recovery guidance missing from output before the ✗ status: {before!r}"
        )

        # The credential menu re-renders -- the TUI is alive; quit it cleanly.
        child.expect(re.compile(rb"select or add a credential"), timeout=60)
        _read_quiet(child, settle=1.0)
        child.send("\x1b")  # Esc: back to the harness overview
        _read_quiet(child, settle=1.0)
        child.send("\x1b")  # Esc: exit setup
        child.expect(pexpect.EOF, timeout=60)
        child.close()
        assert child.exitstatus == 0, (
            f"setup exited {child.exitstatus} (signal {child.signalstatus}) after the failed add"
        )

        # No half-configured provider was persisted.
        config_path = home / ".omnigent" / "config.yaml"
        if config_path.exists():
            config = config_path.read_text()
            assert "databricks" not in config, (
                f"half-configured provider persisted in {config_path}:\n{config}"
            )
    finally:
        with contextlib.suppress(Exception):
            child.close(force=True)
