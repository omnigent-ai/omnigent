"""Tests for mirroring Kiro's persisted CLI session into Omnigent."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

import omnigent.kiro_native_session_forwarder as forwarder


def _write_kiro_session(
    root: Path,
    *,
    session_id: str,
    cwd: Path,
    created_at: str = "2026-06-21T01:39:34.528139806Z",
    updated_at: str = "2026-06-21T01:40:41.838294036Z",
    lines: list[dict[str, Any]] | None = None,
) -> Path:
    """Create a minimal Kiro CLI session metadata + JSONL fixture."""
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{session_id}.json").write_text(
        json.dumps(
            {
                "session_id": session_id,
                "cwd": str(cwd),
                "created_at": created_at,
                "updated_at": updated_at,
                "title": "hello",
            }
        ),
        encoding="utf-8",
    )
    jsonl_path = root / f"{session_id}.jsonl"
    jsonl_path.write_text(
        "\n".join(json.dumps(line) for line in (lines or [])) + "\n",
        encoding="utf-8",
    )
    return jsonl_path


def test_discover_kiro_session_jsonl_filters_by_workspace_and_launch_time(
    tmp_path: Path,
) -> None:
    """Discovery chooses the newest Kiro session for the runner workspace."""
    sessions_dir = tmp_path / "sessions" / "cli"
    workspace = tmp_path / "repo"
    other = tmp_path / "other"
    workspace.mkdir()
    other.mkdir()
    _write_kiro_session(
        sessions_dir,
        session_id="old",
        cwd=workspace,
        created_at="2026-06-21T01:00:00Z",
        updated_at="2026-06-21T01:00:01Z",
    )
    _write_kiro_session(
        sessions_dir,
        session_id="wrong-cwd",
        cwd=other,
        created_at="2026-06-21T01:39:35Z",
        updated_at="2026-06-21T01:41:00Z",
    )
    expected = _write_kiro_session(
        sessions_dir,
        session_id="current",
        cwd=workspace,
        created_at="2026-06-21T01:39:35Z",
        updated_at="2026-06-21T01:40:00Z",
    )

    discovered = forwarder._discover_kiro_session_jsonl(
        workspace=str(workspace),
        launch_epoch_ms=forwarder._parse_iso_epoch_ms("2026-06-21T01:39:34Z"),
        sessions_dir=sessions_dir,
    )

    assert discovered == ("current", expected)


def test_read_new_kiro_messages_returns_user_and_assistant_text(tmp_path: Path) -> None:
    """The JSONL reader mirrors Kiro prompt and assistant message text."""
    jsonl_path = tmp_path / "session.jsonl"
    jsonl_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "version": "v1",
                        "kind": "Prompt",
                        "data": {
                            "message_id": "user-1",
                            "content": [{"kind": "text", "data": "hey"}],
                        },
                    }
                ),
                json.dumps(
                    {
                        "version": "v1",
                        "kind": "AssistantMessage",
                        "data": {
                            "message_id": "assistant-1",
                            "content": [{"kind": "text", "data": "Hello there"}],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    messages, byte_offset = forwarder._read_new_kiro_messages(jsonl_path, 0)

    assert messages == [
        forwarder._KiroConversationMessage(message_id="user-1", role="user", text="hey"),
        forwarder._KiroConversationMessage(
            message_id="assistant-1", role="assistant", text="Hello there"
        ),
    ]
    assert byte_offset == jsonl_path.stat().st_size


def test_read_new_kiro_messages_holds_offset_at_partial_trailing_line(tmp_path: Path) -> None:
    """A record still mid-write (no trailing newline) is not skipped.

    Kiro appends to the JSONL live, so a poll can catch a partial final line.
    The reader must hold the offset at the last complete (newline-terminated)
    line so the partial record is re-read once Kiro finishes it — persisting
    ``handle.tell()`` past the partial line would drop that record for good.
    """
    complete = json.dumps(
        {
            "version": "v1",
            "kind": "Prompt",
            "data": {"message_id": "user-1", "content": [{"kind": "text", "data": "first"}]},
        }
    )
    partial = json.dumps(
        {
            "version": "v1",
            "kind": "AssistantMessage",
            "data": {"message_id": "assistant-1", "content": [{"kind": "text", "data": "second"}]},
        }
    )
    jsonl_path = tmp_path / "session.jsonl"
    # Complete line + a partial second line with NO trailing newline.
    jsonl_path.write_text(complete + "\n" + partial, encoding="utf-8")

    messages, offset = forwarder._read_new_kiro_messages(jsonl_path, 0)

    # Only the complete record is delivered; the offset stops at its newline,
    # not at EOF (which would skip the partial record once it's finished).
    assert messages == [
        forwarder._KiroConversationMessage(message_id="user-1", role="user", text="first")
    ]
    assert offset == len((complete + "\n").encode("utf-8"))
    assert offset < jsonl_path.stat().st_size

    # Kiro finishes the second record; re-reading from the held offset delivers it.
    with jsonl_path.open("a", encoding="utf-8") as handle:
        handle.write("\n")
    messages, offset = forwarder._read_new_kiro_messages(jsonl_path, offset)

    assert messages == [
        forwarder._KiroConversationMessage(
            message_id="assistant-1", role="assistant", text="second"
        )
    ]
    assert offset == jsonl_path.stat().st_size


@pytest.mark.asyncio
async def test_forward_kiro_session_posts_conversation_messages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One forwarder poll posts Kiro assistant messages as external items."""
    sessions_dir = tmp_path / "home" / ".kiro" / "sessions" / "cli"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    _write_kiro_session(
        sessions_dir,
        session_id="kiro-session",
        cwd=workspace,
        lines=[
            {
                "version": "v1",
                "kind": "Prompt",
                "data": {"message_id": "user-1", "content": [{"kind": "text", "data": "hey"}]},
            },
            {
                "version": "v1",
                "kind": "AssistantMessage",
                "data": {
                    "message_id": "assistant-1",
                    "content": [{"kind": "text", "data": "Hey!"}],
                },
            },
        ],
    )
    monkeypatch.setattr(forwarder, "_kiro_cli_sessions_dir", lambda: sessions_dir)
    posted: list[tuple[str, str, forwarder._KiroConversationMessage]] = []
    statuses: list[tuple[str, str, str | None]] = []
    external_ids: list[tuple[str, str]] = []

    async def _fake_post(
        client: httpx.AsyncClient,
        *,
        session_id: str,
        agent_name: str,
        message: forwarder._KiroConversationMessage,
    ) -> None:
        del client
        posted.append((session_id, agent_name, message))

    async def _fake_status(
        client: httpx.AsyncClient,
        *,
        session_id: str,
        status: str,
        response_id: str | None = None,
    ) -> None:
        del client
        statuses.append((session_id, status, response_id))

    async def _fake_patch_external_session_id(
        client: httpx.AsyncClient,
        *,
        session_id: str,
        external_session_id: str,
    ) -> None:
        del client
        external_ids.append((session_id, external_session_id))

    async def _cancel_sleep(_seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(forwarder, "_post_conversation_message", _fake_post)
    monkeypatch.setattr(forwarder, "_post_session_status", _fake_status)
    monkeypatch.setattr(
        forwarder,
        "_patch_external_session_id",
        _fake_patch_external_session_id,
    )
    monkeypatch.setattr(forwarder.asyncio, "sleep", _cancel_sleep)

    with pytest.raises(asyncio.CancelledError):
        await forwarder.forward_kiro_session_to_omnigent(
            base_url="http://127.0.0.1:6767",
            headers={},
            session_id="conv_kiro",
            bridge_dir=tmp_path / "bridge",
            agent_name="kiro-native-ui",
            workspace=str(workspace),
            launch_epoch_ms=forwarder._parse_iso_epoch_ms("2026-06-21T01:39:34Z"),
        )

    assert posted == [
        (
            "conv_kiro",
            "kiro-native-ui",
            forwarder._KiroConversationMessage(message_id="user-1", role="user", text="hey"),
        ),
        (
            "conv_kiro",
            "kiro-native-ui",
            forwarder._KiroConversationMessage(
                message_id="assistant-1", role="assistant", text="Hey!"
            ),
        ),
    ]
    assert statuses == [
        ("conv_kiro", "running", None),
        ("conv_kiro", "idle", "kiro:assistant-1"),
    ]
    assert external_ids == [("conv_kiro", "kiro-session")]
    state = json.loads((tmp_path / "bridge" / "kiro_session_forwarder.json").read_text())
    assert state["session_id"] == "kiro-session"
    assert state["byte_offset"] > 0


@pytest.mark.asyncio
async def test_forward_kiro_session_prefers_expected_resume_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resumed Kiro session id is authoritative over discovery and stale state."""
    sessions_dir = tmp_path / "home" / ".kiro" / "sessions" / "cli"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    _write_kiro_session(
        sessions_dir,
        session_id="resumed-session",
        cwd=workspace,
        created_at="2026-06-21T01:00:00Z",
        updated_at="2026-06-21T01:05:00Z",
        lines=[
            {
                "version": "v1",
                "kind": "Prompt",
                "data": {
                    "message_id": "resumed-user",
                    "content": [{"kind": "text", "data": ":0"}],
                },
            },
            {
                "version": "v1",
                "kind": "AssistantMessage",
                "data": {
                    "message_id": "resumed-assistant",
                    "content": [{"kind": "text", "data": "resumed reply"}],
                },
            },
        ],
    )
    _write_kiro_session(
        sessions_dir,
        session_id="newer-discovery-session",
        cwd=workspace,
        created_at="2026-06-21T02:00:00Z",
        updated_at="2026-06-21T02:01:00Z",
        lines=[
            {
                "version": "v1",
                "kind": "AssistantMessage",
                "data": {
                    "message_id": "wrong-assistant",
                    "content": [{"kind": "text", "data": "wrong reply"}],
                },
            }
        ],
    )
    bridge_dir = tmp_path / "bridge"
    forwarder._write_state(
        bridge_dir,
        forwarder._ForwardState(session_id="stale-session", byte_offset=0),
    )
    monkeypatch.setattr(forwarder, "_kiro_cli_sessions_dir", lambda: sessions_dir)
    posted: list[forwarder._KiroConversationMessage] = []
    statuses: list[tuple[str, str | None]] = []
    external_ids: list[str] = []

    async def _fake_post(
        client: httpx.AsyncClient,
        *,
        session_id: str,
        agent_name: str,
        message: forwarder._KiroConversationMessage,
    ) -> None:
        del client, session_id, agent_name
        posted.append(message)

    async def _fake_status(
        client: httpx.AsyncClient,
        *,
        session_id: str,
        status: str,
        response_id: str | None = None,
    ) -> None:
        del client, session_id
        statuses.append((status, response_id))

    async def _fake_patch_external_session_id(
        client: httpx.AsyncClient,
        *,
        session_id: str,
        external_session_id: str,
    ) -> None:
        del client, session_id
        external_ids.append(external_session_id)

    async def _cancel_sleep(_seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(forwarder, "_post_conversation_message", _fake_post)
    monkeypatch.setattr(forwarder, "_post_session_status", _fake_status)
    monkeypatch.setattr(
        forwarder,
        "_patch_external_session_id",
        _fake_patch_external_session_id,
    )
    monkeypatch.setattr(forwarder.asyncio, "sleep", _cancel_sleep)

    with pytest.raises(asyncio.CancelledError):
        await forwarder.forward_kiro_session_to_omnigent(
            base_url="http://127.0.0.1:6767",
            headers={},
            session_id="conv_kiro",
            bridge_dir=bridge_dir,
            agent_name="kiro-native-ui",
            workspace=str(workspace),
            launch_epoch_ms=forwarder._parse_iso_epoch_ms("2026-06-21T02:00:00Z"),
            expected_session_id="resumed-session",
        )

    assert posted == [
        forwarder._KiroConversationMessage(message_id="resumed-user", role="user", text=":0"),
        forwarder._KiroConversationMessage(
            message_id="resumed-assistant", role="assistant", text="resumed reply"
        ),
    ]
    assert statuses == [("running", None), ("idle", "kiro:resumed-assistant")]
    assert external_ids == ["resumed-session"]
    state = json.loads((bridge_dir / "kiro_session_forwarder.json").read_text())
    assert state["session_id"] == "resumed-session"
    assert state["byte_offset"] > 0


@pytest.mark.asyncio
async def test_forward_kiro_session_waits_for_expected_resume_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When resuming, do not fall back to a different Kiro session id."""
    sessions_dir = tmp_path / "home" / ".kiro" / "sessions" / "cli"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    _write_kiro_session(
        sessions_dir,
        session_id="wrong-session",
        cwd=workspace,
        created_at="2026-06-21T02:00:00Z",
        updated_at="2026-06-21T02:01:00Z",
        lines=[
            {
                "version": "v1",
                "kind": "AssistantMessage",
                "data": {
                    "message_id": "wrong-assistant",
                    "content": [{"kind": "text", "data": "wrong reply"}],
                },
            }
        ],
    )
    monkeypatch.setattr(forwarder, "_kiro_cli_sessions_dir", lambda: sessions_dir)
    posted: list[forwarder._KiroConversationMessage] = []
    external_ids: list[str] = []

    async def _fake_post(
        client: httpx.AsyncClient,
        *,
        session_id: str,
        agent_name: str,
        message: forwarder._KiroConversationMessage,
    ) -> None:
        del client, session_id, agent_name
        posted.append(message)

    async def _fake_patch_external_session_id(
        client: httpx.AsyncClient,
        *,
        session_id: str,
        external_session_id: str,
    ) -> None:
        del client, session_id
        external_ids.append(external_session_id)

    async def _cancel_sleep(_seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(forwarder, "_post_conversation_message", _fake_post)
    monkeypatch.setattr(
        forwarder,
        "_patch_external_session_id",
        _fake_patch_external_session_id,
    )
    monkeypatch.setattr(forwarder.asyncio, "sleep", _cancel_sleep)

    with pytest.raises(asyncio.CancelledError):
        await forwarder.forward_kiro_session_to_omnigent(
            base_url="http://127.0.0.1:6767",
            headers={},
            session_id="conv_kiro",
            bridge_dir=tmp_path / "bridge",
            agent_name="kiro-native-ui",
            workspace=str(workspace),
            launch_epoch_ms=forwarder._parse_iso_epoch_ms("2026-06-21T02:00:00Z"),
            expected_session_id="missing-resume-session",
        )

    assert posted == []
    assert external_ids == []
