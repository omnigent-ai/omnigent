"""E2E regression: a 100-item sub-agent batch must complete within the
forwarder timeout against a store with per-append overhead.

Bug
---
POST /v1/sessions/{id}/events with a JSON-array body ran every entry through
_post_event_impl serially: one access-check and one conversation_store.append
per item. On a store where each append incurs a network or encryption round
trip 100 serial appends exceed the forwarder's 10 s client timeout, causing
ReadTimeout, retry storms, and eventual batch drop with
"sub-agent transcript incomplete: an item could not be delivered".

Mechanism
---------
The session-events route persists each run of consecutive non-user
external_conversation_item entries with ONE conversation_store.append call, so
per-append overhead is paid once per run regardless of item count.

What this test drives
---------------------
A real omnigent server whose SqlAlchemyConversationStore.append is patched to
sleep 150 ms per call (emulating one remote-store round trip), a real
claude-native parent session, a real sub-agent child session created via
external_subagent_start, and the real _post_external_conversation_items
client function configured with the forwarder's 10 s POST timeout.

Appended one entry at a time, 100 items x 150 ms/append = 15 s > the 10 s
timeout; appended as one run, the batch costs a single 150 ms append.

Assertions: no ReadTimeout, all 100 items land exactly once in order, and a
re-post of the same batch (same source_ids) adds no items.
"""

from __future__ import annotations

import asyncio
import io
import json
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests._helpers.live_server import isolated_local_server

# CI shells can carry an egress proxy; every HTTP call here targets 127.0.0.1.
_http = httpx.Client(trust_env=False)


# Server bootstrap: patch SqlAlchemyConversationStore.append to sleep 150 ms
# per call, emulating one remote-store round trip, so per-entry appends cost
# 100 x 150 ms = 15 s (past the 10 s client timeout) and one run costs 150 ms.
_SERVER_BOOTSTRAP_SLOW_APPEND = """\
import time
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

_real_append = SqlAlchemyConversationStore.append


def _slow_append(self, conversation_id, items):
    time.sleep(0.15)
    return _real_append(self, conversation_id, items)


SqlAlchemyConversationStore.append = _slow_append

from omnigent.cli import main

main()
"""

# 100 items saturates MAX_SESSION_EVENT_BATCH_EVENTS; each has a distinct
# source_id so the server's stable_id dedup can prove idempotency on re-post.
_BATCH_SIZE = 100
_APPEND_DELAY_S = 0.15
_RESPONSE_ID = "resp-subagent-batch-timeout-test"

# One 150 ms append per run is far inside the forwarder's 10 s timeout; 8 s
# leaves generous headroom for a loaded CI box.
_MAX_ELAPSED_S = 8.0


