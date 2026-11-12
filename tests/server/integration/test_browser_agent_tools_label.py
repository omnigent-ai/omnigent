"""The advanced browser tools switch reaches the session and its turns."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from omnigent.tools.browser_agent_tools import (
    BROWSER_AGENT_TOOLS_HEADER,
    BROWSER_AGENT_TOOLS_LABEL,
)
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


def _route_to_runner(monkeypatch: pytest.MonkeyPatch, runner: httpx.AsyncClient) -> None:
    async def resolve(session_id: str, runner_router: object, *, conversation: Any = None):
        return runner

    monkeypatch.setattr("omnigent.server.routes.sessions._get_runner_client", resolve)


@pytest.mark.parametrize(
    ("header", "label", "enabled", "trusted"),
    [
        ("on", "on", True, True),
        ("trusted", "trusted", False, True),
        ("off", None, False, False),
        (None, None, False, False),
    ],
)
async def test_switch_header_labels_the_session_and_flags_each_turn(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    header: str | None,
    label: str | None,
    enabled: bool,
    trusted: bool,
) -> None:
    forwarded: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/events"):
            forwarded.append(json.loads(request.content))
        return httpx.Response(202, json={"queued": True})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_handler), base_url="http://runner"
    ) as fake_runner:
        _route_to_runner(monkeypatch, fake_runner)
        agent = await create_test_agent(client, name="browser-agent")
        headers = {} if header is None else {BROWSER_AGENT_TOOLS_HEADER: header}
        created = await client.post(
            "/v1/sessions", json={"agent_id": agent["id"]}, headers=headers
        )
        assert created.status_code == 201, created.text
        session_id = created.json()["id"]

        labels = (await client.get(f"/v1/sessions/{session_id}/labels")).json()
        labels = labels.get("labels", labels)
        assert labels.get(BROWSER_AGENT_TOOLS_LABEL) == label

        # Later requests don't change it: the switch applies to new sessions.
        resp = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "message",
                "data": {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            },
            headers={BROWSER_AGENT_TOOLS_HEADER: "off" if enabled else "on"},
        )
        assert resp.status_code == 202, resp.text

    turns = [body for body in forwarded if body.get("type") == "message"]
    assert turns, forwarded
    assert all(turn["browser_agent_tools"] is enabled for turn in turns)
    assert all(turn["browser_trusted_input"] is trusted for turn in turns)


@pytest.mark.parametrize("side_chat", [False, True], ids=["clone", "side-chat"])
@pytest.mark.parametrize(
    ("source", "forking", "label"),
    [
        (None, "on", "on"),  # switch turned on after the source started
        ("on", "trusted", "trusted"),  # switch turned off since
        ("trusted", None, None),  # forked from an app without trusted input
        ("on", None, None),
    ],
)
async def test_a_fork_takes_the_forking_apps_mode_not_its_sources(
    client: httpx.AsyncClient,
    source: str | None,
    forking: str | None,
    label: str | None,
    side_chat: bool,
) -> None:
    agent = await create_test_agent(client, name="browser-agent")
    created = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"]},
        headers={} if source is None else {BROWSER_AGENT_TOOLS_HEADER: source},
    )
    assert created.status_code == 201, created.text
    forked = await client.post(
        f"/v1/sessions/{created.json()['id']}/fork",
        json={"side_chat": True} if side_chat else {},
        headers={} if forking is None else {BROWSER_AGENT_TOOLS_HEADER: forking},
    )
    assert forked.status_code == 201, forked.text
    labels = (await client.get(f"/v1/sessions/{forked.json()['id']}/labels")).json()
    labels = labels.get("labels", labels)
    assert labels.get(BROWSER_AGENT_TOOLS_LABEL) == label


async def test_the_label_is_set_at_creation_and_never_changed_by_clients(
    client: httpx.AsyncClient,
) -> None:
    # A create takes the app's header, or keeps a mode a continuation copied
    # from its predecessor (a native /clear replacement); it never changes
    # afterwards, so an open window's cached "this session has them" stays true.
    agent = await create_test_agent(client, name="browser-agent")

    async def created_label(labels: dict[str, str], headers: dict[str, str]) -> str | None:
        created = await client.post(
            "/v1/sessions", json={"agent_id": agent["id"], "labels": labels}, headers=headers
        )
        assert created.status_code == 201, created.text
        got = (await client.get(f"/v1/sessions/{created.json()['id']}/labels")).json()
        return got.get("labels", got).get(BROWSER_AGENT_TOOLS_LABEL)

    assert await created_label({BROWSER_AGENT_TOOLS_LABEL: "trusted"}, {}) == "trusted"
    assert await created_label({BROWSER_AGENT_TOOLS_LABEL: "bogus"}, {}) is None
    header = {BROWSER_AGENT_TOOLS_HEADER: "on"}
    assert await created_label({BROWSER_AGENT_TOOLS_LABEL: "trusted"}, header) == "on"

    created = await client.post("/v1/sessions", json={"agent_id": agent["id"]}, headers=header)
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    for mode in ("trusted", "on"):
        changed = await client.patch(
            f"/v1/sessions/{session_id}", json={"labels": {BROWSER_AGENT_TOOLS_LABEL: mode}}
        )
        assert changed.status_code == 400, changed.text
    labels = (await client.get(f"/v1/sessions/{session_id}/labels")).json()
    assert labels.get("labels", labels).get(BROWSER_AGENT_TOOLS_LABEL) == "on"
