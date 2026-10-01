"""E2E regression test: a 300s forward-loop stall must not duplicate committed items.

Reproduces the user-reported bug: the claude-native transcript forwarder wraps
each poll iteration in ``asyncio.timeout(_FORWARD_LOOP_STALL_DEADLINE_S)`` (300s
in production). When one iteration stalls mid-batch (a loaded host, a slow POST,
a large backlog), the deadline cancels the stalled await and the loop resumes.

The forwarder advances its persisted transcript cursor only after a whole batch
is consumed, and the in-memory ``seen_source_ids`` accumulated during the
cancelled iteration is discarded with the interrupted call frame. So the next
iteration re-reads the batch from its start and re-POSTs every item. Because
``_post_external_conversation_item`` sends no ``source_id`` and the server does
not dedupe ``external_conversation_item`` events, every re-post commits a
**duplicate row**. On a session whose iteration reliably exceeds the deadline
this is a permanent 5-minute replay loop that duplicated three production
transcripts 500-750x and grew ``conversation_items`` to 26 GB, taking the
database read-only.

This test drives the REAL user path end to end: a real ``omnigent server``
subprocess (so the real ``POST /v1/sessions/<id>/events`` route and the real
``_persist_external_conversation_item`` commit path run -- the exact "no
server-side idempotency" seam), a real claude-native session, and the real
``forward_claude_transcript_to_session`` loop tailing a seeded Claude JSONL
transcript. The reported trigger -- a stalled await mid-iteration -- is injected
as a fault: the POST of the *second* transcript item hangs on its first attempt,
so the (shortened) iteration deadline cancels it exactly as a slow production
POST would, and the loop resumes and replays.

Desired behavior (asserted): each distinct transcript item is committed to the
server EXACTLY ONCE, even across a stall + resume. Buggy behavior: the item that
was already committed before the stall is committed a SECOND time when the whole
batch replays, so ``GET /v1/sessions/<id>/items`` returns it twice and this test
FAILS with the duplicate count in the message.

Run::

    .venv/bin/python -m pytest \
        tests/e2e/test_claude_native_forwarder_stall_replay_dedup_e2e.py -v
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests._helpers.live_server import isolated_local_server

# CI shells can carry an egress proxy; every HTTP call here targets 127.0.0.1.
_http = httpx.Client(trust_env=False)


# Three assistant transcript entries; the forwarder mirrors each as one
# ``external_conversation_item``. Distinct markers let the assertion count how
# many times each landed in the server's committed items.
_MARKER_ALPHA = "stall-replay-alpha-committed-before-the-stall"
_MARKER_BRAVO = "stall-replay-bravo-the-stalled-post"
_MARKER_CHARLIE = "stall-replay-charlie-after-the-stall"


def _create_claude_native_session(base_url: str) -> str:
    """Create a claude-native wrapper session exactly like ``omnigent claude``.

    Reuses the production spec materializer and stamps the same wrapper /
    terminal-first labels the CLI writes, so the created session is a real
    claude-native conversation -- the kind whose transcript the forwarder
    mirrors in production.

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
        # Non-config.yaml arcname routes through the omnigent compat translator
        # (the wrapper spec has no ``spec_version``).
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
        files={
            "bundle": (
                "claude-native-ui.tar.gz",
                buf.getvalue(),
                "application/gzip",
            )
        },
        timeout=30.0,
    )
    create.raise_for_status()
    return str(create.json()["session_id"])


def _seed_transcript(bridge_dir: Path) -> Path:
    """Write a three-item Claude JSONL transcript + a Stop hook.

    Mirrors the seeding pattern of the forwarder's own unit tests: three
    assistant text entries with distinct uuids, each of which the forwarder
    mirrors as one ``external_conversation_item``. A recorded ``Stop`` hook
    reports the transcript path so the loop resolves it on the first poll.

    :param bridge_dir: Native Claude bridge directory.
    :returns: The transcript path.
    """
    from omnigent.harnesses.claude_native.bridge import record_hook_event

    transcript_path = bridge_dir / "transcript.jsonl"
    lines = [
        {
            "type": "assistant",
            "uuid": "assistant-alpha",
            "message": {"role": "assistant", "content": [{"type": "text", "text": _MARKER_ALPHA}]},
        },
        {
            "type": "assistant",
            "uuid": "assistant-bravo",
            "message": {"role": "assistant", "content": [{"type": "text", "text": _MARKER_BRAVO}]},
        },
        {
            "type": "assistant",
            "uuid": "assistant-charlie",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": _MARKER_CHARLIE}],
            },
        },
    ]
    transcript_path.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "Stop",
            "session_id": "claude-session-stall-replay",
            "transcript_path": str(transcript_path),
        },
    )
    return transcript_path


def _count_marker(base_url: str, session_id: str, marker: str) -> int:
    """Count committed conversation items whose payload contains *marker*.

    :param base_url: Spawned server base URL.
    :param session_id: Conversation to query.
    :param marker: Substring to match against each item's serialized data.
    :returns: Number of committed items carrying the marker.
    """
    resp = _http.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"limit": 1000, "order": "asc"},
        timeout=30.0,
    )
    resp.raise_for_status()
    return sum(1 for item in resp.json()["data"] if marker in json.dumps(item))