def _create_claude_native_session(base_url: str) -> str:
    """Create a claude-native wrapper session exactly like ``omnigent claude``.

    Reuses the production spec materializer and stamps the same wrapper /
    terminal-first labels the CLI writes, so the created session is a real
    claude-native conversation with an agent_id (required by the sub-agent
    start handler).

    :param base_url: Spawned server base URL.
    :returns: The new session/conversation id.
    """
    from omnigent._wrapper_labels import (
        CLAUDE_NATIVE_WRAPPER_VALUE,
        UI_MODE_LABEL_KEY,
        UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY,
    )
    from omnigent.harnesses.claude_native.main import _materialize_claude_agent_spec

    with tempfile.TemporaryDirectory() as tmp:
        yaml_text = _materialize_claude_agent_spec(Path(tmp)).read_text()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        info = tarfile.TarInfo("claude-native-ui.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    labels = {
        UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY: CLAUDE_NATIVE_WRAPPER_VALUE,
    }
    create = _http.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({"labels": labels})},
        files={"bundle": ("claude-native-ui.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    return str(create.json()["session_id"])


def _build_subagent_items(n: int) -> list[Any]:
    """Build *n* _PendingSubagentItem objects shaped like what the forwarder sends.

    Each item is a batchable external_conversation_item (assistant message or
    function_call/function_call_output pair) with a distinct source_id so the
    server's stable_id path deduplicates re-posts correctly.

    :param n: Number of items; must equal MAX_SESSION_EVENT_BATCH_EVENTS (100).
    :returns: List of _PendingSubagentItem ready for _post_external_conversation_items.
    """
    from omnigent.harnesses.claude_native.bridge import ClaudeTranscriptItem
    from omnigent.harnesses.claude_native.forwarder import _PendingSubagentItem

    items: list[Any] = []
    i = 0
    while len(items) < n:
        # Every third triple is a function_call / function_call_output pair
        # followed by an assistant reply; the rest are plain assistant messages.
        # None are user messages, so the server appends them as one run.
        if i % 3 == 0 and len(items) + 3 <= n:
            call_id = f"toolu_batch_test_{i:03d}"
            items.append(
                _PendingSubagentItem(
                    item=ClaudeTranscriptItem(
                        source_id=f"subagent-batch-timeout-test:{i}:function_call",
                        item_type="function_call",
                        data={
                            "agent": "claude-native-ui",
                            "name": "read_file",
                            "call_id": call_id,
                            "arguments": "{}",
                        },
                        response_id=_RESPONSE_ID,
                    )
                )
            )
            items.append(
                _PendingSubagentItem(
                    item=ClaudeTranscriptItem(
                        source_id=f"subagent-batch-timeout-test:{i}:function_call_output",
                        item_type="function_call_output",
                        data={"call_id": call_id, "output": f"result-{i:03d}"},
                        response_id=_RESPONSE_ID,
                    )
                )
            )
            items.append(
                _PendingSubagentItem(
                    item=ClaudeTranscriptItem(
                        source_id=f"subagent-batch-timeout-test:{i}:message",
                        item_type="message",
                        data={
                            "role": "assistant",
                            "agent": "claude-native-ui",
                            "content": [{"type": "output_text", "text": f"batch-item-{i:03d}"}],
                        },
                        response_id=_RESPONSE_ID,
                    )
                )
            )
            i += 3
        else:
            items.append(
                _PendingSubagentItem(
                    item=ClaudeTranscriptItem(
                        source_id=f"subagent-batch-timeout-test:{i}:message",
                        item_type="message",
                        data={
                            "role": "assistant",
                            "agent": "claude-native-ui",
                            "content": [{"type": "output_text", "text": f"batch-item-{i:03d}"}],
                        },
                        response_id=_RESPONSE_ID,
                    )
                )
            )
            i += 1
    return items[:n]


async def _setup_sessions(base_url: str, parent_id: str) -> str:
    """Create a sub-agent child session via external_subagent_start.

    Uses the same POST /v1/sessions/{id}/events + external_subagent_start path
    the real forwarder takes when it discovers a new agent-*.meta.json on disk.

    :param base_url: Spawned server base URL.
    :param parent_id: Parent claude-native session id.
    :returns: The minted child session id.
    """
    from omnigent.harnesses.claude_native.forwarder import _post_external_subagent_start

    async with httpx.AsyncClient(base_url=base_url, timeout=30.0, trust_env=False) as client:
        return await _post_external_subagent_start(
            client,
            parent_session_id=parent_id,
            subagent_id="batch-timeout-regression-subagent-001",
            agent_type="Explore",
            description="sub-agent batch timeout regression fixture",
            tool_use_id="toolu_batch_timeout_regression_001",
        )


async def _post_batch(base_url: str, session_id: str, items: list[Any]) -> float:
    """Post *items* to *session_id* using the real forwarder client path.

    Configures httpx.AsyncClient with the forwarder's production 10 s POST
    timeout so the test exercises the exact timeout the real forwarder sees.

    :param base_url: Spawned server base URL.
    :param session_id: Child conversation id to post items into.
    :param items: _PendingSubagentItem list built by :func:`_build_subagent_items`.
    :returns: Wall-clock elapsed seconds for the POST.
    """
    from omnigent.harnesses.claude_native.forwarder import (
        _POST_TIMEOUT_S,
        _post_external_conversation_items,
        _SessionEventBatchCapability,
    )

    timeout = httpx.Timeout(_POST_TIMEOUT_S)
    started = time.monotonic()
    async with httpx.AsyncClient(base_url=base_url, timeout=timeout, trust_env=False) as client:
        cap = _SessionEventBatchCapability()
        await _post_external_conversation_items(
            client,
            session_id=session_id,
            items=items,
            batch_capability=cap,
        )
    return time.monotonic() - started


def _item_signature(item_type: str, data: dict[str, Any]) -> str:
    """Identify an item by type plus its call id or message text."""
    if item_type in ("function_call", "function_call_output"):
        return f"{item_type}:{data.get('call_id')}"
    texts = [block.get("text", "") for block in data.get("content") or []]
    return f"{item_type}:{'|'.join(texts)}"


def _committed_signatures(base_url: str, session_id: str) -> list[str]:
    """Return the committed items of *session_id*, oldest first, as signatures.

    :param base_url: Spawned server base URL.
    :param session_id: Conversation to query.
    :returns: One :func:`_item_signature` per committed item.
    """
    resp = _http.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"limit": 200, "order": "asc"},
        timeout=30.0,
    )
    resp.raise_for_status()
    return [_item_signature(item["type"], item) for item in resp.json()["data"]]


