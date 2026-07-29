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
    user_turn_metadatas: list[dict[str, Any]] | None = None,
    model_id: str | None = None,
) -> Path:
    """Create a minimal Kiro CLI session metadata + JSONL fixture.

    Pass ``user_turn_metadatas`` / ``model_id`` to populate the credit-metering
    fields the ``.json`` snapshot carries (``session_state.conversation_metadata
    .user_turn_metadatas`` and ``session_state.rts_model_state.model_info``).
    """
    root.mkdir(parents=True, exist_ok=True)
    metadata: dict[str, Any] = {
        "session_id": session_id,
        "cwd": str(cwd),
        "created_at": created_at,
        "updated_at": updated_at,
        "title": "hello",
    }
    if user_turn_metadatas is not None or model_id is not None:
        session_state: dict[str, Any] = {}
        if user_turn_metadatas is not None:
            session_state["conversation_metadata"] = {"user_turn_metadatas": user_turn_metadatas}
        if model_id is not None:
            session_state["rts_model_state"] = {"model_info": {"model_id": model_id}}
        metadata["session_state"] = session_state
    (root / f"{session_id}.json").write_text(json.dumps(metadata), encoding="utf-8")
    jsonl_path = root / f"{session_id}.jsonl"
    jsonl_path.write_text(
        "\n".join(json.dumps(line) for line in (lines or [])) + "\n",
        encoding="utf-8",
    )
    return jsonl_path


def _prompt_record(message_id: str, text: str) -> dict[str, Any]:
    """One ``Prompt`` transcript record with a single text block."""
    return {
        "version": "v1",
        "kind": "Prompt",
        "data": {"message_id": message_id, "content": [{"kind": "text", "data": text}]},
    }


