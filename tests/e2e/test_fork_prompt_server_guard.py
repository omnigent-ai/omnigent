"""Fork/prompt conflicts are rejected before local or server dispatch, without a traceback.

``omnigent run --fork <id> -p ...`` must print the same one-line usage error for
a local YAML target and for ``--server <url>``. The server shape runs against a
real runner-less ``omnigent server``: when the guard was bypassed, the CLI only
failed later with an unhandled ``RuntimeError`` and the crash reporter.
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

# Substring of the guard message so it survives minor phrasing tweaks around it.
_GUARD_MSG = "--fork requires interactive REPL mode"

# Fragments of the former unhandled-crash output; their absence is asserted.
_CRASH_FRAGMENTS = (
    "Traceback (most recent call last)",
    "no online runner",
)

# The guard fires during argument validation, so the id is never dereferenced.
_DUMMY_SESSION_ID = "0" * 32


def _isolated_env(root: Path) -> dict[str, str]:
    """Env for an ``omnigent`` subprocess with a no-auth config and its own data dir.

    The ambient ``PYTHONPATH`` is kept (its relative entries resolve against
    ``cwd=_REPO_ROOT``) so the subprocess imports this worktree's packages.
    """
    config_home = root / "omnigent-config"
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "config.yaml").write_text("auth:\n  type: none\n", encoding="utf-8")
    env = {
        **os.environ,
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "OMNIGENT_DATA_DIR": str(root / "omnigent-data"),
        "OPENAI_API_KEY": "mock-key",
    }
    env.pop("DATABRICKS_TOKEN", None)
    return env


def _run_cli(args: list[str], env: dict[str, str]) -> tuple[int, str]:
    """Run ``omnigent <args>`` and return ``(exit_code, stdout + stderr)``."""
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
    """A real ``omnigent server`` with one seeded session and no runner online.

    :returns: ``(base_url, session_id)``.
    """
    work = tmp_path_factory.mktemp("fork_prompt_guard")
    port = find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    server_log = work / "server.log"
    with server_log.open("w") as log_handle:
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
            env=_isolated_env(work / "server"),
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
            except httpx.HTTPError:
                pass  # not serving yet; keep polling until the deadline
            time.sleep(POLL_INTERVAL_S)
        if not healthy:
            raise RuntimeError(
                f"server never became healthy within 120s. Log:\n{server_log.read_text()[-3000:]}"
            )

        agents_resp = httpx.get(f"{base_url}/v1/agents", timeout=30.0)
        agents_resp.raise_for_status()
        agents = agents_resp.json()["data"]
        assert agents, "server seeded no built-in agents"
        resp = httpx.post(
            f"{base_url}/v1/sessions",
            json={"agent_id": agents[0]["id"]},
            headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
            timeout=30.0,
        )
        resp.raise_for_status()
        yield base_url, str(resp.json()["id"])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


def test_fork_prompt_local_yaml_target_is_guarded(tmp_path: Path) -> None:
    """Control: the local-YAML shape rejects ``--fork`` + ``-p`` before any backend work."""
    code, out = _run_cli(
        ["run", str(_HELLO_WORLD_YAML), "--fork", _DUMMY_SESSION_ID, "-p", "test"],
        _isolated_env(tmp_path),
    )
    assert code != 0, out
    assert _GUARD_MSG in out, out
    for fragment in _CRASH_FRAGMENTS:
        assert fragment not in out, f"found {fragment!r}\nexit={code}\noutput:\n{out}"


def test_fork_prompt_server_url_target_is_guarded(
    bare_server_with_session: tuple[str, str],
    tmp_path: Path,
) -> None:
    """The ``--server <url>`` shape prints the same guard instead of crashing."""
    base_url, session_id = bare_server_with_session
    code, out = _run_cli(
        ["run", "--server", base_url, "--fork", session_id, "-p", "test"],
        _isolated_env(tmp_path),
    )
    for fragment in _CRASH_FRAGMENTS:
        assert fragment not in out, f"found {fragment!r}\nexit={code}\noutput:\n{out}"
    assert code != 0, out
    assert _GUARD_MSG in out, out
