"""Tests for importing local coding-harness sessions."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from omnigent.harnesses.kimi_native.forwarder import KimiWireItem, read_kimi_wire_items
from omnigent.harnesses.kiro_native.session_forwarder import (
    KiroConversationMessage,
    parse_kiro_jsonl_line,
)
from omnigent.session_import import local as local_import
from omnigent.session_import.local import (
    list_recent_local_session_ids,
    load_claude_session,
    load_codex_session,
    load_kimi_session,
    load_kiro_session,
    load_opencode_session,
    load_pi_session,
    load_qwen_session,
)
from omnigent.session_import.models import LocalSessionImport, SessionImportNotFoundError
from tests._helpers.codex_rollout import (
    CodexRollout,
    CodexThreadRow,
    codex_message,
    write_codex_thread_store,
)


def test_import_adapters_use_stable_forwarder_parser_contracts(tmp_path: Path) -> None:
    """Shared Kiro and Kimi parsers expose the fields offline import consumes."""
    kiro = parse_kiro_jsonl_line(
        json.dumps(
            {
                "kind": "Prompt",
                "data": {
                    "message_id": "kiro-1",
                    "content": [{"kind": "text", "data": "hello"}],
                },
            }
        )
    )
    assert kiro == KiroConversationMessage(message_id="kiro-1", role="user", text="hello")

    wire = tmp_path / "wire.jsonl"
    wire.write_text(
        json.dumps(
            {
                "type": "turn.prompt",
                "origin": {"kind": "user"},
                "input": [{"type": "text", "text": "hello"}],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    items = read_kimi_wire_items(wire, 0)
    assert items == [
        KimiWireItem(
            line_no=0,
            kind="message",
            role="user",
            text="hello",
            response_id="kimi:turn:0",
        )
    ]


@pytest.mark.parametrize("source", ["qwen", "kiro", "pi", "kimi"])
def test_long_source_ids_get_distinct_bounded_response_ids(
    tmp_path: Path,
    source: str,
) -> None:
    """Long native entry ids remain distinct after normalization."""
    native_ids = ("x" * 100 + "a", "x" * 100 + "b")
    if source == "qwen":
        home = tmp_path / "qwen"
        session_id = "qwen-session"
        transcript = home / "projects" / "-repo" / "chats" / f"{session_id}.jsonl"
        records = [
            {
                "uuid": native_ids[0],
                "parentUuid": None,
                "type": "user",
                "message": {"parts": [{"text": "first"}]},
            },
            {
                "uuid": native_ids[1],
                "parentUuid": native_ids[0],
                "type": "assistant",
                "message": {"parts": [{"text": "second"}]},
            },
        ]
        loader = load_qwen_session
        loader_kwargs = {"qwen_home": home}
    elif source == "kiro":
        home = tmp_path / "kiro"
        session_id = "kiro-session"
        root = home / ".kiro" / "sessions" / "cli"
        transcript = root / f"{session_id}.jsonl"
        root.mkdir(parents=True)
        (root / f"{session_id}.json").write_text("{}\n", encoding="utf-8")
        records = [
            {
                "kind": kind,
                "data": {
                    "message_id": native_id,
                    "content": [{"kind": "text", "data": text}],
                },
            }
            for kind, native_id, text in zip(
                ("Prompt", "AssistantMessage"),
                native_ids,
                ("first", "second"),
                strict=True,
            )
        ]
        loader = load_kiro_session
        loader_kwargs = {"kiro_home": home}
    elif source == "pi":
        home = tmp_path / "pi"
        session_id = "pi-session"
        transcript = home / "sessions" / "--repo--" / f"stamp_{session_id}.jsonl"
        records = [
            {"type": "session", "version": 3, "id": session_id},
            {
                "type": "message",
                "id": native_ids[0],
                "parentId": None,
                "message": {"role": "user", "content": "first"},
            },
            {
                "type": "message",
                "id": native_ids[1],
                "parentId": native_ids[0],
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "second"}],
                },
            },
        ]
        loader = load_pi_session
        loader_kwargs = {"pi_home": home}
    else:
        home = tmp_path / "kimi"
        session_id = "session_long_ids"
        transcript = home / "sessions" / "wd_repo" / session_id / "agents" / "main" / "wire.jsonl"
        records = [
            {
                "type": "context.append_loop_event",
                "event": {
                    "type": "content.part",
                    "uuid": native_id,
                    "part": {"type": "text", "text": text},
                },
            }
            for native_id, text in zip(native_ids, ("first", "second"), strict=True)
        ]
        loader = load_kimi_session
        loader_kwargs = {"kimi_home": home}

    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text(
        "".join(f"{json.dumps(record)}\n" for record in records), encoding="utf-8"
    )

    response_ids = [item.response_id for item in loader(session_id, **loader_kwargs).items]
    assert len(response_ids) == 2
    assert response_ids[0] != response_ids[1]
    assert all(len(response_id) <= 64 for response_id in response_ids)


def test_list_recent_opencode_sessions_uses_public_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenCode batch discovery uses its supported JSON listing command."""
    calls: list[tuple[str, ...]] = []

    def fake_run(
        *arguments: str, opencode_path: str | None = None, empty_ok: bool = False
    ) -> object:
        assert opencode_path is None
        calls.append(arguments)
        return [
            {"id": "ses_old", "updated": 10, "directory": "/old"},
            {"id": "ses_child", "updated": 30, "parentID": "ses_parent"},
            {"id": "ses_new", "updated": 20, "directory": "/new"},
        ]

    monkeypatch.setattr(local_import, "_run_opencode_json", fake_run)

    assert list_recent_local_session_ids("opencode", limit=2) == ("ses_new", "ses_old")
    assert calls == [("session", "list", "--format", "json", "--pure")]


def test_list_recent_opencode_sessions_rejects_schema_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A changed public listing schema reports a contract error."""
    monkeypatch.setattr(local_import, "_run_opencode_json", lambda *arguments, **kwargs: {})

    with pytest.raises(SessionImportNotFoundError, match="invalid session list"):
        list_recent_local_session_ids("opencode", limit=1)


def test_list_recent_opencode_sessions_treats_empty_output_as_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no sessions OpenCode prints nothing (exit 0) — not an error."""
    from types import SimpleNamespace

    monkeypatch.setattr(local_import, "find_opencode_cli", lambda path: "/fake/opencode")
    monkeypatch.setattr(
        local_import.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    assert list_recent_local_session_ids("opencode", limit=5) == ()


def test_load_opencode_session_preserves_messages_files_and_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public export maps ordered parts to durable Omnigent items."""
    export = {
        "info": {
            "id": "ses_import",
            "directory": "/repo",
            "title": "OpenCode session title",
            "version": "1.17.18",
        },
        "messages": [
            {
                "info": {"id": "msg_user", "role": "user"},
                "parts": [
                    {"type": "text", "text": "inspect TODO.md"},
                    {
                        "type": "file",
                        "mime": "image/png",
                        "url": "data:image/png;base64,AAAA",
                    },
                ],
            },
            {
                "info": {"id": "msg_assistant", "role": "assistant"},
                "parts": [
                    {"type": "reasoning", "text": "private reasoning"},
                    {"type": "text", "text": "Checking."},
                    {
                        "type": "tool",
                        "callID": "call_1",
                        "tool": "bash",
                        "state": {
                            "status": "completed",
                            "input": {"command": "rg TODO"},
                            "output": "",
                            "metadata": {"output": "TODO.md:1:item"},
                        },
                    },
                    {"type": "text", "text": "Done."},
                ],
            },
        ],
    }

    def fake_run(*arguments: str, opencode_path: str | None = None) -> object:
        assert arguments == ("export", "ses_import", "--pure")
        assert opencode_path is None
        return export

    monkeypatch.setattr(local_import, "_run_opencode_json", fake_run)

    imported = load_opencode_session("ses_import")
    dumped = [item.data.model_dump(mode="json", exclude_none=True) for item in imported.items]

    assert imported.source == "opencode"
    assert imported.external_session_id == "ses_import"
    assert imported.workspace == "/repo"
    assert imported.native_title == "OpenCode session title"
    assert imported.title == "OpenCode session title"
    assert [item.type for item in imported.items] == [
        "message",
        "message",
        "function_call",
        "function_call_output",
        "message",
    ]
    assert dumped[0] == {
        "role": "user",
        "content": [
            {"type": "input_text", "text": "inspect TODO.md"},
            {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
        ],
    }
    assert dumped[1]["content"] == [{"type": "output_text", "text": "Checking."}]
    assert dumped[1]["agent"] == "opencode-native-ui"
    assert dumped[2] == {
        "agent": "opencode-native-ui",
        "name": "bash",
        "arguments": '{"command":"rg TODO"}',
        "call_id": "call_1",
    }
    assert dumped[3] == {"call_id": "call_1", "output": "TODO.md:1:item"}
    assert dumped[4]["content"] == [{"type": "output_text", "text": "Done."}]
    assert {item.response_id for item in imported.items[1:]} == {"opencode:msg_assistant"}


def test_load_opencode_session_rejects_invalid_or_mismatched_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unsafe CLI arguments and mismatched exports cannot claim an import id."""
    with pytest.raises(SessionImportNotFoundError, match="was not found"):
        load_opencode_session("--help")

    monkeypatch.setattr(
        local_import,
        "_run_opencode_json",
        lambda *arguments, opencode_path=None: {
            "info": {"id": "ses_other"},
            "messages": [],
        },
    )
    with pytest.raises(SessionImportNotFoundError, match="did not match"):
        load_opencode_session("ses_expected")


def test_load_claude_session_normalizes_parent_transcript(tmp_path: Path) -> None:
    """Claude parent messages and tools become ordinary Omnigent items."""
    session_id = "a1b2c3d4-1234-5678-9abc-def012345678"
    transcript = tmp_path / "projects" / "-repo" / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    records = [
        {
            "type": "user",
            "uuid": "user-1",
            "cwd": "/repo",
            "message": {"role": "user", "content": "inspect TODO.md"},
        },
        {
            "type": "assistant",
            "uuid": "assistant-1",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_read_1",
                        "name": "Read",
                        "input": {"file_path": "TODO.md"},
                    }
                ],
            },
        },
        {
            "type": "user",
            "uuid": "result-1",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_read_1",
                        "content": "contents",
                    }
                ],
            },
        },
        {
            "type": "assistant",
            "uuid": "assistant-2",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Done."}],
            },
        },
    ]
    transcript.write_text(
        "".join(f"{json.dumps(record)}\n" for record in records),
        encoding="utf-8",
    )
    # A same-id sub-agent transcript must never be selected as the parent.
    subagent = tmp_path / "projects" / "-repo" / "subagents" / f"{session_id}.jsonl"
    subagent.parent.mkdir()
    subagent.write_text("{}\n", encoding="utf-8")

    imported = load_claude_session(session_id, claude_home=tmp_path)

    assert imported.source == "claude"
    assert imported.external_session_id == session_id
    assert imported.workspace == "/repo"
    assert imported.title == "inspect TODO.md"
    assert [item.type for item in imported.items] == [
        "message",
        "function_call",
        "function_call_output",
        "message",
    ]
    assert imported.items[1].data.model_dump()["call_id"] == "toolu_read_1"
    assert imported.items[3].data.model_dump()["agent"] == "claude-native-ui"


