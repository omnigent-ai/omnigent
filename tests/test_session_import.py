"""Tests for importing local coding-harness sessions."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from omnigent.kimi_native_forwarder import KimiWireItem, read_kimi_wire_items
from omnigent.kiro_native_session_forwarder import (
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
from omnigent.session_import.models import SessionImportNotFoundError


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

    def fake_run(*arguments: str, opencode_path: str | None = None) -> object:
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
    monkeypatch.setattr(local_import, "_run_opencode_json", lambda *arguments: {})

    with pytest.raises(SessionImportNotFoundError, match="invalid session list"):
        list_recent_local_session_ids("opencode", limit=1)


def test_load_opencode_session_preserves_messages_files_and_tools(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public export maps ordered parts to durable Omnigent items."""
    export = {
        "info": {
            "id": "ses_import",
            "directory": "/repo",
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


def test_load_claude_session_rejects_empty_history(tmp_path: Path) -> None:
    """An empty Claude transcript cannot create a claimed import."""
    session_id = "a1b2c3d4-1234-5678-9abc-def012345678"
    transcript = tmp_path / "projects" / "-repo" / f"{session_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.touch()

    with pytest.raises(SessionImportNotFoundError, match="no importable history"):
        load_claude_session(session_id, claude_home=tmp_path)


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
