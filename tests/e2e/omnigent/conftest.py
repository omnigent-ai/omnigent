"""Fixtures for Omnigent e2e tests (mock LLM).

All tests use the in-process mock LLM server via :func:`mock_credentials_env`
and :func:`mock_llm_server_url`. Real-credential fixtures have been removed
since the migration to mock LLM completed.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

# Root of the Omnigent checkout that ships the ``omnigent``
# package, the example YAMLs, and (in the main checkout) the
# ``.venv`` with omnigent + pexpect + openai-agents installed.
#
# Derived from the conftest's own location so git worktrees work
# naturally: this file lives at
# ``<root>/tests/e2e/omnigent/conftest.py`` (post-unification), so
# the checkout root is three levels up. Hardcoding an absolute path
# broke worktrees because a subprocess spawned there would still
# exec the main-checkout ``omnigent`` (via the editable install),
# missing any per-worktree edits.
_OMNIGENT_REPO = Path(__file__).resolve().parents[3]


def _resolve_venv_python() -> Path:
    """
    Return the Python interpreter path for the worktree's venv.

    Git worktrees don't have their own ``.venv`` — they share the
    main checkout's venv. Walk up the directory tree from the
    current repo root, looking for ``.venv/bin/python`` in this
    directory then in each parent, stopping when we find one.
    Stops at the filesystem root if none is found (which surfaces
    the misconfiguration loudly from the fixture).

    :returns: Absolute path to the Python interpreter.
    :raises RuntimeError: If no venv python is found up to the
        filesystem root.
    """
    current = _OMNIGENT_REPO
    while True:
        candidate = current / ".venv" / "bin" / "python"
        if candidate.is_file():
            return candidate
        if current.parent == current:
            # Reached filesystem root without finding a venv.
            raise RuntimeError(
                f"no .venv/bin/python found walking up from "
                f"{_OMNIGENT_REPO} — worktrees share the main "
                f"checkout's venv, so one parent of this path "
                f"should contain ``.venv``."
            )
        current = current.parent


_OMNIGENT_VENV_PYTHON = _resolve_venv_python()


@pytest.fixture(scope="session")
def omnigent_python() -> Path:
    """
    Path to the Python interpreter that has the ``omnigent``
    package + its harness dependencies installed.

    The Omnigent repo ships its own ``.venv`` with
    ``omnigent``, ``pexpect``, ``openai-agents``,
    ``claude-agent-sdk``, etc. pre-installed. Agent-plane's e2e
    tests use that interpreter directly rather than adding
    omnigent as an omnigent dep (omnigent is not
    distributed as a package yet).

    :returns: Absolute path to the Omnigent ``.venv`` Python
        interpreter, e.g.
        ``"/path/to/omnigent/.venv/bin/python"``.
    :raises RuntimeError: If the interpreter is not present at
        the expected path — indicates the Omnigent checkout is
        missing or its .venv hasn't been created.
    """
    if not _OMNIGENT_VENV_PYTHON.is_file():
        raise RuntimeError(
            f"Omnigent venv python not found at {_OMNIGENT_VENV_PYTHON}. "
            f"These e2e tests require the sibling checkout at "
            f"{_OMNIGENT_REPO} with .venv set up."
        )
    return _OMNIGENT_VENV_PYTHON


@pytest.fixture(scope="session")
def omnigent_repo_root() -> Path:
    """
    Root of the Omnigent checkout used as the subprocess cwd.

    Omnigent YAMLs reference example tool modules via dotted
    paths like ``tests.resources.examples._shared.tool_functions.get_current_time``, so
    the subprocess must run with the repo root on sys.path
    (i.e. as its cwd).

    :returns: Absolute path to the Omnigent repo root, e.g.
        ``"/path/to/omnigent"``.
    """
    return _OMNIGENT_REPO


@pytest.fixture(scope="session")
def mock_credentials_env(
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, str]:
    """
    Environment dict for subprocess invocations against the mock LLM.

    Wires ``OPENAI_BASE_URL`` to the session-scoped mock LLM server
    so all LLM calls go to deterministic canned responses instead of
    a real Databricks gateway.

    :param mock_llm_server_url: Base URL of the mock LLM server,
        e.g. ``"http://127.0.0.1:12345"``.
    :param tmp_path_factory: Pytest factory for a session-scoped
        config home.
    :returns: A dict suitable for ``subprocess.Popen(env=...)``.
    """
    env = dict(os.environ)
    env["OPENAI_BASE_URL"] = f"{mock_llm_server_url}/v1"
    env["OPENAI_API_KEY"] = "mock-key"
    # Strip vars that could interfere with the mock path.
    for stale in (
        "ANTHROPIC_API_KEY",
        "DATABRICKS_TOKEN",
        "CLAUDE_CODE",
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_EXECPATH",
        "CODEX",
        "DATABRICKS_CONFIG_PROFILE",
    ):
        env.pop(stale, None)
    env["OMNIGENT_SKIP_ONBOARD"] = "1"
    env["OMNIGENT_NO_UPDATE_CHECK"] = "1"
    config_home = tmp_path_factory.mktemp("omnigent-mock-e2e-config")
    (config_home / "config.yaml").write_text(
        "auth:\n  type: api_key\n",
        encoding="utf-8",
    )
    env["OMNIGENT_CONFIG_HOME"] = str(config_home)
    repo = str(_OMNIGENT_REPO)
    omnigent_path = str(_OMNIGENT_REPO / "omnigent")
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(p for p in (repo, omnigent_path, existing_pp) if p)
    return env


# ── Mock LLM server fixtures ────────────────────────────────


def _find_free_port() -> int:
    """Find a free TCP port by binding to port 0."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def mock_llm_server_url(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[str]:
    """
    Start a mock LLM server for the test session.

    Spawns ``tests/unit/server/integration/mock_llm_server.py`` as a
    subprocess and waits for its ``/stats`` endpoint to respond.
    The fixture yields the base URL (e.g.
    ``http://127.0.0.1:<port>``) and kills the process on teardown.

    :param tmp_path_factory: Pytest temp path factory for logs.
    :yields: The mock server base URL.
    """
    mock_port = _find_free_port()
    mock_log = tmp_path_factory.mktemp("mock_llm_logs") / "mock_llm.log"
    log_handle = open(mock_log, "w")  # noqa: SIM115

    proc = subprocess.Popen(
        [
            sys.executable,
            str(
                _OMNIGENT_REPO / "tests" / "unit" / "server" / "integration" / "mock_llm_server.py"
            ),
            str(mock_port),
        ],
        env={**os.environ, "PYTHONPATH": str(_OMNIGENT_REPO)},
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{mock_port}"

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(f"{base_url}/stats", timeout=1.0)
            if resp.status_code == 200:
                break
        except httpx.ConnectError:
            continue
        time.sleep(0.1)
    else:
        proc.kill()
        log_handle.close()
        log_contents = mock_log.read_text() if mock_log.exists() else ""
        raise RuntimeError(
            f"Mock LLM server didn't start within 10s.\nLog at {mock_log}:\n{log_contents[-2000:]}"
        )

    try:
        yield base_url
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        log_handle.close()


def configure_mock_llm(
    mock_llm_server_url: str,
    responses: list[dict],
    *,
    key: str = "default",
) -> None:
    """
    Configure a keyed response queue on the mock LLM server.

    :param mock_llm_server_url: Mock server URL.
    :param responses: List of response config dicts.
    :param key: Queue key (typically model name).
    """
    resp = httpx.post(
        f"{mock_llm_server_url}/mock/configure",
        json={"key": key, "responses": responses},
        timeout=5.0,
    )
    resp.raise_for_status()


def reset_mock_llm(mock_llm_server_url: str) -> None:
    """Clear all keyed queues, captured requests, and gates."""
    resp = httpx.post(f"{mock_llm_server_url}/mock/reset", timeout=5.0)
    resp.raise_for_status()