def _write_claude_transcript_with_titles(
    tmp_path: Path,
    session_id: str,
    *,
    ai_title: str | None,
    custom_title: str | None,
) -> None:
    """Write a minimal Claude transcript, optionally stamped with title lines."""
    transcript = tmp_path / "projects" / "-repo" / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    records: list[dict[str, object]] = [
        {
            "type": "user",
            "uuid": "user-1",
            "cwd": "/repo",
            "message": {"role": "user", "content": "inspect TODO.md"},
        }
    ]
    if ai_title is not None:
        records.append({"type": "ai-title", "aiTitle": ai_title, "sessionId": session_id})
    if custom_title is not None:
        records.append(
            {"type": "custom-title", "customTitle": custom_title, "sessionId": session_id}
        )
    transcript.write_text(
        "".join(f"{json.dumps(record)}\n" for record in records), encoding="utf-8"
    )


def test_load_claude_session_prefers_custom_title_over_ai_title(tmp_path: Path) -> None:
    """A user rename (custom-title) wins over the generated ai-title."""
    session_id = "a1b2c3d4-1234-5678-9abc-def012345678"
    _write_claude_transcript_with_titles(
        tmp_path, session_id, ai_title="Generated summary", custom_title="My renamed thread"
    )

    imported = load_claude_session(session_id, claude_home=tmp_path)

    assert imported.native_title == "My renamed thread"
    assert imported.title == "My renamed thread"


def test_load_claude_session_falls_back_to_ai_title(tmp_path: Path) -> None:
    """With no rename, the generated ai-title is used over the first message."""
    session_id = "a1b2c3d4-1234-5678-9abc-def012345678"
    _write_claude_transcript_with_titles(
        tmp_path, session_id, ai_title="Generated summary", custom_title=None
    )

    imported = load_claude_session(session_id, claude_home=tmp_path)

    assert imported.native_title == "Generated summary"
    assert imported.title == "Generated summary"


def test_load_claude_session_synthesizes_title_without_native(tmp_path: Path) -> None:
    """No title lines → the title is synthesized from the first user message."""
    session_id = "a1b2c3d4-1234-5678-9abc-def012345678"
    _write_claude_transcript_with_titles(tmp_path, session_id, ai_title=None, custom_title=None)

    imported = load_claude_session(session_id, claude_home=tmp_path)

    assert imported.native_title is None
    assert imported.title == "inspect TODO.md"


def test_load_claude_session_rejects_empty_history(tmp_path: Path) -> None:
    """An empty Claude transcript cannot create a claimed import."""
    session_id = "a1b2c3d4-1234-5678-9abc-def012345678"
    transcript = tmp_path / "projects" / "-repo" / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.touch()

    with pytest.raises(SessionImportNotFoundError, match="no importable history"):
        load_claude_session(session_id, claude_home=tmp_path)


def _write_claude_transcript_with_compaction(tmp_path: Path, session_id: str) -> Path:
    """Write a transcript with a compaction summary splitting two turns."""
    transcript = tmp_path / "projects" / "-repo" / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    records = [
        {
            "type": "user",
            "uuid": "user-pre",
            "cwd": "/repo",
            "message": {"role": "user", "content": "first question before compaction"},
        },
        {
            "type": "assistant",
            "uuid": "assistant-pre",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "early reply"}]},
        },
        {
            "type": "user",
            "uuid": "compact-1",
            "isCompactSummary": True,
            "message": {"role": "user", "content": "summary of the conversation so far"},
        },
        {
            "type": "user",
            "uuid": "user-post",
            "message": {"role": "user", "content": "follow-up after compaction"},
        },
        {
            "type": "assistant",
            "uuid": "assistant-post",
            "message": {"role": "assistant", "content": [{"type": "text", "text": "later reply"}]},
        },
    ]
    transcript.write_text(
        "".join(f"{json.dumps(record)}\n" for record in records), encoding="utf-8"
    )
    return transcript


def test_load_claude_session_keeps_full_history_below_size_threshold(tmp_path: Path) -> None:
    """A small transcript imports whole, even when it contains a compaction summary."""
    session_id = "a1b2c3d4-1234-5678-9abc-def012345678"
    _write_claude_transcript_with_compaction(tmp_path, session_id)

    imported = load_claude_session(session_id, claude_home=tmp_path)

    # Pre-compaction turn, the summary, and the post-compaction turn all present.
    texts = [
        block["text"]
        for item in imported.items
        for block in item.data.model_dump().get("content", [])
        if isinstance(block, dict) and "text" in block
    ]
    assert "first question before compaction" in texts
    assert "summary of the conversation so far" in texts
    assert "follow-up after compaction" in texts


