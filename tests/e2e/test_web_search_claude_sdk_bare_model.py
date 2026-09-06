"""E2E regression tests: web_search on claude-sdk with bare / prefixed models.

A ``web_search`` builtin with an explicit ``search_provider`` must be
exposed to a ``claude-sdk`` session even when the agent spec pins a
**bare** model string (no ``provider/`` prefix). Today
``parse_model_string`` defaults bare models to provider ``"openai"``,
so ``WebSearchTool.get_schema()`` returns the native
``{"type": "web_search_preview"}`` passthrough, which the claude-sdk
harness drops ("skipping tool schema with no name") — the tool is
silently absent and the agent falls back to training knowledge.

The documented workaround (pinning ``anthropic/<model>``) must also
work: the ``anthropic/`` routing prefix must not be forwarded verbatim
to the Anthropic endpoint (the claude CLI rejects vendor-prefixed
model ids).

Both tests drive the real user journey — register the agent bundle,
create a runner-bound session (real claude CLI subprocess), send a
turn — and assert on the tool surface / model id actually observed on
the Anthropic wire via the mock LLM's request capture.

Usage::

    pytest tests/e2e/test_web_search_claude_sdk_bare_model.py -v --timeout=300
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


def _register_web_search_agent(
    client: httpx.Client,
    *,
    name: str,
    model: str,
    mock_llm_base_url: str,
) -> str:
    """Register a claude-sdk agent whose spec declares the ``web_search``
    builtin with an explicit non-OpenAI ``search_provider`` (the reported
    repro spec, with a keyless backend so no credential is needed).

    Bundled as a strict ``config.yaml`` spec so ``tools.builtins`` parses
    into ``BuiltinToolConfig`` entries exactly as a user's on-disk agent
    would.

    :param client: HTTP client pointed at the live e2e server.
    :param name: Agent name to register.
    :param model: The ``executor.model`` string under test (bare or
        ``anthropic/``-prefixed).
    :param mock_llm_base_url: Mock LLM base URL (no ``/v1`` — the
        Anthropic SDK appends ``/v1/messages``).
    :returns: The registered agent name.
    """
    config = {
        "spec_version": 1,
        "name": name,
        "description": "web_search provider-inference regression agent",
        "instructions": (
            "You are a research assistant. Use the web_search tool for any live question."
        ),
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
        "tools": {
            "builtins": [
                {"name": "web_search", "search_provider": "duckduckgo"},
            ]
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


def _drive_one_turn_and_capture(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
    *,
    model: str,
    agent_label: str,
) -> list[dict]:
    """Run the journey once and return the captured Anthropic requests.

    Registers the agent, creates a runner-bound session (spawning the
    real claude CLI), sends one user turn asking for a live web search,
    waits for the turn to complete, and returns the ``/v1/messages``
    request bodies the claude CLI sent to the mock — each carries the
    ``model`` and ``tools`` the SDK actually advertised.
    """
    reset_mock_llm(mock_llm_server_url)
    agent_name = _register_web_search_agent(
        http_client,
        name=f"wsbare-{agent_label}-{uuid.uuid4().hex[:6]}",
        model=model,
        mock_llm_base_url=mock_llm_server_url,
    )
    # One scripted text turn; the assertions read the request side.
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "Acknowledged."}],
        key=model,
    )
    # Post-fix the harness may send a stripped model id upstream; route
    # that key to the same queue so the turn still completes.
    if "/" in model:
        configure_mock_llm(
            mock_llm_server_url,
            [{"text": "Acknowledged."}],
            key=model.split("/", 1)[1],
        )
    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )
    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content="What is the latest omnigent release? Search the live web.",
    )
    body = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=response_id, timeout=180
    )
    assert body["status"] == "completed", f"turn failed: {body.get('error', 'unknown')!r}"
    reqs = get_mock_requests(mock_llm_server_url, key=model)
    if "/" in model:
        reqs = reqs + get_mock_requests(mock_llm_server_url, key=model.split("/", 1)[1])
    assert reqs, "mock LLM captured no Anthropic requests for this session"
    return reqs


def _advertised_tool_names(reqs: list[dict]) -> set[str]:
    """Collect every tool name advertised across the captured requests."""
    names: set[str] = set()
    for req in reqs:
        for tool in req.get("tools", []) or []:
            name = tool.get("name")
            if name:
                names.add(name)
    return names


def test_web_search_advertised_with_bare_model_on_claude_sdk(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """A bare (unprefixed) model on claude-sdk must still expose web_search.

    Reproduces the primary facet of the bug: with
    ``executor.model: claude-...`` (no ``anthropic/`` prefix) the provider
    is inferred as ``openai``, the passthrough schema is emitted, the
    harness drops it, and the tool is silently absent from the session.
    """
    model = f"claude-wsbare-{uuid.uuid4().hex[:6]}"
    reqs = _drive_one_turn_and_capture(
        http_client,
        live_runner_id,
        mock_llm_server_url,
        model=model,
        agent_label="bare",
    )
    names = _advertised_tool_names(reqs)
    # Guard the guard: the Omnigent MCP relay must be alive, otherwise a
    # missing web_search would prove nothing about provider inference.
    assert any(n.startswith("mcp__omnigent__") for n in names), (
        f"no Omnigent MCP tools advertised at all — relay broken? tools: {sorted(names)}"
    )
    assert "mcp__omnigent__web_search" in names, (
        "web_search builtin (search_provider: duckduckgo) is absent from the "
        "claude-sdk session's tool surface when executor.model is a bare "
        "string — parse_model_string inferred provider 'openai', the "
        "web_search_preview passthrough schema was emitted, and the harness "
        f"dropped it. Advertised tools: {sorted(names)}"
    )


def test_prefixed_model_not_forwarded_verbatim_to_anthropic(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """An ``anthropic/``-prefixed model must not reach the Anthropic wire verbatim.

    Reproduces the second facet of the bug: pinning
    ``model: anthropic/<name>`` fixes web_search provider inference, but the
    prefixed string is handed to the Claude SDK unmodified
    (``HARNESS_CLAUDE_SDK_MODEL``), and the claude CLI rejects
    vendor-prefixed model ids — so specs on claude-sdk cannot express a
    prefixed model and the workaround is unusable.
    """
    bare = f"claude-wsbare-{uuid.uuid4().hex[:6]}"
    model = f"anthropic/{bare}"
    reqs = _drive_one_turn_and_capture(
        http_client,
        live_runner_id,
        mock_llm_server_url,
        model=model,
        agent_label="prefixed",
    )
    # With the prefix the provider inference is correct, so web_search must
    # be present on this path already (guards the facet-1 mechanism).
    names = _advertised_tool_names(reqs)
    assert "mcp__omnigent__web_search" in names, (
        f"web_search missing even with anthropic/ prefix; tools: {sorted(names)}"
    )
    wire_models = {req.get("model") for req in reqs}
    assert all(not str(m).startswith("anthropic/") for m in wire_models), (
        "the spec's anthropic/-prefixed executor.model was forwarded "
        "verbatim to the Anthropic Messages endpoint — the real claude CLI "
        "rejects vendor-prefixed model ids ('There's an issue with the "
        f"selected model'), so the documented workaround fails. Wire model "
        f"fields observed: {sorted(str(m) for m in wire_models)}"
    )
