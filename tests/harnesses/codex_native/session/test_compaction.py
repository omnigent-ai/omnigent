"""Compaction tests for Codex session."""

from __future__ import annotations

import asyncio
import base64
import json
from io import BytesIO
from pathlib import Path
from typing import Any

import click
import httpx
import pytest
from PIL import Image

from omnigent.entities import CompactionData
from omnigent.harnesses.codex_native import forwarder as codex_native_forwarder
from omnigent.harnesses.codex_native import main as codex_native
from tests.harnesses.codex_native.session._support import (
    _forwarder_context,
    _write_forwarder_bridge,
)


def test_forwarder_mirrors_codex_context_compaction(tmp_path: Path) -> None:
    """
    Codex context-compaction surfaces as external_compaction_status (#1255).

    A ``contextCompaction`` item/started shows the spinner (in_progress) and
    the ``thread/compacted`` notification clears it (completed). Both signals
    were previously dropped, so the web UI never indicated Codex compacted —
    increasingly relevant with GPT-5.1-Codex-Max auto-compaction.
    """
    _write_forwarder_bridge(tmp_path, active_turn_id="turn_123")
    forwarder_state = codex_native_forwarder._CodexForwarderState()
    posted: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        """Record /events bodies; 202 for events, 200 otherwise."""
        if request.url.path.endswith("/events"):
            posted.append(json.loads(request.content))
            return httpx.Response(202, json={"queued": False})
        return httpx.Response(200, json={})

    async def run() -> None:
        """Drive a compaction start item then the completion notification."""
        async with httpx.AsyncClient(
            base_url="http://127.0.0.1:8000",
            transport=httpx.MockTransport(handler),
        ) as client:
            for event in [
                {
                    "method": "item/started",
                    "params": {
                        "threadId": "thread_123",
                        "turnId": "turn_123",
                        "item": {"type": "contextCompaction", "id": "item_c"},
                    },
                },
                {"method": "thread/compacted", "params": {"threadId": "thread_123"}},
            ]:
                await codex_native_forwarder._handle_event(
                    client,
                    **_forwarder_context(client, tmp_path),
                    event=event,
                    forwarder_state=forwarder_state,
                )

    asyncio.run(run())

    compaction = [p for p in posted if p.get("type") == "external_compaction_status"]
    assert compaction == [
        {"type": "external_compaction_status", "data": {"status": "in_progress"}},
        {"type": "external_compaction_status", "data": {"status": "completed"}},
    ]


def test_rollout_records_includes_compacted_entry_from_compaction_item() -> None:
    """Compaction items emit a Compacted rollout record and discard prior items."""
    items: list[dict[str, Any]] = [
        {
            "id": "msg_1",
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "hello"}],
            "response_id": "resp_1",
        },
        {
            "id": "msg_2",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "hi there"}],
            "response_id": "resp_1",
        },
        {
            "id": "cmp_1",
            "type": "compaction",
            "summary": "compaction summary",
            "compacted_messages": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "hello"}],
                },
                {
                    "type": "compaction",
                    "encrypted_content": "gAAAA_encrypted",
                },
            ],
            "window_id": "01a070e2-2665-7d62-9b74-973decf239b7",
            "response_id": "compact_1",
        },
        {
            "id": "msg_3",
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "after compaction"}],
            "response_id": "resp_2",
        },
    ]
    records = codex_native._codex_rollout_records_from_session_items(
        items,
        session_id="conv_test",
        external_session_id="019f-thread",
        cwd=Path("/tmp/test"),
        model_provider="openai",
        cli_version="0.140.0",
    )
    # Should have: session_meta, compacted, turn_context, response_item (msg_3), event_msg
    types = [r["type"] for r in records]
    assert "session_meta" in types
    assert "compacted" in types
    # Pre-compaction response_items should be gone
    pre_compaction_items = [
        r
        for r in records
        if r["type"] == "response_item"
        and r["payload"].get("content") == [{"type": "input_text", "text": "hello"}]
    ]
    assert len(pre_compaction_items) == 0, "Pre-compaction items should be discarded"
    # The compacted record should have replacement_history and window_id
    compacted_records = [r for r in records if r["type"] == "compacted"]
    assert len(compacted_records) == 1
    cp = compacted_records[0]["payload"]
    assert cp["window_id"] == "01a070e2-2665-7d62-9b74-973decf239b7"
    assert len(cp["replacement_history"]) == 2
    assert cp["replacement_history"][1]["encrypted_content"] == "gAAAA_encrypted"
    # Post-compaction message should still be present
    post_items = [
        r
        for r in records
        if r["type"] == "response_item"
        and r["payload"].get("content") == [{"type": "input_text", "text": "after compaction"}]
    ]
    assert len(post_items) == 1