def _assistant_record(
    message_id: str,
    text: str | None = None,
    tool_uses: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One ``AssistantMessage`` record with optional text and ``toolUse`` blocks."""
    content: list[dict[str, Any]] = []
    if text is not None:
        content.append({"kind": "text", "data": text})
    for tool_use in tool_uses or []:
        content.append({"kind": "toolUse", "data": tool_use})
    return {
        "version": "v1",
        "kind": "AssistantMessage",
        "data": {"message_id": message_id, "content": content},
    }


def _tool_results_record(results: list[dict[str, Any]]) -> dict[str, Any]:
    """One ``ToolResults`` record with ``toolResult`` blocks.

    The list field is ``results`` (kiro-cli 2.15.1 serde metadata:
    ``ToolResults{results}``), each block adjacently tagged ``toolResult`` with
    its fields under ``data``.
    """
    return {
        "version": "v1",
        "kind": "ToolResults",
        "data": {"results": [{"kind": "toolResult", "data": result} for result in results]},
    }


def _stub_status_posts(
    monkeypatch: pytest.MonkeyPatch,
    sink: list[tuple[str, str | None]] | None = None,
) -> None:
    """Replace the forwarder's status-edge post with a capturing stub."""

    async def _fake_status(
        client: httpx.AsyncClient,
        *,
        session_id: str,
        status: str,
        output: str | None = None,
        background_task_count: int | None = None,
        response_id: str | None = None,
    ) -> None:
        del client, session_id, output, background_task_count
        if sink is not None:
            sink.append((status, response_id))

    monkeypatch.setattr(forwarder, "post_external_session_status", _fake_status)


def test_discover_kiro_session_jsonl_filters_by_workspace_and_launch_time(
    tmp_path: Path,
) -> None:
    """Discovery binds the unique qualifying Kiro session for the workspace.

    The wrong-workspace and below-floor sessions are filtered out, leaving
    exactly one candidate, which is bound.
    """
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


def test_discover_kiro_session_jsonl_returns_none_when_multiple_candidates(
    tmp_path: Path,
) -> None:
    """Two concurrent same-workspace sessions are ambiguous → bind nothing.

    Each Kiro session is its own JSONL, so two fresh sessions launched in the
    same workspace within the discovery window both qualify. Picking newest-by-
    ``updated_at`` could latch onto the other session's transcript (cross-talk),
    so discovery must return ``None`` and retry rather than guess. Regression for
    the #1137 'forwarder can bind the wrong session' item.
    """
    sessions_dir = tmp_path / "sessions" / "cli"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    # Both created after the launch floor, same workspace — indistinguishable.
    _write_kiro_session(
        sessions_dir,
        session_id="session-a",
        cwd=workspace,
        created_at="2026-06-21T01:39:35Z",
        updated_at="2026-06-21T01:40:10Z",
    )
    _write_kiro_session(
        sessions_dir,
        session_id="session-b",
        cwd=workspace,
        created_at="2026-06-21T01:39:36Z",
        updated_at="2026-06-21T01:41:00Z",  # newest updated_at — the old tie-break
    )

    discovered = forwarder._discover_kiro_session_jsonl(
        workspace=str(workspace),
        launch_epoch_ms=forwarder._parse_iso_epoch_ms("2026-06-21T01:39:34Z"),
        sessions_dir=sessions_dir,
    )

    assert discovered is None


def test_discover_kiro_session_jsonl_skips_session_with_unparseable_created_at(
    tmp_path: Path,
) -> None:
    """An undateable same-workspace session can't poison the unique-bind count.

    A session whose ``created_at`` won't parse can't be confirmed in-window, so it
    is dropped rather than counted as a second candidate — otherwise a single
    garbage straggler would make discovery permanently ambiguous and bind nothing.
    """
    sessions_dir = tmp_path / "sessions" / "cli"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    _write_kiro_session(
        sessions_dir,
        session_id="undateable",
        cwd=workspace,
        created_at="garbage",
        updated_at="2026-06-21T01:41:00Z",
    )
    expected = _write_kiro_session(
        sessions_dir,
        session_id="current",
        cwd=workspace,
        created_at="2026-06-21T01:39:35Z",
        updated_at="2026-06-21T01:40:10Z",
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
    monkeypatch.setattr(forwarder, "kiro_cli_sessions_dir", lambda: sessions_dir)
    posted: list[tuple[str, str, forwarder._KiroConversationMessage]] = []
    external_ids: list[tuple[str, str]] = []

    async def _fake_post(
        client: httpx.AsyncClient,
        *,
        session_id: str,
        agent_name: str,
        message: forwarder._KiroConversationMessage,
        response_id: str | None = None,
    ) -> None:
        del client, response_id
        posted.append((session_id, agent_name, message))

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
    monkeypatch.setattr(
        forwarder,
        "_patch_external_session_id",
        _fake_patch_external_session_id,
    )
    _stub_status_posts(monkeypatch)
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
    # The forwarder's status edges always carry a response_id; the id-less
    # session badge stays PTY-owned (#1137). See
    # test_forward_kiro_session_posts_only_id_bearing_status for the guard.
    assert external_ids == [("conv_kiro", "kiro-session")]


def test_read_kiro_cumulative_credits_sums_every_turn(tmp_path: Path) -> None:
    """Cumulative credits = sum of every turn's metering_usage values."""
    sessions_dir = tmp_path / "cli"
    _write_kiro_session(
        sessions_dir,
        session_id="k",
        cwd=tmp_path,
        user_turn_metadatas=[
            {
                "metering_usage": [
                    {"value": 0.06, "unit": "credit"},
                    {"value": 0.03, "unit": "credit"},
                ],
                "input_token_count": 0,
            },
            {"metering_usage": [{"value": 0.04, "unit": "credit"}], "input_token_count": 0},
        ],
        model_id="auto",
    )

    total, model = forwarder._read_kiro_cumulative_credits(sessions_dir / "k.json")

    assert total == pytest.approx(0.13)
    assert model == "auto"


def test_read_kiro_cumulative_credits_none_without_metering(tmp_path: Path) -> None:
    """No metering (or missing file) yields ``(None, None)`` so callers skip."""
    sessions_dir = tmp_path / "cli"
    _write_kiro_session(sessions_dir, session_id="k", cwd=tmp_path)

    assert forwarder._read_kiro_cumulative_credits(sessions_dir / "k.json") == (None, None)
    assert forwarder._read_kiro_cumulative_credits(sessions_dir / "missing.json") == (None, None)


@pytest.mark.asyncio
async def test_forward_kiro_session_posts_cumulative_cost_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The forwarder posts kiro's summed credits as cumulative cost, then dedupes."""
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
                "kind": "AssistantMessage",
                "data": {"message_id": "a1", "content": [{"kind": "text", "data": "hi"}]},
            },
        ],
        user_turn_metadatas=[
            {
                "metering_usage": [
                    {"value": 0.06, "unit": "credit"},
                    {"value": 0.03, "unit": "credit"},
                ]
            },
            {"metering_usage": [{"value": 0.04, "unit": "credit"}]},
        ],
        model_id="auto",
    )
    monkeypatch.setattr(forwarder, "kiro_cli_sessions_dir", lambda: sessions_dir)
    costs: list[tuple[str, float, str | None]] = []

    async def _fake_post_cost(
        client: httpx.AsyncClient,
        *,
        session_id: str,
        cumulative_cost_usd: float,
        model: str | None,
    ) -> None:
        del client
        costs.append((session_id, cumulative_cost_usd, model))

    async def _noop_message(
        client: httpx.AsyncClient,
        *,
        session_id: str,
        agent_name: str,
        message: forwarder._KiroConversationMessage,
        response_id: str | None = None,
    ) -> None:
        del client

    async def _noop_patch(
        client: httpx.AsyncClient, *, session_id: str, external_session_id: str
    ) -> None:
        del client

    calls = {"n": 0}

    async def _cancel_after_two_polls(_seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] >= 2:
            raise asyncio.CancelledError

    async def _noop_model(client: httpx.AsyncClient, *, session_id: str, model: str) -> None:
        del client

    monkeypatch.setattr(forwarder, "_post_session_cost", _fake_post_cost)
    monkeypatch.setattr(forwarder, "_post_external_model_change", _noop_model)
    monkeypatch.setattr(forwarder, "_post_conversation_message", _noop_message)
    monkeypatch.setattr(forwarder, "_patch_external_session_id", _noop_patch)
    _stub_status_posts(monkeypatch)
    monkeypatch.setattr(forwarder.asyncio, "sleep", _cancel_after_two_polls)

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

    # Posted once with the summed credits + model; the unchanged second poll
    # does not re-post (server treats cumulative_cost_usd as monotonic).
    assert costs == [("conv_kiro", pytest.approx(0.13), "auto")]
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
    monkeypatch.setattr(forwarder, "kiro_cli_sessions_dir", lambda: sessions_dir)
    posted: list[forwarder._KiroConversationMessage] = []
    external_ids: list[str] = []

    async def _fake_post(
        client: httpx.AsyncClient,
        *,
        session_id: str,
        agent_name: str,
        message: forwarder._KiroConversationMessage,
        response_id: str | None = None,
    ) -> None:
        del client, session_id, agent_name, response_id
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
    _stub_status_posts(monkeypatch)
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
    # Status edges (stubbed above) always carry the turn's response_id (#1137).
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
    monkeypatch.setattr(forwarder, "kiro_cli_sessions_dir", lambda: sessions_dir)
    posted: list[forwarder._KiroConversationMessage] = []
    external_ids: list[str] = []

    async def _fake_post(
        client: httpx.AsyncClient,
        *,
        session_id: str,
        agent_name: str,
        message: forwarder._KiroConversationMessage,
        response_id: str | None = None,
    ) -> None:
        del client, session_id, agent_name, response_id
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
    _stub_status_posts(monkeypatch)
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


