"""E2E regression guard: ``--fork`` + ``-p`` must be rejected on every dispatch shape.

``omni run --fork <id> -p "..."`` is an invalid combination: ``--fork``
opens the interactive REPL on the fork, and ``-p`` is a one-shot prompt
that exits immediately. The CLI is supposed to reject it up front with a
friendly ``ClickException``:

    Error: --fork requires interactive REPL mode; remove -p/--prompt.

The guard must fire on every dispatch shape of ``omnigent/cli._dispatch_run``.
The hazard these tests pin: the direct-server (no-AGENT) branch returns
early, so a guard placed below it is bypassed and the invalid combination
proceeds into ``run_chat``, where it dies with an *unhandled*
``RuntimeError`` (``"This server has no online runner to run the turn..."``)
plus a full traceback / crash report instead of the friendly one-line error.

These two tests pin both dispatch shapes:

* ``test_fork_prompt_local_yaml_target_is_guarded`` -- the local-YAML shape.
* ``test_fork_prompt_server_url_target_is_guarded`` -- the ``--server <url>``
  shape. Drives a real bare ``omnigent server`` (no online runner), seeds one
  session so agent discovery succeeds, then invokes the real CLI and asserts
  the friendly guard error is emitted with no crash.

Usage::

    pytest tests/e2e/test_fork_prompt_server_guard.py -v
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests.e2e.conftest import find_free_port
from tests.e2e.helpers import POLL_INTERVAL_S

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HELLO_WORLD_YAML = _REPO_ROOT / "tests" / "resources" / "examples" / "hello_world.yaml"

# The friendly guard message both dispatch shapes must produce. Substring so
# it survives minor phrasing tweaks around it.
_GUARD_MSG = "--fork requires interactive REPL mode"

# Fragments of the reported unhandled-crash path. Their ABSENCE is asserted:
# with the guard in place the CLI never reaches run_chat / runner resolution.
_CRASH_FRAGMENTS = (
    "Traceback (most recent call last)",
    "no online runner",
)

# A dummy session id is fine for the local-YAML case: the guard fires during
# CLI arg validation, before any backend/network work, so the id is never
# dereferenced.
_DUMMY_SESSION_ID = "0" * 32


def _cli_env(tmp_path: Path) -> dict[str, str]:
    """Env for a ``omnigent`` CLI subprocess with no-auth local config.

    Inherits the ambient ``PYTHONPATH`` untouched (its relative
    ``sdks/python-client`` / ``sdks/ui`` entries resolve against the
    subprocess ``cwd``, which we pin to the repo root) so the CLI imports the
    worktree's ``omnigent`` and ``omnigent_client``.

    :param tmp_path: Per-test tmp dir for an isolated OMNIGENT_CONFIG_HOME.
    :returns: Env mapping for ``subprocess.run``.
    """
    config_home = tmp_path / "omnigent-config"
    config_home.mkdir(exist_ok=True)
    (config_home / "config.yaml").write_text("auth:\n  type: none\n", encoding="utf-8")
    env = {
        **os.environ,
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "OPENAI_API_KEY": "mock-key",
    }
    env.pop("DATABRICKS_TOKEN", None)
    return env


def _run_cli(args: list[str], env: dict[str, str]) -> tuple[int, str]:
    """Run ``omnigent <args>`` and return (exit_code, combined stdout+stderr).

    :param args: CLI arguments after the program name, e.g.
        ``["run", "--server", url, "--fork", sid, "-p", "test"]``.
    :param env: Subprocess environment.
    :returns: The process exit code and the combined output text.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "omnigent.cli", *args],
        env=env,
        cwd=str(_REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    return proc.returncode, proc.stdout + proc.stderr


@pytest.fixture(scope="module")
def bare_server_with_session(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """Start a real ``omnigent server`` with NO online runner and one session.

    Mirrors the reported environment: a reachable server URL whose sessions
    reference a registered agent, but with no runner online to dispatch turns.
    A session is seeded so the CLI's ``_pick_agent`` discovers an agent name
    and the invalid ``--fork``/``-p`` combination (pre-fix) reaches the runner
    resolution that raises the unhandled ``RuntimeError``.

    :param tmp_path_factory: Pytest temp path factory.
    :returns: ``(base_url, session_id)``.
    """
    work = tmp_path_factory.mktemp("fork_prompt_guard")
    port = find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    server_log = work / "server.log"
    log_handle = open(server_log, "w")  # noqa: SIM115 -- lives for Popen's lifetime

    # Inherit the ambient env untouched (relative PYTHONPATH entries resolve
    # against cwd=_REPO_ROOT). Only add the LLM key the config may require.
    env = {**os.environ, "OPENAI_API_KEY": "mock-key"}
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "omnigent.cli",
            "server",
            "--port",
            str(port),
            "--database-uri",
            f"sqlite:///{work / 'server.db'}",
            "--artifact-location",
            str(work / "artifacts"),
        ],
        env=env,
        cwd=str(_REPO_ROOT),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 120.0
        healthy = False
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(
                    "server exited during startup (code "
                    f"{proc.returncode}). Log:\n{server_log.read_text()[-3000:]}"
                )
            try:
                if httpx.get(f"{base_url}/health", timeout=2.0).status_code == 200:
                    healthy = True
                    break
            except httpx.ConnectError:
                pass
            time.sleep(POLL_INTERVAL_S)
        if not healthy:
            raise RuntimeError(
                f"server never became healthy within 120s. Log:\n{server_log.read_text()[-3000:]}"
            )

        # The server seeds built-in agents at startup. Bind a session to the
        # first one so agent discovery has something to find.
        agents = httpx.get(f"{base_url}/v1/agents", timeout=30.0).json()["data"]
        assert agents, "server seeded no built-in agents"
        agent_id = agents[0]["id"]
        resp = httpx.post(
            f"{base_url}/v1/sessions",
            json={"agent_id": agent_id},
            headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
            timeout=30.0,
        )
        resp.raise_for_status()
        session_id = str(resp.json()["id"])

        yield base_url, session_id
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_handle.close()


def test_fork_prompt_local_yaml_target_is_guarded(tmp_path: Path) -> None:
    """Control: the local-YAML dispatch shape rejects ``--fork`` + ``-p``.

    The guard fires during CLI arg validation before any backend work, so no
    live server is needed.
    """
    code, out = _run_cli(
        [
            "run",
            str(_HELLO_WORLD_YAML),
            "--fork",
            _DUMMY_SESSION_ID,
            "-p",
            "test",
        ],
        _cli_env(tmp_path),
    )
    assert _GUARD_MSG in out, (
        f"expected the REPL-mode guard on the local-YAML shape.\nexit={code}\noutput:\n{out}"
    )
    for fragment in _CRASH_FRAGMENTS:
        assert fragment not in out, (
            f"local-YAML shape should error cleanly, not crash "
            f"(found {fragment!r}).\nexit={code}\noutput:\n{out}"
        )


def test_fork_prompt_server_url_target_is_guarded(
    bare_server_with_session: tuple[str, str],
    tmp_path: Path,
) -> None:
    """The ``--server <url>`` shape must reject ``--fork`` + ``-p``.

    If the guard is bypassed, the invalid combination proceeds into
    ``run_chat`` and dies with an unhandled ``RuntimeError`` (``"This server
    has no online runner..."``) plus a traceback. Guarded, the friendly
    message is emitted and no crash occurs -- matching the local-YAML shape.
    """
    base_url, session_id = bare_server_with_session
    code, out = _run_cli(
        [
            "run",
            "--server",
            base_url,
            "--fork",
            session_id,
            "-p",
            "test",
        ],
        _cli_env(tmp_path),
    )
    # No unhandled crash: the guard must stop dispatch before runner resolution.
    for fragment in _CRASH_FRAGMENTS:
        assert fragment not in out, (
            f"--server shape bypassed the guard and crashed (found "
            f"{fragment!r}) instead of emitting the friendly REPL-mode error.\n"
            f"exit={code}\noutput:\n{out}"
        )
    # The same friendly guard the local-YAML shape produces.
    assert _GUARD_MSG in out, (
        f"expected the REPL-mode guard on the --server shape too.\nexit={code}\noutput:\n{out}"
    )