def test_load_claude_session_trims_to_last_compaction_when_large(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transcript past the size threshold imports only from the last compaction summary."""
    session_id = "a1b2c3d4-1234-5678-9abc-def012345678"
    _write_claude_transcript_with_compaction(tmp_path, session_id)
    # Force the large-file path without writing multiple megabytes.
    monkeypatch.setattr(local_import, "_IMPORT_COMPACT_TRIM_BYTES", 0)

    imported = load_claude_session(session_id, claude_home=tmp_path)

    texts = [
        block["text"]
        for item in imported.items
        for block in item.data.model_dump().get("content", [])
        if isinstance(block, dict) and "text" in block
    ]
    # Pre-compaction records the live agent no longer sees are dropped; the
    # summary and everything after it are kept.
    assert "first question before compaction" not in texts
    assert "early reply" not in texts
    assert "summary of the conversation so far" in texts
    assert "follow-up after compaction" in texts
    assert "later reply" in texts


def test_load_claude_session_large_without_compaction_imports_all(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A large transcript that never compacted keeps its full history."""
    session_id = "a1b2c3d4-1234-5678-9abc-def012345678"
    _write_claude_transcript_with_titles(tmp_path, session_id, ai_title=None, custom_title=None)
    monkeypatch.setattr(local_import, "_IMPORT_COMPACT_TRIM_BYTES", 0)

    imported = load_claude_session(session_id, claude_home=tmp_path)

    # No isCompactSummary boundary → trimming is a no-op, first message stays.
    assert imported.title == "inspect TODO.md"
    assert imported.items


def test_load_claude_session_trims_to_final_compaction_with_multiple(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With several compactions, only the last boundary onward is imported."""
    session_id = "a1b2c3d4-1234-5678-9abc-def012345678"
    transcript = tmp_path / "projects" / "-repo" / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    records = [
        {
            "type": "user",
            "uuid": "user-0",
            "cwd": "/repo",
            "message": {"role": "user", "content": "original"},
        },
        {
            "type": "user",
            "uuid": "compact-1",
            "isCompactSummary": True,
            "message": {"role": "user", "content": "first summary"},
        },
        {
            "type": "user",
            "uuid": "compact-2",
            "isCompactSummary": True,
            "message": {"role": "user", "content": "second summary"},
        },
        {
            "type": "user",
            "uuid": "user-final",
            "message": {"role": "user", "content": "after second"},
        },
    ]
    transcript.write_text(
        "".join(f"{json.dumps(record)}\n" for record in records), encoding="utf-8"
    )
    monkeypatch.setattr(local_import, "_IMPORT_COMPACT_TRIM_BYTES", 0)

    imported = load_claude_session(session_id, claude_home=tmp_path)

    texts = [
        block["text"]
        for item in imported.items
        for block in item.data.model_dump().get("content", [])
        if isinstance(block, dict) and "text" in block
    ]
    assert "original" not in texts
    assert "first summary" not in texts
    assert "second summary" in texts
    assert "after second" in texts


def test_load_claude_session_trims_at_real_two_mb_threshold(tmp_path: Path) -> None:
    """Past the real 2 MB threshold, pre-compaction records are dropped (no patch)."""
    session_id = "a1b2c3d4-1234-5678-9abc-def012345678"
    transcript = tmp_path / "projects" / "-repo" / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    filler = "x" * 4096  # ~4 KB per record; ~600 records clears 2 MB.
    records: list[dict[str, object]] = [
        {
            "type": "user",
            "uuid": f"pre-{i}",
            "cwd": "/repo",
            "message": {"role": "user", "content": f"pre-compaction {i} {filler}"},
        }
        for i in range(600)
    ]
    records.append(
        {
            "type": "user",
            "uuid": "compact-1",
            "isCompactSummary": True,
            "message": {"role": "user", "content": "post compaction summary marker"},
        }
    )
    records.append(
        {
            "type": "user",
            "uuid": "post-1",
            "message": {"role": "user", "content": "question after compaction marker"},
        }
    )
    transcript.write_text(
        "".join(f"{json.dumps(record)}\n" for record in records), encoding="utf-8"
    )
    assert transcript.stat().st_size > 2 * 1024 * 1024

    imported = load_claude_session(session_id, claude_home=tmp_path)

    texts = [
        block["text"]
        for item in imported.items
        for block in item.data.model_dump().get("content", [])
        if isinstance(block, dict) and "text" in block
    ]
    assert not any(text.startswith("pre-compaction") for text in texts)
    assert any("post compaction summary marker" in text for text in texts)
    assert any("question after compaction marker" in text for text in texts)


def test_load_claude_session_titles_from_user_message_not_compaction_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A trimmed-to-compaction import titles from a real user turn, not the summary.

    The continuation summary leads the trimmed items, so without flagging it meta
    the title would be "summary of the conversation so far", an instruction-like
    title. It is durable context (is_meta), so the title falls through to the
    first real user message.
    """
    session_id = "a1b2c3d4-1234-5678-9abc-def012345678"
    _write_claude_transcript_with_compaction(tmp_path, session_id)
    monkeypatch.setattr(local_import, "_IMPORT_COMPACT_TRIM_BYTES", 0)

    imported = load_claude_session(session_id, claude_home=tmp_path)

    assert imported.title == "follow-up after compaction"
    # The summary still imports (durable context) but is flagged meta so it is
    # hidden from the user-facing transcript and skipped for the title.
    summary = imported.items[0].data.model_dump()
    assert summary["content"][0]["text"] == "summary of the conversation so far"
    assert summary["is_meta"] is True


def test_list_recent_codex_sessions_excludes_non_interactive_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recent-import lists only interactive Codex sessions, like Codex's own picker.

    ``exec`` runs and sub-agent threads are automation the user never opened
    interactively (their first message is an injected instruction), so they are
    excluded; a rollout predating the ``source`` field defaults to interactive.
    """
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    sessions = tmp_path / "sessions" / "2026" / "07" / "16"
    sessions.mkdir(parents=True)
    cases = {
        "019f7777-0001-7000-8000-00000000000c": ("cli", 4),
        "019f7777-0001-7000-8000-00000000000e": ("exec", 3),
        "019f7777-0001-7000-8000-00000000000a": ({"subagent": {"review": {}}}, 2),
        "019f7777-0001-7000-8000-00000000000f": (None, 1),  # no source field
    }
    for session_id, (source, modified_at) in cases.items():
        rollout = sessions / f"rollout-2026-07-16T00-00-0{modified_at}-{session_id}.jsonl"
        payload: dict[str, object] = {"id": session_id, "cwd": "/repo"}
        if source is not None:
            payload["source"] = source
        rollout.write_text(
            json.dumps({"type": "session_meta", "payload": payload}) + "\n",
            encoding="utf-8",
        )
        os.utime(rollout, (modified_at, modified_at))

    recent = list_recent_local_session_ids("codex", limit=10)

    # cli (newest) and the source-less legacy rollout only; exec + subagent dropped.
    assert recent == (
        "019f7777-0001-7000-8000-00000000000c",
        "019f7777-0001-7000-8000-00000000000f",
    )


def test_imported_teammate_context_preserves_assistant_response_identity(tmp_path: Path) -> None:
    session_id = "a1b2c3d4-1234-5678-9abc-def012345678"
    transcript = tmp_path / "projects" / "-repo" / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    records = [
        ("assistant", "Before"),
        ("user", '<teammate-message teammate_id="reviewer">Done.</teammate-message>'),
        ("assistant", "After"),
        ("user", "A new question"),
        ("assistant", "A new answer"),
    ]
    transcript.write_text(
        "".join(
            json.dumps(
                {"type": role, "uuid": str(index), "message": {"role": role, "content": text}}
            )
            + "\n"
            for index, (role, text) in enumerate(records)
        ),
        encoding="utf-8",
    )
    imported = load_claude_session(session_id, claude_home=tmp_path)
    assert imported.title == "A new question"
    assert imported.items[1].data.is_meta is True
    assert imported.items[1].data.content[0]["text"] == records[1][1]
    assert not imported.items[3].data.model_dump().get("is_meta")
    assert imported.items[0].response_id == imported.items[2].response_id
    assert imported.items[4].response_id != imported.items[2].response_id


def test_list_recent_claude_sessions_orders_parents_and_applies_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claude batch discovery returns only the newest parent transcripts."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    project = tmp_path / "projects" / "-repo"
    project.mkdir(parents=True)
    transcripts = [
        (project / "old.jsonl", 1),
        (project / "middle.jsonl", 2),
        (project / "new.jsonl", 3),
    ]
    for path, modified_at in transcripts:
        path.touch()
        os.utime(path, (modified_at, modified_at))
    subagent = project / "subagents" / "subagent.jsonl"
    subagent.parent.mkdir()
    subagent.touch()
    os.utime(subagent, (4, 4))

    recent = list_recent_local_session_ids("claude", limit=2)

    assert recent == ("new", "middle")


def test_load_codex_session_normalizes_response_items(tmp_path: Path) -> None:
    """Codex response items retain turn grouping and omit scaffolding."""
    session_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    rollout = (
        tmp_path
        / "sessions"
        / "2026"
        / "07"
        / "15"
        / f"rollout-2026-07-15T12-00-00-{session_id}.jsonl"
    )
    rollout.parent.mkdir(parents=True)
    records = [
        {
            "type": "session_meta",
            "payload": {"id": session_id, "cwd": "/repo"},
        },
        {
            "type": "turn_context",
            "payload": {"turn_id": "turn_1", "cwd": "/repo"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "developer",
                "content": [{"type": "input_text", "text": "internal"}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "<environment_context>\n<cwd>/repo</cwd>\n</environment_context>",
                    }
                ],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "inspect TODO.md"},
                    {"type": "input_image", "image_url": "data:image/png;base64,abc"},
                ],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell",
                "arguments": '{"command":"cat TODO.md"}',
                "call_id": "call_1",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": [
                    {"type": "input_text", "text": "first line\n"},
                    {"type": "input_text", "text": "second line"},
                ],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call",
                "name": "apply_patch",
                "input": "*** Begin Patch",
                "call_id": "call_2",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "call_id": "call_2",
                "output": [{"type": "output_text", "text": ""}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "sleep",
                "namespace": "container",
                "arguments": '{"seconds":2}',
                "call_id": "call_3",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_3",
                "output": "slept",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Done."}],
            },
        },
    ]
    rollout.write_text(
        "".join(f"{json.dumps(record)}\n" for record in records),
        encoding="utf-8",
    )

    imported = load_codex_session(session_id, codex_home=tmp_path)

    assert imported.source == "codex"
    assert imported.workspace == "/repo"
    assert imported.title == "inspect TODO.md"
    assert [item.type for item in imported.items] == [
        "message",
        "message",
        "function_call",
        "function_call_output",
        "function_call",
        "function_call_output",
        "function_call",
        "function_call_output",
        "message",
    ]
    assert {item.response_id for item in imported.items} == {"codex:turn_1"}
    assert imported.items[0].data.model_dump()["is_meta"] is True
    assert imported.items[1].data.model_dump()["content"][1] == {
        "type": "input_image",
        "image_url": "data:image/png;base64,abc",
    }
    assert imported.items[2].data.model_dump() == {
        "agent": "codex-native-ui",
        "name": "shell",
        "arguments": '{"command":"cat TODO.md"}',
        "call_id": "call_1",
    }
    assert imported.items[3].data.model_dump() == {
        "call_id": "call_1",
        "output": "first line\nsecond line",
    }
    assert imported.items[5].data.model_dump() == {
        "call_id": "call_2",
        "output": "",
    }
    # The Responses backend refuses to continue a thread whose replayed
    # namespaced call lost its namespace, so import must keep it.
    assert imported.items[6].data.model_dump() == {
        "agent": "codex-native-ui",
        "name": "sleep",
        "arguments": '{"seconds":2}',
        "call_id": "call_3",
        "namespace": "container",
    }


def _codex_item_texts(imported: LocalSessionImport) -> list[str]:
    """Flatten the text of every message item in an imported Codex session."""
    return [
        block["text"]
        for item in imported.items
        for block in item.data.model_dump().get("content", [])
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    ]


def _write_codex_rollout_with_compaction(
    tmp_path: Path, session_id: str, *, extra_pre: list[dict] | None = None
) -> Path:
    """Write a Codex rollout with a ``compacted`` record between two turns."""
    rollout = (
        tmp_path
        / "sessions"
        / "2026"
        / "07"
        / "15"
        / f"rollout-2026-07-15T12-00-00-{session_id}.jsonl"
    )
    rollout.parent.mkdir(parents=True)
    records: list[dict] = [
        {"type": "session_meta", "payload": {"id": session_id, "cwd": "/repo"}},
        {"type": "turn_context", "payload": {"turn_id": "turn_1", "cwd": "/repo"}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "pre compaction question"}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "pre compaction answer"}],
            },
        },
    ]
    records.extend(extra_pre or [])
    records.append(
        {
            "type": "compacted",
            "payload": {
                "replacement_history": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "compaction summary baseline"}],
                    }
                ],
                "window_id": 1,
            },
        }
    )
    records.extend(
        [
            {"type": "turn_context", "payload": {"turn_id": "turn_2", "cwd": "/repo"}},
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "post compaction question"}],
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "post compaction answer"}],
                },
            },
        ]
    )
    rollout.write_text("".join(f"{json.dumps(record)}\n" for record in records), encoding="utf-8")
    return rollout


def _codex_compaction_baselines(imported: LocalSessionImport) -> list[list[str]]:
    """Return the texts carried by each compaction item's ``compacted_messages``, in order."""
    return [
        [
            block["text"]
            for message in item.data.model_dump()["compacted_messages"]
            for block in message.get("content", [])
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        ]
        for item in imported.items
        if item.type == "compaction"
    ]


def test_load_codex_session_keeps_history_and_records_compaction_boundary(tmp_path: Path) -> None:
    """Every turn stays visible; the ``compacted`` record becomes a compaction item in place."""
    session_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    _write_codex_rollout_with_compaction(tmp_path, session_id)

    imported = load_codex_session(session_id, codex_home=tmp_path)

    assert _codex_item_texts(imported) == [
        "pre compaction question",
        "pre compaction answer",
        "post compaction question",
        "post compaction answer",
    ]
    assert [item.type for item in imported.items] == [
        "message",
        "message",
        "compaction",
        "message",
        "message",
    ]
    # The replacement_history baseline rides on the compaction item, as a live
    # codex-native session persists it, so a cold resume rebuilds the same context.
    assert _codex_compaction_baselines(imported) == [["compaction summary baseline"]]
    assert imported.items[2].data.model_dump()["window_id"] == 1


def test_load_codex_session_carries_compaction_summary_and_window(tmp_path: Path) -> None:
    """The compacted record's ``message`` and uuid ``window_id`` land on the compaction item."""
    session_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    rollout = tmp_path / "sessions" / "2026" / "10" / "02" / f"rollout-x-{session_id}.jsonl"
    rollout.parent.mkdir(parents=True)
    window_id = "01a11f4f-af66-74e3-92a6-6ea6e33fd77d"
    records = [
        {"type": "session_meta", "payload": {"id": session_id, "cwd": "/repo"}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "first question"}],
            },
        },
        {
            "type": "compacted",
            "payload": {
                "message": "Summary of the first question.",
                "replacement_history": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "Summary of the first question."}
                        ],
                    }
                ],
                "window_id": window_id,
            },
        },
    ]
    rollout.write_text("".join(f"{json.dumps(record)}\n" for record in records), encoding="utf-8")

    imported = load_codex_session(session_id, codex_home=tmp_path)

    assert [item.type for item in imported.items] == ["message", "compaction"]
    compaction = imported.items[1].data.model_dump()
    assert compaction["summary"] == "Summary of the first question."
    assert compaction["window_id"] == window_id
    assert imported.title == "first question"


