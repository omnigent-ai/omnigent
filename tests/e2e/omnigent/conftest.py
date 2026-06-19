"""Fixtures for Omnigent Phase 0 characterization e2e tests.

These tests shell out to the real ``omnigent`` CLI bundled in
the sibling checkout (resolved from this file's location) and
drive it against the Databricks workspace resolved from
``--profile``. They reuse the ``--llm-api-key`` CLI option
registered by the parent ``tests/e2e/conftest.py`` — no new flag
is introduced.

Design reference: ``designs/OMNIGENT_INTEGRATION.md`` §Phase 0.
"""

from __future__ import annotations

import configparser
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from filelock import FileLock

from tests.e2e.helpers import lookup_databricks_host

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

# Default workspace when ``--profile`` isn't passed. Matches the
# ``--profile default`` that CI invocations pass explicitly, so a
# bare local run behaves like CI; a developer running locally with
# ``--profile test-profile`` (or any other valid cfg profile) gets
# that workspace's host + token instead via the
# :func:`databricks_workspace` fixture.
_DEFAULT_PROFILE = "default"

# Omnigent' ClaudeSDKExecutor and DatabricksExecutor read
# ``~/.databrickscfg`` directly and do not honor
# ``DATABRICKS_CONFIG_FILE``. The active profile on dev
# machines is typically configured with
# ``auth_type = databricks-cli`` (OAuth), which omnigent
# harnesses silently break on (403). To run claude-sdk harness
# tests, we temporarily patch the profile to a PAT-based entry
# and restore it on teardown — same pattern as
# ``run-omnigent.sh`` in the Omnigent repo.
_DATABRICKSCFG_PATH = Path.home() / ".databrickscfg"

# Cross-process lock for ~/.databrickscfg rewrites under
# pytest-xdist (without this, parallel workers' teardowns race).
_DATABRICKSCFG_LOCK_PATH = _DATABRICKSCFG_PATH.with_suffix(
    _DATABRICKSCFG_PATH.suffix + ".e2e-lock"
)


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
def databricks_workspace(request: pytest.FixtureRequest) -> tuple[str, str]:
    """
    Resolve the Databricks workspace these tests should target.

    Reads the ``--profile`` CLI option (registered in the
    top-level :mod:`tests/conftest`) and looks the host up in
    ``~/.databrickscfg``. When ``--profile`` is empty, falls back
    to :data:`_DEFAULT_PROFILE` (``default``, the profile CI
    invocations pass explicitly). When the resolved profile is
    missing from the cfg or has no ``host`` entry, raises
    :class:`pytest.UsageError` — failing loud beats letting tests
    403 against a stale cached URL.

    :param request: pytest request — used to read ``--profile``.
    :returns: ``(profile_name, host_url)``, e.g.
        ``("test-profile", "https://example.cloud.databricks.com")``.
        The host has any trailing ``/`` stripped so callers can
        append AI Gateway paths cleanly.
    :raises pytest.UsageError: When the resolved profile isn't
        configured in ``~/.databrickscfg``.
    """
    profile = request.config.getoption("--profile") or _DEFAULT_PROFILE
    host = lookup_databricks_host(profile)
    if host is None:
        raise pytest.UsageError(
            f"Databricks profile {profile!r} is missing from "
            f"~/.databrickscfg or has no ``host`` entry. "
            f"Either pass ``--profile`` for a profile that exists, "
            f"or add the section to your databrickscfg."
        )
    return profile, host


