"""Runner-owned session reads ask the server for a trimmed snapshot.

These readers need stored-row fields only, so each ``GET /v1/sessions/{id}``
must pass all four trim flags. A bare read makes the server build the full
page — transcript items, liveness, usage, and on a status-cache miss a
live-status probe of the very runner performing the read while it waits
on the answer. ``sys_session_get_info`` projects ``status`` and so keeps
only the live-status probe.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.runner import app as runner_app
from omnigent.runner import subagent_work, tool_dispatch
from omnigent.runner.identity import RUNNER_SLICE_KEY_ENV_VAR
from omnigent.runner.native.orchestration import (
    _SESSION_METADATA_PARAMS,
    _claude_native_session_wants_rebuild,
    _fetch_native_launch_snapshot,
    _load_legacy_claude_launch_metadata,
    _session_payload_for_host_spawn_check,
)
from omnigent.runner.subagent_routing import (
    SessionRoutingClass,
    forget_session_routing_class,
    remember_session_routing_class,
)

_SESSION_ID = "conv_1"

# Spelled out rather than importing the production constant: the wire contract
# is these exact pairs, and the test must keep failing if they change.
_EXPECTED_TRIM_PARAMS = {
    "include_items": "false",
    "include_liveness": "false",
    "include_usage": "false",
    "include_live_status": "false",
}


# Readers that make more than the single read of ``_SESSION_ID``, in order.
_MULTI_READ_CASES = {
    "spawn_turn_actor_and_parent_agent": [_SESSION_ID, _SESSION_ID],
    "close_target_and_caller": [_SESSION_ID, "conv_caller"],
}


class _RecordingClient:
    """Async client stub that records each GET's url and query params.

    Every GET answers an empty snapshot, so each reader takes its
    nothing-found branch after the read under test.
    """

    def __init__(self) -> None:
        self.get_calls: list[tuple[str, dict[str, str] | None]] = []

    async def get(
        self,
        url: str,
        params: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> Any:
        self.get_calls.append((url, params))
        return SimpleNamespace(status_code=200, json=dict)

    async def post(self, url: str, json: object = None, timeout: float | None = None) -> Any:
        return SimpleNamespace(status_code=201, json=lambda: {"id": "conv_new"}, text="")


@pytest.fixture(autouse=True)
def _isolate_runner_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Route every reader to the server: no host identity, no cached ids."""
    monkeypatch.delenv(RUNNER_SLICE_KEY_ENV_VAR, raising=False)
    monkeypatch.setattr(runner_app, "get_session_agent_id", lambda _session_id: None)
    remember_session_routing_class(_SESSION_ID, SessionRoutingClass(routing_enabled=True))
    yield
    forget_session_routing_class(_SESSION_ID)
    tool_dispatch.forget_spawn_family(_SESSION_ID)
    subagent_work._session_inboxes_ref.pop(_SESSION_ID, None)