def test_rollout_records_preserve_function_call_completed_after_compaction() -> None:
    """A delayed tool output keeps its pre-compaction function call."""
    records = codex_native._codex_rollout_records_from_session_items(
        [
            {
                "id": "fc_abandoned",
                "response_id": "codex_turn_abandoned",
                "type": "function_call",
                "name": "exec_command",
                "arguments": '{"cmd":"abandoned-command"}',
                "call_id": "call_abandoned",
            },
            {
                "id": "fc_slow",
                "response_id": "codex_turn_slow",
                "type": "function_call",
                "name": "exec_command",
                "arguments": '{"cmd":"slow-command"}',
                "call_id": "call_slow",
            },
            {
                "id": "cmp_1",
                "response_id": "compact_1",
                "type": "compaction",
                "summary": "slow command still running",
                "compacted_messages": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "run it"}],
                    }
                ],
            },
            {
                "id": "fco_slow",
                "response_id": "codex_turn_slow",
                "type": "function_call_output",
                "call_id": "call_slow",
                "output": "finished after compaction",
            },
        ],
        session_id="conv_test",
        external_session_id="019f-thread",
        cwd=Path("/tmp/test"),
        model_provider="openai",
        cli_version="0.154.0",
    )

    replacement_history = next(record for record in records if record["type"] == "compacted")[
        "payload"
    ]["replacement_history"]
    calls = [item for item in replacement_history if item.get("type") == "function_call"]
    assert calls == [
        {
            "id": "fc_slow",
            "type": "function_call",
            "name": "exec_command",
            "arguments": '{"cmd":"slow-command"}',
            "call_id": "call_slow",
        }
    ]
    outputs = [
        record["payload"]
        for record in records
        if record["type"] == "response_item"
        and record["payload"].get("type") == "function_call_output"
    ]
    assert outputs == [
        {
            "id": "fco_slow",
            "type": "function_call_output",
            "call_id": "call_slow",
            "output": "finished after compaction",
        }
    ]


def test_rollout_records_do_not_duplicate_function_call_in_compaction() -> None:
    """A compaction snapshot that already has the open call remains unchanged."""
    function_call = {
        "id": "fc_slow",
        "type": "function_call",
        "name": "exec_command",
        "arguments": '{"cmd":"slow-command"}',
        "call_id": "call_slow",
    }
    records = codex_native._codex_rollout_records_from_session_items(
        [
            {**function_call, "response_id": "codex_turn_slow"},
            {
                "id": "cmp_1",
                "response_id": "compact_1",
                "type": "compaction",
                "summary": "slow command still running",
                "compacted_messages": [function_call],
            },
            {
                "id": "fco_slow",
                "response_id": "codex_turn_slow",
                "type": "function_call_output",
                "call_id": "call_slow",
                "output": "finished after compaction",
            },
        ],
        session_id="conv_test",
        external_session_id="019f-thread",
        cwd=Path("/tmp/test"),
        model_provider="openai",
        cli_version="0.154.0",
    )

    replacement_history = next(record for record in records if record["type"] == "compacted")[
        "payload"
    ]["replacement_history"]
    assert [
        item.get("call_id") for item in replacement_history if item.get("type") == "function_call"
    ] == ["call_slow"]