@pytest.fixture(scope="session")
def omnigent_credentials_env(
    llm_api_key: str,
    databricks_workspace: tuple[str, str],
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, str]:
    """
    Environment dict for subprocess invocations of ``omnigent``.

    Sets ``OPENAI_BASE_URL`` and ``OPENAI_API_KEY`` for the
    ``openai-agents`` harness (which honors these), and
    ``DATABRICKS_CONFIG_PROFILE`` for harnesses that route through
    ``~/.databrickscfg``. The PAT comes from the parent e2e
    suite's ``--llm-api-key`` flag; the workspace host comes from
    :func:`databricks_workspace` (driven by ``--profile``).

    Modeled on ``run-omnigent.sh`` in the Omnigent repo.

    :param llm_api_key: The Databricks PAT from ``--llm-api-key``,
        e.g. ``"dapi..."``.
    :param databricks_workspace: ``(profile, host)`` pair for the
        active workspace. Determines which AI Gateway OpenAI
        Responses URL openai-agents hits.
    :param tmp_path_factory: Pytest factory for a session-scoped
        config home that is cleaned up by pytest at the end of the
        run.
    :returns: A dict suitable for ``subprocess.Popen(env=...)``,
        starting from ``os.environ`` so system PATH, HOME, etc.
        propagate.
    """
    profile, host = databricks_workspace
    env = dict(os.environ)
    env["OPENAI_BASE_URL"] = f"{host}/ai-gateway/openai/v1"
    env["OPENAI_API_KEY"] = llm_api_key
    env["DATABRICKS_CONFIG_PROFILE"] = profile
    # Omnigent' openai_agents_sdk harness and ClaudeSDKExecutor
    # both use MCP servers that would otherwise inherit stale
    # tokens from the outer shell. Explicit unset for any token
    # vars that could shadow our PAT.
    # ``CLAUDECODE`` (no underscore) is set to "1" whenever this
    # test suite is driven by a Claude Code CLI session. The
    # ``claude-sdk`` harness detects this env var and refuses to
    # launch a nested Claude Code session — it prints
    # "Claude Code cannot be launched inside another Claude Code
    # session" and hangs the subprocess waiting for a control
    # response that will never come. Strip it so the harness can
    # boot a fresh session.
    for stale in (
        "ANTHROPIC_API_KEY",
        "DATABRICKS_TOKEN",
        "CLAUDE_CODE",
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_EXECPATH",
        "CODEX",
    ):
        env.pop(stale, None)
    # Suppress the interactive onboarding prompt
    # ``omnigent/onboarding/setup.py`` emits when the active
    # databrickscfg is missing canonical profiles. The prompt's
    # ``input()`` blocks on stdin; in an interactive shell the
    # subprocess inherits the tty and hangs forever waiting for
    # a Y/n that never comes.
    env["OMNIGENT_SKIP_ONBOARD"] = "1"
    # Suppress the "Update available — origin/main is N commits
    # ahead" banner that ``omnigent.update_check`` writes to
    # stderr when the checkout is behind upstream. CI runners
    # often run from a stale revision (1000+ commits behind on
    # GitHub-hosted runners), which trips the
    # ``stderr_is_clean`` snapshot assertions in
    # ``test_yaml_hello_world*`` and ``test_per_harness_*``.
    env["OMNIGENT_NO_UPDATE_CHECK"] = "1"
    # Isolate subprocesses from the developer/global Omnigent config.
    # These e2e tests pass every relevant knob explicitly; inheriting
    # ``~/.omnigent/config.yaml`` can inject a default ``server`` and
    # accidentally route one-shot local YAML tests through an unrelated
    # remote server.
    config_home = tmp_path_factory.mktemp("omnigent-e2e-config")
    # The ``--profile`` CLI flag was removed from every omnigent command;
    # the supported replacement is the global config's ``auth:`` block.
    # Write it into the isolated config home so spawned CLIs resolve
    # Databricks model/gateway routing from the active test profile
    # (consumed by ``omnigent.runtime.workflow._load_global_auth`` and
    # the native-wrapper resolvers).
    (config_home / "config.yaml").write_text(
        f"auth:\n  type: databricks\n  profile: {profile}\n",
        encoding="utf-8",
    )
    env["OMNIGENT_CONFIG_HOME"] = str(config_home)
    env["OMNIGENT_REMOTE_AUTH_TOKEN"] = llm_api_key
    # PYTHONPATH points at the worktree's ``omnigent`` +
    # ``omnigent`` sources so the subprocess imports this
    # worktree's code, not whatever the editable install in
    # ``.venv`` happens to point at. Essential for git worktrees:
    # without this, subprocesses would exec the main-checkout
    # ``omnigent`` and miss any per-worktree edits under test.
    # Prepend (don't overwrite) so any PYTHONPATH the developer
    # set in their shell still takes effect.
    repo = str(_OMNIGENT_REPO)
    omnigent_path = str(_OMNIGENT_REPO / "omnigent")
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(p for p in (repo, omnigent_path, existing_pp) if p)
    return env