@pytest.mark.parametrize(
    "read",
    [
        pytest.param(
            lambda client: _fetch_native_launch_snapshot(
                server_client=client, session_id=_SESSION_ID, runtime_label="Codex"
            ),
            id="native_launch_snapshot",
        ),
        pytest.param(
            lambda client: _session_payload_for_host_spawn_check(client, _SESSION_ID),
            id="host_spawn_check",
        ),
        pytest.param(
            lambda client: _load_legacy_claude_launch_metadata(client, _SESSION_ID),
            id="legacy_claude_launch_metadata",
        ),
        pytest.param(
            lambda client: _claude_native_session_wants_rebuild(client, _SESSION_ID),
            id="claude_rebuild_check",
        ),
        pytest.param(
            lambda client: tool_dispatch.execute_tool(
                tool_name="sys_session_send",
                arguments='{"agent": "researcher", "args": "go"}',
                server_client=client,
                conversation_id=_SESSION_ID,
                agent_spec=SimpleNamespace(sub_agents=[SimpleNamespace(name="researcher")]),
                session_inbox=asyncio.Queue(),
            ),
            id="spawn_turn_actor_and_parent_agent",
        ),
        pytest.param(
            lambda client: tool_dispatch._inherited_parent_model(
                server_client=client,
                conversation_id=_SESSION_ID,
                sub_agent_name="researcher",
                agent_spec=None,
                child_harness="codex-native",
            ),
            id="spawn_inherited_parent_model",
        ),
        pytest.param(
            lambda client: tool_dispatch._spawn_family(client, _SESSION_ID),
            id="agent_list_spawn_family",
        ),
        pytest.param(
            lambda client: tool_dispatch._agent_list_host_readiness(client, _SESSION_ID),
            id="agent_list_host_readiness",
        ),
        pytest.param(
            lambda client: tool_dispatch._session_parent_id(_SESSION_ID, client),
            id="session_list_parent_id",
        ),
        pytest.param(
            lambda client: tool_dispatch._send_to_existing_session(
                _SESSION_ID, "go", server_client=client, conversation_id="conv_parent"
            ),
            id="send_to_existing_session",
        ),
        pytest.param(
            lambda client: tool_dispatch._fetch_peek_meta(_SESSION_ID, client),
            id="peek_meta",
        ),
        pytest.param(
            lambda client: tool_dispatch._session_close_via_rest(
                {"conversation_id": _SESSION_ID}, "conv_caller", client
            ),
            id="close_target_and_caller",
        ),
        pytest.param(
            lambda client: tool_dispatch._rename_current_session_via_rest(
                {"title": "Debug auth timeout"}, _SESSION_ID, client
            ),
            id="rename_top_level_check",
        ),
        pytest.param(
            lambda client: tool_dispatch._execute_rest_tool(
                "sys_call_async", {"prompt": "go"}, client, "ag_parent", _SESSION_ID
            ),
            id="call_async_parent_runner",
        ),
        pytest.param(
            lambda client: tool_dispatch._cancel_evicted_native_subagent(
                _SESSION_ID, conversation_id="conv_parent", server_client=client
            ),
            id="cancel_evicted_native_subagent",
        ),
    ],
)
@pytest.mark.asyncio
async def test_runner_owned_session_reads_pass_trim_params(
    read: Callable[[Any], Awaitable[Any]],
    request: pytest.FixtureRequest,
) -> None:
    """Every runner-owned session read carries the four trim flags."""
    client = _RecordingClient()

    await read(client)

    expected_ids = _MULTI_READ_CASES.get(request.node.callspec.id, [_SESSION_ID])
    assert [url for url, _ in client.get_calls] == [
        f"/v1/sessions/{session_id}" for session_id in expected_ids
    ]
    for url, params in client.get_calls:
        assert params == _EXPECTED_TRIM_PARAMS, (
            f"session read {url} went out without the snapshot-trimming params: {params}"
        )


@pytest.mark.asyncio
async def test_session_get_info_keeps_only_the_live_status_probe() -> None:
    """``sys_session_get_info`` reports ``status``, so only that probe stays on."""
    client = _RecordingClient()

    await tool_dispatch._session_get_info_via_rest({}, _SESSION_ID, client)

    assert client.get_calls == [
        (f"/v1/sessions/{_SESSION_ID}", {**_EXPECTED_TRIM_PARAMS, "include_live_status": "true"})
    ]


def test_session_metadata_params_includes_live_status() -> None:
    """``_SESSION_METADATA_PARAMS`` must include ``include_live_status`` false.

    The runner is the caller; probing it for its own status only delays
    the answer it is waiting on.
    """
    assert _SESSION_METADATA_PARAMS.get("include_live_status") == "false", (
        "_SESSION_METADATA_PARAMS is missing include_live_status=false; "
        "runner-owned reads will trigger a circular live-status probe"
    )
