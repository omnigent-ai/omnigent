"""
Regression test: ``omnigent setup`` warns when the installed tmux is too old.

The harness-dependency preflight (``_warn_missing_harness_dependencies`` in
``omnigent/cli_config.py``) only checks that a ``tmux`` binary exists on
``PATH`` — it never checks its version. The native tmux-backed harnesses
(Claude, Codex, and every managed terminal in ``omnigent/inner/terminal.py``)
rely on tmux 3.x features, including the ``allow-passthrough`` option added
in tmux 3.3. An ancient tmux (e.g. the 1.8 that CentOS 7 ships) therefore
fails late instead of being flagged up front.

This test spawns the real ``omni setup`` flow under a pseudo-TTY (the same
code path a human types into) with a stub ``tmux`` first on ``PATH`` whose
``tmux -V`` reports an ancient version, and asserts that the pre-menu
warning block mentions tmux. On the buggy build no tmux warning is printed
at all — the assertion FAILS — and it passes once setup gains a minimum
tmux version check. A companion test pins the complement: a modern tmux
must NOT trigger a warning, so the fix can't over-warn.

Usage::

    python -m pytest tests/e2e/test_setup_tmux_version_warning_e2e.py -v --timeout=300
"""

from __future__ import annotations

import contextlib
import os
import re
import sys
import time
from pathlib import Path

import pytest

pexpect = pytest.importorskip("pexpect")

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Strip ANSI escape sequences (CSI, OSC, and keypad-mode toggles) so the
# pre-menu output can be matched as plain text.
_ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[=>]")

# An ancient tmux comfortably below the managed-terminal 3.3 floor.
_OUTDATED_TMUX_VERSION = "tmux 1.8"

# A current tmux release, at/above every feature the managed terminals and
# pane integration use — must never trigger the outdated-version warning.
_MODERN_TMUX_VERSION = "tmux 3.5a"


def _make_stub_tmux(bin_dir: Path, version_line: str) -> None:
    """
    Write an executable ``tmux`` stub that answers ``tmux -V``.

    The dependency preflight only needs the version string; any other
    invocation exits non-zero so an unexpected use is visible.

    :param bin_dir: Directory to create the stub in (prepended to PATH).
    :param version_line: The ``tmux -V`` output, e.g. ``"tmux 1.8"``.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "tmux"
    stub.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "-V" ]; then echo "{version_line}"; exit 0; fi\n'
        'echo "stub tmux: unsupported invocation: $*" >&2\n'
        "exit 1\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)


def _setup_pre_menu_output(tmp_path: Path, version_line: str) -> str:
    """
    Run ``omni setup`` with a stubbed tmux and capture pre-menu output.

    Spawns ``python -m omnigent setup`` under a PTY with a fresh ``HOME``
    (deterministic first-run flow, no touching the real user config) and a
    stub ``tmux`` first on ``PATH`` reporting *version_line*. Captures
    everything printed up to the "Configure harnesses" menu title — the
    window where ``_warn_missing_harness_dependencies`` emits its warning
    block — then quits the menu.

    :param tmp_path: Per-test temp dir for the fake ``$HOME`` and stub bin.
    :param version_line: What the stub's ``tmux -V`` prints.
    :returns: The ANSI-stripped text printed before the menu title.
    """
    stub_bin = tmp_path / "stub-bin"
    _make_stub_tmux(stub_bin, version_line)
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "HOME": str(home),
        "OMNIGENT_CONFIG_HOME": str(home / ".omnigent"),
        "OMNIGENT_DATA_DIR": str(home / ".omnigent-data"),
        "PYTHONPATH": str(_REPO_ROOT),
        "NO_COLOR": "1",
        "TERM": "xterm",
        # Stub first so the preflight resolves it instead of any real tmux.
        "PATH": f"{stub_bin}{os.pathsep}{os.environ.get('PATH', '')}",
    }
    # Keep the first-run flow deterministic: no ambient provider keys to
    # adopt, and no inherited runner identity from a server-spawned CI env.
    for key in list(env):
        if key.startswith(
            (
                "ANTHROPIC",
                "OPENAI_",
                "GEMINI",
                "OPENROUTER",
                "OMNIGENT_ANTHROPIC",
                "OMNIGENT_OPENAI_",
                "OMNIGENT_GEMINI",
                "OMNIGENT_OPENROUTER",
                "OMNIGENT_RUNNER",
            )
        ):
            env.pop(key)
    child = pexpect.spawn(
        sys.executable,
        ["-m", "omnigent", "setup"],
        env=env,
        encoding=None,
        dimensions=(40, 120),
        timeout=180,
        cwd=str(_REPO_ROOT),
    )
    try:
        # The level-1 harness overview title. Everything the dependency
        # preflight prints lands before it.
        child.expect(re.compile(rb"Configure harnesses"), timeout=180)
        pre_menu = child.before + child.after
    finally:
        with contextlib.suppress(Exception):
            child.sendcontrol("c")
            time.sleep(0.3)
            child.close(force=True)
    return _ANSI_RE.sub(b"", pre_menu).decode("utf-8", "replace")


def test_setup_warns_on_outdated_tmux(tmp_path: Path) -> None:
    """An ancient tmux (1.8) on PATH must be flagged before the menu.

    On the buggy build the preflight only probes tmux *presence*, so an
    outdated-but-present tmux produces no warning at all and this fails.
    """
    text = _setup_pre_menu_output(tmp_path, _OUTDATED_TMUX_VERSION)
    assert "tmux is too old (detected tmux 1.8)" in text.lower(), (
        "`omnigent setup` printed no outdated-version warning although the tmux on PATH "
        f"reports '{_OUTDATED_TMUX_VERSION}' — far below what the native "
        "tmux-backed harnesses need. The dependency preflight must check the "
        "minimum supported tmux version, not just that a tmux binary exists.\n"
        f"Pre-menu output was:\n{text}"
    )


def test_setup_quiet_on_modern_tmux(tmp_path: Path) -> None:
    """A current tmux must not trigger the outdated-version warning.

    Guards the fix against over-warning: with a modern tmux on PATH the
    pre-menu output must stay free of tmux complaints.
    """
    text = _setup_pre_menu_output(tmp_path, _MODERN_TMUX_VERSION)
    assert "tmux is too old" not in text.lower(), (
        "`omnigent setup` warned about an outdated tmux although the tmux on PATH "
        f"reports '{_MODERN_TMUX_VERSION}', which is a current release.\n"
        f"Pre-menu output was:\n{text}"
    )