def test_load_codex_session_records_each_compaction_in_order(tmp_path: Path) -> None:
    """With two compactions, the history between them stays and both baselines are kept."""
    session_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    extra_pre = [
        {
            "type": "compacted",
            "payload": {
                "replacement_history": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "first summary baseline"}],
                    }
                ]
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "between compactions"}],
            },
        },
    ]
    _write_codex_rollout_with_compaction(tmp_path, session_id, extra_pre=extra_pre)

    imported = load_codex_session(session_id, codex_home=tmp_path)

    texts = _codex_item_texts(imported)
    assert "pre compaction question" in texts
    assert "between compactions" in texts
    assert "post compaction question" in texts
    assert _codex_compaction_baselines(imported) == [
        ["first summary baseline"],
        ["compaction summary baseline"],
    ]


def test_load_codex_session_skips_compaction_boundary_without_baseline(tmp_path: Path) -> None:
    """A compacted record with no usable replacement_history adds no compaction item."""
    session_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    rollout = (
        tmp_path
        / "sessions"
        / "2026"
        / "07"
        / "15"
        / f"rollout-2026-07-15T12-00-00-{session_id}.jsonl"
    )
    rollout.parent.mkdir(parents=True)
    records = [
        {"type": "session_meta", "payload": {"id": session_id, "cwd": "/repo"}},
        {"type": "turn_context", "payload": {"turn_id": "turn_1", "cwd": "/repo"}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "only real turn"}],
            },
        },
        {"type": "compacted", "payload": {"replacement_history": []}},
    ]
    rollout.write_text("".join(f"{json.dumps(record)}\n" for record in records), encoding="utf-8")

    imported = load_codex_session(session_id, codex_home=tmp_path)

    assert _codex_item_texts(imported) == ["only real turn"]
    assert [item.type for item in imported.items] == ["message"]


def _codex_cold_resume(
    imported: LocalSessionImport, *, cwd: Path
) -> tuple[list[list[str]], list[str]]:
    """Rebuild a Codex rollout from the imported items, as a cold resume does.

    Returns each ``compacted`` record's baseline texts and the message texts replayed after them.
    """
    from omnigent.harnesses.codex_native.main import _codex_rollout_records_from_session_items

    stored_items = [
        {
            "id": f"item_{index}",
            "type": item.type,
            "response_id": item.response_id,
            **item.data.model_dump(mode="json", exclude_none=True),
        }
        for index, item in enumerate(imported.items)
    ]
    records = _codex_rollout_records_from_session_items(
        stored_items,
        session_id="conv_import",
        external_session_id=imported.external_session_id,
        cwd=cwd,
        model_provider="openai",
        cli_version="0.154.0",
    )
    baselines = [
        [entry["content"][0]["text"] for entry in record["payload"]["replacement_history"]]
        for record in records
        if record["type"] == "compacted"
    ]
    replayed = [
        record["payload"]["content"][0]["text"]
        for record in records
        if record["type"] == "response_item" and record["payload"].get("type") == "message"
    ]
    return baselines, replayed


def test_imported_codex_compaction_rebuilds_the_resume_rollout_baseline(tmp_path: Path) -> None:
    """A cold resume rebuilt from the imported items restarts at the compaction baseline."""
    session_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    _write_codex_rollout_with_compaction(tmp_path, session_id)
    imported = load_codex_session(session_id, codex_home=tmp_path)

    baselines, replayed = _codex_cold_resume(imported, cwd=tmp_path)

    assert baselines == [["compaction summary baseline"]]
    assert replayed == ["post compaction question", "post compaction answer"]


def test_load_codex_session_keeps_full_history_past_two_mb(tmp_path: Path) -> None:
    """A multi-megabyte compacted rollout still imports every turn; size never trims it."""
    session_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    filler = "x" * 4096
    extra_pre = [
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": f"pre bulk {i} {filler}"}],
            },
        }
        for i in range(600)
    ]
    rollout = _write_codex_rollout_with_compaction(tmp_path, session_id, extra_pre=extra_pre)
    assert rollout.stat().st_size > 2 * 1024 * 1024

    imported = load_codex_session(session_id, codex_home=tmp_path)

    texts = _codex_item_texts(imported)
    assert sum(text.startswith("pre bulk") for text in texts) == 600
    assert "pre compaction question" in texts
    assert "post compaction question" in texts
    assert _codex_compaction_baselines(imported) == [["compaction summary baseline"]]


def _write_codex_rollout(tmp_path: Path, session_id: str, *, first_message: str) -> None:
    """Write a minimal importable Codex rollout with one user message."""
    rollout = (
        tmp_path
        / "sessions"
        / "2026"
        / "07"
        / "15"
        / f"rollout-2026-07-15T12-00-00-{session_id}.jsonl"
    )
    rollout.parent.mkdir(parents=True)
    records = [
        {"type": "session_meta", "payload": {"id": session_id, "cwd": "/repo"}},
        {"type": "turn_context", "payload": {"turn_id": "turn_1", "cwd": "/repo"}},
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": first_message}],
            },
        },
    ]
    rollout.write_text("".join(f"{json.dumps(record)}\n" for record in records), encoding="utf-8")


def _write_codex_threads_db(
    tmp_path: Path,
    session_id: str,
    *,
    title: str,
    first_user_message: str,
    rollout_path: Path | None = None,
    archived: bool = False,
    legacy_schema: bool = False,
) -> None:
    """Write a codex ``state_5.sqlite`` holding one thread's row."""
    write_codex_thread_store(
        tmp_path,
        [CodexThreadRow(session_id, title, first_user_message, rollout_path, archived)],
        legacy_schema=legacy_schema,
    )


def _write_codex_turns(
    path: Path,
    session_id: str,
    turns: list[tuple[str, str]],
    *,
    start_ordinal: int = 0,
    **meta: object,
) -> int:
    """Write paginated Codex turns from ``start_ordinal``; return the next ordinal."""
    rollout = CodexRollout(session_id, start_ordinal=start_ordinal, **{"cwd": "/repo", **meta})
    for index, (question, answer) in enumerate(turns, start=1):
        rollout.turn(index, question, answer)
    rollout.write(path)
    return rollout.next_ordinal


@pytest.mark.parametrize(
    "home_name", ["codex", "codex?query", "codex#fragment", "codex%3F", "co dex"]
)
def test_load_codex_session_reads_rollout_named_by_threads_rollout_path(
    tmp_path: Path, home_name: str
) -> None:
    """The state DB's rollout_path wins over an older rollout whose filename matches the id.

    URI metacharacters in the Codex home path must not break the read-only state DB open.
    """
    session_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    home = tmp_path / home_name
    sessions = home / "sessions" / "2026" / "10" / "02"
    _write_codex_turns(
        sessions / f"rollout-2026-10-02T09-00-00-{session_id}.jsonl",
        session_id,
        [("question 1", "answer 1"), ("question 2", "answer 2")],
    )
    current = sessions / "rollout-2026-10-02T09-30-00-019f680e-3edc-7fa3-9d50-1c4be395fa27.jsonl"
    _write_codex_turns(
        current,
        session_id,
        [("question 1", "answer 1"), ("question 2", "answer 2"), ("question 3", "final answer")],
    )
    _write_codex_threads_db(
        home,
        session_id,
        title="question 1",
        first_user_message="question 1",
        rollout_path=current,
    )

    imported = load_codex_session(session_id, codex_home=home)

    assert _codex_item_texts(imported)[-1] == "final answer"
    # A misparsed URI would open (and create) a database at the truncated path.
    assert sorted(path.name for path in tmp_path.iterdir()) == [home_name]


@pytest.mark.parametrize("base_has_ordinals", [True, False], ids=["paginated", "legacy-base"])
def test_load_codex_session_follows_history_base_of_forked_thread(
    tmp_path: Path, base_has_ordinals: bool
) -> None:
    """A fork child inherits the parent records before ``history_base.end_ordinal_exclusive``.

    The base turn that starts at the cutoff is excluded. ``end_byte_offset`` only lets Codex
    check that the base file still holds the prefix, so its value here is deliberately unrelated.
    A base written without ``ordinal`` fields is cut at the same line position.
    """
    parent_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    child_id = "019f680e-3edc-7fa3-9d50-1c4be395fa27"
    sessions = tmp_path / "sessions" / "2026" / "10" / "02"
    parent = sessions / f"rollout-2026-10-02T10-00-00-{parent_id}.jsonl"
    _write_codex_turns(
        parent,
        parent_id,
        [("parent question 1", "parent answer 1"), ("after the fork", "after the fork")],
    )
    if not base_has_ordinals:
        records = [json.loads(line) for line in parent.read_text().splitlines()]
        parent.write_text(
            "".join(
                json.dumps({k: v for k, v in r.items() if k != "ordinal"}) + "\n" for r in records
            ),
            encoding="utf-8",
        )
    cutoff = _codex_turn_end(0, 1)
    _write_codex_turns(
        sessions / f"rollout-2026-10-02T10-30-00-{child_id}.jsonl",
        child_id,
        [("child question", "child answer")],
        start_ordinal=cutoff,
        forked_from_id=parent_id,
        forked_from_ordinal_exclusive=cutoff,
        history_base={
            "thread_id": parent_id,
            "end_ordinal_exclusive": cutoff,
            "end_byte_offset": 0,
        },
    )

    imported = load_codex_session(child_id, codex_home=tmp_path)

    assert _codex_item_texts(imported) == [
        "parent question 1",
        "parent answer 1",
        "child question",
        "child answer",
    ]


