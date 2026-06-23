"""Unit tests for the Codex transcript adapter."""

from __future__ import annotations

import json
from pathlib import Path

from omnigent.entities import (
    FunctionCallData,
    FunctionCallOutputData,
    MessageData,
    ReasoningData,
)
from omnigent.importers.base import epoch_from_iso8601
from omnigent.importers.codex import CodexAdapter


def _parse(fixtures_dir: Path):
    """:returns: The parsed Codex fixture rollout."""
    return CodexAdapter().parse(fixtures_dir / "codex_session.jsonl")


def test_parse_item_types_and_order(fixtures_dir: Path) -> None:
    """Response items map ~1:1; ``session_meta`` / ``turn_context`` /
    ``event_msg`` envelopes and the ``developer`` message are skipped."""
    parsed = _parse(fixtures_dir)
    types = [(item.type, getattr(item.data, "role", None)) for item in parsed.items]
    assert types == [
        ("message", "user"),
        ("reasoning", None),
        ("function_call", None),
        ("function_call_output", None),
        ("message", "assistant"),
        ("function_call", None),
        ("function_call_output", None),
    ]


def test_developer_message_is_skipped(fixtures_dir: Path) -> None:
    """The injected ``developer`` scaffolding message is not imported."""
    parsed = _parse(fixtures_dir)
    blob = json.dumps([item.data.model_dump() for item in parsed.items])
    assert "operating in a sandbox" not in blob


def test_user_message_no_agent(fixtures_dir: Path) -> None:
    """The user message keeps ``input_text`` content and sets no agent."""
    parsed = _parse(fixtures_dir)
    user = parsed.items[0].data
    assert isinstance(user, MessageData)
    assert user.role == "user"
    assert user.agent is None
    assert user.content == [{"type": "input_text", "text": "Refactor the parser"}]


def test_reasoning_passthrough(fixtures_dir: Path) -> None:
    """Reasoning summary and encrypted content pass through, with the agent
    set to the resolved model."""
    parsed = _parse(fixtures_dir)
    reasoning = parsed.items[1].data
    assert isinstance(reasoning, ReasoningData)
    assert reasoning.agent == "gpt-5.5"
    assert reasoning.summary == [
        {"type": "summary_text", "text": "Plan: locate the parser, then refactor."}
    ]
    assert reasoning.encrypted_content == "enc-xyz"


def test_function_call_arguments_passthrough(fixtures_dir: Path) -> None:
    """Codex ``arguments`` are already a JSON string and pass through."""
    parsed = _parse(fixtures_dir)
    call = parsed.items[2].data
    assert isinstance(call, FunctionCallData)
    assert call.name == "shell"
    assert call.call_id == "call_1"
    assert call.agent == "gpt-5.5"
    assert isinstance(call.arguments, str)
    assert json.loads(call.arguments) == {"command": "ls"}


def test_function_call_output_passthrough(fixtures_dir: Path) -> None:
    """``function_call_output.output`` is a string and pairs by call id."""
    parsed = _parse(fixtures_dir)
    output = parsed.items[3].data
    assert isinstance(output, FunctionCallOutputData)
    assert output.call_id == "call_1"
    assert output.output == "parser.py\nmain.py"


def test_custom_tool_call_maps_to_function_call(fixtures_dir: Path) -> None:
    """``custom_tool_call`` (e.g. apply_patch) imports as a function_call with
    its ``input`` string carried as arguments; its output pairs by call id."""
    parsed = _parse(fixtures_dir)
    call = parsed.items[5].data
    assert isinstance(call, FunctionCallData)
    assert call.name == "apply_patch"
    assert call.call_id == "call_2"
    assert call.arguments == "*** Begin Patch\n*** End Patch"

    output = parsed.items[6].data
    assert isinstance(output, FunctionCallOutputData)
    assert output.call_id == "call_2"
    assert output.output == "patch applied"


def test_response_id_grouping(fixtures_dir: Path) -> None:
    """The user prompt opens its own response; the rest of the turn (reasoning,
    calls, outputs, assistant text) shares one assistant-turn response id."""
    parsed = _parse(fixtures_dir)
    response_ids = [item.response_id for item in parsed.items]
    assert response_ids[0] != response_ids[1]
    assert len(set(response_ids[1:])) == 1


def test_metadata(fixtures_dir: Path) -> None:
    """Session id, model, cwd, git branch, derived title, and created_at are
    harvested from the metadata envelopes and the first user message."""
    parsed = _parse(fixtures_dir)
    assert parsed.external_session_id == "019e42f3-07d6-7c83-b04d-caee8078cf51"
    assert parsed.model == "gpt-5.5"
    assert parsed.cwd == "/home/me/project"
    assert parsed.git_branch == "main"
    assert parsed.title == "Refactor the parser"
    assert parsed.created_at is not None


def test_created_at_from_envelope_when_meta_payload_lacks_timestamp(tmp_path: Path) -> None:
    """Regression: ``created_at`` is the earliest top-level envelope timestamp
    across all records, even when the ``session_meta`` payload omits one.

    Real Codex rollouts carry the canonical timestamp on the record envelope;
    the ``session_meta`` payload may not. Reading only the payload would lose
    the earliest transcript time (``created_at`` would be ``None``). Here the
    earliest timestamp sits on the ``turn_context`` envelope to prove the scan
    covers every record, not just ``session_meta``."""
    records = [
        {
            "timestamp": "2026-05-20T01:14:44.054Z",
            "type": "session_meta",
            "payload": {"id": "sess-1", "cwd": "/w"},  # note: no payload timestamp
        },
        {
            "timestamp": "2026-05-20T01:14:40.000Z",  # earliest, and not on session_meta
            "type": "turn_context",
            "payload": {"model": "gpt-5.5"},
        },
        {
            "timestamp": "2026-05-20T01:14:45.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "hi"}],
            },
        },
    ]
    path = tmp_path / "rollout-2026-05-20T01-14-44-sess-1.jsonl"
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

    parsed = CodexAdapter().parse(path)

    assert parsed.created_at is not None
    assert parsed.created_at == epoch_from_iso8601("2026-05-20T01:14:40.000Z")


def test_discover_extracts_session_uuid(tmp_path: Path) -> None:
    """Discovery extracts the trailing uuid from the rollout filename as the
    session id hint."""
    day = tmp_path / "2026" / "05" / "20"
    day.mkdir(parents=True)
    name = "rollout-2026-05-20T01-14-44-019e42f3-07d6-7c83-b04d-caee8078cf51.jsonl"
    (day / name).write_text("{}\n", encoding="utf-8")

    refs = CodexAdapter().discover(tmp_path)

    assert [ref.session_id for ref in refs] == ["019e42f3-07d6-7c83-b04d-caee8078cf51"]