def test_rollout_records_preserve_function_call_across_repeated_compactions() -> None:
    """An open call survives every compaction before its delayed output."""
    records = codex_native._codex_rollout_records_from_session_items(
        [
            {
                "id": "fc_slow",
                "response_id": "codex_turn_slow",
                "type": "function_call",
                "name": "exec_command",
                "arguments": '{"cmd":"slow-command"}',
                "call_id": "call_slow",
            },
            {
                "id": "cmp_1",
                "response_id": "compact_1",
                "type": "compaction",
                "summary": "slow command still running",
                "compacted_messages": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "run it"}],
                    }
                ],
            },
            {
                "id": "cmp_2",
                "response_id": "compact_2",
                "type": "compaction",
                "summary": "slow command is still running",
                "compacted_messages": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "run it"}],
                    }
                ],
            },
            {
                "id": "fco_slow",
                "response_id": "codex_turn_slow",
                "type": "function_call_output",
                "call_id": "call_slow",
                "output": "finished after both compactions",
            },
        ],
        session_id="conv_test",
        external_session_id="019f-thread",
        cwd=Path("/tmp/test"),
        model_provider="openai",
        cli_version="0.154.0",
    )

    compacted_records = [record for record in records if record["type"] == "compacted"]
    assert len(compacted_records) == 1
    replacement_history = compacted_records[0]["payload"]["replacement_history"]
    assert [
        item.get("call_id") for item in replacement_history if item.get("type") == "function_call"
    ] == ["call_slow"]
    assert [
        record["payload"].get("call_id")
        for record in records
        if record["type"] == "response_item"
        and record["payload"].get("type") == "function_call_output"
    ] == ["call_slow"]


def test_rollout_records_do_not_carry_interrupted_call_across_compaction() -> None:
    """An interrupted tool interaction is absent from resumed history."""
    records = codex_native._codex_rollout_records_from_session_items(
        [
            {
                "id": "fc_cancelled",
                "response_id": "codex_turn_cancelled",
                "type": "function_call",
                "name": "exec_command",
                "arguments": '{"cmd":"cancelled-command"}',
                "call_id": "call_cancelled",
            },
            {
                "id": "cmp_1",
                "response_id": "compact_1",
                "type": "compaction",
                "summary": "cancelled command was still running",
                "compacted_messages": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "run it"}],
                    }
                ],
            },
            {
                "id": "fco_cancelled",
                "response_id": "codex_turn_cancelled",
                "type": "function_call_output",
                "call_id": "call_cancelled",
                "output": "finished after cancellation",
            },
            {
                "id": "msg_cancelled",
                "response_id": "codex_turn_cancelled",
                "type": "message",
                "role": "assistant",
                "interrupted": True,
                "content": [{"type": "output_text", "text": "cancelled"}],
            },
        ],
        session_id="conv_test",
        external_session_id="019f-thread",
        cwd=Path("/tmp/test"),
        model_provider="openai",
        cli_version="0.154.0",
    )

    replacement_history = next(record for record in records if record["type"] == "compacted")[
        "payload"
    ]["replacement_history"]
    assert not any(
        item.get("call_id") == "call_cancelled"
        for item in replacement_history
        if isinstance(item, dict)
    )
    assert not any(
        record["type"] == "response_item" and record["payload"].get("call_id") == "call_cancelled"
        for record in records
    )