@pytest.mark.asyncio
async def test_forward_kiro_session_posts_only_id_bearing_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression for #1137, updated for live tool-call cards: every
    ``external_session_status`` the forwarder posts must carry a ``response_id``.
    The id-less session badge stays owned by the PTY watcher's ``emit_status``
    (runner/resource_registry.py) — double-sourcing *that* was #1137 — while the
    id-bearing edges drive only the per-turn card lifecycle (the goose-native
    split; an id-less idle is a no-op for a still-streaming card web-side).
    Captures real HTTP via a mock transport so it guards the behaviour however
    status might be sent."""
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
    monkeypatch.setattr(forwarder, "kiro_cli_sessions_dir", lambda: sessions_dir)

    posted_events: list[dict[str, Any]] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/events"):
            posted_events.append(json.loads(request.content))
        return httpx.Response(200, json={})

    real_async_client = forwarder.httpx.AsyncClient

    def _client_factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs.pop("transport", None)
        return real_async_client(*args, transport=httpx.MockTransport(_handler), **kwargs)

    monkeypatch.setattr(forwarder.httpx, "AsyncClient", _client_factory)

    async def _cancel_sleep(_seconds: float) -> None:
        raise asyncio.CancelledError

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

    posted_event_types = [event["type"] for event in posted_events]
    # The transcript is still mirrored…
    assert "external_conversation_item" in posted_event_types
    # …and every status edge the forwarder posts carries the turn's id (the
    # id-less session badge remains PTY-owned, per #1137).
    status_edges = [
        event["data"] for event in posted_events if event["type"] == "external_session_status"
    ]
    assert status_edges, "the prose turn should open and close its card"
    assert all(edge.get("response_id") for edge in status_edges)
    assert [edge["status"] for edge in status_edges] == ["running", "idle"]
    turn_id = "kiro:turn:user-1"
    assert {edge["response_id"] for edge in status_edges} == {turn_id}


def test_read_kiro_current_model_reads_without_metering(tmp_path: Path) -> None:
    """The model id is read from rts_model_state even before any turn/metering."""
    sessions_dir = tmp_path / "cli"
    _write_kiro_session(sessions_dir, session_id="k", cwd=tmp_path, model_id="claude-haiku-4.5")
    assert forwarder._read_kiro_current_model(sessions_dir / "k.json") == "claude-haiku-4.5"

    # Missing file and a session with no model state both yield None.
    assert forwarder._read_kiro_current_model(sessions_dir / "missing.json") is None
    _write_kiro_session(sessions_dir, session_id="bare", cwd=tmp_path)
    assert forwarder._read_kiro_current_model(sessions_dir / "bare.json") is None


@pytest.mark.asyncio
async def test_forward_kiro_session_mirrors_current_model_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The forwarder mirrors kiro's current model via external_model_change, deduped."""
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
                "kind": "AssistantMessage",
                "data": {"message_id": "a1", "content": [{"kind": "text", "data": "hi"}]},
            },
        ],
        # No metering: proves the model mirror fires at launch, before a turn's cost.
        model_id="claude-haiku-4.5",
    )
    monkeypatch.setattr(forwarder, "kiro_cli_sessions_dir", lambda: sessions_dir)
    models: list[tuple[str, str]] = []

    async def _fake_post_model(client: httpx.AsyncClient, *, session_id: str, model: str) -> None:
        del client
        models.append((session_id, model))

    async def _noop_message(
        client: httpx.AsyncClient,
        *,
        session_id: str,
        agent_name: str,
        message: forwarder._KiroConversationMessage,
        response_id: str | None = None,
    ) -> None:
        del client

    async def _noop_patch(
        client: httpx.AsyncClient, *, session_id: str, external_session_id: str
    ) -> None:
        del client

    calls = {"n": 0}

    async def _cancel_after_two_polls(_seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(forwarder, "_post_external_model_change", _fake_post_model)
    monkeypatch.setattr(forwarder, "_post_conversation_message", _noop_message)
    monkeypatch.setattr(forwarder, "_patch_external_session_id", _noop_patch)
    _stub_status_posts(monkeypatch)
    monkeypatch.setattr(forwarder.asyncio, "sleep", _cancel_after_two_polls)

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

    # Mirrored once; the unchanged second poll does not re-post.
    assert models == [("conv_kiro", "claude-haiku-4.5")]


def test_parse_kiro_jsonl_record_extracts_tool_vocabulary() -> None:
    """``toolUse`` blocks and ``ToolResults`` records parse into the superset
    contract, while :func:`parse_kiro_jsonl_line` stays message-only."""
    assistant_line = json.dumps(
        _assistant_record(
            "a1",
            text="Let me check",
            tool_uses=[{"tool_use_id": "tooluse_A", "name": "read", "input": {"path": "f.txt"}}],
        )
    )
    record = forwarder.parse_kiro_jsonl_record(assistant_line)
    assert record is not None
    assert record.kind == "AssistantMessage"
    assert record.text == "Let me check"
    assert record.tool_calls == (
        forwarder.KiroToolCall(
            call_id="tooluse_A", name="read", arguments=json.dumps({"path": "f.txt"})
        ),
    )

    # A tool-only assistant record has no prose: the superset parser keeps it,
    # the stable message-only projection drops it (as it always did).
    tool_only_line = json.dumps(
        _assistant_record(
            "a2", tool_uses=[{"tool_use_id": "tooluse_B", "name": "shell", "input": {"cmd": "ls"}}]
        )
    )
    tool_only = forwarder.parse_kiro_jsonl_record(tool_only_line)
    assert tool_only is not None
    assert tool_only.text == "" and len(tool_only.tool_calls) == 1
    assert forwarder.parse_kiro_jsonl_line(tool_only_line) is None

    results_line = json.dumps(
        _tool_results_record([{"tool_use_id": "tooluse_A", "content": "file contents"}])
    )
    results = forwarder.parse_kiro_jsonl_record(results_line)
    assert results is not None
    assert results.kind == "ToolResults"
    assert results.tool_results == (
        forwarder.KiroToolResult(call_id="tooluse_A", output="file contents"),
    )
    assert forwarder.parse_kiro_jsonl_line(results_line) is None


def test_parse_kiro_jsonl_record_tool_results_field_and_fallback() -> None:
    """``ToolResults`` items read from ``results`` (kiro-cli 2.15.1) and, as a
    tolerant fallback, from ``content`` for an older/variant shape."""
    primary = json.dumps(
        {
            "version": "v1",
            "kind": "ToolResults",
            "data": {
                "results": [
                    {"kind": "toolResult", "data": {"toolUseId": "tooluse_A", "content": "out-a"}}
                ]
            },
        }
    )
    record = forwarder.parse_kiro_jsonl_record(primary)
    assert record is not None
    assert record.tool_results == (forwarder.KiroToolResult(call_id="tooluse_A", output="out-a"),)

    # structuredContent is accepted when there is no plain content block.
    structured = json.dumps(
        {
            "version": "v1",
            "kind": "ToolResults",
            "data": {
                "results": [
                    {
                        "kind": "toolResult",
                        "data": {"toolUseId": "tooluse_B", "structuredContent": {"rows": 3}},
                    }
                ]
            },
        }
    )
    structured_record = forwarder.parse_kiro_jsonl_record(structured)
    assert structured_record is not None
    assert structured_record.tool_results[0].call_id == "tooluse_B"
    assert "rows" in structured_record.tool_results[0].output

    fallback = json.dumps(
        {
            "version": "v1",
            "kind": "ToolResults",
            "data": {
                "content": [
                    {
                        "kind": "toolResult",
                        "data": {"tool_use_id": "tooluse_C", "content": "out-c"},
                    }
                ]
            },
        }
    )
    fallback_record = forwarder.parse_kiro_jsonl_record(fallback)
    assert fallback_record is not None
    assert fallback_record.tool_results == (
        forwarder.KiroToolResult(call_id="tooluse_C", output="out-c"),
    )


def test_parse_kiro_jsonl_record_accepts_flat_acp_style_tool_blocks() -> None:
    """Tool blocks parse under the ACP-style tag/key spellings too.

    kiro-cli is closed-source and no public capture pins the block-level key
    spellings, so the parser accepts both the transcript's ``kind``/``data``
    envelope style and the flat ACP wire style until a captured transcript
    locks them.
    """
    line = json.dumps(
        {
            "version": "v1",
            "kind": "AssistantMessage",
            "data": {
                "message_id": "a1",
                "content": [
                    {"type": "tool_use", "toolUseId": "tooluse_C", "toolName": "write", "args": {}}
                ],
            },
        }
    )
    record = forwarder.parse_kiro_jsonl_record(line)
    assert record is not None
    assert record.tool_calls == (
        forwarder.KiroToolCall(call_id="tooluse_C", name="write", arguments="{}"),
    )


def test_forward_state_round_trips_turn_fields(tmp_path: Path) -> None:
    """The persisted cursor carries the open turn's card state across restarts."""
    state = forwarder._ForwardState(
        session_id="kiro-session",
        byte_offset=123,
        turn_response_id="kiro:turn:user-1",
        turn_live=True,
        pending_call_ids={"tooluse_A", "tooluse_B"},
    )
    forwarder._write_state(tmp_path, state)
    restored = forwarder._read_state(tmp_path)
    assert restored == state

    # A legacy state file (no turn fields) restores with a closed turn.
    (tmp_path / forwarder._STATE_FILE).write_text(
        json.dumps({"session_id": "kiro-session", "byte_offset": 7}), encoding="utf-8"
    )
    legacy = forwarder._read_state(tmp_path)
    assert legacy.turn_response_id is None
    assert legacy.turn_live is False
    assert legacy.pending_call_ids == set()


def _capture_events_client(
    monkeypatch: pytest.MonkeyPatch,
    posted_events: list[dict[str, Any]],
) -> None:
    """Route the forwarder's HTTP through a mock transport capturing /events."""

    def _handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path.endswith("/events"):
            posted_events.append(json.loads(request.content))
        return httpx.Response(200, json={})

    real_async_client = forwarder.httpx.AsyncClient

    def _client_factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs.pop("transport", None)
        return real_async_client(*args, transport=httpx.MockTransport(_handler), **kwargs)

    monkeypatch.setattr(forwarder.httpx, "AsyncClient", _client_factory)


@pytest.mark.asyncio
async def test_forward_kiro_session_multi_call_turn_shares_turn_response_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn with parallel tool calls mirrors as one streaming group.

    One ``running`` edge before the turn's first item; the assistant prose,
    both ``function_call`` items, both ``function_call_output`` items, and the
    closing ``idle`` all share ``kiro:turn:{prompt_message_id}``; prose posts
    before the calls so the web spinner sits on the trailing item; the final
    prose record closes the card immediately.
    """
    sessions_dir = tmp_path / "home" / ".kiro" / "sessions" / "cli"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    _write_kiro_session(
        sessions_dir,
        session_id="kiro-session",
        cwd=workspace,
        lines=[
            _prompt_record("user-1", "run it"),
            _assistant_record(
                "a1",
                text="Let me check",
                tool_uses=[
                    {"tool_use_id": "tooluse_A", "name": "read", "input": {"path": "f.txt"}},
                    {"tool_use_id": "tooluse_B", "name": "shell", "input": {"cmd": "ls"}},
                ],
            ),
            _tool_results_record(
                [
                    {"tool_use_id": "tooluse_A", "content": "data-a"},
                    {"tool_use_id": "tooluse_B", "content": "data-b"},
                ]
            ),
            _assistant_record("a2", text="Done"),
        ],
    )
    monkeypatch.setattr(forwarder, "kiro_cli_sessions_dir", lambda: sessions_dir)
    posted_events: list[dict[str, Any]] = []
    _capture_events_client(monkeypatch, posted_events)

    async def _cancel_sleep(_seconds: float) -> None:
        raise asyncio.CancelledError

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

    turn_id = "kiro:turn:user-1"
    lifecycle = [
        (event["type"], event["data"].get("item_type"), event["data"].get("status"))
        for event in posted_events
        if event["type"] in ("external_conversation_item", "external_session_status")
    ]
    assert lifecycle == [
        ("external_conversation_item", "message", None),  # user prompt
        ("external_session_status", None, "running"),
        ("external_conversation_item", "message", None),  # prose before the calls
        ("external_conversation_item", "function_call", None),
        ("external_conversation_item", "function_call", None),
        ("external_conversation_item", "function_call_output", None),
        ("external_conversation_item", "function_call_output", None),
        ("external_conversation_item", "message", None),  # final prose
        ("external_session_status", None, "idle"),
    ]
    items = [e["data"] for e in posted_events if e["type"] == "external_conversation_item"]
    # The user prompt keeps its per-message id; everything else shares the turn id.
    assert items[0]["response_id"] == "kiro:user-1"
    assert {item["response_id"] for item in items[1:]} == {turn_id}
    calls = [item["item_data"] for item in items if item["item_type"] == "function_call"]
    assert [(call["call_id"], call["name"]) for call in calls] == [
        ("tooluse_A", "read"),
        ("tooluse_B", "shell"),
    ]
    outputs = [item["item_data"] for item in items if item["item_type"] == "function_call_output"]
    assert [(out["call_id"], out["output"]) for out in outputs] == [
        ("tooluse_A", "data-a"),
        ("tooluse_B", "data-b"),
    ]
    edges = [e["data"] for e in posted_events if e["type"] == "external_session_status"]
    assert [(edge["status"], edge["response_id"]) for edge in edges] == [
        ("running", turn_id),
        ("idle", turn_id),
    ]


@pytest.mark.asyncio
async def test_forward_kiro_session_restart_mid_turn_keeps_turn_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A restart between a call and its result resumes the same streaming group.

    The persisted turn state means the second run posts no second ``running``,
    stamps the remaining items with the original turn id, and closes the card
    once the final prose record lands — instead of splitting the turn across
    two response ids (the failure goose needed open-turn replay to fix).
    """
    sessions_dir = tmp_path / "home" / ".kiro" / "sessions" / "cli"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    jsonl_path = _write_kiro_session(
        sessions_dir,
        session_id="kiro-session",
        cwd=workspace,
        lines=[
            _prompt_record("user-1", "run it"),
            _assistant_record(
                "a1",
                text="Checking",
                tool_uses=[{"tool_use_id": "tooluse_A", "name": "read", "input": {}}],
            ),
        ],
    )
    monkeypatch.setattr(forwarder, "kiro_cli_sessions_dir", lambda: sessions_dir)
    bridge_dir = tmp_path / "bridge"
    turn_id = "kiro:turn:user-1"

    async def _cancel_sleep(_seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(forwarder.asyncio, "sleep", _cancel_sleep)

    # One shared sink for both runs: patching the client factory twice would
    # wrap the first patch and route the second run's events to the first sink.
    all_events: list[dict[str, Any]] = []
    _capture_events_client(monkeypatch, all_events)
    with pytest.raises(asyncio.CancelledError):
        await forwarder.forward_kiro_session_to_omnigent(
            base_url="http://127.0.0.1:6767",
            headers={},
            session_id="conv_kiro",
            bridge_dir=bridge_dir,
            agent_name="kiro-native-ui",
            workspace=str(workspace),
            launch_epoch_ms=forwarder._parse_iso_epoch_ms("2026-06-21T01:39:34Z"),
        )
    first_run_events = list(all_events)

    first_edges = [e["data"] for e in first_run_events if e["type"] == "external_session_status"]
    assert [(edge["status"], edge["response_id"]) for edge in first_edges] == [
        ("running", turn_id)
    ]
    persisted = json.loads((bridge_dir / forwarder._STATE_FILE).read_text())
    assert persisted["turn_response_id"] == turn_id
    assert persisted["turn_live"] is True
    assert persisted["pending_call_ids"] == ["tooluse_A"]

    # The tool finishes and the turn ends while the forwarder is down.
    with jsonl_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(_tool_results_record([{"tool_use_id": "tooluse_A", "content": "data"}]))
            + "\n"
        )
        handle.write(json.dumps(_assistant_record("a2", text="Done")) + "\n")

    with pytest.raises(asyncio.CancelledError):
        await forwarder.forward_kiro_session_to_omnigent(
            base_url="http://127.0.0.1:6767",
            headers={},
            session_id="conv_kiro",
            bridge_dir=bridge_dir,
            agent_name="kiro-native-ui",
            workspace=str(workspace),
            launch_epoch_ms=forwarder._parse_iso_epoch_ms("2026-06-21T01:39:34Z"),
        )
    second_run_events = all_events[len(first_run_events) :]

    items = [e["data"] for e in second_run_events if e["type"] == "external_conversation_item"]
    assert [item["item_type"] for item in items] == ["function_call_output", "message"]
    assert {item["response_id"] for item in items} == {turn_id}
    second_edges = [e["data"] for e in second_run_events if e["type"] == "external_session_status"]
    # No second running; the original turn closes under its original id.
    assert [(edge["status"], edge["response_id"]) for edge in second_edges] == [("idle", turn_id)]


@pytest.mark.asyncio
async def test_forward_kiro_session_missed_opener_anchors_to_first_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attaching mid-turn (no ``Prompt`` seen) still yields a stable turn id."""
    sessions_dir = tmp_path / "home" / ".kiro" / "sessions" / "cli"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    _write_kiro_session(
        sessions_dir,
        session_id="kiro-session",
        cwd=workspace,
        lines=[
            _assistant_record(
                "a1",
                text="Working",
                tool_uses=[{"tool_use_id": "tooluse_A", "name": "shell", "input": {}}],
            ),
        ],
    )
    monkeypatch.setattr(forwarder, "kiro_cli_sessions_dir", lambda: sessions_dir)
    posted_events: list[dict[str, Any]] = []
    _capture_events_client(monkeypatch, posted_events)

    async def _cancel_sleep(_seconds: float) -> None:
        raise asyncio.CancelledError

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

    turn_id = "kiro:turn:a1"
    edges = [e["data"] for e in posted_events if e["type"] == "external_session_status"]
    assert [(edge["status"], edge["response_id"]) for edge in edges] == [("running", turn_id)]
    items = [e["data"] for e in posted_events if e["type"] == "external_conversation_item"]
    assert {item["response_id"] for item in items} == {turn_id}


@pytest.mark.asyncio
async def test_forward_kiro_session_stalled_turn_backstop_closes_card(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn that dies without its normal close settles after the backstop.

    A pending tool call with no result and no further transcript activity (TUI
    interrupt, kiro-cli crash) must not leave a spinner forever: after
    ``_STALLED_TURN_IDLE_S`` of transcript inactivity the card is closed with
    the turn's own id.
    """
    sessions_dir = tmp_path / "home" / ".kiro" / "sessions" / "cli"
    workspace = tmp_path / "repo"
    workspace.mkdir()
    _write_kiro_session(
        sessions_dir,
        session_id="kiro-session",
        cwd=workspace,
        lines=[
            _prompt_record("user-1", "run it"),
            _assistant_record(
                "a1",
                text="Running",
                tool_uses=[{"tool_use_id": "tooluse_A", "name": "shell", "input": {}}],
            ),
        ],
    )
    monkeypatch.setattr(forwarder, "kiro_cli_sessions_dir", lambda: sessions_dir)
    posted_events: list[dict[str, Any]] = []
    _capture_events_client(monkeypatch, posted_events)

    clock = {"now": 0.0}
    monkeypatch.setattr(forwarder, "_turn_monotonic", lambda: clock["now"])

    polls = {"n": 0}

    async def _advance_clock_then_cancel(_seconds: float) -> None:
        polls["n"] += 1
        if polls["n"] == 1:
            # Poll 1 mirrored the open turn; silence past the backstop window.
            clock["now"] = forwarder._STALLED_TURN_IDLE_S + 1.0
            return
        raise asyncio.CancelledError

    monkeypatch.setattr(forwarder.asyncio, "sleep", _advance_clock_then_cancel)

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

    turn_id = "kiro:turn:user-1"
    edges = [e["data"] for e in posted_events if e["type"] == "external_session_status"]
    assert [(edge["status"], edge["response_id"]) for edge in edges] == [
        ("running", turn_id),
        ("idle", turn_id),
    ]
    # The settled turn is persisted closed, so a restart won't re-close it.
    persisted = json.loads(
        (tmp_path / "bridge" / forwarder._STATE_FILE).read_text(encoding="utf-8")
    )
    assert persisted["turn_live"] is False
    assert persisted["turn_response_id"] == turn_id
