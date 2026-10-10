"""Restart cancellation is session-bound and never replays a command."""

from __future__ import annotations

import json

import httpx
import pytest

from omnigent.host.restart_approvals import RESTART_NOTICE, retire_restart_approvals


def _session(session_id: str, **overrides: object) -> dict[str, object]:
    return {
        "id": session_id,
        "host_id": "our-host",
        "runner_id": "dead-runner",
        "agent_name": "codex-native-ui",
        "external_session_id": "native-id",
        "created_at": 1,
        "pending_elicitations_count": 1,
        **overrides,
    }


@pytest.mark.parametrize("agent_name", ["codex-native-ui", "claude-native-ui"])
async def test_cancel_all_old_approvals_before_one_native_notice(agent_name: str) -> None:
    calls: list[tuple[str, str, object]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        calls.append((request.method, request.url.path, body))
        if request.url.path == "/v1/sessions":
            return httpx.Response(200, json={"data": [_session("session", agent_name=agent_name)]})
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    **_session("session"),
                    "pending_elicitations": [
                        {"elicitation_id": "one"},
                        {"elicitation_id": "two"},
                        {"elicitation_id": "child", "params": {"target_session_id": "child"}},
                    ],
                },
            )
        return httpx.Response(202, json={"queued": False})

    async with httpx.AsyncClient(
        base_url="https://test", transport=httpx.MockTransport(handle)
    ) as c:
        assert (
            await retire_restart_approvals(
                c, host_id="our-host", started_at=10, runner_is_live=lambda _: False
            )
            == 1
        )
    posts = [(path, body) for method, path, body in calls if method == "POST"]
    assert posts == [
        ("/v1/sessions/session/elicitations/one/resolve", {"action": "cancel"}),
        ("/v1/sessions/session/elicitations/two/resolve", {"action": "cancel"}),
        (
            "/v1/sessions/session/events",
            {
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": RESTART_NOTICE}],
                },
            },
        ),
    ]


async def test_ignore_other_hosts_new_sessions_and_surviving_runners() -> None:
    calls: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.method == "GET"
        assert request.url.path == "/v1/sessions"
        return httpx.Response(
            200,
            json={
                "data": [
                    _session("other", host_id="other-host"),
                    _session("new", created_at=10),
                    _session("live", runner_id="live-runner"),
                    _session("idle", pending_elicitations_count=0),
                    _session("unnamed", external_session_id=None),
                ]
            },
        )

    async with httpx.AsyncClient(
        base_url="https://test", transport=httpx.MockTransport(handle)
    ) as c:
        assert (
            await retire_restart_approvals(
                c, host_id="our-host", started_at=10, runner_is_live=lambda r: r == "live-runner"
            )
            == 0
        )
    assert calls == ["/v1/sessions"]


async def test_failed_cancellation_never_sends_notice_or_retries_command() -> None:
    posts: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append(request.url.path)
            return httpx.Response(503)
        if request.url.path == "/v1/sessions":
            return httpx.Response(200, json={"data": [_session("session")]})
        return httpx.Response(
            200, json={**_session("session"), "pending_elicitations": [{"elicitation_id": "one"}]}
        )

    async with httpx.AsyncClient(
        base_url="https://test", transport=httpx.MockTransport(handle)
    ) as c:
        assert (
            await retire_restart_approvals(
                c, host_id="our-host", started_at=10, runner_is_live=lambda _: False
            )
            == 0
        )
    assert posts == ["/v1/sessions/session/elicitations/one/resolve"]


async def test_paginate_without_touching_other_host_sessions() -> None:
    pages: list[str | None] = []

    def handle(request: httpx.Request) -> httpx.Response:
        pages.append(request.url.params.get("after"))
        if len(pages) == 1:
            return httpx.Response(200, json={"data": [], "has_more": True, "last_id": "cursor"})
        return httpx.Response(200, json={"data": []})

    async with httpx.AsyncClient(
        base_url="https://test", transport=httpx.MockTransport(handle)
    ) as c:
        await retire_restart_approvals(
            c, host_id="our-host", started_at=10, runner_is_live=lambda _: False
        )
    assert pages == [None, "cursor"]


async def test_ambiguous_notice_failure_is_not_retried() -> None:
    posts: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append(request.url.path)
            if request.url.path.endswith("/events"):
                raise httpx.ReadTimeout("reply lost", request=request)
            return httpx.Response(202)
        if request.url.path == "/v1/sessions":
            return httpx.Response(200, json={"data": [_session("session")]})
        return httpx.Response(
            200, json={**_session("session"), "pending_elicitations": [{"elicitation_id": "one"}]}
        )

    async with httpx.AsyncClient(
        base_url="https://test", transport=httpx.MockTransport(handle)
    ) as c:
        assert (
            await retire_restart_approvals(
                c, host_id="our-host", started_at=10, runner_is_live=lambda _: False
            )
            == 0
        )
    assert posts == [
        "/v1/sessions/session/elicitations/one/resolve",
        "/v1/sessions/session/events",
    ]
