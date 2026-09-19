"""
Regression test: ``omni setup`` misreports natively-credentialed Codex and Pi.

A user who signed in to Pi with Pi's own CLI (``~/.pi/agent/auth.json``, so
``pi`` runs directly) or configured Codex through its own
``~/.codex/config.toml`` custom provider with a populated ``env_key`` (so a
bare ``codex`` resolves that provider as ready) still sees the harness
overview report those harnesses as ``✗ Not configured``.

The test drives the real ``omni setup`` flow under a pseudo-TTY against a
sandboxed ``$HOME`` seeded with exactly those native credentials, captures the
level-1 harness overview, and asserts each row does not read "Not configured".
Both assertions FAIL on the buggy build and pass once setup credits the
harnesses' own credential state.

Usage::

    python -m pytest tests/e2e/test_setup_native_harness_credentials.py -v --timeout=300
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

import pytest

pexpect = pytest.importorskip("pexpect")

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Strip ANSI escape sequences (CSI, OSC, and keypad-mode toggles) so menu
# rows can be matched as plain text.
_ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[=>]")

_CODEX_CONFIG_TOML = """\
model = "gpt-5"
model_provider = "myproxy"

[model_providers.myproxy]
name = "My Proxy"
base_url = "https://myproxy.example.com/v1"
env_key = "MYPROXY_API_KEY"
"""


def _seed_native_credentials(home: Path) -> None:
    pi_agent_dir = home / ".pi" / "agent"
    pi_agent_dir.mkdir(parents=True, exist_ok=True)
    (pi_agent_dir / "auth.json").write_text(
        json.dumps(
            {
                "anthropic": {
                    "type": "oauth",
                    "access": "fake-oauth-access-token",
                    "refresh": "fake-oauth-refresh-token",
                    "expires": int((time.time() + 30 * 24 * 3600) * 1000),
                }
            }
        )
    )
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True, exist_ok=True)
    (codex_dir / "config.toml").write_text(_CODEX_CONFIG_TOML)


def _sandbox_env(home: Path) -> dict[str, str]:
    env = dict(os.environ)
    for var in list(env):
        if "API_KEY" in var or "_TOKEN" in var or var.startswith(
            ("DATABRICKS_", "OMNIGENT_", "ANTHROPIC_", "OPENAI_", "CLAUDE_", "PI_", "CODEX_")
        ):
            env.pop(var, None)
    env.update(
        {
            "HOME": str(home),
            "PYTHONPATH": str(_REPO_ROOT),
            "NO_COLOR": "1",
            "TERM": "xterm",
            "MYPROXY_API_KEY": "populated-proxy-token",
        }
    )
    return env


@pytest.fixture(scope="module")
def harness_overview(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Capture the ``omni setup`` level-1 harness overview as plain text."""
    if shutil.which("codex") is None or shutil.which("pi") is None:
        pytest.skip("codex and pi CLIs must be on PATH to exhibit this bug")
    home = tmp_path_factory.mktemp("home")
    _seed_native_credentials(home)
    child = pexpect.spawn(
        sys.executable,
        ["-m", "omnigent", "setup"],
        env=_sandbox_env(home),
        encoding=None,
        dimensions=(40, 120),
        timeout=180,
    )
    try:
        child.expect(rb"Configure harnesses", timeout=180)
        # The Quit row is the last menu entry; its arrival means every
        # harness row above it has rendered.
        child.expect(rb"Quit", timeout=180)
        frame = bytearray(child.before or b"")
        frame += b"Quit"
        deadline = time.time() + 5
        while time.time() < deadline:
            try:
                frame += child.read_nonblocking(size=65536, timeout=1)
            except (pexpect.TIMEOUT, pexpect.EOF):
                break
        child.send("q")
        with contextlib.suppress(pexpect.TIMEOUT, pexpect.EOF):
            child.expect(pexpect.EOF, timeout=30)
    finally:
        with contextlib.suppress(Exception):
            child.close(force=True)
    return _ANSI_RE.sub(b"", bytes(frame)).decode("utf-8", "replace")


def _overview_row(frame: str, harness: str) -> str:
    pattern = re.compile(rf"^\s*(?:❯\s*)?{re.escape(harness)}\s{{2,}}(\S.*?)\s*$", re.MULTILINE)
    match = pattern.search(frame)
    assert match is not None, f"no {harness} row in the harness overview:\n{frame}"
    return match.group(1)


def _require_cli_seen(harness: str, status: str) -> None:
    if "Not installed" in status or "Needs upgrade" in status:
        pytest.skip(f"{harness} CLI on PATH is outside the supported range: {status!r}")


def test_pi_native_login_is_not_reported_as_unconfigured(harness_overview: str) -> None:
    status = _overview_row(harness_overview, "Pi")
    _require_cli_seen("Pi", status)
    assert "Not configured" not in status, (
        "pi is signed in via its own CLI (~/.pi/agent/auth.json) and runs "
        f"directly, but `omni setup` reports Pi as {status!r}"
    )


def test_codex_config_provider_is_not_reported_as_unconfigured(harness_overview: str) -> None:
    status = _overview_row(harness_overview, "Codex")
    _require_cli_seen("Codex", status)
    assert "Not configured" not in status, (
        "codex's own ~/.codex/config.toml selects a custom provider whose "
        "env_key is populated (a bare `codex` resolves it provider-ready), "
        f"but `omni setup` reports Codex as {status!r}"
    )
