"""Verify a duplicated ``[DEFAULT]`` in ``~/.databrickscfg`` does not crash ``omnigent run``.

The test spawns the real ``omnigent`` console script under a pseudo-TTY with an
isolated ``$HOME`` and asserts the launch never reaches the crash handler. It
runs twice: with ``databricks-sdk`` importable (its config loader parses the
file) and with the SDK hidden, as in a ``uv tool install omnigent`` without the
extra (the ``_read_databrickscfg_file_fallback`` path in the crash report).

Usage::

    python -m pytest tests/e2e/test_databrickscfg_duplicate_default_launch_e2e.py -v
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

pexpect = pytest.importorskip("pexpect")

from tests.e2e.omnigent._pexpect_harness import strip_ansi  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Both outcomes surface seconds after the local server boots: the crash prompt,
# or the first post-credential-read marker ("Connecting…"), which prints before
# runner bring-up. The ceiling is headroom for a slow CI host, not a budget.
_LAUNCH_TIMEOUT_S = 120

# Ambient credentials and proxy settings that would change the credential chain or
# route the loopback server through a proxy; ``DATABRICKS_*`` and ``OMNIGENT_*``
# (auth modes, state dirs, leaked runner/host identity) are cleared by prefix.
_ENV_TO_CLEAR = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)
_ENV_PREFIXES_TO_CLEAR = ("DATABRICKS_", "OMNIGENT_")

# The shape the Databricks VS Code extension leaves behind.
_DUPLICATE_DEFAULT_CFG = """\
[DEFAULT]
host = https://first.example.cloud.databricks.com
token = dapi-first-placeholder

