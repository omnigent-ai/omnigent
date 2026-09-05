"""E2E coverage for fresh registered-agent sessions on a remote-URL target.

``omnigent chat <remote-url>`` connects to a server that already has its
agents registered, so the CLI has no local bundle to upload.  Session
creation must therefore bind by ``agent_id`` through the JSON
``POST /v1/sessions`` route rather than the multipart bundle route.

Three paths are covered:

1. **Interactive fresh session.** ``_SessionsChatReplAdapter`` with
   ``session_bundle=None`` resolves the registered ``agent_id`` and creates
   the session, instead of refusing for lack of a bundle.
2. **Server JSON route.** ``POST /v1/sessions`` with ``{"agent_id": ...}``
   creates a session that completes a turn end-to-end.
3. **Headless ``-p``.** ``_query_sessions_once`` with ``session_bundle=None``
   goes through the sessions API rather than the removed legacy
   ``/v1/responses`` endpoint.

Run locally (mock LLM, no real API key needed)::

    pytest tests/e2e/test_remote_url_chat_fresh_session_2437.py -v
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import yaml as _yaml

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests.e2e.conftest import (
    configure_mock_llm,
    find_free_port,
    poll_session_until_terminal,
    reset_mock_llm,
    send_user_message_to_session,
)
from tests.e2e.helpers import final_assistant_text

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_agent_yaml(
    tmp_path: Path,
    *,
    agent_name: str,
    model: str,
    mock_llm_server_url: str | None,
) -> Path:
    """Write a minimal openai-agents YAML wired to the mock LLM server."""
    yaml_path = tmp_path / f"{agent_name}.yaml"
    yaml_path.write_text(
        _yaml.safe_dump(
            {
                "name": agent_name,
                "prompt": "You are a terse test assistant.",
                "executor": {
                    "harness": "openai-agents",
                    "model": model,
                    "profile": "",
                    "auth": {
                        "type": "api_key",
                        "api_key": "mock-key",
                        "base_url": f"{mock_llm_server_url}/v1",
                    },
                },
            }
        )
    )
    return yaml_path


class _RegisteredAgentServer:
    """A booted local server plus the registered agent's identifiers."""

    def __init__(self, base_url: str, agent_name: str, runner_id: str, marker: str) -> None:
        self.base_url = base_url
        self.agent_name = agent_name
        self.runner_id = runner_id
        self.marker = marker

    def client(self) -> httpx.Client:
        """Return an HTTP client bound to this server."""
        return httpx.Client(base_url=self.base_url, timeout=30.0)

    def agent_id(self) -> str:
        """Resolve the registered agent id through ``GET /v1/agents``."""
        with self.client() as client:
            resp = client.get("/v1/agents", params={"limit": 100})
            resp.raise_for_status()
            data = resp.json().get("data", [])
        for item in data:
            if item.get("name") == self.agent_name:
                return str(item["id"])
        raise AssertionError(
            f"agent {self.agent_name!r} not registered. Available: {[d.get('name') for d in data]}"
        )


@pytest.fixture
def registered_agent_server(
    mock_llm_server_url: str | None,
    tmp_path: Path,
) -> Iterator[_RegisteredAgentServer]:
    """
    Boot a local server whose agent is registered but has no client bundle.

    Mirrors the remote-URL target: the caller knows only the agent's name,
    and must resolve its id from the server to create a session.
    """
    from omnigent.chat import (
        _start_local_server,
        _stop_local_server,
        _wait_for_server,
    )

    suffix = uuid.uuid4().hex[:6]
    agent_name = f"remote-fresh-{suffix}"
    model = f"mock-{suffix}"
    marker = f"REMOTE_FRESH_OK_{suffix}"

    os.environ["OPENAI_API_KEY"] = "mock-key"
    os.environ["OPENAI_BASE_URL"] = f"{mock_llm_server_url}/v1"

    yaml_path = _write_agent_yaml(
        tmp_path,
        agent_name=agent_name,
        model=model,
        mock_llm_server_url=mock_llm_server_url,
    )
    reset_mock_llm(mock_llm_server_url)
    # Every test in this module drives at most a couple of turns; repeat the
    # marker so a retried or multi-turn path still gets a scripted reply.
    configure_mock_llm(mock_llm_server_url, [{"text": marker}] * 4, key=model)

    port = find_free_port()
    server = _start_local_server(yaml_path, port, ephemeral=True)
    try:
        _wait_for_server(port, server)
        yield _RegisteredAgentServer(
            base_url=f"http://127.0.0.1:{port}",
            agent_name=agent_name,
            runner_id=server.runner_id or "",
            marker=marker,
        )
    finally:
        _stop_local_server(server)


def _assistant_text(base_url: str, session_id: str) -> str:
    """Concatenate persisted assistant text for a session."""
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        resp = client.get(f"/v1/sessions/{session_id}")
        resp.raise_for_status()
        body = resp.json()
    parts: list[str] = []
    for item in body.get("items", []):
        data = item.get("data", {})
        if data.get("role") != "assistant":
            continue
        for chunk in data.get("content", []):
            text = chunk.get("text")
            if text:
                parts.append(text)
    return "".join(parts)