@pytest.mark.timeout(120)
def test_subagent_batch_100_items_completes_within_timeout(tmp_path: Path) -> None:
    """A 100-item sub-agent batch must land within the forwarder timeout.

    Journey: a claude-native forwarder sends a 100-item external_conversation_item
    array to a server whose store has 150 ms/append overhead. Appended one entry
    at a time that is 15 s, past the 10 s client timeout, so the forwarder gets
    ReadTimeout on every retry and drops the batch with "sub-agent transcript
    incomplete". Appended as one run it is a single 150 ms append.

    Expected: no ReadTimeout, exactly the 100 posted items in the child session
    in posted order, and a fast re-post of the same batch adds no items.
    Buggy behavior: ReadTimeout after 10 s, partial or zero items persisted.

    :param tmp_path: Per-test temp dir supplied by pytest.
    """
    from omnigent.harnesses.claude_native.forwarder import _POST_TIMEOUT_S

    with isolated_local_server(tmp_path, bootstrap=_SERVER_BOOTSTRAP_SLOW_APPEND) as base_url:
        parent_id = _create_claude_native_session(base_url)
        child_id = asyncio.run(_setup_sessions(base_url, parent_id))

        items = _build_subagent_items(_BATCH_SIZE)
        assert len(items) == _BATCH_SIZE, f"_build_subagent_items returned {len(items)}"

        elapsed = asyncio.run(_post_batch(base_url, child_id, items))

        server_tail = (tmp_path / "server.log").read_text()[-2000:]

        assert elapsed < _MAX_ELAPSED_S, (
            f"Batch POST took {elapsed:.3f} s, exceeding the {_MAX_ELAPSED_S} s "
            f"guard (forwarder timeout is {_POST_TIMEOUT_S} s). Per-entry appends "
            f"cost {_BATCH_SIZE} x {_APPEND_DELAY_S} s = "
            f"{_BATCH_SIZE * _APPEND_DELAY_S} s; one run costs one append "
            f"(~{_APPEND_DELAY_S} s). server log tail:\n{server_tail}"
        )

        expected = [_item_signature(p.item.item_type, p.item.data) for p in items]
        committed = _committed_signatures(base_url, child_id)
        assert committed == expected, (
            f"Expected the {_BATCH_SIZE} posted items once each in posted order, got "
            f"{len(committed)} items. elapsed={elapsed:.3f} s. server log tail:\n{server_tail}"
        )

        # A client retry re-posts the identical batch: source_id-keyed dedup must
        # add nothing, and the re-post must also finish inside the timeout.
        repost_elapsed = asyncio.run(_post_batch(base_url, child_id, items))
        assert repost_elapsed < _MAX_ELAPSED_S, (
            f"Re-post of the same batch took {repost_elapsed:.3f} s, exceeding the "
            f"{_MAX_ELAPSED_S} s guard"
        )
        assert _committed_signatures(base_url, child_id) == expected, (
            "Re-post of the same batch changed the committed items; source_id-keyed "
            "dedup must be idempotent"
        )