def test_load_codex_session_keeps_a_compaction_inherited_from_the_fork_base(
    tmp_path: Path,
) -> None:
    """A base compaction before the fork cutoff is imported, and cold resume starts from it."""
    parent_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    child_id = "019f680e-3edc-7fa3-9d50-1c4be395fa27"
    sessions = tmp_path / "sessions" / "2026" / "10" / "02"
    parent = CodexRollout(parent_id, cwd="/repo")
    parent.turn(1, "parent question 1", "parent answer 1")
    parent.append(
        "compacted",
        {
            "message": "summary of turn 1",
            "replacement_history": [codex_message("user", "summary of turn 1")],
        },
    )
    parent.turn(2, "parent question 2", "parent answer 2")
    cutoff = parent.next_ordinal
    parent.turn(3, "after the fork", "after the fork")
    parent.write(sessions / f"rollout-2026-10-02T10-00-00-{parent_id}.jsonl")
    child = CodexRollout(
        child_id,
        cwd="/repo",
        history_base={"thread_id": parent_id, "end_ordinal_exclusive": cutoff},
    )
    child.turn(1, "child question", "child answer")
    child.write(sessions / f"rollout-2026-10-02T10-30-00-{child_id}.jsonl")

    imported = load_codex_session(child_id, codex_home=tmp_path)

    assert _codex_item_texts(imported) == [
        "parent question 1",
        "parent answer 1",
        "parent question 2",
        "parent answer 2",
        "child question",
        "child answer",
    ]
    assert _codex_compaction_baselines(imported) == [["summary of turn 1"]]
    baselines, replayed = _codex_cold_resume(imported, cwd=tmp_path)
    assert baselines == [["summary of turn 1"]]
    assert replayed == ["parent question 2", "parent answer 2", "child question", "child answer"]


def test_load_codex_session_falls_back_to_filename_when_rollout_path_is_stale(
    tmp_path: Path,
) -> None:
    """A rollout_path whose file is gone still resolves the thread by filename."""
    session_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    sessions = tmp_path / "sessions" / "2026" / "10" / "02"
    _write_codex_turns(
        sessions / f"rollout-2026-10-02T09-00-00-{session_id}.jsonl",
        session_id,
        [("question 1", "answer 1")],
    )
    _write_codex_threads_db(
        tmp_path,
        session_id,
        title="question 1",
        first_user_message="question 1",
        rollout_path=sessions / "rollout-2026-10-02T09-30-00-gone.jsonl",
    )

    imported = load_codex_session(session_id, codex_home=tmp_path)

    assert _codex_item_texts(imported) == ["question 1", "answer 1"]


def test_load_codex_session_imports_metadata_only_fork_from_its_base(tmp_path: Path) -> None:
    """A fork that has not taken a turn yet imports the history it inherits."""
    parent_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    child_id = "019f680e-3edc-7fa3-9d50-1c4be395fa27"
    sessions = tmp_path / "sessions" / "2026" / "10" / "02"
    parent_end = _write_codex_turns(
        sessions / f"rollout-2026-10-02T10-00-00-{parent_id}.jsonl",
        parent_id,
        [("parent question 1", "parent answer 1")],
    )
    _write_codex_turns(
        sessions / f"rollout-2026-10-02T10-30-00-{child_id}.jsonl",
        child_id,
        [],
        start_ordinal=parent_end,
        cwd="/fork",
        history_base={
            "thread_id": parent_id,
            "end_ordinal_exclusive": parent_end,
            "end_byte_offset": 0,
        },
    )

    imported = load_codex_session(child_id, codex_home=tmp_path)

    assert _codex_item_texts(imported) == ["parent question 1", "parent answer 1"]
    # The fork's own cwd wins over the inherited session_meta.
    assert imported.workspace == "/fork"
    assert imported.title == "parent question 1"


def test_load_codex_session_follows_fork_lineage_through_intermediate_forks(
    tmp_path: Path,
) -> None:
    """A fork of a fork inherits the whole lineage, each cut at its history_base ordinal."""
    root_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    middle_id = "019f680e-3edc-7fa3-9d50-1c4be395fa27"
    leaf_id = "01a11f4f-af66-74e3-92a6-6e9c14bf68a2"
    sessions = tmp_path / "sessions" / "2026" / "10" / "02"

    def fork_meta(base_id: str, end: int) -> dict[str, object]:
        return {
            "history_base": {"thread_id": base_id, "end_ordinal_exclusive": end},
        }

    root_end = _write_codex_turns(
        sessions / f"rollout-2026-10-02T10-00-00-{root_id}.jsonl",
        root_id,
        [("root question", "root answer")],
    )
    middle_end = _write_codex_turns(
        sessions / f"rollout-2026-10-02T10-30-00-{middle_id}.jsonl",
        middle_id,
        [("middle question", "middle answer")],
        start_ordinal=root_end,
        **fork_meta(root_id, root_end),
    )
    _write_codex_turns(
        sessions / f"rollout-2026-10-02T11-00-00-{leaf_id}.jsonl",
        leaf_id,
        [("leaf question", "leaf answer")],
        start_ordinal=middle_end,
        **fork_meta(middle_id, middle_end),
    )

    imported = load_codex_session(leaf_id, codex_home=tmp_path)

    assert _codex_item_texts(imported) == [
        "root question",
        "root answer",
        "middle question",
        "middle answer",
        "leaf question",
        "leaf answer",
    ]


def _codex_turn_end(start_ordinal: int, turns: int) -> int:
    """Exclusive ordinal after ``turns`` turns of a rollout written by ``_write_codex_turns``."""
    return start_ordinal + 1 + 3 * turns


@pytest.mark.parametrize("with_thread_store", [True, False])
def test_load_codex_session_keeps_history_from_before_a_revert(
    tmp_path: Path, with_thread_store: bool
) -> None:
    """A reverted thread's new rollout inherits the original rollout up to the revert point."""
    thread_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    rollout_id = "019f680e-3edc-7fa3-9d50-1c4be395fa27"
    sessions = tmp_path / "sessions" / "2026" / "10" / "02"
    original = sessions / f"rollout-2026-10-02T09-00-00-{thread_id}.jsonl"
    _write_codex_turns(
        original,
        thread_id,
        [("question 1", "answer 1"), ("question 2", "answer 2"), ("dropped", "dropped")],
    )
    revert_point = _codex_turn_end(0, 2)
    reverted = sessions / f"rollout-2026-10-02T09-30-00-{thread_id}_{rollout_id}.jsonl"
    _write_codex_turns(
        reverted,
        thread_id,
        [("question after revert", "answer after revert")],
        start_ordinal=revert_point,
        history_base={"thread_id": thread_id, "end_ordinal_exclusive": revert_point},
    )
    os.utime(original, (1, 1))
    if with_thread_store:
        _write_codex_threads_db(
            tmp_path,
            thread_id,
            title="question 1",
            first_user_message="question 1",
            rollout_path=reverted,
        )

    imported = load_codex_session(thread_id, codex_home=tmp_path)

    assert _codex_item_texts(imported) == [
        "question 1",
        "answer 1",
        "question 2",
        "answer 2",
        "question after revert",
        "answer after revert",
    ]


def test_load_codex_session_reads_fork_base_by_rollout_id_after_base_revert(
    tmp_path: Path,
) -> None:
    """A fork inherits the base rollout it names, not the base thread's later revert rollout."""
    parent_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    revert_rollout_id = "01a11f4f-af66-74e3-92a6-6e9c14bf68a2"
    child_id = "019f680e-3edc-7fa3-9d50-1c4be395fa27"
    sessions = tmp_path / "sessions" / "2026" / "10" / "02"
    parent_end = _write_codex_turns(
        sessions / f"rollout-2026-10-02T10-00-00-{parent_id}.jsonl",
        parent_id,
        [("parent question 1", "parent answer 1"), ("parent question 2", "parent answer 2")],
    )
    _write_codex_turns(
        sessions / f"rollout-2026-10-02T10-30-00-{child_id}.jsonl",
        child_id,
        [("child question", "child answer")],
        start_ordinal=parent_end,
        history_base={"thread_id": parent_id, "end_ordinal_exclusive": parent_end},
    )
    # The parent is reverted to its first turn after the fork and continues elsewhere.
    revert_point = _codex_turn_end(0, 1)
    parent_current = (
        sessions / f"rollout-2026-10-02T11-00-00-{parent_id}_{revert_rollout_id}.jsonl"
    )
    _write_codex_turns(
        parent_current,
        parent_id,
        [("parent question after revert", "parent answer after revert")],
        start_ordinal=revert_point,
        history_base={"thread_id": parent_id, "end_ordinal_exclusive": revert_point},
    )
    _write_codex_threads_db(
        tmp_path,
        parent_id,
        title="parent question 1",
        first_user_message="parent question 1",
        rollout_path=parent_current,
    )

    imported = load_codex_session(child_id, codex_home=tmp_path)

    assert _codex_item_texts(imported) == [
        "parent question 1",
        "parent answer 1",
        "parent question 2",
        "parent answer 2",
        "child question",
        "child answer",
    ]


def test_load_codex_session_caps_nested_fork_cutoffs_at_the_outer_cutoff(
    tmp_path: Path,
) -> None:
    """A cutoff inside a base's own inherited range also bounds that base's ancestors."""
    root_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    middle_id = "019f680e-3edc-7fa3-9d50-1c4be395fa27"
    leaf_id = "01a11f4f-af66-74e3-92a6-6e9c14bf68a2"
    sessions = tmp_path / "sessions" / "2026" / "10" / "02"
    root_end = _write_codex_turns(
        sessions / f"rollout-2026-10-02T10-00-00-{root_id}.jsonl",
        root_id,
        [("root question 1", "root answer 1"), ("root question 2", "root answer 2")],
    )
    _write_codex_turns(
        sessions / f"rollout-2026-10-02T10-30-00-{middle_id}.jsonl",
        middle_id,
        [("middle question", "middle answer")],
        start_ordinal=root_end,
        history_base={"thread_id": root_id, "end_ordinal_exclusive": root_end},
    )
    leaf_cutoff = _codex_turn_end(0, 1)
    _write_codex_turns(
        sessions / f"rollout-2026-10-02T11-00-00-{leaf_id}.jsonl",
        leaf_id,
        [("leaf question", "leaf answer")],
        start_ordinal=leaf_cutoff,
        history_base={"thread_id": middle_id, "end_ordinal_exclusive": leaf_cutoff},
    )

    imported = load_codex_session(leaf_id, codex_home=tmp_path)

    assert _codex_item_texts(imported) == [
        "root question 1",
        "root answer 1",
        "leaf question",
        "leaf answer",
    ]