# ---------------------------------------------------------------------------
# Interactive fresh session (no local bundle)
# ---------------------------------------------------------------------------


def test_repl_adapter_creates_session_without_bundle(
    registered_agent_server: _RegisteredAgentServer,
) -> None:
    """The REPL adapter creates a session by agent_id when it has no bundle.

    ``omnigent chat <remote-url>`` reaches ``_SessionsChatReplAdapter`` with
    ``session_bundle=None`` and ``session_id=None``.  The adapter must resolve
    the registered agent and create the session rather than refusing for lack
    of a bundle.

    Passes ``runner_id=None`` because that is what ``run_chat`` supplies for a
    URL target: a remote client owns no runner, so the adapter has to adopt one
    the server already has online or the first turn fails to dispatch.
    """
    from omnigent_client import OmnigentClient

    from omnigent.repl._repl import _SessionsChatReplAdapter

    server = registered_agent_server

    async def _drive() -> str:
        async with OmnigentClient(base_url=server.base_url) as client:
            adapter = _SessionsChatReplAdapter(
                client=client,
                agent_name=server.agent_name,
                session_bundle=None,
                session_id=None,
                runner_id=None,
            )
            async for _ in adapter.send("Say hello briefly."):
                pass
            session_id = adapter._session_id
            assert session_id is not None, "adapter did not record a session id"
            return session_id

    session_id = asyncio.run(_drive())
    text = _assistant_text(server.base_url, session_id)
    assert server.marker in text, f"Expected {server.marker!r} in transcript. Got: {text!r}"


def test_repl_adapter_unknown_agent_name_is_reported(
    registered_agent_server: _RegisteredAgentServer,
) -> None:
    """An unregistered agent name fails with a message naming the agent.

    Guards the resolve step: a typo'd or missing agent must surface as a clear
    lookup failure rather than a confusing session-create error.
    """
    from omnigent_client import OmnigentClient

    from omnigent.repl._repl import _SessionsChatReplAdapter

    server = registered_agent_server

    async def _drive() -> None:
        async with OmnigentClient(base_url=server.base_url) as client:
            adapter = _SessionsChatReplAdapter(
                client=client,
                agent_name="no-such-agent",
                session_bundle=None,
                session_id=None,
                runner_id=server.runner_id,
            )
            async for _ in adapter.send("hello"):
                pass

    with pytest.raises(LookupError, match="no-such-agent"):
        asyncio.run(_drive())


# ---------------------------------------------------------------------------
# Server JSON agent_id route
# ---------------------------------------------------------------------------


def test_server_creates_fresh_session_via_json_agent_id(
    registered_agent_server: _RegisteredAgentServer,
) -> None:
    """``POST /v1/sessions`` with a JSON agent_id creates a working session.

    This is the route the fixed client paths depend on, so it is worth
    pinning independently of them.
    """
    server = registered_agent_server
    agent_id = server.agent_id()
    assert agent_id, f"expected non-empty agent_id for {server.agent_name!r}"

    with server.client() as client:
        resp = client.post(
            "/v1/sessions",
            json={"agent_id": agent_id},
            headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
        )
        resp.raise_for_status()
        session_id = str(resp.json()["id"])
        assert session_id, "expected a session id from the JSON agent_id POST"

        resp = client.patch(f"/v1/sessions/{session_id}", json={"runner_id": server.runner_id})
        resp.raise_for_status()

        response_id = send_user_message_to_session(
            client,
            session_id=session_id,
            content="Say hello briefly.",
        )
        body = poll_session_until_terminal(
            client,
            session_id=session_id,
            response_id=response_id,
            timeout=120,
        )

    assert body["status"] == "completed", (
        f"Status: {body['status']!r}. Output: {body.get('output', [])}"
    )
    text = final_assistant_text(body)
    assert server.marker in text, f"Expected {server.marker!r} in output. Got: {text!r}"


# ---------------------------------------------------------------------------
# Headless -p without a bundle
# ---------------------------------------------------------------------------


def test_headless_prompt_without_bundle_uses_sessions_api(
    registered_agent_server: _RegisteredAgentServer,
) -> None:
    """Headless ``-p`` against a remote target answers via the sessions API.

    The no-bundle path previously fell through to the legacy
    ``/v1/responses`` endpoint, which the server no longer exposes.  It must
    now resolve the registered agent and use the sessions API instead.
    """
    from omnigent_client import OmnigentClient

    from omnigent.chat import _query_sessions_once

    server = registered_agent_server

    async def _one_shot() -> str | None:
        async with OmnigentClient(base_url=server.base_url) as client:
            return await _query_sessions_once(
                client=client,
                agent_name=server.agent_name,
                tool_handler=None,
                prompt="Say hello briefly.",
                session_bundle=None,
                session_bundle_filename="agent.tar.gz",
                runner_id=None,
            )

    result = asyncio.run(_one_shot())
    assert result is not None, "headless one-shot returned no text"
    assert server.marker in result, f"Expected {server.marker!r} in output. Got: {result!r}"


