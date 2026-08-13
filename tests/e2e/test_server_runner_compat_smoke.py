"""Compatibility smoke tests: newer server ↔ older runner, and vice versa.

These tests guard the two most dangerous deployment orderings:

Config 1 (new server, old runner):
  Deploy the server first; some runners are still running the previous release.
  Smoke: can an old runner still create a session and complete a turn?

Config 2 (new runner, old server):
  Deploy runners first; the server is still running the previous release.
  Smoke: can a new runner still create a session and complete a turn on an
  old server?

Each test performs the minimal end-to-end path:

  1. Register an agent (openai-agents harness, mock-LLM model).
  2. Create a runner-bound session.
  3. Send a user message.
  4. Poll until the session is idle.
  5. Assert the turn produced a text reply and the session never 500-ed.

The ``mock_llm_server_url`` fixture is always available (the mock LLM server
is started unconditionally by the e2e conftest); tests use a keyed model name
so their response queue is isolated from concurrent workers.

Activation
----------
Normal ``pytest tests/e2e/`` runs include these tests automatically — they are
**not** excluded by the default ``--ignore`` list because they exercise the
compat infrastructure itself and should run on every PR.

For compat CI (old server or old runner), set the relevant env vars before
running::

  # Config 1 — old server, new runner:
  OMNIGENT_COMPAT_SERVER_PYTHON=/tmp/old-server-venv/bin/python  \\
  OMNIGENT_COMPAT_SERVER_VERSION=0.9.0                            \\
  pytest tests/e2e/test_server_runner_compat_smoke.py -v

  # Config 2 — old runner, new server:
  OMNIGENT_COMPAT_RUNNER_PYTHON=/tmp/old-runner-venv/bin/python  \\
  OMNIGENT_COMPAT_RUNNER_VERSION=0.9.0                            \\
  pytest tests/e2e/test_server_runner_compat_smoke.py -v

See ``docs/SERVER_VERSION_COMPAT_CI.md`` for the full compat workflow.
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    poll_session_until_terminal,
    register_inline_agent,
    reset_mock_llm,
    send_user_message_to_session,
)

# ── Helpers ──────────────────────────────────────────────────────────────────


def _register_echo_agent(
    client: httpx.Client,
    *,
    uid: str,
    mock_llm_server_url: str,
) -> tuple[str, str]:
    """Register a single-turn echo agent and configure its mock-LLM queue.

    :param client: HTTP client pointed at the live server.
    :param uid: Short unique suffix for model and agent names so parallel
        workers don't share queues.
    :param mock_llm_server_url: Mock LLM base URL (without ``/v1``).
    :returns: ``(agent_name, model_key)`` — the registered agent name and
        the model key used to configure the mock-LLM queue.
    """
    model = f"mock-compat-{uid}"
    mock_base = f"{mock_llm_server_url}/v1"
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": f"compat-echo-{uid}"}],
        key=model,
    )
    agent_name = register_inline_agent(
        client,
        name=f"compat-smoke-{uid}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt="Reply briefly.",
        mock_llm_base_url=mock_base,
    )
    return agent_name, model


# ── Tests ─────────────────────────────────────────────────────────────────────


def test_new_server_old_runner_compat_smoke(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
    server_version: str,
) -> None:
    """A session turn completes when the server is newer than the runner.

    Config 1: the server is at HEAD (or pinned to a newer version) and
    the runner subprocess runs an older build, activated by setting
    ``OMNIGENT_COMPAT_RUNNER_PYTHON`` and ``OMNIGENT_COMPAT_RUNNER_VERSION``
    before running. In normal (non-compat) runs both components are HEAD and
    the test still exercises the full dispatch path.

    Assertion: the turn completes (status == "completed"), the agent's text
    reply is present in the output, and no HTTP 500 was encountered.

    :param http_client: HTTP client pointed at the live server.
    :param live_runner_id: Runner id to bind the session to.
    :param mock_llm_server_url: Mock LLM base URL (without ``/v1``).
    :param server_version: Version reported by the live server — logged for
        diagnostics; not used as a skip guard here.
    """
    uid = uuid.uuid4().hex[:8]
    agent_name, _model = _register_echo_agent(
        http_client,
        uid=uid,
        mock_llm_server_url=mock_llm_server_url,
    )

    session_id = create_runner_bound_session(
        http_client,
        agent_name=agent_name,
        runner_id=live_runner_id,
    )

    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=f"ping-{uid}",
    )

    result = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=response_id,
        timeout=60,
    )

    assert result["status"] == "completed", (
        f"Config 1 (new server / old runner) smoke: turn did not complete.\n"
        f"server_version={server_version!r}, session={session_id!r}\n"
        f"result={result}"
    )
    output_texts = [
        block.get("text", "")
        for item in result.get("output", [])
        for block in (item.get("content") or [])
        if block.get("type") == "output_text"
    ]
    assert any(f"compat-echo-{uid}" in t for t in output_texts), (
        f"Config 1 smoke: expected echo text not found in output.\noutput_texts={output_texts!r}"
    )


@pytest.mark.min_server_version("0.9.0")
def test_new_runner_old_server_compat_smoke(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
    server_version: str,
) -> None:
    """A session turn completes when the runner is newer than the server.

    Config 2: the runner subprocess is at HEAD (or a newer build) and the
    server runs an older version, activated by setting
    ``OMNIGENT_COMPAT_SERVER_PYTHON`` and ``OMNIGENT_COMPAT_SERVER_VERSION``.
    In normal (non-compat) runs both components are HEAD.

    The ``min_server_version("0.9.0")`` marker skips this test when the
    server is older than 0.9.0 — below that baseline the session-init
    envelope format (``RunnerSessionInitEnvelope``) and the
    ``/api/version`` probe may not be present, which are prerequisites
    for the cross-version path being tested. Tests below that baseline
    belong to narrower, feature-specific guards (e.g.
    ``test_waiting_status_compat_e2e.py``).

    Assertion: the turn completes (status == "completed"), the agent's text
    reply is present in the output, and no HTTP 500 was encountered.

    :param http_client: HTTP client pointed at the live server.
    :param live_runner_id: Runner id to bind the session to.
    :param mock_llm_server_url: Mock LLM base URL (without ``/v1``).
    :param server_version: Version reported by the live server — logged in
        the failure message; also used by the ``min_server_version`` marker
        to skip on genuinely old servers.
    """
    uid = uuid.uuid4().hex[:8]
    agent_name, _model = _register_echo_agent(
        http_client,
        uid=uid,
        mock_llm_server_url=mock_llm_server_url,
    )

    session_id = create_runner_bound_session(
        http_client,
        agent_name=agent_name,
        runner_id=live_runner_id,
    )

    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=f"ping-{uid}",
    )

    result = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=response_id,
        timeout=60,
    )

    assert result["status"] == "completed", (
        f"Config 2 (new runner / old server) smoke: turn did not complete.\n"
        f"server_version={server_version!r}, session={session_id!r}\n"
        f"result={result}"
    )
    output_texts = [
        block.get("text", "")
        for item in result.get("output", [])
        for block in (item.get("content") or [])
        if block.get("type") == "output_text"
    ]
    assert any(f"compat-echo-{uid}" in t for t in output_texts), (
        f"Config 2 smoke: expected echo text not found in output.\noutput_texts={output_texts!r}"
    )
