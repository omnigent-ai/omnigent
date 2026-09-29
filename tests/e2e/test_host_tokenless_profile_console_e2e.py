"""``omnigent host`` boot with a token-less Databricks OAuth profile.

Reconstructs the reported journey: ``~/.databrickscfg`` names an OAuth
profile (``auth_type = databricks-cli``, no static ``token``) whose session
cannot mint a token, ucode state exists for that workspace, and the global
Omnigent auth routes native Claude through that profile. ``omnigent host``
run in a real terminal (pexpect PTY — host logging mirrors to stderr exactly
as for a user) prewarms the native model catalogs at registration; live
Databricks model discovery fails and falls back to cached ucode models.

That fallback is expected and recoverable, so the host terminal must not
dump internal credential-resolver tracebacks (the log file may keep the
detail, and a concise warning line is allowed). On the buggy build the
console printed the full ``_SectionNeedsSdk``/``OSError`` chain for
native-claude, plus a twin for native-codex.

Usage::

    python -m pytest tests/e2e/test_host_tokenless_profile_console_e2e.py -v
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import signal
import sys
import time
from pathlib import Path

import httpx
import pytest

pexpect = pytest.importorskip("pexpect")

_REPO_ROOT = Path(__file__).resolve().parents[2]

_PROFILE = "e2e-tokenless-prod"
# Never contacted: credential resolution fails before any network call.
_WORKSPACE_URL = "https://e2e-tokenless.cloud.databricks.com"

_ANSI_RE = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]")

_CONNECTED_RE = re.compile(rb"Connected as '[^']+' \(([0-9a-f]{32})\)")

# Internal artifacts of the credential-resolver traceback the buggy build
# spilled onto the terminal; neither belongs in a concise user-facing message.
_TRACEBACK_MARKERS = ("_SectionNeedsSdk", "in resolve_databricks_workspace")


def _stage_home(home: Path) -> None:
    """Seed a host ``$HOME`` that routes native Claude through a token-less
    Databricks OAuth profile the databricks-sdk cannot mint a token for."""
    (home / ".ucode").mkdir(parents=True)
    (home / ".omnigent").mkdir()
    (home / ".databrickscfg").write_text(
        f"[{_PROFILE}]\nhost      = {_WORKSPACE_URL}\nauth_type = databricks-cli\n"
    )
    (home / ".ucode" / "state.json").write_text(
        json.dumps(
            {
                "state_version": 1,
                "current_workspace": _WORKSPACE_URL,
                "workspaces": {
                    _WORKSPACE_URL: {
                        "workspace": _WORKSPACE_URL,
                        "claude_models": {
                            "opus": "databricks-claude-opus-4-6",
                            "sonnet": "databricks-claude-sonnet-4-5",
                        },
                        "available_tools": ["claude"],
                        "agents": {
                            "claude": {
                                "model": "databricks-claude-opus-4-6",
                                "base_url": f"{_WORKSPACE_URL}/ai-gateway/anthropic",
                                "auth_command": f"databricks auth token --profile {_PROFILE}",
                                "auth_refresh_interval_ms": 1800000,
                            }
                        },
                    }
                },
            }
        )
    )
    (home / ".omnigent" / "config.yaml").write_text(
        f"auth:\n  type: databricks\n  profile: {_PROFILE}\n"
    )


def _host_env(home: Path) -> dict[str, str]:
    """Subprocess env: isolated ``$HOME``, ambient Databricks credentials
    stripped so resolution sees only the staged token-less profile."""
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["NO_COLOR"] = "1"
    env["TERM"] = "xterm"
    for var in [name for name in env if name.startswith("DATABRICKS_")]:
        del env[var]
    env.pop("OMNIGENT_DATA_DIR", None)
    env["OMNIGENT_CONFIG_HOME"] = str(home / ".omnigent")
    pythonpath = [
        str(_REPO_ROOT),
        str(_REPO_ROOT / "sdks" / "python-client"),
        str(_REPO_ROOT / "sdks" / "ui"),
    ]
    if env.get("PYTHONPATH"):
        pythonpath.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pythonpath)
    return env


def test_host_boot_with_tokenless_profile_keeps_terminal_traceback_free(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
) -> None:
    """A host booted onto a token-less databricks-cli profile connects, serves
    cached models, and spills no credential-resolver traceback to the terminal."""
    home = tmp_path / "home"
    home.mkdir()
    _stage_home(home)

    child = pexpect.spawn(
        sys.executable,
        ["-m", "omnigent", "host", "--server", live_server, "--non-interactive", "--no-open"],
        env=_host_env(home),
        encoding=None,
        dimensions=(50, 200),
        timeout=120,
        cwd=str(tmp_path),
    )
    console = io.BytesIO()
    child.logfile_read = console
    try:
        child.expect(_CONNECTED_RE, timeout=120)
        host_id = child.match.group(1).decode()

        # Barrier: the pre-launch picker lookup joins the boot prewarm's
        # single-flight claude probe, so a completed response (any status)
        # means the host finished resolving the native-claude launch config —
        # the step whose discovery failure produced the reported console spew.
        with contextlib.suppress(httpx.HTTPError):
            http_client.get(
                f"/v1/hosts/{host_id}/harnesses/claude-native/model-options",
                timeout=90,
            )

        # Drain what the probe mirrored to the PTY after the barrier.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            try:
                child.read_nonblocking(size=65536, timeout=1)
            except pexpect.TIMEOUT:
                continue
            except pexpect.EOF:
                break
    finally:
        with contextlib.suppress(OSError):
            child.kill(signal.SIGINT)
        with contextlib.suppress(pexpect.TIMEOUT, pexpect.EOF):
            child.expect(pexpect.EOF, timeout=15)
        child.close(force=True)

    output = _ANSI_RE.sub(b"", console.getvalue()).decode("utf-8", "replace")

    host_log_dir = home / ".omnigent" / "logs" / "host"
    host_logs = (
        "\n".join(p.read_text(errors="replace") for p in sorted(host_log_dir.glob("host-*.log")))
        if host_log_dir.is_dir()
        else ""
    )
    assert _PROFILE in output + host_logs, (
        "the host never routed native Claude through the staged profile — the "
        f"scenario did not exercise the token-less resolution path:\n{output}"
    )

    offending = [marker for marker in _TRACEBACK_MARKERS if marker in output]
    assert not offending, (
        "`omnigent host` spilled internal credential-resolver tracebacks onto the "
        f"terminal for a recoverable cached-models fallback ({offending}):\n{output}"
    )
