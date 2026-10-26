"""E2E: a pi-native cold resume (real server + runner) must synthesize one Pi
``toolResult`` per ``toolCallId`` even when history holds two outputs for one call.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

import httpx
import pytest

from omnigent.harnesses.pi_native.main import _SESSION_LABELS, _materialize_pi_agent_spec
from tests._helpers.server_runner import server_runner
from tests._helpers.session import bundle_files, post_session_bundle

# Every HTTP call targets 127.0.0.1; bypass any CI egress proxy autodetection.
_http = httpx.Client(trust_env=False)


# Valid Pi UUID-shaped session id (is_safe_pi_session_id) bound as the prior
# Pi session -- the state a user resumes into.
_EXTERNAL_SID = "019efdb8-54c8-7c02-be27-875eb2620635"

# One tool call whose result is committed twice, in Bedrock's tool_use id shape.
_CALL_ID = "toolu_bdrk_01DuplicateToolResult"

_POLL_S = 1.0
# Terminal auto-create includes bridge prep + provider probe + tmux boot.
_RESUME_TIMEOUT_S = 180.0

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None,
    reason="pi-native terminals run inside tmux; tmux not installed",
)


def _create_pi_native_session(base_url: str) -> str:
    """Create a pi-native wrapper session (production spec + labels) like ``omnigent pi``."""
    with tempfile.TemporaryDirectory() as tmp:
        spec = _materialize_pi_agent_spec(Path(tmp))
        bundle = bundle_files({spec.name: spec.read_bytes()})
    create = post_session_bundle(
        _http.post,
        f"{base_url}/v1/sessions",
        bundle,
        metadata={"labels": dict(_SESSION_LABELS)},
        timeout=30.0,
    )
    create.raise_for_status()
    return str(create.json()["session_id"])


def _seed_history_with_duplicate_output(database_uri: str, session_id: str) -> None:
    """Seed one function_call plus two function_call_output items for one call_id,
    the history left behind when the pi extension mirrors a tool result twice.
    """
    from omnigent.entities import (
        FunctionCallData,
        FunctionCallOutputData,
        MessageData,
        NewConversationItem,
    )
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )

    store = SqlAlchemyConversationStore(database_uri)
    items = [
        NewConversationItem(
            type="message",
            response_id="resp_0",
            data=MessageData(
                role="user",
                content=[{"type": "input_text", "text": "What time is it?"}],
            ),
        ),
        NewConversationItem(
            type="function_call",
            response_id="resp_1",
            data=FunctionCallData(
                agent="pi",
                name="get_time",
                arguments="{}",
                call_id=_CALL_ID,
            ),
        ),
        NewConversationItem(
            type="function_call_output",
            response_id="resp_1",
            data=FunctionCallOutputData(call_id=_CALL_ID, output="12:00 (first mirror)"),
        ),
        NewConversationItem(
            type="function_call_output",
            response_id="resp_1",
            data=FunctionCallOutputData(call_id=_CALL_ID, output="12:00 (second mirror)"),
        ),
        NewConversationItem(
            type="message",
            response_id="resp_1",
            data=MessageData(
                role="assistant",
                agent="pi",
                content=[{"type": "output_text", "text": "It is 12:00."}],
            ),
        ),
    ]
    store.append(session_id, items)


def _written_pi_session_file(runner_home: Path, session_id: str) -> Path | None:
    """Locate the synthesized Pi session JSONL under the runner's bridge dir,
    ``$HOME/.omnigent/pi-native/<sha256(id)[:32]>/sessions/<stamp>_<sid>.jsonl``.
    """
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
    sessions_dir = runner_home / ".omnigent" / "pi-native" / digest / "sessions"
    if not sessions_dir.is_dir():
        return None
    for entry in sessions_dir.iterdir():
        if entry.is_file() and entry.name.endswith(f"_{_EXTERNAL_SID}.jsonl"):
            return entry
    return None


def _tool_result_messages(session_file: Path) -> list[dict]:
    """Return every ``toolResult`` message, in file order."""
    messages: list[dict] = []
    for line in session_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        record = json.loads(line)
        message = record.get("message")
        if isinstance(message, dict) and message.get("role") == "toolResult":
            messages.append(message)
    return messages


def test_cold_resume_does_not_replay_duplicate_tool_result(tmp_path: Path) -> None:
    """Cold resume rebuilds exactly one toolResult for a doubly-committed output."""
    # Stub Pi CLI: parks so the tmux pane stays alive. The resume JSONL is
    # written before Pi is launched, so no login or real model is needed.
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    stub = stub_bin / "pi"
    stub.write_text("#!/bin/sh\nexec sleep 600\n")
    stub.chmod(0o755)

    with server_runner(tmp_path) as stack:
        base_url, runner_id = stack.base_url, stack.runner_id
        database_uri, runner_home = stack.database_uri, stack.runner_home
        stack.start_runner(
            env={
                "PATH": f"{stub_bin}{os.pathsep}{os.environ.get('PATH', '')}",
                "OMNIGENT_PI_PATH": str(stub),
            }
        )

        # A prior pi-native conversation with the Pi session id captured and
        # committed history holding a doubly-mirrored tool result -- the state
        # a user resumes into.
        session_id = _create_pi_native_session(base_url)
        _http.patch(
            f"{base_url}/v1/sessions/{session_id}",
            json={"external_session_id": _EXTERNAL_SID},
            timeout=10.0,
        ).raise_for_status()
        _seed_history_with_duplicate_output(database_uri, session_id)

        # Sanity: the server really committed both outputs for the one call_id.
        items = _http.get(
            f"{base_url}/v1/sessions/{session_id}/items",
            params={"limit": 1000, "order": "asc"},
            timeout=30.0,
        )
        items.raise_for_status()
        outputs = [
            i
            for i in items.json()["data"]
            if i.get("type") == "function_call_output" and i.get("call_id") == _CALL_ID
        ]
        assert len(outputs) == 2, f"expected two committed outputs for {_CALL_ID}, saw {outputs}"

        # THE RESUME: bind the session to the runner (what a web-UI reopen / a
        # daemon relaunch does) -> the runner auto-creates the Pi terminal,
        # whose _resolve_pi_resume_session synthesizes the Pi session JSONL.
        _http.patch(
            f"{base_url}/v1/sessions/{session_id}",
            json={"runner_id": runner_id},
            timeout=_RESUME_TIMEOUT_S,
        ).raise_for_status()

        deadline = time.monotonic() + _RESUME_TIMEOUT_S
        session_file: Path | None = None
        while time.monotonic() < deadline:
            session_file = _written_pi_session_file(runner_home, session_id)
            if session_file is not None:
                break
            time.sleep(_POLL_S)
        assert session_file is not None, (
            "runner never synthesized a Pi resume session file; runner log:\n"
            f"{(tmp_path / 'runner.log').read_text()[-3000:]}"
        )

        results = _tool_result_messages(session_file)
        call_ids = [message.get("toolCallId") for message in results]
        # Sanity: the rebuild produced the tool result at all (history intact).
        assert _CALL_ID in call_ids, (
            f"rebuilt Pi session has no toolResult for {_CALL_ID}; "
            f"toolResult ids: {call_ids}; file: {session_file.read_text()[:2000]}"
        )
        # The bug: two toolResult records share one toolCallId, so the next
        # Anthropic turn 400s with "each tool_use must have a single result".
        assert call_ids.count(_CALL_ID) == 1, (
            f"cold resume replayed {call_ids.count(_CALL_ID)} tool_result records "
            f"for one call_id ({_CALL_ID}); the resumed session is unrecoverable "
            f"(Anthropic rejects multiple tool_result blocks for one tool_use id). "
            f"toolResult ids in rebuilt session: {call_ids}"
        )
        # Gemini requires the matching tool name on replayed results.
        assert [
            message.get("toolName") for message in results if message.get("toolCallId") == _CALL_ID
        ] == ["get_time"]