def test_load_codex_session_stops_following_a_history_base_cycle(tmp_path: Path) -> None:
    """Rollouts whose ``history_base`` pointers form a cycle import each rollout once."""
    first_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    second_id = "019f680e-3edc-7fa3-9d50-1c4be395fa27"
    sessions = tmp_path / "sessions" / "2026" / "10" / "02"
    cutoff = _codex_turn_end(0, 1)
    _write_codex_turns(
        sessions / f"rollout-2026-10-02T10-00-00-{second_id}.jsonl",
        second_id,
        [("second question", "second answer")],
        history_base={"thread_id": first_id, "end_ordinal_exclusive": cutoff},
    )
    _write_codex_turns(
        sessions / f"rollout-2026-10-02T10-30-00-{first_id}.jsonl",
        first_id,
        [("first question", "first answer")],
        start_ordinal=cutoff,
        history_base={"thread_id": second_id, "end_ordinal_exclusive": cutoff},
    )

    imported = load_codex_session(first_id, codex_home=tmp_path)

    assert _codex_item_texts(imported) == [
        "second question",
        "second answer",
        "first question",
        "first answer",
    ]


@pytest.mark.parametrize("cutoff", [True, 0, -1, "7", None])
def test_load_codex_session_ignores_a_history_base_with_an_invalid_cutoff(
    tmp_path: Path, cutoff: object
) -> None:
    """A non-positive or non-integer ``end_ordinal_exclusive`` inherits nothing."""
    parent_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    child_id = "019f680e-3edc-7fa3-9d50-1c4be395fa27"
    sessions = tmp_path / "sessions" / "2026" / "10" / "02"
    _write_codex_turns(
        sessions / f"rollout-2026-10-02T10-00-00-{parent_id}.jsonl",
        parent_id,
        [("parent question", "parent answer")],
    )
    history_base: dict[str, object] = {"thread_id": parent_id}
    if cutoff is not None:
        history_base["end_ordinal_exclusive"] = cutoff
    _write_codex_turns(
        sessions / f"rollout-2026-10-02T10-30-00-{child_id}.jsonl",
        child_id,
        [("child question", "child answer")],
        history_base=history_base,
    )

    imported = load_codex_session(child_id, codex_home=tmp_path)

    assert _codex_item_texts(imported) == ["child question", "child answer"]


def test_load_codex_session_imports_fork_alone_when_base_rollout_is_missing(
    tmp_path: Path,
) -> None:
    """A fork whose base thread is gone from this machine still imports its own turns."""
    child_id = "019f680e-3edc-7fa3-9d50-1c4be395fa27"
    sessions = tmp_path / "sessions" / "2026" / "10" / "02"
    _write_codex_turns(
        sessions / f"rollout-2026-10-02T10-30-00-{child_id}.jsonl",
        child_id,
        [("child question", "child answer")],
        start_ordinal=10,
        history_base={
            "thread_id": "019e96aa-0be2-7343-8d3b-6f914d60936b",
            "end_ordinal_exclusive": 10,
        },
    )

    imported = load_codex_session(child_id, codex_home=tmp_path)

    assert _codex_item_texts(imported) == ["child question", "child answer"]


def test_load_codex_session_reports_archived_state_from_thread_store(tmp_path: Path) -> None:
    """``threads.archived`` marks the import archived; an active thread stays active."""
    session_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    for home, archived in ((tmp_path / "archived", True), (tmp_path / "active", False)):
        rollout = home / "sessions" / "2026" / "10" / "02" / f"rollout-x-{session_id}.jsonl"
        _write_codex_turns(rollout, session_id, [("question", "answer")])
        _write_codex_threads_db(
            home,
            session_id,
            title="question",
            first_user_message="question",
            rollout_path=rollout,
            archived=archived,
        )

        imported = load_codex_session(session_id, codex_home=home)

        assert imported.archived is archived


@pytest.mark.parametrize("legacy_schema", [False, True], ids=["current", "legacy-schema"])
def test_load_codex_session_uses_custom_thread_title(tmp_path: Path, legacy_schema: bool) -> None:
    """A renamed Codex thread (title != first message) carries its custom name.

    An older thread store without ``rollout_path``/``archived`` still supplies the title;
    the rollout then comes from the filename and the thread is not archived.
    """
    session_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    _write_codex_rollout(tmp_path, session_id, first_message="inspect TODO.md")
    _write_codex_threads_db(
        tmp_path,
        session_id,
        title="my renamed thread",
        first_user_message="inspect TODO.md",
        legacy_schema=legacy_schema,
    )

    imported = load_codex_session(session_id, codex_home=tmp_path)

    assert imported.native_title == "my renamed thread"
    assert imported.title == "my renamed thread"
    assert _codex_item_texts(imported) == ["inspect TODO.md"]
    assert imported.archived is False


def test_load_codex_session_ignores_auto_thread_title(tmp_path: Path) -> None:
    """An un-renamed thread (title == first message) synthesizes from items instead."""
    session_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    _write_codex_rollout(tmp_path, session_id, first_message="inspect TODO.md")
    _write_codex_threads_db(
        tmp_path, session_id, title="inspect TODO.md", first_user_message="inspect TODO.md"
    )

    imported = load_codex_session(session_id, codex_home=tmp_path)

    assert imported.native_title is None
    assert imported.title == "inspect TODO.md"


def test_load_codex_session_uses_session_index_rename(tmp_path: Path) -> None:
    """A rename lives in session_index.jsonl even when threads.title still lags.

    Renaming a Codex thread records ``thread_name`` in ``session_index.jsonl``
    while ``threads.title`` can keep the original first message — so the index
    must win.
    """
    session_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    _write_codex_rollout(tmp_path, session_id, first_message="inspect TODO.md")
    _write_codex_threads_db(
        tmp_path, session_id, title="inspect TODO.md", first_user_message="inspect TODO.md"
    )
    # An earlier auto entry, then the user's rename — last entry for the id wins.
    (tmp_path / "session_index.jsonl").write_text(
        json.dumps({"id": session_id, "thread_name": "inspect TODO.md"})
        + "\n"
        + json.dumps({"id": session_id, "thread_name": "my-renamed-thread"})
        + "\n",
        encoding="utf-8",
    )

    imported = load_codex_session(session_id, codex_home=tmp_path)

    assert imported.native_title == "my-renamed-thread"
    assert imported.title == "my-renamed-thread"


def test_load_codex_session_finds_archived_rollout(tmp_path: Path) -> None:
    """Archived Codex sessions remain importable by their original id."""
    session_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    rollout = tmp_path / "archived_sessions" / f"rollout-2026-07-15-{session_id}.jsonl"
    rollout.parent.mkdir()
    rollout.write_text(
        "".join(
            [
                json.dumps(
                    {"type": "session_meta", "payload": {"id": session_id, "cwd": "/repo"}}
                ),
                "\n",
                json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": "archived prompt"}],
                        },
                    }
                ),
                "\n",
            ]
        ),
        encoding="utf-8",
    )

    imported = load_codex_session(session_id, codex_home=tmp_path)

    assert imported.workspace == "/repo"
    assert imported.title == "archived prompt"
    assert imported.archived is True


def test_load_codex_session_rejects_empty_history(tmp_path: Path) -> None:
    """A structurally present but unreadable history must not claim an import."""
    session_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    rollout = tmp_path / "sessions" / "2026" / "07" / "15" / f"rollout-x-{session_id}.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text("not-json\n", encoding="utf-8")

    with pytest.raises(SessionImportNotFoundError, match="no importable history"):
        load_codex_session(session_id, codex_home=tmp_path)


def test_list_recent_codex_sessions_includes_archived_and_deduplicates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex batch discovery combines active and archived rollout identities."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    first_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    second_id = "019f680e-3edc-7fa3-9d50-1c4be395fa27"
    active_dir = tmp_path / "sessions" / "2026" / "07" / "16"
    archived_dir = tmp_path / "archived_sessions"
    active_dir.mkdir(parents=True)
    archived_dir.mkdir()
    rollouts = [
        (active_dir / f"rollout-old-{first_id}.jsonl", 1),
        (active_dir / f"rollout-new-{second_id}.jsonl", 3),
        (archived_dir / f"rollout-archived-{first_id}.jsonl", 4),
        (active_dir / "rollout-malformed.jsonl", 5),
    ]
    for path, modified_at in rollouts:
        path.touch()
        os.utime(path, (modified_at, modified_at))

    recent = list_recent_local_session_ids("codex", limit=10)

    assert recent == (first_id, second_id)