@pytest.fixture
def patched_databrickscfg(
    llm_api_key: str,
    databricks_workspace: tuple[str, str],
) -> Iterator[None]:
    """
    Temporarily rewrite the active profile's section of
    ``~/.databrickscfg`` to use a PAT instead of
    ``databricks-cli`` OAuth.

    Omnigent' ClaudeSDKExecutor reads ``~/.databrickscfg``
    directly and its ``_read_databrickscfg`` treats the
    ``token`` field as a Bearer token — OAuth profiles
    (``auth_type = databricks-cli``) silently 403. This fixture
    backs up the file, rewrites the active profile to PAT form,
    and restores the original on teardown. Required by tests
    that exercise the claude-sdk or codex harnesses; not needed
    by tests that only use openai-agents (which honors env
    vars).

    Same strategy as ``run-omnigent.sh`` in the Omnigent
    repo. The design doc flags this as "to be replaced once
    omnigent'_read_databrickscfg is rewritten to use the
    databricks-sdk" — until then, file patching is the
    documented workaround.

    Acquires a cross-process file lock on
    ``~/.databrickscfg.e2e-lock`` for the backup → patch → restore
    sequence so parallel xdist workers serialize on the rewrite.

    :param llm_api_key: The Databricks PAT from
        ``--llm-api-key``, e.g. ``"dapi..."``.
    :param databricks_workspace: ``(profile, host)`` pair from
        :func:`databricks_workspace`. Selects which cfg section
        to rewrite; matches the profile :func:`omnigent_credentials_env`
        sets ``DATABRICKS_CONFIG_PROFILE`` to.
    :yields: None. The caller runs the harness inside the
        with-block; teardown restores the original file.
    """
    profile, host = databricks_workspace
    backup_path = _DATABRICKSCFG_PATH.with_suffix(_DATABRICKSCFG_PATH.suffix + ".e2e-bak")
    with FileLock(str(_DATABRICKSCFG_LOCK_PATH)):
        had_original = _DATABRICKSCFG_PATH.exists()
        if had_original:
            shutil.copy2(_DATABRICKSCFG_PATH, backup_path)
        cfg = configparser.ConfigParser()
        if had_original:
            cfg.read(_DATABRICKSCFG_PATH)
        if profile not in cfg:
            cfg.add_section(profile)
        cfg[profile]["host"] = host
        cfg[profile]["token"] = llm_api_key
        # Drop auth_type so _read_databrickscfg's PAT path is taken
        # cleanly instead of an OAuth profile omnigent harnesses
        # don't honor.
        cfg[profile].pop("auth_type", None)
        with open(_DATABRICKSCFG_PATH, "w") as f:
            cfg.write(f)
        try:
            yield
        finally:
            if had_original:
                shutil.move(str(backup_path), str(_DATABRICKSCFG_PATH))
            else:
                _DATABRICKSCFG_PATH.unlink(missing_ok=True)


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

    Spawns ``tests/server/integration/mock_llm_server.py`` as a
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
            str(_OMNIGENT_REPO / "tests" / "server" / "integration" / "mock_llm_server.py"),
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


@pytest.fixture(scope="session")
def mock_credentials_env(
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, str]:
    """
    Environment dict for subprocess invocations of ``omnigent``
    that point at the mock LLM server instead of a real LLM.

    Drop-in replacement for ``omnigent_credentials_env`` in tests
    that can run against canned responses.

    :param mock_llm_server_url: Base URL of the mock server.
    :param tmp_path_factory: Pytest factory for a session-scoped
        config home.
    :returns: A dict suitable for ``subprocess.Popen(env=...)``.
    """
    env = dict(os.environ)
    env["OPENAI_BASE_URL"] = f"{mock_llm_server_url}/v1"
    env["OPENAI_API_KEY"] = "mock-key"
    # Strip stale / conflicting auth vars — same as the real
    # omnigent_credentials_env fixture does.
    for stale in (
        "ANTHROPIC_API_KEY",
        "DATABRICKS_TOKEN",
        "CLAUDE_CODE",
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_EXECPATH",
        "CODEX",
    ):
        env.pop(stale, None)
    env["OMNIGENT_SKIP_ONBOARD"] = "1"
    env["OMNIGENT_NO_UPDATE_CHECK"] = "1"
    config_home = tmp_path_factory.mktemp("omnigent-mock-config")
    (config_home / "config.yaml").write_text(
        "auth:\n  type: api_key\n",
        encoding="utf-8",
    )
    env["OMNIGENT_CONFIG_HOME"] = str(config_home)
    env["OMNIGENT_REMOTE_AUTH_TOKEN"] = "mock-key"
    # PYTHONPATH — same worktree-first logic as the real fixture.
    repo = str(_OMNIGENT_REPO)
    omnigent_path = str(_OMNIGENT_REPO / "omnigent")
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(p for p in (repo, omnigent_path, existing_pp) if p)
    return env
