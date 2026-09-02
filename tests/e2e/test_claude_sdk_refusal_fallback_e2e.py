"""E2E regression test: claude-sdk refusal-fallback routes to a served model.

Claude Code's safeguards can flag a message (e.g. a security-reproduction
prompt) and return an API *refusal*. On a refusal the CLI arms its
refusal-fallback and re-issues the turn on a different family — Opus for the
``cyber`` category — resolving that family through
``ANTHROPIC_DEFAULT_OPUS_MODEL``. On the Databricks gateway that env is only
set if Omnigent pins it: unpinned, the alias resolves to a **bare** canonical
id (``claude-opus-4-8``) the gateway does not serve (it serves only
``databricks-`` spelled ids), so the fallback request fails
``model_not_found`` and the whole turn dies with
``inner executor error: There's an issue with the selected model
(claude-opus-4-8)`` while the UI shows ``<synthetic>`` for the model.

The fix pins each family alias to the workspace's served id, so the fallback
re-issues on ``databricks-claude-opus-4-8`` (servable) and the turn completes.

This drives the real journey — register a claude-sdk agent, create a
runner-bound session (real ``claude`` CLI subprocess), send a turn the mock
LLM refuses — and asserts the fallback landed on the served Opus id (visible
on the Anthropic wire via the mock's request capture) and the turn completed.

Usage::

    pytest tests/e2e/test_claude_sdk_refusal_fallback_e2e.py -v --timeout=300
"""

from __future__ import annotations

import io
import json
import tarfile
import uuid

import httpx
import yaml

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    get_mock_requests,
    poll_session_until_terminal,
    reset_mock_llm,
    send_user_message_to_session,
)

# The workspace's served Claude ids the mock advertises on its gateway model
# listing (see ``SERVED_CLAUDE_MODELS`` in the mock server). The launch model
# and the Opus the refusal-fallback must route to.
_LAUNCH_MODEL = "databricks-claude-fable-5"
_SERVED_FALLBACK_MODEL = "databricks-claude-opus-4-8"


def _register_claude_sdk_agent(
    client: httpx.Client,
    *,
    name: str,
    model: str,
    mock_llm_base_url: str,
) -> str:
    """Register a minimal claude-sdk agent bound to the mock gateway.

    ``executor.auth`` is an ``api_key`` with the mock base URL — the wiring a
    Databricks AI Gateway provider produces — and the model is a
    ``databricks-`` id, which marks the gateway as one whose family aliases
    need served-id pins.
    """
    config = {
        "spec_version": 1,
        "name": name,
        "description": "claude-sdk refusal-fallback regression agent",
        "instructions": "You are a helpful assistant. Answer the user directly.",
        "executor": {
            "type": "omnigent",
            "model": model,
            "config": {"harness": "claude-sdk"},
            "auth": {
                "type": "api_key",
                "api_key": "mock-key",
                "base_url": mock_llm_base_url,
            },
        },
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml.safe_dump(config).encode()
        info = tarfile.TarInfo("config.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    resp = client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", buf.getvalue(), "application/gzip")},
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )
    if resp.status_code not in (200, 201, 409):
        raise RuntimeError(f"agent register failed: {resp.status_code} {resp.text[:500]}")
    return name


def test_claude_sdk_refusal_fallback_routes_to_served_model(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """A safeguard refusal on claude-sdk falls back to the served Opus id.

    The mock refuses the launch-model (Fable) turn with a ``cyber`` refusal.
    With the alias pins the fix injects, Claude Code re-issues the turn on the
    servable ``databricks-claude-opus-4-8`` and completes — instead of dying on
    a bare ``claude-opus-4-8`` the gateway rejects.
    """
    reset_mock_llm(mock_llm_server_url)
    agent_name = _register_claude_sdk_agent(
        http_client,
        name=f"refusal-fb-{uuid.uuid4().hex[:6]}",
        model=_LAUNCH_MODEL,
        mock_llm_base_url=mock_llm_server_url,
    )
    # The Fable turn is refused (cyber). The Opus re-issue and any preflight
    # calls fall through to the queue default (a plain text answer), so the
    # turn can complete once the fallback lands on a served model.
    configure_mock_llm(
        mock_llm_server_url,
        [{"refusal_category": "cyber"}],
        key=_LAUNCH_MODEL,
    )
    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )
    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="Please greet me.",
    )
    body = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=response_id, timeout=180
    )

    reqs = get_mock_requests(mock_llm_server_url)
    wire_models = [req.get("model") for req in reqs]

    # The launch turn ran on Fable and was refused — the precondition.
    assert _LAUNCH_MODEL in wire_models, (
        f"the launch model {_LAUNCH_MODEL!r} never reached the wire; models seen: {wire_models}"
    )
    # The fix pinned the Opus alias to the served id, so the refusal-fallback
    # re-issued on it. Without the fix the alias resolves to a bare
    # ``claude-opus-4-8`` the gateway rejects, and no such request is made.
    assert _SERVED_FALLBACK_MODEL in wire_models, (
        f"the refusal-fallback did not re-issue on the served Opus id "
        f"{_SERVED_FALLBACK_MODEL!r} — the ANTHROPIC_DEFAULT_OPUS_MODEL pin is "
        f"missing, so the alias resolved to a canonical id the gateway rejects. "
        f"Models seen on the wire: {wire_models}"
    )
    # And the turn completed rather than dying with the model_not_found error.
    assert body["status"] == "completed", (
        f"the turn did not complete after the refusal-fallback: "
        f"status={body['status']!r} error={body.get('error')!r}"
    )