def test_run_one_shot_without_bundle_answers(
    registered_agent_server: _RegisteredAgentServer,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``run --server <url> -p`` answers without a local bundle.

    ``_chat_with_server`` routes ``-p`` through ``_run_one_shot``, a
    separate entry point from ``_run_headless_prompt``.  It also used to
    require a bundle and fall back to the legacy client query, so a
    remote-URL one-shot failed with ``Not Found``.
    """
    from omnigent.chat import _run_one_shot

    server = registered_agent_server

    _run_one_shot(
        base_url=server.base_url,
        agent_name=server.agent_name,
        tool_handler=None,
        prompt="Say hello briefly.",
        # What _chat_with_server passes for a URL target.
        runner_id=None,
        session_bundle=None,
    )

    out = capsys.readouterr().out
    assert server.marker in out, f"Expected {server.marker!r} on stdout. Got: {out!r}"


# ---------------------------------------------------------------------------
# Contracts the remote-URL path depends on
# ---------------------------------------------------------------------------


def test_json_create_returns_full_session_snapshot(
    registered_agent_server: _RegisteredAgentServer,
) -> None:
    """The JSON create route returns a full snapshot, not just an id.

    ``create_from_agent_id`` parses the POST response directly with
    ``Session.from_dict`` and skips the follow-up ``GET`` that the multipart
    ``create`` needs.  If the server ever narrowed this route to
    ``{"session_id": ...}`` that parse would break, so pin the shape.
    """
    server = registered_agent_server

    with server.client() as client:
        resp = client.post(
            "/v1/sessions",
            json={"agent_id": server.agent_id()},
            headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
        )
        resp.raise_for_status()
        body = resp.json()

    for field_name in ("id", "agent_id", "status", "created_at"):
        assert field_name in body, (
            f"JSON create response is missing {field_name!r}; "
            f"Session.from_dict would fail. Got keys: {sorted(body)}"
        )


def test_no_online_runner_reports_actionable_error(
    registered_agent_server: _RegisteredAgentServer,
) -> None:
    """A server with no online runner fails with a message naming the fix.

    A remote client adopts one of the server's online runners.  When the
    server has none, the turn cannot dispatch, so the error should point at
    starting a host rather than at the ``--server`` flag the user already used.
    """
    from omnigent_client import OmnigentClient

    from omnigent.repl._repl import _SessionsChatReplAdapter

    server = registered_agent_server

    async def _drive() -> None:
        async with OmnigentClient(base_url=server.base_url) as client:
            adapter = _SessionsChatReplAdapter(
                client=client,
                agent_name=server.agent_name,
                session_bundle=None,
                session_id=None,
                runner_id=None,
            )
            # Simulate "server has no online runner" without tearing the real
            # one down: discovery finds nothing, so binding must fail loudly.
            adapter._client.sessions.resolve_online_runner = _no_runner  # type: ignore[method-assign]
            async for _ in adapter.send("hello"):
                pass

    with pytest.raises(RuntimeError, match="no online runner"):
        asyncio.run(_drive())


async def _no_runner(
    *, harness: str | None = None, canonicalize: object | None = None
) -> str | None:
    """Stand in for runner discovery on a server with no online runner."""
    return None


@pytest.mark.parametrize(
    ("agent_harness", "advertised"),
    [
        ("claude", "claude-sdk"),  # agent reports an alias
        ("claude-sdk", "claude"),  # runner advertises an alias
    ],
)
def test_runner_adoption_matches_harness_aliases(agent_harness: str, advertised: str) -> None:
    """Runner adoption accepts either spelling of a harness name.

    The server canonicalizes harness names before matching a runner
    (``_runner_supports_harness``), so the client must too: an agent whose spec
    says ``claude`` has to match a runner advertising ``claude-sdk``, or a
    compatible runner looks absent and the turn fails with "no online runner".
    """
    import json

    from omnigent_client._sessions import SessionsNamespace

    from omnigent.harness_aliases import canonicalize_harness

    class _Resp:
        status_code = 200
        headers = {"content-type": "application/json"}

        def __init__(self, harnesses: list[str]) -> None:
            self.text = json.dumps(
                {"data": [{"runner_id": "r1", "online": True, "harnesses": harnesses}]}
            )

        def json(self) -> object:
            return json.loads(self.text)

    class _Http:
        def __init__(self, harnesses: list[str]) -> None:
            self._harnesses = harnesses

        async def get(self, *_args: object, **_kwargs: object) -> _Resp:
            return _Resp(self._harnesses)

    namespace = SessionsNamespace(_Http([advertised]), "http://example")  # type: ignore[arg-type]

    async def _resolve(canonicalize: object) -> str | None:
        return await namespace.resolve_online_runner(
            harness=agent_harness,
            canonicalize=canonicalize,  # type: ignore[arg-type]
        )

    assert asyncio.run(_resolve(lambda name: canonicalize_harness(name) or name)) == "r1"
    # Without canonicalization the alias would not match at all — the bug this guards.
    assert asyncio.run(_resolve(None)) is None