def test_rollout_records_downgrade_image_stripped_by_compaction_storage() -> None:
    """A stored Responses image marker cannot poison Codex replacement history."""
    pixels = bytes(range(256)) * 3
    png = BytesIO()
    Image.frombytes("RGB", (16, 16), pixels).save(png, format="PNG")
    data_uri = f"data:image/png;base64,{base64.b64encode(png.getvalue()).decode()}"
    messages = [
        {
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": "before"},
                {"type": "input_image", "image_url": data_uri, "detail": "high"},
                {"type": "input_image", "image_url": "https://example.com/kept.png"},
                {"type": "input_image", "file_id": "file_kept"},
                {"type": "input_text", "text": "after"},
            ],
        },
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "answer"}],
        },
    ]
    stored = CompactionData(
        summary="summary",
        last_item_id="msg_before",
        token_count=1,
        compacted_messages=messages,
    )
    assert stored.compacted_messages is not None
    stored_content = stored.compacted_messages[0]["content"]
    assert stored_content[1]["image_url"] == (
        "[image/png content omitted from the compaction snapshot]"
    )

    records = codex_native._codex_rollout_records_from_session_items(
        [{"id": "cmp_image", "type": "compaction", **stored.model_dump(exclude_none=True)}],
        session_id="conv_test",
        external_session_id="019f-thread",
        cwd=Path("/tmp/test"),
        model_provider="openai",
        cli_version="0.140.0",
    )

    history = next(record for record in records if record["type"] == "compacted")["payload"][
        "replacement_history"
    ]
    assert [message["role"] for message in history] == ["user", "assistant"]
    replayed_content = history[0]["content"]
    assert [block["type"] for block in replayed_content] == [
        "input_text",
        "input_text",
        "input_image",
        "input_image",
        "input_text",
    ]
    assert "image/png" in replayed_content[1]["text"]
    assert replayed_content[2:] == stored_content[2:]
    assert messages[0]["content"][1]["image_url"] == data_uri


def test_codex_event_msg_record_ignores_non_list_content() -> None:
    """Malformed message content is skipped as it was before type narrowing."""
    assert (
        codex_native._codex_event_msg_record_for_message(
            {"type": "message", "role": "user", "content": "not-a-list"},
            timestamp="2026-08-02T00:00:00.000Z",
        )
        is None
    )


def test_codex_event_msg_record_reports_non_string_text_cleanly() -> None:
    """Malformed block text produces a user-facing CLI error."""
    with pytest.raises(
        click.ClickException,
        match="Codex message content text must be a string",
    ):
        codex_native._codex_event_msg_record_for_message(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": 42}],
            },
            timestamp="2026-08-02T00:00:00.000Z",
        )


def test_rollout_records_without_compaction_item_has_no_compacted_entry() -> None:
    """Sessions without compaction produce no Compacted rollout record."""
    items: list[dict[str, Any]] = [
        {
            "id": "msg_1",
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "hello"}],
            "response_id": "resp_1",
        },
    ]
    records = codex_native._codex_rollout_records_from_session_items(
        items,
        session_id="conv_test",
        external_session_id="019f-thread",
        cwd=Path("/tmp/test"),
        model_provider="openai",
        cli_version="0.140.0",
    )
    types = [r["type"] for r in records]
    assert "compacted" not in types


@pytest.mark.parametrize(
    "terminal_launch_args",
    [
        ["--sandbox", "danger-full-access", "--ask-for-approval", "never"],
        ["-c", 'default_permissions=":danger-full-access"'],
    ],
)
def test_rollout_records_use_terminal_launch_permission_args(
    terminal_launch_args: list[str],
) -> None:
    """Cold-resume turn_context preserves the persisted Codex permission mode."""
    records = codex_native._codex_rollout_records_from_session_items(
        [
            {
                "id": "msg_1",
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "hello"}],
                "response_id": "resp_1",
            }
        ],
        session_id="conv_test",
        external_session_id="019f-thread",
        cwd=Path("/tmp/test"),
        model_provider="openai",
        cli_version="0.140.0",
        terminal_launch_args=terminal_launch_args,
    )

    turn_context = next(r for r in records if r["type"] == "turn_context")["payload"]
    assert turn_context["approval_policy"] == "never"
    assert turn_context["sandbox_policy"] == {"type": "danger-full-access"}
