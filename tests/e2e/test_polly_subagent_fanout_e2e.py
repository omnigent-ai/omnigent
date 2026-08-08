"""Mock-LLM e2e for claude-profile fan-out across sub-agent spawns.

Boots a throwaway LOCAL server from this working tree and drives the polly
orchestrator headless using a mock LLM. The mock brain emits two scripted
``sys_session_send`` tool calls, so the runner exercises the fan-out
assignment path without real Claude logins: fan-out only decides *which*
profile name rides along on the child create, and the name is a lookup key
into the operator's config, so no profile dir is ever authenticated here.

What only this layer proves: an operator's ``claude_profiles.fanout_pool``
in ``~/.omnigent/config.yaml`` reaches the runner, the runner assigns one
pool profile per spawn, and each child conversation row persists a distinct
one. The dispatch unit tests stub the config loader and the server, so they
cannot show the config file -> spawn -> persisted row chain.

The assertion reads the throwaway server's own sqlite DB: ``claude_profile``
is deliberately write-only over REST (the server never echoes it back), so
the persisted ``session_overrides`` blob is the only place to observe it.

Run::

    pytest tests/e2e/test_polly_subagent_fanout_e2e.py -v
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
import urllib.request
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from tests.e2e.test_polly_e2e import (
    _MOCK_BRAIN_MODEL,
    _REPO,
    _SERVER_BOOT_TIMEOUT_SEC,
    _free_port,
    _mock_env,
    _mock_polly_spec_dir,
    _wait_for_health,
)

# Mock runs are fast (no real model inference) so a short timeout is enough.
_RUN_TIMEOUT_SEC = 300

# The operator-declared profiles and the pool the runner fans out across.
# config_dir values are never opened in this test (the children run on the
# openai-agents harness against the mock server); only the names travel.
_POOL = ["work", "personal"]


def _api(base_url: str, path: str) -> dict[str, Any]:
    """
    GET a local-server API path and decode the JSON body.

    :param base_url: Server base URL, e.g. ``"http://127.0.0.1:8811"``.
    :param path: API path starting with ``/``, e.g. ``"/v1/sessions"``.
    :returns: Decoded JSON object.
    """
    with urllib.request.urlopen(f"{base_url}{path}", timeout=15) as resp:
        return json.load(resp)


@pytest.fixture
def local_polly_server(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    """
    Start a throwaway local ``omnigent server`` from this working tree.

    Mirrors ``test_polly_subagent_model_e2e.local_polly_server`` (own sqlite DB
    + artifact dir under ``tmp_path``), and additionally yields the DB path:
    ``claude_profile`` never comes back over REST, so the row is read directly.

    :param tmp_path: pytest-provided per-test temp dir for the DB + artifacts.
    :yields: ``(base_url, db_path)`` for the running server.
    """
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    db_path = tmp_path / "polly_fanout_e2e.db"
    import os

    env = {
        **os.environ,
        "OMNIGENT_SKIP_ONBOARD": "1",
        "OMNIGENT_NO_UPDATE_CHECK": "1",
    }
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "omnigent",
            "server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--database-uri",
            f"sqlite:///{db_path}",
            "--artifact-location",
            str(tmp_path / "artifacts"),
        ],
        cwd=str(_REPO),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_for_health(base_url, time.monotonic() + _SERVER_BOOT_TIMEOUT_SEC)
        yield base_url, db_path
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


def _env_with_fanout_pool(mock_llm_server_url: str, profile_root: Path) -> dict[str, str]:
    """
    Build the runner env, then declare a fan-out pool in its config home.

    ``_mock_env`` points ``OMNIGENT_CONFIG_HOME`` at a scratch dir with an
    empty ``config.yaml``; this writes the operator's ``claude_profiles``
    block into that file so the runner in this process tree loads it.

    :param mock_llm_server_url: The mock LLM server base URL.
    :param profile_root: Directory the (never authenticated) profile
        ``config_dir`` paths are written under.
    :returns: The env mapping for the ``omnigent run`` subprocess.
    """
    env = _mock_env(mock_llm_server_url)
    profiles = "".join(
        f"    - name: {name}\n      config_dir: {profile_root / name}\n" for name in _POOL
    )
    Path(env["OMNIGENT_CONFIG_HOME"], "config.yaml").write_text(
        f"claude_profiles:\n  profiles:\n{profiles}  fanout_pool: {json.dumps(_POOL)}\n",
        encoding="utf-8",
    )
    return env


def _persisted_claude_profiles(db_path: Path) -> list[str]:
    """
    Read every persisted ``claude_profile`` from the server's sqlite DB.

    Reads all conversation rows rather than looking up child ids: the ``id``
    column is a packed 16-byte uuid, and the parent row carries no profile,
    so the non-null values ARE the fan-out assignments.

    :param db_path: Path to the throwaway server's sqlite file.
    :returns: Sorted profile names found on conversation rows.
    """
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT session_overrides FROM conversations").fetchall()
    found: list[str] = []
    for (raw,) in rows:
        if not raw:
            continue
        profile = json.loads(raw).get("claude_profile")
        if profile is not None:
            found.append(str(profile))
    return sorted(found)


def test_polly_fans_sub_agents_across_the_claude_profile_pool(
    local_polly_server: tuple[str, Path],
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """
    One turn, two workers, two pool profiles — each child persists its own.

    The mock brain dispatches both workers in a single turn; the runner
    round-robins the configured pool per parent, so the two child rows must
    carry ``work`` and ``personal`` (one each), not the same profile twice.
    That distinctness is the whole point of the feature: two sub-agents
    consuming two Claude budgets concurrently instead of sharing one.

    :param local_polly_server: ``(base_url, db_path)`` of the local server.
    :param mock_llm_server_url: Mock LLM server base URL.
    :param tmp_path: Per-test temp dir for the mock polly spec copy.
    """
    from tests.e2e.conftest import configure_mock_llm, reset_mock_llm

    base_url, db_path = local_polly_server
    reset_mock_llm(mock_llm_server_url)
    # rewrite_sub_agent_harnesses=True replaces native CLI harnesses so child
    # sessions are created even when the binaries are absent (e.g. on CI).
    # Fan-out assignment is harness-agnostic: it only sets the create body's
    # claude_profile, which the server persists on the child row.
    polly_dir = _mock_polly_spec_dir(
        tmp_path, mock_llm_server_url, rewrite_sub_agent_harnesses=True
    )
    tag = uuid.uuid4().hex[:8]

    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": f"call-cc-{tag}",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {
                                "agent": "claude_code",
                                "title": "explore-readme",
                                "args": {
                                    "purpose": "explore",
                                    "input": "Report the first heading line of README.md.",
                                },
                            }
                        ),
                    },
                    {
                        "call_id": f"call-cx-{tag}",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {
                                "agent": "codex",
                                "title": "explore-pyproject",
                                "args": {
                                    "purpose": "explore",
                                    "input": "Report the project name from pyproject.toml.",
                                },
                            }
                        ),
                    },
                ]
            },
            # After the tool results arrive, end the turn.
            {"text": "Dispatched both workers. Waiting for inbox notices."},
            # Synthesis after the sub-agents settle.
            {"text": "Both workers done."},
        ],
        key=_MOCK_BRAIN_MODEL,
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnigent",
            "run",
            str(polly_dir),
            "--server",
            base_url,
            "-p",
            "Dispatch two read-only explore tasks, one per worker.",
        ],
        cwd=str(_REPO),
        env=_env_with_fanout_pool(mock_llm_server_url, tmp_path / "claude-profiles"),
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SEC,
    )
    assert result.returncode == 0, (
        f"polly run exited {result.returncode}\n--- stdout ---\n{result.stdout}\n"
        f"--- stderr ---\n{result.stderr}"
    )

    sessions = _api(base_url, "/v1/sessions").get("data", [])
    parents = [s["id"] for s in sessions if s.get("agent_name") == "polly"]
    assert parents, f"no polly session found among {len(sessions)} sessions"
    kids = _api(base_url, f"/v1/sessions/{parents[0]}/child_sessions").get("data", [])
    tools = sorted(k.get("tool") or "" for k in kids)
    assert tools == ["claude_code", "codex"], (
        f"expected one child per worker, got {tools}; run stdout tail: {result.stdout[-400:]!r}"
    )

    # The core assertion: the pool was fanned out, not reused.
    assert _persisted_claude_profiles(db_path) == sorted(_POOL), (
        f"expected one child per pool profile; run stdout tail: {result.stdout[-400:]!r}"
    )