async def _drive_forwarder_through_a_stall(
    base_url: str, session_id: str, bridge_dir: Path
) -> None:
    """Run the real forwarder loop through one injected mid-batch stall.

    Shortens the production 300s iteration deadline and injects the reported
    trigger -- a stalled await mid-iteration -- by hanging the POST of the
    second transcript item on its first attempt. ALPHA is committed, BRAVO's
    post hangs until the deadline cancels it, the loop logs the stall warning
    and resumes, and the next iteration replays the whole batch.

    :param base_url: Spawned server base URL.
    :param session_id: Conversation the forwarder mirrors into.
    :param bridge_dir: Seeded native Claude bridge directory.
    """
    import omnigent.harnesses.claude_native.forwarder as fwd

    real_post = fwd._post_external_conversation_item
    stalled_once = {"done": False}

    async def _post_with_one_stall(client: Any, *, session_id: str, item: Any) -> None:
        text = json.dumps(item.data)
        # First attempt at BRAVO: never return, so the iteration deadline
        # cancels this await -- exactly a slow/hung production POST. ALPHA has
        # already been posted (and committed) by now, so the resume replays it.
        if _MARKER_BRAVO in text and not stalled_once["done"]:
            stalled_once["done"] = True
            await asyncio.Event().wait()
        await real_post(client, session_id=session_id, item=item)

    fwd._post_external_conversation_item = _post_with_one_stall
    # 2.0s is comfortably longer than the localhost preamble + ALPHA's post, so
    # ALPHA commits first; the infinite BRAVO hang then trips the deadline.
    fwd._FORWARD_LOOP_STALL_DEADLINE_S = 2.0
    try:
        task = asyncio.create_task(
            fwd.forward_claude_transcript_to_session(
                base_url=base_url,
                headers={},
                session_id=session_id,
                bridge_dir=bridge_dir,
                agent_name="claude-native-ui",
                start_at_end=False,
                poll_interval_s=0.02,
            )
        )
        try:
            # Give the loop enough wall time for: iteration 1 (post ALPHA,
            # stall on BRAVO, trip the 2.0s deadline) + iteration 2 (replay the
            # whole batch). 12s leaves generous headroom on a loaded CI box.
            await asyncio.sleep(12.0)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    finally:
        fwd._post_external_conversation_item = real_post


@pytest.mark.timeout(300)
def test_forward_loop_stall_does_not_duplicate_committed_items(tmp_path: Path) -> None:
    """
    A stalled forwarder iteration must not re-commit already-committed items.

    Journey (the reporter's): a claude-native session's forwarder is mirroring
    its transcript to the server; one poll iteration stalls mid-batch and the
    300s deadline cancels it; the loop resumes and re-posts the same items;
    with no server-side idempotency each re-post commits a duplicate row.

    Expected: every transcript item is committed exactly once across the stall
    + resume. Buggy behavior: the item committed before the stall (ALPHA) is
    committed a SECOND time when the whole batch replays, so it appears twice
    in ``/items`` -- this test FAILS with the duplicate count.

    :param tmp_path: Per-test temp dir (server DB, artifacts, bridge dir).
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bridge_dir: Path | None = None

    try:
        with isolated_local_server(tmp_path) as base_url:
            session_id = _create_claude_native_session(base_url)
            # Root the bridge dir under the production claude-native bridge root
            # (prepare_bridge_dir is the same helper the runner uses at launch), so
            # the forwarder tails a genuinely-rooted bridge exactly as in production.
            from omnigent.harnesses.claude_native.bridge import prepare_bridge_dir

            bridge_dir = prepare_bridge_dir(session_id, workspace=workspace)
            _seed_transcript(bridge_dir)

            # Drive the real forwarder loop through one injected mid-batch stall.
            asyncio.run(_drive_forwarder_through_a_stall(base_url, session_id, bridge_dir))

            alpha = _count_marker(base_url, session_id, _MARKER_ALPHA)
            bravo = _count_marker(base_url, session_id, _MARKER_BRAVO)
            charlie = _count_marker(base_url, session_id, _MARKER_CHARLIE)

            # Sanity: the batch was forwarded at all (the stall must not silently
            # drop everything -- otherwise the duplication assertion is vacuous).
            assert bravo >= 1 and charlie >= 1, (
                "forwarder never delivered the post-stall items; "
                f"alpha={alpha} bravo={bravo} charlie={charlie} -- "
                f"server log tail:\n{(tmp_path / 'server.log').read_text()[-2000:]}"
            )

            # The bug: ALPHA was committed before the stall, then re-committed when
            # the deadline-cancelled batch replayed. Correct behavior is exactly
            # one committed copy of each item.
            assert alpha == 1, (
                "The 300s forward-loop stall replayed the whole batch and "
                "re-committed an already-committed transcript item: "
                f"ALPHA committed {alpha} times (expected exactly 1). "
                f"bravo={bravo} charlie={charlie}. Because "
                "_post_external_conversation_item sends no source_id and the server "
                "does not dedupe external_conversation_item events, every stall "
                "replay duplicates rows until the database fills."
            )
    finally:
        if bridge_dir is not None:
            shutil.rmtree(bridge_dir, ignore_errors=True)