def test_list_recent_codex_sessions_lists_a_reverted_thread_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reverted thread's ``<thread>_<rollout>`` file lists under the thread id, and loads.

    Without a thread-store row the loader picks the newest file for that thread id, as Codex does.
    """
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    thread_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    other_id = "019f680e-3edc-7fa3-9d50-1c4be395fa27"
    rollout_id = "01a11f4f-af66-74e3-92a6-6e9c14bf68a2"
    sessions = tmp_path / "sessions" / "2026" / "10" / "02"
    stale = sessions / f"rollout-2026-10-02T09-00-00-{thread_id}.jsonl"
    other = sessions / f"rollout-2026-10-02T09-10-00-{other_id}.jsonl"
    current = sessions / f"rollout-2026-10-02T09-30-00-{thread_id}_{rollout_id}.jsonl"
    for path, session_id, answer, modified_at in (
        (stale, thread_id, "stale answer", 1),
        (other, other_id, "other answer", 2),
        (current, thread_id, "current answer", 3),
    ):
        _write_codex_turns(path, session_id, [("question", answer)])
        os.utime(path, (modified_at, modified_at))

    recent = list_recent_local_session_ids("codex", limit=10)

    # The current file's rollout id never surfaces as a separate session.
    assert recent == (thread_id, other_id)
    assert _codex_item_texts(load_codex_session(thread_id, codex_home=tmp_path)) == [
        "question",
        "current answer",
    ]


def test_list_recent_codex_sessions_uses_the_id_the_loader_resolves(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rollout whose filename uuid differs from ``session_meta.id`` lists under the former."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    filename_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    sessions = tmp_path / "sessions" / "2026" / "10" / "02"
    _write_codex_turns(
        sessions / f"rollout-2026-10-02T09-00-00-{filename_id}.jsonl",
        "019f680e-3edc-7fa3-9d50-1c4be395fa27",
        [("question", "answer")],
    )
    # Without a thread id in its name the loader cannot find it, so it is not listed.
    _write_codex_turns(
        sessions / "rollout-oddname.jsonl",
        "01a11f4f-af66-74e3-92a6-6e9c14bf68a2",
        [("unlisted question", "unlisted answer")],
    )

    recent = list_recent_local_session_ids("codex", limit=10)

    assert recent == (filename_id,)
    assert _codex_item_texts(load_codex_session(filename_id, codex_home=tmp_path)) == [
        "question",
        "answer",
    ]


def test_load_qwen_session_normalizes_recorded_messages(tmp_path: Path) -> None:
    """A Qwen recording imports its visible user and assistant messages."""
    session_id = "019f8648-2797-7170-bf73-837f2655c47e"
    transcript = tmp_path / "projects" / "-repo" / "chats" / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    records = [
        {
            "uuid": "user-1",
            "sessionId": session_id,
            "type": "user",
            "cwd": "/repo",
            "message": {"role": "user", "parts": [{"text": "inspect TODO.md"}]},
        },
        {
            "uuid": "assistant-1",
            "sessionId": session_id,
            "type": "assistant",
            "cwd": "/repo",
            "message": {"role": "model", "parts": [{"text": "Done."}]},
        },
    ]
    transcript.write_text(
        "".join(f"{json.dumps(record)}\n" for record in records),
        encoding="utf-8",
    )

    imported = load_qwen_session(session_id, qwen_home=tmp_path)

    assert imported.source == "qwen"
    assert imported.external_session_id == f"-repo:{session_id}"
    assert imported.workspace == "/repo"
    assert imported.title == "inspect TODO.md"
    assert [item.data.model_dump()["role"] for item in imported.items] == [
        "user",
        "assistant",
    ]
    assert imported.items[1].data.model_dump()["agent"] == "qwen-native-ui"


def test_load_qwen_session_follows_the_current_branch(tmp_path: Path) -> None:
    """Qwen import excludes stale siblings from its linked recording."""
    session_id = "019f8648-2797-7170-bf73-837f2655c47e"
    transcript = tmp_path / "projects" / "-repo" / "chats" / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    records = [
        {
            "uuid": "root-user",
            "parentUuid": None,
            "type": "user",
            "cwd": "/repo",
            "message": {"parts": [{"text": "start"}]},
        },
        {
            "uuid": "stale-assistant",
            "parentUuid": "root-user",
            "type": "assistant",
            "message": {"parts": [{"text": "stale answer"}]},
        },
        {
            "uuid": "active-user",
            "parentUuid": "root-user",
            "type": "user",
            "message": {"parts": [{"text": "try again"}]},
        },
        {
            "uuid": "active-assistant",
            "parentUuid": "active-user",
            "type": "assistant",
            "message": {"parts": [{"text": "active answer"}]},
        },
    ]
    transcript.write_text(
        "".join(f"{json.dumps(record)}\n" for record in records), encoding="utf-8"
    )

    imported = load_qwen_session(session_id, qwen_home=tmp_path)

    assert [item.data.model_dump()["content"][0]["text"] for item in imported.items] == [
        "start",
        "try again",
        "active answer",
    ]


@pytest.mark.parametrize(
    "records",
    [
        [
            {
                "uuid": "duplicate",
                "parentUuid": None,
                "type": "user",
                "message": {"parts": [{"text": "first"}]},
            },
            {
                "uuid": "duplicate",
                "parentUuid": None,
                "type": "assistant",
                "message": {"parts": [{"text": "second"}]},
            },
        ],
        [
            {
                "uuid": "orphan",
                "parentUuid": "missing",
                "type": "user",
                "message": {"parts": [{"text": "partial"}]},
            }
        ],
    ],
)
def test_load_qwen_session_rejects_malformed_links(
    tmp_path: Path,
    records: list[dict[str, object]],
) -> None:
    """Malformed Qwen links cannot create a permanently partial import."""
    session_id = "019f8648-2797-7170-bf73-837f2655c47e"
    transcript = tmp_path / "projects" / "-repo" / "chats" / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        "".join(f"{json.dumps(record)}\n" for record in records), encoding="utf-8"
    )

    with pytest.raises(SessionImportNotFoundError, match="no importable history"):
        load_qwen_session(session_id, qwen_home=tmp_path)


def test_load_qwen_session_qualifies_ambiguous_project_id(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Project-qualified Qwen locators keep duplicate native ids importable."""
    session_id = "019f8648-2797-7170-bf73-837f2655c47e"
    monkeypatch.setenv("QWEN_HOME", str(tmp_path))
    for project in ("-repo-a", "-repo-b"):
        transcript = tmp_path / "projects" / project / "chats" / f"{session_id}.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text(
            json.dumps(
                {
                    "uuid": f"user-{project}",
                    "type": "user",
                    "message": {"parts": [{"text": project}]},
                }
            )
            + "\n",
            encoding="utf-8",
        )

    with pytest.raises(SessionImportNotFoundError, match="ambiguous; use one of"):
        load_qwen_session(session_id, qwen_home=tmp_path)
    locators = list_recent_local_session_ids("qwen", limit=10)
    assert set(locators) == {f"-repo-a:{session_id}", f"-repo-b:{session_id}"}
    imported = load_qwen_session(f"-repo-a:{session_id}", qwen_home=tmp_path)
    assert imported.external_session_id == f"-repo-a:{session_id}"
    assert imported.title == "-repo-a"


def test_list_recent_qwen_sessions_scans_projects(tmp_path: Path, monkeypatch) -> None:
    """Qwen batch discovery returns the newest recordings across projects."""
    monkeypatch.setenv("QWEN_HOME", str(tmp_path))
    recordings = [
        (tmp_path / "projects" / "-old" / "chats" / "old.jsonl", 1),
        (tmp_path / "projects" / "-new" / "chats" / "new.jsonl", 3),
        (tmp_path / "projects" / "-middle" / "chats" / "middle.jsonl", 2),
    ]
    for path, modified_at in recordings:
        path.parent.mkdir(parents=True)
        path.touch()
        os.utime(path, (modified_at, modified_at))

    recent = list_recent_local_session_ids("qwen", limit=2)

    assert recent == ("-new:new", "-middle:middle")


def test_qwen_locator_bounds_an_overlong_session_stem(tmp_path: Path, monkeypatch) -> None:
    """Canonical Qwen identity always fits the import API's 128-char limit."""
    monkeypatch.setenv("QWEN_HOME", str(tmp_path))
    session_id = "s" * 180
    transcript = tmp_path / "projects" / "-repo" / "chats" / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps(
            {
                "uuid": "user-1",
                "type": "user",
                "message": {"parts": [{"text": "hello"}]},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    (locator,) = list_recent_local_session_ids("qwen", limit=1)
    imported = load_qwen_session(locator, qwen_home=tmp_path)

    assert len(locator) <= 128
    assert imported.external_session_id == locator


def test_load_kiro_session_uses_metadata_and_visible_messages(tmp_path: Path) -> None:
    """A Kiro session imports JSONL messages with workspace metadata."""
    session_id = "kiro-session-1"
    sessions = tmp_path / ".kiro" / "sessions" / "cli"
    sessions.mkdir(parents=True)
    (sessions / f"{session_id}.json").write_text(
        json.dumps({"cwd": "/repo", "created_at": "2026-07-21T12:00:00Z"}),
        encoding="utf-8",
    )
    records = [
        {
            "kind": "Prompt",
            "data": {
                "message_id": "user-1",
                "content": [{"kind": "text", "data": "inspect TODO.md"}],
            },
        },
        {
            "kind": "AssistantMessage",
            "data": {
                "message_id": "assistant-1",
                "content": [{"kind": "text", "data": "Done."}],
            },
        },
    ]
    (sessions / f"{session_id}.jsonl").write_text(
        "\n".join(json.dumps(record) for record in records),
        encoding="utf-8",
    )

    imported = load_kiro_session(session_id, kiro_home=tmp_path)

    assert imported.source == "kiro"
    assert imported.workspace == "/repo"
    assert imported.title == "inspect TODO.md"
    assert [item.data.model_dump()["role"] for item in imported.items] == [
        "user",
        "assistant",
    ]
    assert imported.items[1].data.model_dump()["agent"] == "kiro-native-ui"


def test_list_recent_kiro_sessions_requires_metadata(tmp_path: Path, monkeypatch) -> None:
    """Kiro batch discovery orders complete metadata/transcript pairs."""
    monkeypatch.setenv("HOME", str(tmp_path))
    sessions = tmp_path / ".kiro" / "sessions" / "cli"
    sessions.mkdir(parents=True)
    for session_id, modified_at in (("old", 1), ("new", 3)):
        (sessions / f"{session_id}.json").write_text(
            json.dumps({"cwd": "/repo"}), encoding="utf-8"
        )
        transcript = sessions / f"{session_id}.jsonl"
        transcript.touch()
        os.utime(transcript, (modified_at, modified_at))
    incomplete = sessions / "incomplete.jsonl"
    incomplete.touch()
    os.utime(incomplete, (4, 4))

    recent = list_recent_local_session_ids("kiro", limit=10)

    assert recent == ("new", "old")


def test_load_pi_session_follows_the_current_branch(tmp_path: Path) -> None:
    """Pi import follows parent links from the last entry instead of stale branches."""
    session_id = "019f8648-2797-7170-bf73-837f2655c47e"
    transcript = tmp_path / "sessions" / "--repo--" / f"stamp_{session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    records = [
        {"type": "session", "version": 3, "id": session_id, "cwd": "/repo"},
        {
            "type": "message",
            "id": "root-user",
            "parentId": None,
            "message": {"role": "user", "content": [{"type": "text", "text": "start"}]},
        },
        {
            "type": "message",
            "id": "stale-assistant",
            "parentId": "root-user",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "stale branch"}],
            },
        },
        {
            "type": "message",
            "id": "active-user",
            "parentId": "root-user",
            "message": {"role": "user", "content": "take another approach"},
        },
        {
            "type": "message",
            "id": "active-assistant",
            "parentId": "active-user",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "active answer"}],
            },
        },
    ]
    transcript.write_text(
        "".join(f"{json.dumps(record)}\n" for record in records), encoding="utf-8"
    )

    imported = load_pi_session(session_id, pi_home=tmp_path)

    assert imported.source == "pi"
    assert imported.workspace == "/repo"
    assert [item.data.model_dump()["content"][0]["text"] for item in imported.items] == [
        "start",
        "take another approach",
        "active answer",
    ]


