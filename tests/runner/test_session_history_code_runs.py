"""Reloaded runner history rebuilds each user message's code-run notice."""

from __future__ import annotations

from typing import Any

import httpx

from omnigent.inner.native_attachments import FRAMEWORK_NOTICE_BLOCK_TYPE
from omnigent.runner.session_history import build_session_history


async def _no_persist(conversation_id: str, items: list[dict[str, Any]]) -> None:
    del conversation_id, items


def _convert(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    history = build_session_history(
        _background_tasks=set(),
        _last_server_item_id={},
        _persist_cancellation_items=_no_persist,
        _session_histories={},
        _session_spec_cache={},
        server_client=httpx.AsyncClient(base_url="http://server.test"),
    )
    return history.convert_raw_items_to_input(items)


def _user(text: str) -> dict[str, Any]:
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


def _run(run_id: str, output: str) -> list[dict[str, Any]]:
    return [
        {
            "type": "code_run",
            "run_id": run_id,
            "phase": "started",
            "message_id": "msg_a",
            "block_hash": run_id,
            "language": "shell",
            "command": f"echo {output}",
        },
        {
            "type": "code_run",
            "run_id": run_id,
            "phase": "finished",
            "status": "exited",
            "exit_code": 0,
            "duration_ms": 5,
            "output": [{"stream": "stdout", "text": output}],
            "truncated": False,
        },
    ]


def _notice_text(message: dict[str, Any]) -> str:
    return "".join(
        block["text"]
        for block in message["content"]
        if block.get("type") == FRAMEWORK_NOTICE_BLOCK_TYPE
    )


def test_each_user_message_gets_the_runs_reported_with_it() -> None:
    assistant = {"type": "message", "role": "assistant", "content": "ok"}
    items = [
        _user("first"),
        assistant,
        *_run("r1", "one"),
        _user("second"),
        assistant,
        _user("third"),
        *_run("r2", "two"),
        _user("fourth"),
    ]

    users = [item for item in _convert(items) if item.get("role") == "user"]

    assert [_notice_text(user) != "" for user in users] == [False, True, False, True]
    assert "one" in _notice_text(users[1])
    assert "two" not in _notice_text(users[1])
    assert "two" in _notice_text(users[3])
    assert "one" not in _notice_text(users[3])
    assert users[1]["content"][0] == {"type": "input_text", "text": "second"}


def test_history_without_runs_is_unchanged() -> None:
    items = [_user("first"), {"type": "message", "role": "assistant", "content": "ok"}]

    assert _convert(items) == items


def test_reloaded_history_leaves_out_started_only_runs() -> None:
    started_only = _run("r1", "one")[0]
    items = [_user("first"), started_only, _user("second")]

    assert _notice_text(_convert(items)[1]) == ""
