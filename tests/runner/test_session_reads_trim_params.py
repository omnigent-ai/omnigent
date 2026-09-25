"""Runner-owned session reads ask the server for a trimmed snapshot.

These readers need stored-row fields only, so each ``GET /v1/sessions/{id}``
must pass all four trim flags. A bare read makes the server build the full
page — transcript items, liveness, usage, and on a status-cache miss a
live-status probe of the very runner performing the read while it waits
on the answer.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.runner.native.orchestration import (
    _SESSION_METADATA_PARAMS,
    _claude_native_session_wants_rebuild,
    _fetch_native_launch_snapshot,
    _load_legacy_claude_launch_metadata,
    _session_payload_for_host_spawn_check,
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


class _RecordingClient:
    """Async client stub that records each GET's url and query params."""

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
    ],
)
@pytest.mark.asyncio
async def test_runner_owned_session_reads_pass_trim_params(
    read: Callable[[Any], Awaitable[Any]],
) -> None:
    """Every runner-owned session read carries the four trim flags."""
    client = _RecordingClient()

    await read(client)

    assert client.get_calls, "the reader never issued its session GET"
    for url, params in client.get_calls:
        assert url == f"/v1/sessions/{_SESSION_ID}"
        assert params == _EXPECTED_TRIM_PARAMS, (
            f"session read {url} went out without the snapshot-trimming params: {params}"
        )


def test_session_metadata_params_includes_live_status() -> None:
    """``_SESSION_METADATA_PARAMS`` must include ``include_live_status`` false.

    The runner is the caller; probing it for its own status only delays
    the answer it is waiting on.
    """
    assert _SESSION_METADATA_PARAMS.get("include_live_status") == "false", (
        "_SESSION_METADATA_PARAMS is missing include_live_status=false; "
        "runner-owned reads will trigger a circular live-status probe"
    )