def test_load_pi_session_preserves_tool_calls_and_results(tmp_path: Path) -> None:
    """Pi assistant tool blocks and tool results remain ordinary tool items."""
    session_id = "019f8648-2797-7170-bf73-837f2655c47e"
    transcript = tmp_path / "sessions" / "--repo--" / f"stamp_{session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    records = [
        {"type": "session", "version": 3, "id": session_id, "cwd": "/repo"},
        {
            "type": "message",
            "id": "assistant-tool",
            "parentId": None,
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Checking."},
                    {
                        "type": "toolCall",
                        "id": "call-1",
                        "name": "bash",
                        "arguments": {"cmd": "ls"},
                    },
                ],
            },
        },
        {
            "type": "message",
            "id": "tool-result",
            "parentId": "assistant-tool",
            "message": {
                "role": "toolResult",
                "toolCallId": "call-1",
                "content": [{"type": "text", "text": "README.md"}],
            },
        },
    ]
    transcript.write_text(
        "".join(f"{json.dumps(record)}\n" for record in records), encoding="utf-8"
    )

    imported = load_pi_session(session_id, pi_home=tmp_path)

    assert [item.type for item in imported.items] == [
        "message",
        "function_call",
        "function_call_output",
    ]
    assert imported.items[1].data.model_dump() == {
        "agent": "pi-native-ui",
        "name": "bash",
        "arguments": '{"cmd":"ls"}',
        "call_id": "call-1",
    }
    assert imported.items[2].data.model_dump() == {
        "call_id": "call-1",
        "output": "README.md",
    }


def test_load_pi_session_rejects_an_orphaned_active_leaf(tmp_path: Path) -> None:
    """A broken Pi parent chain cannot be claimed as a partial import."""
    session_id = "019f8648-2797-7170-bf73-837f2655c47e"
    transcript = tmp_path / "sessions" / "--repo--" / f"stamp_{session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    records = [
        {"type": "session", "version": 3, "id": session_id, "cwd": "/repo"},
        {
            "type": "message",
            "id": "orphan",
            "parentId": "missing",
            "message": {"role": "user", "content": "partial"},
        },
    ]
    transcript.write_text(
        "".join(f"{json.dumps(record)}\n" for record in records), encoding="utf-8"
    )

    with pytest.raises(SessionImportNotFoundError, match="no importable history"):
        load_pi_session(session_id, pi_home=tmp_path)


def test_load_pi_session_migrates_legacy_linear_history(tmp_path: Path) -> None:
    """Pi v1 entries without tree ids import in their original linear order."""
    session_id = "legacy.session"
    transcript = tmp_path / "sessions" / "--repo--" / f"stamp_{session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    records = [
        {"type": "session", "version": 1, "id": session_id, "cwd": "/repo"},
        {"type": "message", "message": {"role": "user", "content": "hello"}},
        {
            "type": "message",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "hi"}],
            },
        },
    ]
    transcript.write_text(
        "".join(f"{json.dumps(record)}\n" for record in records), encoding="utf-8"
    )

    imported = load_pi_session(session_id, pi_home=tmp_path)

    assert [item.data.model_dump()["content"][0]["text"] for item in imported.items] == [
        "hello",
        "hi",
    ]


def test_load_pi_session_preserves_images_tool_order_and_aborted_state(tmp_path: Path) -> None:
    """Pi content retains images, tool position, and interrupted assistant state."""
    session_id = "my-feature"
    transcript = tmp_path / "sessions" / "--repo--" / f"stamp_{session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    records = [
        {"type": "session", "version": 3, "id": session_id, "cwd": "/repo"},
        {
            "type": "message",
            "id": "11111111",
            "parentId": None,
            "message": {
                "role": "user",
                "content": [
                    {"type": "image", "data": "AAAA", "mimeType": "image/png"},
                    {"type": "text", "text": "inspect this"},
                ],
            },
        },
        {
            "type": "message",
            "id": "22222222",
            "parentId": "11111111",
            "message": {
                "role": "assistant",
                "stopReason": "aborted",
                "content": [
                    {"type": "text", "text": "Before."},
                    {
                        "type": "toolCall",
                        "id": "call-1",
                        "name": "bash",
                        "arguments": {"cmd": "ls"},
                    },
                    {"type": "text", "text": "After."},
                ],
            },
        },
    ]
    transcript.write_text(
        "".join(f"{json.dumps(record)}\n" for record in records), encoding="utf-8"
    )

    imported = load_pi_session(session_id, pi_home=tmp_path)

    assert [item.type for item in imported.items] == [
        "message",
        "message",
        "function_call",
        "message",
    ]
    assert imported.items[0].data.model_dump()["content"] == [
        {"type": "input_image", "image_url": "data:image/png;base64,AAAA"},
        {"type": "input_text", "text": "inspect this"},
    ]
    assert imported.items[1].data.model_dump()["interrupted"] is True
    assert imported.items[3].data.model_dump()["interrupted"] is True


def test_load_pi_session_preserves_active_branch_summary(tmp_path: Path) -> None:
    """Pi branch summaries remain durable context for later active turns."""
    session_id = "branch-summary"
    transcript = tmp_path / "sessions" / "--repo--" / f"stamp_{session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    records = [
        {"type": "session", "version": 3, "id": session_id, "cwd": "/repo"},
        {
            "type": "message",
            "id": "11111111",
            "parentId": None,
            "message": {"role": "user", "content": "start"},
        },
        {
            "type": "branch_summary",
            "id": "22222222",
            "parentId": "11111111",
            "fromId": "stale-leaf",
            "summary": "Changed auth.py and found a token race.",
        },
        {
            "type": "message",
            "id": "33333333",
            "parentId": "22222222",
            "message": {"role": "user", "content": "continue"},
        },
    ]
    transcript.write_text(
        "".join(f"{json.dumps(record)}\n" for record in records), encoding="utf-8"
    )

    imported = load_pi_session(session_id, pi_home=tmp_path)

    assert [item.data.is_meta for item in imported.items] == [False, True, False]
    assert "Changed auth.py" in imported.items[1].data.model_dump()["content"][0]["text"]


def test_list_recent_pi_sessions_scans_project_directories(tmp_path: Path, monkeypatch) -> None:
    """Pi batch discovery extracts session UUIDs from timestamped files."""
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path))
    session_ids = (
        "019f8648-2797-7170-bf73-837f2655c471",
        "019f8648-2797-7170-bf73-837f2655c472",
    )
    for index, session_id in enumerate(session_ids, start=1):
        transcript = tmp_path / "sessions" / f"--repo-{index}--" / f"stamp_{session_id}.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text(
            json.dumps({"type": "session", "version": 3, "id": session_id}) + "\n",
            encoding="utf-8",
        )
        os.utime(transcript, (index, index))

    recent = list_recent_local_session_ids("pi", limit=10)

    assert recent == tuple(reversed(session_ids))


def test_list_recent_pi_sessions_supports_custom_ids(tmp_path: Path, monkeypatch) -> None:
    """Pi discovery reads safe custom session ids from transcript headers."""
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path))
    transcript = tmp_path / "sessions" / "--repo--" / "stamp_my-feature.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps({"type": "session", "version": 3, "id": "my-feature", "cwd": "/repo"}) + "\n",
        encoding="utf-8",
    )

    assert list_recent_local_session_ids("pi", limit=10) == ("my-feature",)


def test_load_kimi_session_normalizes_wire_messages(tmp_path: Path) -> None:
    """A Kimi wire log imports visible prompts and completed assistant text."""
    session_id = "session_20260721_abc"
    session_dir = tmp_path / "sessions" / "wd_repo" / session_id
    wire = session_dir / "agents" / "main" / "wire.jsonl"
    wire.parent.mkdir(parents=True)
    (tmp_path / "session_index.jsonl").write_text(
        json.dumps({"sessionDir": str(session_dir), "workDir": "/repo"}) + "\n",
        encoding="utf-8",
    )
    records = [
        {
            "type": "turn.prompt",
            "origin": {"kind": "user"},
            "input": [{"type": "text", "text": "inspect TODO.md"}],
        },
        {
            "type": "context.append_loop_event",
            "event": {
                "type": "content.part",
                "uuid": "assistant-1",
                "part": {"type": "think", "think": "private reasoning"},
            },
        },
        {
            "type": "context.append_loop_event",
            "event": {
                "type": "content.part",
                "uuid": "assistant-1",
                "part": {"type": "text", "text": "Done."},
            },
        },
    ]
    wire.write_text("".join(f"{json.dumps(record)}\n" for record in records), encoding="utf-8")

    imported = load_kimi_session(session_id, kimi_home=tmp_path)

    assert imported.source == "kimi"
    assert imported.workspace == "/repo"
    assert imported.title == "inspect TODO.md"
    assert [item.data.model_dump()["role"] for item in imported.items] == [
        "user",
        "assistant",
    ]
    assert imported.items[1].data.model_dump()["agent"] == "kimi-native-ui"


def test_list_recent_kimi_sessions_uses_wire_recency(tmp_path: Path, monkeypatch) -> None:
    """Kimi batch discovery identifies session directories by wire-log recency."""
    monkeypatch.setenv("KIMI_CODE_HOME", str(tmp_path))
    for session_id, modified_at in (("session_old", 1), ("session_new", 3)):
        wire = tmp_path / "sessions" / "wd_repo" / session_id / "agents" / "main" / "wire.jsonl"
        wire.parent.mkdir(parents=True)
        wire.touch()
        os.utime(wire, (modified_at, modified_at))

    recent = list_recent_local_session_ids("kimi", limit=10)

    assert recent == ("session_new", "session_old")


def test_list_recent_sessions_across_harnesses_merges_by_global_recency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """'all' keeps the globally-newest sessions, not `limit` per harness."""
    per_source = {
        "claude": [("c_new", 100.0), ("c_old", 10.0)],
        "codex": [("x_mid", 50.0), ("x_older", 5.0)],
    }

    def fake_recency(source: str, *, limit: int) -> list[tuple[str, float]]:
        return per_source.get(source, [])[:limit]

    monkeypatch.setattr(local_import, "_recent_local_sessions_with_recency", fake_recency)

    result = local_import.list_recent_sessions_across_harnesses(limit=3)

    # Top 3 by global recency, newest first — not 3 from each harness.
    assert result == [("claude", "c_new"), ("codex", "x_mid"), ("claude", "c_old")]


def test_list_recent_sessions_across_harnesses_normalizes_millisecond_recency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A millisecond timestamp (OpenCode) must not always outrank mtime seconds."""
    per_source = {
        # OpenCode reports epoch millis; claude's mtime seconds is actually newer.
        "opencode": [("o1", 1_700_000_000_000.0)],
        "claude": [("c1", 1_700_000_500.0)],
    }

    def fake_recency(source: str, *, limit: int) -> list[tuple[str, float]]:
        return per_source.get(source, [])[:limit]

    monkeypatch.setattr(local_import, "_recent_local_sessions_with_recency", fake_recency)

    result = local_import.list_recent_sessions_across_harnesses(limit=1)

    assert result == [("claude", "c1")]
