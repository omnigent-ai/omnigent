"""Opening a session must not cost a database query per sub-agent below it.

A session snapshot replays the approval prompts its sub-agents are waiting on,
so a parent chat opened later can still answer them. While any prompt is
pending, finding those prompts must not scale with the size of the opened
session's tree: a root that fans out into many sub-agents is opened and polled
repeatedly, and a per-sub-agent cost there multiplies into server-wide load.

Everything runs through a real server process. The root and its nested
sub-agents are created over the sessions API, a real Claude Code
``PermissionRequest`` hook call parks a prompt on one grandchild until the test
answers it, and the root is opened with the query the web chat sends. The
server's own OpenTelemetry export reports the database work of each open: with
``OTEL_EXPORTER_OTLP_ENDPOINT`` set, every SQL statement and connection becomes
a span in the request's trace.
"""

from __future__ import annotations

import secrets
import threading
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

import httpx

from tests._helpers.live_server import isolated_local_server
from tests._helpers.native_session import create_native_session
from tests._helpers.otlp import OtlpTraceCollector
from tests._helpers.polling import poll_until

# Query string the web chat sends when it opens a session.
SNAPSHOT_QUERY = (
    "include_items=false&include_liveness=false&include_usage=false&refresh_state=true"
)
SNAPSHOT_ROUTE_SPAN = "GET /v1/sessions/{session_id}"
CHILDREN = 3
GRANDCHILDREN_PER_CHILD_BEFORE = 3
GRANDCHILDREN_PER_CHILD_AFTER = 12


def _telemetry_env(collector: OtlpTraceCollector) -> dict[str, str]:
    return {
        "OMNIGENT_TELEMETRY_ENABLED": "true",
        "OTEL_EXPORTER_OTLP_ENDPOINT": collector.endpoint,
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_METRICS_EXPORTER": "none",
        "OTEL_LOGS_EXPORTER": "none",
        # Export each request's spans promptly instead of on the default 5s timer.
        "OTEL_BSP_SCHEDULE_DELAY": "200",
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }


def _create_sub_agents(
    client: httpx.Client, *, agent_id: str, parent_id: str, count: int
) -> list[str]:
    session_ids = []
    for _ in range(count):
        response = client.post(
            "/v1/sessions", json={"agent_id": agent_id, "parent_session_id": parent_id}
        )
        assert response.status_code == 201, response.text
        session_ids.append(response.json()["id"])
    return session_ids


@contextmanager
def _parked_permission_request(base_url: str, session_id: str) -> Iterator[dict[str, Any]]:
    """
    Hold a Claude Code ``PermissionRequest`` hook call open on ``session_id``.

    The hook long-polls until the prompt is answered, so it runs on its own
    thread and client. The yielded dict receives the hook's HTTP response, or
    the error its call raised. A prompt still parked on exit, because the test
    failed before answering it, is cancelled so the call returns promptly.
    """
    outcome: dict[str, Any] = {}

    def call_hook() -> None:
        try:
            with httpx.Client(base_url=base_url, trust_env=False, timeout=300) as hook_client:
                outcome["response"] = hook_client.post(
                    f"/v1/sessions/{session_id}/hooks/permission-request",
                    json={"tool_name": "Bash", "tool_input": {"command": "make deploy"}},
                )
        except httpx.HTTPError as error:
            outcome["error"] = error

    thread = threading.Thread(target=call_hook, daemon=True)
    thread.start()
    try:
        yield outcome
    finally:
        if thread.is_alive():
            _cancel_pending_prompts(base_url, session_id)
        thread.join(timeout=30)


def _cancel_pending_prompts(base_url: str, session_id: str) -> None:
    """Best-effort cleanup: cancel ``session_id``'s own pending prompts."""
    with (
        suppress(httpx.HTTPError, ValueError),
        httpx.Client(base_url=base_url, trust_env=False, timeout=10) as client,
    ):
        snapshot = client.get(f"/v1/sessions/{session_id}").json()
        for prompt in snapshot.get("pending_elicitations") or []:
            client.post(
                f"/v1/sessions/{session_id}/elicitations/{prompt['elicitation_id']}/resolve",
                json={"action": "cancel"},
            )


def _replayed_prompts(snapshot: dict[str, Any], session_id: str) -> list[dict[str, Any]]:
    return [
        prompt
        for prompt in snapshot.get("pending_elicitations") or []
        if prompt.get("params", {}).get("target_session_id") == session_id
    ]


