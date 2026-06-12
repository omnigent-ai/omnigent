"""Integration tests for the session-scoped ``harness_override`` column.

Mirrors ``test_sessions_model_override.py``: create-time persistence,
snapshot read-back, runner-body forwarding (via a ``_get_runner_client``
capture stub), and fail-loud validation. ``harness_override`` is
create-time only — there is intentionally no PATCH path, since the
harness process spawns on the session's first turn.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


class _CaptureClient:
    """Runner-client stub that records the POSTed path + body.

    :param captured: Dict the test inspects after the route fires.
    """

    def __init__(self, captured: dict[str, Any]) -> None:
        self._captured = captured

    async def post(self, path: str, *, json: dict[str, Any], **_: Any) -> Any:
        """Record the path + body and return a fake 202 response."""
        self._captured["path"] = path
        self._captured["body"] = json

        class _Resp:
            status_code = 202
            headers: dict[str, str] = {}
            text = ""

        return _Resp()

    async def get(self, *_: Any, **__: Any) -> Any:
        raise NotImplementedError


def _stub_runner_client(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Patch ``_get_runner_client`` to return a capturing stub.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: A dict the test inspects after the runner POST runs;
        contains ``path`` and ``body`` keys once the route fires.
    """
    from omnigent.server.routes import sessions as sessions_mod

    captured: dict[str, Any] = {}

    async def _stub(*_: Any, **__: Any) -> _CaptureClient:
        return _CaptureClient(captured)

    monkeypatch.setattr(sessions_mod, "_get_runner_client", _stub)
    return captured


async def test_create_with_harness_override_persists_and_snapshot_reflects(
    client: httpx.AsyncClient,
) -> None:
    """Create-time ``harness_override`` makes the snapshot report it.

    The test bundle declares ``executor.config.harness: claude-sdk``;
    after creating with ``harness_override: "pi"`` the snapshot's
    ``harness`` must be ``"pi"`` — what the runner will actually spawn —
    not the spec's declared value. This is the contract the new-chat
    composer and the attach banner rely on.
    """
    agent = await create_test_agent(client)
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "initial_items": [], "harness_override": "pi"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body.get("harness") == "pi", (
        f"Create response harness is {body.get('harness')!r}, expected 'pi'. "
        f"If this is 'claude-sdk', _resolve_harness ignored the persisted "
        f"override and reported the spec default."
    )

    get = await client.get(f"/v1/sessions/{body['id']}")
    assert get.status_code == 200
    assert get.json().get("harness") == "pi", (
        "GET snapshot lost the harness override — the column did not "
        "persist or _resolve_harness stopped preferring it."
    )


async def test_create_without_harness_override_reports_spec_default(
    client: httpx.AsyncClient,
) -> None:
    """No override → the snapshot reports the spec's declared harness.

    Guards the NULL-means-track-the-spec semantics: an un-overridden
    session must keep following the agent bundle's declared harness.
    """
    agent = await create_test_agent(client)
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "initial_items": []},
    )
    assert resp.status_code == 201, resp.text
    # The test bundle declares claude-sdk (see build_agent_bundle).
    assert resp.json().get("harness") == "claude-sdk"


async def test_create_harness_override_alias_canonicalizes(
    client: httpx.AsyncClient,
) -> None:
    """The ``openai-agents-sdk`` alias persists as canonical ``openai-agents``.

    A raw alias on the row would miss the runner's harness-module
    registry at dispatch (it keys on canonical names).
    """
    agent = await create_test_agent(client)
    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "initial_items": [],
            "harness_override": "openai-agents-sdk",
        },
    )
    assert resp.status_code == 201, resp.text
    assert resp.json().get("harness") == "openai-agents"


async def test_create_rejects_unknown_harness_override(
    client: httpx.AsyncClient,
) -> None:
    """An unknown harness fails loud at create — no orphan session row."""
    agent = await create_test_agent(client)
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "initial_items": [], "harness_override": "bogus"},
    )
    assert resp.status_code == 400, (
        f"Unknown harness_override should 400, got {resp.status_code}: {resp.text}"
    )
    assert "bogus" in resp.text


async def test_create_rejects_harness_override_for_non_omnigent_agent(
    client: httpx.AsyncClient,
) -> None:
    """Non-omnigent executor types reject the override instead of no-opping.

    Mirrors the CLI's ``--harness`` rule: those executors have no
    ``config.harness``, so accepting the value would silently launch the
    spec's own executor while the snapshot claimed otherwise.
    """
    agent = await create_test_agent(
        client,
        name="sdk-typed-agent",
        # A non-omnigent executor type (claude_sdk rejects the helper's
        # executor.connection, so agents_sdk is the representative here);
        # the helper still injects config.harness, which this executor
        # type simply ignores.
        executor={"type": "agents_sdk", "model": "databricks-gpt-5-4"},
    )
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "initial_items": [], "harness_override": "pi"},
    )
    assert resp.status_code == 400, (
        f"harness_override on an agents_sdk-typed agent should 400, got "
        f"{resp.status_code}: {resp.text}"
    )
    assert "agents_sdk" in resp.text


async def test_runner_first_event_forwards_harness_override(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A create-time override reaches the runner body on the first event.

    The runner reads ``harness_override`` from the message body in
    ``_resolve_harness_config`` / the TurnDispatch builder — if the
    server drops it here, the session silently runs the spec's declared
    harness while the snapshot claims the override.
    """
    captured = _stub_runner_client(monkeypatch)

    agent = await create_test_agent(client)
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "initial_items": [], "harness_override": "pi"},
    )
    assert resp.status_code == 201, resp.text
    sid = resp.json()["id"]

    event = await client.post(
        f"/v1/sessions/{sid}/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "first turn"}],
            },
        },
    )
    assert event.status_code == 202, event.text

    assert captured.get("body") is not None, (
        "Runner client was never POSTed to — _forward_event_to_runner "
        "did not run. Check the runner-stub wiring."
    )
    assert captured["body"].get("harness_override") == "pi", (
        f"First-event runner body missing the create-time harness "
        f"override; got {captured['body'].get('harness_override')!r}. The "
        f"create route did not persist harness_override before the first "
        f"turn, or the forwarding site dropped it."
    )


async def test_runner_body_omits_harness_override_when_unset(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No override → the runner body carries no ``harness_override`` key.

    The runner treats a present key as an explicit override; sending the
    spec default redundantly would defeat NULL-means-track-the-spec when
    bundles update between turns.
    """
    captured = _stub_runner_client(monkeypatch)

    agent = await create_test_agent(client)
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "initial_items": []},
    )
    assert resp.status_code == 201, resp.text
    sid = resp.json()["id"]

    event = await client.post(
        f"/v1/sessions/{sid}/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "hi"}],
            },
        },
    )
    assert event.status_code == 202, event.text
    assert captured.get("body") is not None
    assert "harness_override" not in captured["body"]
