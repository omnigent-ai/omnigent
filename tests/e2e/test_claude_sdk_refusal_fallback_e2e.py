"""E2E regression test: claude-sdk refusal-fallback routes to a served model.

Claude Code's safeguards can flag a message (e.g. a security-reproduction
prompt) and return an API *refusal*. On a refusal the CLI arms its
refusal-fallback and re-issues the turn on a different family — Opus for the
``cyber`` category — resolving that family through
``ANTHROPIC_DEFAULT_OPUS_MODEL``. On a gateway that serves Claude under its own
ids, that env is only set if Omnigent pins it: unpinned, the alias resolves to
a **canonical** vendor id (``claude-opus-4-8``) the gateway does not serve, so
the fallback request fails ``model_not_found`` and the whole turn dies with
``inner executor error: There's an issue with the selected model
(claude-opus-4-8)`` while the UI shows ``<synthetic>`` for the model. (Reported
on the Databricks gateway, whose ids are ``databricks-claude-*``; any gateway
with its own spellings has the same failure.)

Pinning the ``opus`` alias is not enough on its own. The alias should track the
newest Opus the gateway serves, while the CLI's refusal route table names an
*older* canonical generation, so the two disagree exactly when a gateway serves
both. The fix therefore also hands Claude Code a ``modelOverrides`` map derived
from the same listing — canonical id to served spelling — so whichever id the
route table names is rewritten to something the gateway answers. This test
serves a newer Opus alongside the fallback target to hold that apart.

This drives the real journey — register a claude-sdk agent on the mock
gateway, create a runner-bound session (real ``claude`` CLI subprocess), send
a turn the mock refuses — and asserts the fallback landed on the served Opus
id (visible on the Anthropic wire via the mock's request capture) and the turn
completed.

Usage::

    pytest tests/e2e/test_claude_sdk_refusal_fallback_e2e.py -v --timeout=300
"""

from __future__ import annotations

import contextlib
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
    set_mock_served_models,
)

# The Claude ids the mock gateway serves under its own spelling — the launch
# model and the Opus the refusal-fallback must route to. Deliberately not a
# real vendor's spelling: the fix must work for any gateway's ids.
_LAUNCH_MODEL = "gw-claude-fable-5"
_SERVED_FALLBACK_MODEL = "gw-claude-opus-4-8"
# A newer Opus, served alongside the fallback target. The ``opus`` alias pins
# to this one (newest per family), so the refusal-fallback can only reach the
# older generation its route table names through the canonical rewrites — which
# is the behavior under test. A gateway serving exactly this pair is where the
# alias-only fix stopped working.
_SERVED_NEWER_OPUS = "gw-claude-opus-5"
_SERVED_MODELS = [
    _LAUNCH_MODEL,
    _SERVED_FALLBACK_MODEL,
    _SERVED_NEWER_OPUS,
    "gw-claude-sonnet-5",
    "gw-claude-haiku-4-5",
]


def _register_claude_sdk_agent(
    client: httpx.Client,
    *,
    name: str,
    model: str,
    mock_llm_base_url: str,
) -> str:
    """Register a minimal claude-sdk agent bound to the mock gateway.

    ``executor.auth`` is an ``api_key`` with the mock base URL — the wiring
    any Anthropic-compatible gateway provider produces.
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
    isolated_mock_llm_server_url: str,
) -> None:
    """A safeguard refusal on claude-sdk falls back to the served Opus id.

    The mock refuses the launch-model (Fable) turn with a ``cyber`` refusal.
    The gateway serves two Opus generations, so the ``opus`` alias points at the
    newer one and cannot carry the fallback: Claude Code names the older
    canonical id from its own route table. The canonical rewrites the fix
    derives from the listing turn that id into the gateway's spelling, so the
    turn re-issues on a served model and completes instead of dying on a
    canonical id the gateway rejects.
    """
    reset_mock_llm(isolated_mock_llm_server_url)
    # What the gateway serves: the executor lists this once per session and
    # pins each family alias to it.
    set_mock_served_models(isolated_mock_llm_server_url, _SERVED_MODELS)
    agent_name = _register_claude_sdk_agent(
        http_client,
        name=f"refusal-fb-{uuid.uuid4().hex[:6]}",
        model=_LAUNCH_MODEL,
        mock_llm_base_url=isolated_mock_llm_server_url,
    )
    # The Fable turn is refused (cyber). The Opus re-issue and any preflight
    # calls fall through to the queue default (a plain text answer), so the
    # turn can complete once the fallback lands on a served model.
    configure_mock_llm(
        isolated_mock_llm_server_url,
        [{"refusal_category": "cyber"}],
        key=_LAUNCH_MODEL,
    )
    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )
    try:
        response_id = send_user_message_to_session(
            http_client,
            session_id=session_id,
            content="Please greet me.",
        )
        body = poll_session_until_terminal(
            http_client, session_id=session_id, response_id=response_id, timeout=180
        )

        reqs = get_mock_requests(isolated_mock_llm_server_url)
        wire_models = [req.get("model") for req in reqs]

        # The launch turn ran on Fable and was refused — the precondition.
        assert _LAUNCH_MODEL in wire_models, (
            f"the launch model {_LAUNCH_MODEL!r} never reached the wire; "
            f"models seen: {wire_models}"
        )
        # The refusal-fallback re-issued on the gateway's spelling of the older
        # Opus its route table names. Without the canonical rewrites the CLI sends
        # the bare canonical id, the gateway rejects it, and no such request lands.
        assert _SERVED_FALLBACK_MODEL in wire_models, (
            f"the refusal-fallback did not re-issue on the served Opus id "
            f"{_SERVED_FALLBACK_MODEL!r} — the canonical id Claude Code names was "
            f"not rewritten to this gateway's spelling, so the fallback request "
            f"named a model the gateway rejects. Models seen: {wire_models}"
        )
        # No canonical spelling ever reached the wire: the rewrite happened before
        # the request, rather than the gateway happening to tolerate a vendor id.
        canonical_leaks = [
            model
            for model in wire_models
            if isinstance(model, str) and model.startswith("claude-")
        ]
        assert not canonical_leaks, (
            f"canonical vendor ids reached the gateway unrewritten: {canonical_leaks}"
        )
        # And the turn completed rather than dying with the model_not_found error.
        assert body["status"] == "completed", (
            f"the turn did not complete after the refusal-fallback: "
            f"status={body['status']!r} error={body.get('error')!r}"
        )
    finally:
        with contextlib.suppress(httpx.HTTPError):
            http_client.delete(f"/v1/sessions/{session_id}", timeout=30.0)