def _open_session(
    client: httpx.Client, collector: OtlpTraceCollector, session_id: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Open ``session_id`` as the web chat does; return the snapshot and its database spans."""
    trace_id = secrets.token_hex(16)
    response = client.get(
        f"/v1/sessions/{session_id}?{SNAPSHOT_QUERY}",
        headers={"traceparent": f"00-{trace_id}-{secrets.token_hex(8)}-01"},
    )
    assert response.status_code == 200, response.text
    # The route span ends last, so once it is exported the statements it waited on are too.
    poll_until(
        lambda: any(span["name"] == SNAPSHOT_ROUTE_SPAN for span in collector.snapshot(trace_id)),
        timeout_s=30,
        interval_s=0.2,
    )
    database_spans = [
        span for span in collector.snapshot(trace_id) if "db.system" in span["attributes"]
    ]
    return response.json(), database_spans


def _steady_open(
    client: httpx.Client, collector: OtlpTraceCollector, session_id: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    # The first open after the tree changes can fill caches; measure the next one.
    _open_session(client, collector, session_id)
    return _open_session(client, collector, session_id)


def test_open_cost_with_a_pending_sub_agent_prompt_does_not_grow_with_the_tree(
    tmp_path: Path,
) -> None:
    with (
        OtlpTraceCollector() as collector,
        isolated_local_server(tmp_path / "server", env=_telemetry_env(collector)) as base_url,
        httpx.Client(base_url=base_url, trust_env=False, timeout=60) as client,
    ):
        root = create_native_session(
            client, base_url, harness="claude", metadata={"title": "fan-out root"}
        )
        root_id = root["session_id"]
        agent_id = root["agent_id"]
        children = _create_sub_agents(client, agent_id=agent_id, parent_id=root_id, count=CHILDREN)
        grandchildren = {
            child: _create_sub_agents(
                client, agent_id=agent_id, parent_id=child, count=GRANDCHILDREN_PER_CHILD_BEFORE
            )
            for child in children
        }
        waiting = grandchildren[children[0]][0]

        with _parked_permission_request(base_url, waiting) as hook:
            parked: list[dict[str, Any]] = []

            def prompt_reaches_root() -> bool:
                snapshot = client.get(f"/v1/sessions/{root_id}?{SNAPSHOT_QUERY}").json()
                parked[:] = _replayed_prompts(snapshot, waiting)
                return bool(parked)

            poll_until(prompt_reaches_root, timeout_s=30, interval_s=0.2)
            assert parked[0]["params"]["content_preview"] == 'Bash({"command": "make deploy"})'

            small_snapshot, small_tree = _steady_open(client, collector, root_id)
            for child in children:
                _create_sub_agents(
                    client,
                    agent_id=agent_id,
                    parent_id=child,
                    count=GRANDCHILDREN_PER_CHILD_AFTER - GRANDCHILDREN_PER_CHILD_BEFORE,
                )
            large_snapshot, large_tree = _steady_open(client, collector, root_id)

            for snapshot in (small_snapshot, large_snapshot):
                assert [
                    prompt["elicitation_id"] for prompt in _replayed_prompts(snapshot, waiting)
                ] == [parked[0]["elicitation_id"]]
            resolve = client.post(
                f"/v1/sessions/{waiting}/elicitations/{parked[0]['elicitation_id']}/resolve",
                json={"action": "accept"},
            )
            assert resolve.status_code == 202, resolve.text

        assert "error" not in hook, f"the parked hook call failed: {hook.get('error')!r}"
        assert "response" in hook, (
            "the parked hook call did not return after its prompt was answered"
        )
        assert hook["response"].status_code == 200, hook["response"].text
        assert hook["response"].json()["hookSpecificOutput"]["decision"] == {"behavior": "allow"}

    small_descendants = CHILDREN * (1 + GRANDCHILDREN_PER_CHILD_BEFORE)
    large_descendants = CHILDREN * (1 + GRANDCHILDREN_PER_CHILD_AFTER)
    assert small_tree, "the server exported no database spans for the open; is tracing installed?"
    assert len(large_tree) == len(small_tree), (
        f"opening the root ran {len(small_tree)} database operations with {small_descendants} "
        f"sub-agents below it and {len(large_tree)} with {large_descendants}: "
        f"{dict(Counter(span['name'] for span in small_tree))} -> "
        f"{dict(Counter(span['name'] for span in large_tree))}"
    )