[DEFAULT]
host = https://second.example.cloud.databricks.com
token = dapi-second-placeholder
"""

# The crash handler's file-an-issue question on a TTY; answered "n" so the
# crashed process exits and prints the saved report path.
_CRASH_PROMPT_RE = r"\[Y/n\]"

# Startup markers printed only after the ambient credential read (the crash
# site), so seeing any of them proves the read succeeded.
_POST_CREDENTIAL_READ_MARKERS = (
    "Connecting…",
    "Launching your agent…",
    "Omnigent session:",
    "No provider credentials",
)


def _sdk_importable() -> bool:
    try:
        import databricks.sdk.config  # noqa: F401
    except ImportError:
        return False
    return True


def _omnigent_command() -> list[str]:
    """The ``omnigent`` console script the user runs (venv sibling of this python)."""
    script = Path(sys.executable).with_name("omnigent")
    if script.exists():
        return [str(script)]
    return [sys.executable, "-c", "from omnigent.cli import main; main()"]


@pytest.fixture
def vscode_extension_home(tmp_path: Path) -> Path:
    """An isolated ``$HOME`` whose ``~/.databrickscfg`` has two ``[DEFAULT]`` blocks."""
    home = tmp_path / "home"
    (home / ".omnigent").mkdir(parents=True)
    (home / ".databrickscfg").write_text(_DUPLICATE_DEFAULT_CFG)
    (home / "agent.yaml").write_text(
        "name: databrickscfg-duplicate-default-repro\n"
        "description: Minimal agent for the duplicate [DEFAULT] launch repro.\n"
        "executor:\n"
        "  model: gpt-4o\n"
        "prompt: |\n"
        "  You are a test agent.\n"
    )
    return home


@pytest.fixture
def no_databricks_sdk_shim(tmp_path: Path) -> Path:
    """A ``databricks`` package that fails to import, like an install without the SDK."""
    shim = tmp_path / "no-databricks-sdk"
    (shim / "databricks").mkdir(parents=True)
    (shim / "databricks" / "__init__.py").write_text(
        "raise ModuleNotFoundError(\"No module named 'databricks'\", name='databricks')\n"
    )
    return shim


def _launch_env(home: Path, *, extra_pythonpath: list[Path]) -> dict[str, str]:
    env = os.environ.copy()
    for key in _ENV_TO_CLEAR:
        env.pop(key, None)
    for key in [k for k in env if k.startswith(_ENV_PREFIXES_TO_CLEAR)]:
        env.pop(key, None)
    env["HOME"] = str(home)
    env["OMNIGENT_DATA_DIR"] = str(home / ".omnigent")
    env["OMNIGENT_NO_UPDATE_CHECK"] = "1"
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    env["TERM"] = "xterm-256color"
    # Skip empty entries: CPython reads an empty PYTHONPATH entry as the cwd.
    env["PYTHONPATH"] = os.pathsep.join(
        entry
        for entry in [
            *(str(p) for p in extra_pythonpath),
            str(_REPO_ROOT),
            str(_REPO_ROOT / "sdks" / "python-client"),
            str(_REPO_ROOT / "sdks" / "ui"),
            env.get("PYTHONPATH", ""),
        ]
        if entry
    )
    return env


@pytest.fixture
def stop_local_daemon() -> Iterator[list[dict[str, str]]]:
    """Stop only the daemon/server each launch recorded under its isolated data dir.

    ``omnigent stop`` would also sweep the canonical port and could kill a
    developer's unrelated server, so daemons go through ``host stop`` and the
    server through its own pidfile.
    """
    envs: list[dict[str, str]] = []
    yield envs
    for env in envs:
        for command in (
            [*_omnigent_command(), "host", "stop", "--all", "--daemon-only", "--force"],
            [
                sys.executable,
                "-c",
                "from omnigent.host.local_server import stop_local_omnigent_server; "
                "stop_local_omnigent_server()",
            ],
        ):
            subprocess.run(
                command, env=env, cwd=env["HOME"], capture_output=True, timeout=60, check=False
            )


def _launch_until_crash_or_past_credential_read(env: dict[str, str], home: Path) -> str:
    """Run ``omnigent run ~/agent.yaml`` on a PTY; return everything it printed."""
    command = [*_omnigent_command(), "run", str(home / "agent.yaml")]
    log = io.StringIO()
    child = pexpect.spawn(
        command[0],
        command[1:],
        env=env,
        cwd=str(home),
        encoding="utf-8",
        codec_errors="replace",
        dimensions=(40, 120),
        timeout=_LAUNCH_TIMEOUT_S,
    )
    child.logfile_read = log
    post_marker_re = "|".join(re.escape(marker) for marker in _POST_CREDENTIAL_READ_MARKERS)
    try:
        index = child.expect([_CRASH_PROMPT_RE, post_marker_re, pexpect.EOF, pexpect.TIMEOUT])
        if index == 0:
            child.sendline("n")
            child.expect([pexpect.EOF, pexpect.TIMEOUT], timeout=30)
        elif index == 3:
            log.write(f"\n[test] no crash prompt or startup marker within {_LAUNCH_TIMEOUT_S}s\n")
    finally:
        child.close(force=True)
    return strip_ansi(log.getvalue())


@pytest.mark.timeout(_LAUNCH_TIMEOUT_S + 90)
@pytest.mark.parametrize("sdk_installed", [True, False], ids=["sdk-installed", "sdk-missing"])
def test_run_with_duplicate_default_databrickscfg_does_not_crash(
    sdk_installed: bool,
    vscode_extension_home: Path,
    no_databricks_sdk_shim: Path,
    stop_local_daemon: list[dict[str, str]],
) -> None:
    """Startup passes duplicate-default credential loading with and without databricks-sdk.

    The launch may still fail gracefully later on missing model credentials; it
    must never reach the crash handler.
    """
    if sdk_installed and not _sdk_importable():
        pytest.skip("databricks-sdk is not installed in this environment")
    extra_pythonpath = [] if sdk_installed else [no_databricks_sdk_shim]
    env = _launch_env(vscode_extension_home, extra_pythonpath=extra_pythonpath)
    stop_local_daemon.append(env)

    output = _launch_until_crash_or_past_credential_read(env, vscode_extension_home)

    assert "DuplicateOptionError" not in output, (
        "omnigent run crashed on a ~/.databrickscfg with a duplicated [DEFAULT] "
        "block: configparser.DuplicateOptionError reached the crash handler "
        f"(databricks-sdk installed={sdk_installed}).\n--- CLI output ---\n{output}"
    )
    assert "ran into an issue" not in output and "A crash report was saved" not in output, (
        f"omnigent run died on the crash-handler screen.\n--- CLI output ---\n{output}"
    )
    reached = [marker for marker in _POST_CREDENTIAL_READ_MARKERS if marker in output]
    assert reached, (
        "omnigent run never got past the ambient credential read (the crash "
        f"site): none of {_POST_CREDENTIAL_READ_MARKERS} appeared in the launch "
        f"output.\n--- CLI output ---\n{output}"
    )
