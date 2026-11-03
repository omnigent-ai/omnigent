"""Approvals tests for Codex session."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.harnesses.codex_native import forwarder as codex_native_forwarder
from tests.harnesses.codex_native.session._support import (
    _elicitation_tracker,
    _FakeCodexAppServerClient,
    _forwarder_context,
    _usage_coalescer,
)


def test_forwarder_sends_codex_command_approval_response_to_app_server(
    tmp_path: Path,
) -> None:
    """
    Codex command-approval request frames use the Omnigent hook path and
    relay its decision result back to app-server.
    """
    fake_client = _FakeCodexAppServerClient()
    codex_event = {
        "id": 14,
        "method": "item/commandExecution/requestApproval",
        "params": {
            "threadId": "thread_123",
            "turnId": "turn_123",
            "itemId": "item_cmd",
            "startedAtMs": 1,
            "command": "date",
            "cwd": "/tmp/workspace",
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Return a command approval result from the Omnigent hook.

        :param request: HTTP request sent by the forwarder.
        :returns: Omnigent hook response.
        """
        assert request.url.path == "/v1/sessions/conv_123/hooks/codex-elicitation-request"
        assert json.loads(request.content) == codex_event
        return httpx.Response(200, json={"decision": "accept"})

    async def run() -> None:
        """
        Drive one command approval frame through the forwarder.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            elicitation_tracker = _elicitation_tracker()
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=_usage_coalescer(client),
                elicitation_tracker=elicitation_tracker,
                event=codex_event,
                codex_client=fake_client,  # type: ignore[arg-type]
            )
            await elicitation_tracker.drain()

    asyncio.run(run())

    assert fake_client.responses == [(14, {"decision": "accept"})]


def test_forwarder_declines_codex_command_when_approval_hook_rejects_request(
    tmp_path: Path,
) -> None:
    """A rejected Omnigent hook must not leave Codex waiting forever."""
    fake_client = _FakeCodexAppServerClient()
    codex_event = {
        "id": 14,
        "method": "item/commandExecution/requestApproval",
        "params": {
            "threadId": "thread_123",
            "turnId": "turn_123",
            "itemId": "item_cmd",
            "command": "date",
            "availableDecisions": [
                {
                    "acceptWithExecpolicyAmendment": {
                        "execpolicy_amendment": [],
                    }
                }
            ],
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sessions/conv_123/hooks/codex-elicitation-request"
        assert json.loads(request.content) == codex_event
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": "invalid_input",
                    "message": ("Codex execpolicy amendment must be a non-empty list of strings."),
                }
            },
        )

    async def run() -> None:
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            elicitation_tracker = _elicitation_tracker()
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=_usage_coalescer(client),
                elicitation_tracker=elicitation_tracker,
                event=codex_event,
                codex_client=fake_client,  # type: ignore[arg-type]
            )
            await elicitation_tracker.drain()

    asyncio.run(run())

    assert fake_client.responses == [(14, {"decision": "decline"})]


def test_forwarder_routes_unregistered_child_command_approval_to_parent(
    tmp_path: Path,
) -> None:
    """
    Command approvals from an unregistered child thread are not dropped.

    Child thread registration can race behind server-to-client request
    frames. For ordinary transcript events an unknown non-parent
    ``threadId`` is stale and should be ignored, but an approval request
    must remain actionable. Until the child AP session is known, the
    forwarder posts the hook to the parent session so Nessie can answer it.
    """
    fake_client = _FakeCodexAppServerClient()
    posted: list[tuple[str, dict[str, Any]]] = []
    state = codex_native_forwarder._CodexForwarderState(
        parent_session_id="conv_parent",
    )
    child_started_event = {
        "method": "thread/started",
        "params": {
            "thread": {
                "id": "thread_child_unregistered",
                "source": {"subAgent": {"thread_spawn": {"parent_thread_id": "thread_parent"}}},
            }
        },
    }
    codex_event = {
        "id": 15,
        "method": "item/commandExecution/requestApproval",
        "params": {
            "threadId": "thread_child_unregistered",
            "turnId": "turn_child",
            "itemId": "item_cmd",
            "command": "date",
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Capture the AP hook request and accept the command.

        :param request: HTTP request sent by the forwarder.
        :returns: AP hook response.
        """
        posted.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"decision": "accept"})

    async def run() -> None:
        """
        Drive one unregistered child-thread approval through the forwarder.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            elicitation_tracker = _elicitation_tracker()
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_parent",
                bridge_dir=tmp_path,
                usage_coalescer=_usage_coalescer(client, "conv_parent"),
                elicitation_tracker=elicitation_tracker,
                event=child_started_event,
                expected_thread_id="thread_parent",
                codex_client=fake_client,  # type: ignore[arg-type]
                forwarder_state=state,
            )
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_parent",
                bridge_dir=tmp_path,
                usage_coalescer=_usage_coalescer(client, "conv_parent"),
                elicitation_tracker=elicitation_tracker,
                event=codex_event,
                expected_thread_id="thread_parent",
                codex_client=fake_client,  # type: ignore[arg-type]
                forwarder_state=state,
            )
            await elicitation_tracker.drain()

    asyncio.run(run())

    assert posted == [
        (
            "/v1/sessions/conv_parent/hooks/codex-elicitation-request",
            codex_event,
        )
    ]
    assert fake_client.responses == [(15, {"decision": "accept"})]


def test_forwarder_drops_unknown_thread_command_approval(
    tmp_path: Path,
) -> None:
    """
    Command approvals from unproven non-parent threads stay stale-dropped.

    The child-registration race exemption only applies after Codex has
    announced a child via ``thread/started`` metadata. A random stale
    thread id must not surface an approval card in the current parent
    session.

    :param tmp_path: Pytest temporary directory.
    """
    fake_client = _FakeCodexAppServerClient()
    posted: list[tuple[str, dict[str, Any]]] = []
    state = codex_native_forwarder._CodexForwarderState(
        parent_session_id="conv_parent",
    )
    codex_event = {
        "id": 16,
        "method": "item/commandExecution/requestApproval",
        "params": {
            "threadId": "thread_stale_unknown",
            "turnId": "turn_stale",
            "itemId": "item_cmd",
            "command": "date",
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Record any unexpected AP hook request.

        :param request: HTTP request sent by the forwarder.
        :returns: AP hook response.
        """
        posted.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"decision": "accept"})

    async def run() -> None:
        """
        Drive one stale-thread approval through the forwarder.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            elicitation_tracker = _elicitation_tracker()
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_parent",
                bridge_dir=tmp_path,
                usage_coalescer=_usage_coalescer(client, "conv_parent"),
                elicitation_tracker=elicitation_tracker,
                event=codex_event,
                expected_thread_id="thread_parent",
                codex_client=fake_client,  # type: ignore[arg-type]
                forwarder_state=state,
            )
            await elicitation_tracker.drain()

    asyncio.run(run())

    assert posted == []
    assert fake_client.responses == []


def test_forwarder_drops_old_parent_child_command_approval(
    tmp_path: Path,
) -> None:
    """
    Announced child approvals must still match the active parent thread.

    A pending child marker from an old parent thread should not make a
    later stale approval actionable after the parent Codex thread has
    rotated.

    :param tmp_path: Pytest temporary directory.
    """
    fake_client = _FakeCodexAppServerClient()
    posted: list[tuple[str, dict[str, Any]]] = []
    state = codex_native_forwarder._CodexForwarderState(
        parent_session_id="conv_parent",
    )
    child_started_event = {
        "method": "thread/started",
        "params": {
            "thread": {
                "id": "thread_child_old_parent",
                "source": {"subAgent": {"thread_spawn": {"parent_thread_id": "thread_old"}}},
            }
        },
    }
    codex_event = {
        "id": 17,
        "method": "item/commandExecution/requestApproval",
        "params": {
            "threadId": "thread_child_old_parent",
            "turnId": "turn_child",
            "itemId": "item_cmd",
            "command": "date",
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Record any unexpected AP hook request.

        :param request: HTTP request sent by the forwarder.
        :returns: AP hook response.
        """
        posted.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"decision": "accept"})

    async def run() -> None:
        """
        Drive one old-parent child approval through the forwarder.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            elicitation_tracker = _elicitation_tracker()
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_parent",
                bridge_dir=tmp_path,
                usage_coalescer=_usage_coalescer(client, "conv_parent"),
                elicitation_tracker=elicitation_tracker,
                event=child_started_event,
                expected_thread_id="thread_old",
                codex_client=fake_client,  # type: ignore[arg-type]
                forwarder_state=state,
            )
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_parent",
                bridge_dir=tmp_path,
                usage_coalescer=_usage_coalescer(client, "conv_parent"),
                elicitation_tracker=elicitation_tracker,
                event=codex_event,
                expected_thread_id="thread_new",
                codex_client=fake_client,  # type: ignore[arg-type]
                forwarder_state=state,
            )
            await elicitation_tracker.drain()

    asyncio.run(run())

    assert posted == []
    assert fake_client.responses == []


def test_forwarder_sends_codex_permissions_response_to_app_server(
    tmp_path: Path,
) -> None:
    """
    Codex permission-profile request frames are relayed through Omnigent and
    answered with the hook's permission-grant result.
    """
    fake_client = _FakeCodexAppServerClient()
    codex_event = {
        "id": 15,
        "method": "item/permissions/requestApproval",
        "params": {
            "threadId": "thread_123",
            "turnId": "turn_123",
            "itemId": "item_permissions",
            "startedAtMs": 1,
            "cwd": "/tmp/workspace",
            "reason": "need network",
            "permissions": {"network": {"enabled": True}, "fileSystem": None},
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Return a permissions approval result from the Omnigent hook.

        :param request: HTTP request sent by the forwarder.
        :returns: Omnigent hook response.
        """
        assert request.url.path == "/v1/sessions/conv_123/hooks/codex-elicitation-request"
        assert json.loads(request.content) == codex_event
        return httpx.Response(
            200,
            json={"permissions": {"network": {"enabled": True}}, "scope": "turn"},
        )

    async def run() -> None:
        """
        Drive one permissions request frame through the forwarder.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            elicitation_tracker = _elicitation_tracker()
            await codex_native_forwarder._handle_event(
                client,
                session_id="conv_123",
                bridge_dir=tmp_path,
                usage_coalescer=_usage_coalescer(client),
                elicitation_tracker=elicitation_tracker,
                event=codex_event,
                codex_client=fake_client,  # type: ignore[arg-type]
            )
            await elicitation_tracker.drain()

    asyncio.run(run())

    assert fake_client.responses == [
        (15, {"permissions": {"network": {"enabled": True}}, "scope": "turn"})
    ]


def test_forwarder_logs_unsupported_codex_server_request(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Unsupported Codex server requests are visible in forwarder logs.

    Without this diagnostic, a new app-server request method can be
    delivered on the observer connection and disappear with no clue
    about which protocol adapter is missing.
    """
    fake_client = _FakeCodexAppServerClient()

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Fail if the unsupported request is accidentally posted to AP.

        :param request: HTTP request sent by the forwarder.
        :returns: Never returns.
        """
        raise AssertionError(f"unexpected Omnigent request: {request.method} {request.url}")

    async def run() -> None:
        """
        Drive one unsupported server request through the forwarder.

        :returns: None.
        """
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            await codex_native_forwarder._handle_event(
                client,
                **_forwarder_context(client, tmp_path),
                event={
                    "id": "unsupported_1",
                    "method": "item/tool/call",
                    "params": {"threadId": "thread_123"},
                },
                codex_client=fake_client,  # type: ignore[arg-type]
            )

    asyncio.run(run())

    assert fake_client.responses == []
    assert (
        "Codex forwarder ignored unsupported server request: method=item/tool/call" in caplog.text
    )
