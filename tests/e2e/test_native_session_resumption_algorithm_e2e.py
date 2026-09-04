"""Credential-free integration coverage for native session resumption.

These tests deliberately stop at the native prepare/resume boundary.  They use
the real session HTTP API and real local artifact conversion, but replace the
Claude/Codex provider process: whether a provider can answer a prompt is not a
property of the resumption resolver and would make this suite credentialed.

The target policy is local-first for both native harnesses:

* a live terminal is reattached without touching local state;
* a structurally valid local artifact wins byte-for-byte;
* missing or corrupt local state is reconstructed from server items;
* a missing native id is minted and persisted before reconstruction;
* non-empty Claude history that converts to nothing must fail rather than
  silently launching a blank session.
"""

from __future__ import annotations

import json
import uuid
from itertools import pairwise
from pathlib import Path
from typing import Any

import click
import httpx
import pytest

import omnigent.claude_native as claude_native
import omnigent.claude_native_forwarder as claude_forwarder
import omnigent.codex_native as codex_native
from omnigent.native_coding_agents import (
    CLAUDE_NATIVE_AGENT_NAME,
    CODEX_NATIVE_AGENT_NAME,
)

_CLAUDE_ID = "11111111-2222-4333-8444-555566667777"
_CODEX_ID = "019f1111-2222-7333-8444-555566667777"


def _native_session(client: httpx.Client, agent_name: str) -> str:
    """Create a native-wrapper session through the public API."""
    agents = client.get("/v1/agents")
    agents.raise_for_status()
    agent_id = next(agent["id"] for agent in agents.json()["data"] if agent["name"] == agent_name)
    response = client.post("/v1/sessions", json={"agent_id": agent_id})
    response.raise_for_status()
    return str(response.json()["id"])


def _patch_external_id(client: httpx.Client, session_id: str, external_id: str) -> None:
    response = client.patch(
        f"/v1/sessions/{session_id}",
        json={"external_session_id": external_id},
    )
    response.raise_for_status()


def _post_item(
    client: httpx.Client,
    session_id: str,
    *,
    item_type: str,
    item_data: dict[str, Any],
    response_id: str,
) -> None:
    response = client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_conversation_item",
            "data": {
                "item_type": item_type,
                "item_data": item_data,
                "response_id": response_id,
            },
        },
    )
    assert response.status_code == 202, response.text


def _post_history(client: httpx.Client, session_id: str) -> None:
    _post_item(
        client,
        session_id,
        item_type="message",
        item_data={
            "role": "user",
            "content": [{"type": "input_text", "text": "remember ALBATROSS"}],
        },
        response_id="resp_history",
    )
    _post_item(
        client,
        session_id,
        item_type="message",
        item_data={
            "role": "assistant",
            "agent": "native-test",
            "content": [{"type": "output_text", "text": "ALBATROSS remembered"}],
        },
        response_id="resp_history",
    )


def _claude_record(session_id: str, *, text: str = "local wins") -> dict[str, Any]:
    return {
        "type": "user",
        "uuid": str(uuid.uuid4()),
        "parentUuid": None,
        "sessionId": session_id,
        "message": {"role": "user", "content": text},
    }


def _write_valid_claude(path: Path, *, session_id: str = _CLAUDE_ID) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(_claude_record(session_id)) + "\n").encode()
    path.write_bytes(raw)
    return raw


def _write_valid_codex(path: Path, *, thread_id: str = _CODEX_ID) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": "2026-09-03T00:00:00.000Z",
        "type": "session_meta",
        "payload": {
            "id": thread_id,
            "timestamp": "2026-09-03T00:00:00.000Z",
            "cwd": "/workspace",
            "originator": "omnigent-test",
            "cli_version": "0.136.0",
            "model_provider": "test-provider",
        },
    }
    raw = (json.dumps(record) + "\n").encode()
    path.write_bytes(raw)
    return raw


def _async_client(server: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=server, timeout=30.0)


@pytest.mark.asyncio
async def test_warm_reattach_leaves_claude_artifacts_untouched(
    http_client: httpx.Client,
    live_server: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Warm prepare returns before transcript resolution or cursor reset."""
    session_id = _native_session(http_client, CLAUDE_NATIVE_AGENT_NAME)
    transcript = tmp_path / f"{_CLAUDE_ID}.jsonl"
    before_transcript = _write_valid_claude(transcript)
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    cursor = bridge_dir / "transcript_forwarder.json"
    before_cursor = b'{"sentinel":"warm-reattach"}\n'
    cursor.write_bytes(before_cursor)

    async def _found(_client: object, _session_id: str) -> str:
        return "terminal_claude_main"

    async def _labels(_client: object, _session_id: str) -> dict[str, str]:
        return {"omnigent.claude_native.bridge_id": "bridge-test"}

    async def _tmux(_client: object, _session_id: str) -> Any:
        return claude_native._ClaudeTerminalTmux(socket=None, target=None)

    monkeypatch.setattr(claude_native, "_find_running_claude_terminal", _found)
    monkeypatch.setattr(claude_native, "_fetch_claude_session_labels", _labels)
    monkeypatch.setattr(claude_native, "_read_claude_terminal_tmux", _tmux)
    monkeypatch.setattr(claude_native, "bridge_dir_for_bridge_id", lambda _id: bridge_dir)

    prepared = await claude_native._prepare_claude_terminal(
        base_url=live_server,
        headers={},
        session_id=session_id,
        runner_id=None,
        session_bundle=None,
        claude_args=(),
        command="provider-must-not-run",
    )

    assert prepared.reattached is True
    assert transcript.read_bytes() == before_transcript
    assert cursor.read_bytes() == before_cursor


@pytest.mark.asyncio
async def test_warm_reattach_leaves_codex_rollout_untouched(
    http_client: httpx.Client,
    live_server: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex warm prepare returns before rollout validation or app-server boot."""
    session_id = _native_session(http_client, CODEX_NATIVE_AGENT_NAME)
    _patch_external_id(http_client, session_id, _CODEX_ID)
    rollout = tmp_path / f"rollout-2026-09-03T00-00-00-{_CODEX_ID}.jsonl"
    before = _write_valid_codex(rollout)
    bridge_dir = tmp_path / "codex-bridge"
    bridge_dir.mkdir()

    async def _found(_client: object, _session_id: str) -> Any:
        return codex_native.LaunchedCodexTerminal(
            terminal_id="terminal_codex_main",
            tmux_socket=None,
            tmux_target=None,
        )

    monkeypatch.setattr(codex_native, "_find_running_codex_terminal", _found)
    monkeypatch.setattr(codex_native, "bridge_dir_for_bridge_id", lambda _id: bridge_dir)
    monkeypatch.setattr(codex_native, "read_bridge_state", lambda _path: None)

    prepared = await codex_native._prepare_codex_terminal(
        base_url=live_server,
        headers={},
        session_id=session_id,
        runner_id=None,
        session_bundle=None,
        codex_args=(),
        command="provider-must-not-run",
        model=None,
    )

    assert prepared.reattached is True
    assert rollout.read_bytes() == before


@pytest.mark.asyncio
async def test_valid_local_claude_transcript_wins_without_server_rewrite(
    http_client: httpx.Client,
    live_server: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A valid transcript is authoritative even when server history differs."""
    session_id = _native_session(http_client, CLAUDE_NATIVE_AGENT_NAME)
    _patch_external_id(http_client, session_id, _CLAUDE_ID)
    _post_history(http_client, session_id)
    projects = tmp_path / ".claude" / "projects"
    target = projects / "current" / f"{_CLAUDE_ID}.jsonl"
    before = _write_valid_claude(target)
    monkeypatch.setattr(claude_native, "_CLAUDE_PROJECTS_DIR", projects)
    monkeypatch.setattr(claude_native, "_claude_project_dir_for_cwd", lambda _cwd: target.parent)

    async with _async_client(live_server) as client:
        args, resolution = await claude_native._resolve_cold_resume_args(client, session_id)

    assert args == ("--resume", _CLAUDE_ID)
    assert resolution.reused_local is True
    assert target.read_bytes() == before
    assert b"ALBATROSS" not in target.read_bytes()


@pytest.mark.asyncio
async def test_valid_local_codex_rollout_wins_without_server_rewrite(
    http_client: httpx.Client,
    live_server: str,
    tmp_path: Path,
) -> None:
    """A structurally valid rollout remains byte-identical on cold resume."""
    session_id = _native_session(http_client, CODEX_NATIVE_AGENT_NAME)
    _patch_external_id(http_client, session_id, _CODEX_ID)
    _post_history(http_client, session_id)
    rollout = (
        tmp_path
        / "codex-home"
        / "sessions"
        / "2026"
        / "09"
        / "03"
        / f"rollout-2026-09-03T00-00-00-{_CODEX_ID}.jsonl"
    )
    before = _write_valid_codex(rollout)

    async with _async_client(live_server) as client:
        resolved = await codex_native._ensure_local_codex_resume_rollout(
            client,
            session_id=session_id,
            external_session_id=_CODEX_ID,
            codex_home=tmp_path / "codex-home",
            workspace=tmp_path,
            model_provider="test-provider",
            codex_path=None,
        )

    assert resolved == rollout
    assert rollout.read_bytes() == before
    assert b"ALBATROSS" not in rollout.read_bytes()


@pytest.mark.asyncio
async def test_missing_claude_id_is_minted_persisted_and_reconstructed(
    http_client: httpx.Client,
    live_server: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing asynchronous Claude identity no longer discards server history."""
    session_id = _native_session(http_client, CLAUDE_NATIVE_AGENT_NAME)
    _post_history(http_client, session_id)
    projects = tmp_path / ".claude" / "projects"
    monkeypatch.setattr(claude_native, "_CLAUDE_PROJECTS_DIR", projects)

    async with _async_client(live_server) as client:
        args, resolution = await claude_native._resolve_cold_resume_args(client, session_id)

    assert args[0] == "--resume"
    assert resolution.synthesized is True
    minted = args[1]
    assert uuid.UUID(minted).version in (4, 7)
    snapshot = http_client.get(f"/v1/sessions/{session_id}")
    snapshot.raise_for_status()
    assert snapshot.json()["external_session_id"] == minted
    artifacts = list(projects.rglob(f"{minted}.jsonl"))
    assert len(artifacts) == 1
    assert "ALBATROSS" in artifacts[0].read_text()


@pytest.mark.asyncio
async def test_missing_codex_id_is_minted_persisted_and_reconstructed(
    http_client: httpx.Client,
    live_server: str,
    tmp_path: Path,
) -> None:
    """Missing Codex identity is repaired instead of hard-erroring."""
    session_id = _native_session(http_client, CODEX_NATIVE_AGENT_NAME)
    _post_history(http_client, session_id)

    async with _async_client(live_server) as client:
        minted = await codex_native._resolve_codex_cold_resume_thread_id(
            client,
            session_id=session_id,
            external_session_id=None,
            codex_home=tmp_path / "codex-home",
            workspace=tmp_path.resolve(),
            model_provider="test-provider",
            codex_path=None,
        )

    assert uuid.UUID(minted).version == 7
    snapshot = http_client.get(f"/v1/sessions/{session_id}")
    snapshot.raise_for_status()
    assert snapshot.json()["external_session_id"] == minted
    rollout = next((tmp_path / "codex-home" / "sessions").rglob(f"rollout-*-{minted}.jsonl"))
    assert "ALBATROSS" in rollout.read_text()


@pytest.mark.asyncio
async def test_corrupt_claude_transcript_is_backed_up_then_reconstructed(
    http_client: httpx.Client,
    live_server: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unparseable Claude bytes survive in a timestamped backup."""
    session_id = _native_session(http_client, CLAUDE_NATIVE_AGENT_NAME)
    _patch_external_id(http_client, session_id, _CLAUDE_ID)
    _post_history(http_client, session_id)
    projects = tmp_path / ".claude" / "projects"
    target = projects / "current" / f"{_CLAUDE_ID}.jsonl"
    target.parent.mkdir(parents=True)
    corrupt = b"\xffnot-json\n"
    target.write_bytes(corrupt)
    monkeypatch.setattr(claude_native, "_CLAUDE_PROJECTS_DIR", projects)
    monkeypatch.setattr(claude_native, "_claude_project_dir_for_cwd", lambda _cwd: target.parent)

    async with _async_client(live_server) as client:
        args, resolution = await claude_native._resolve_cold_resume_args(client, session_id)

    assert args == ("--resume", _CLAUDE_ID)
    assert resolution.synthesized is True
    backups = list(target.parent.glob(f"{target.name}.omnigent-backup-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == corrupt
    assert "ALBATROSS" in target.read_text()


@pytest.mark.asyncio
async def test_corrupt_codex_rollout_is_backed_up_then_reconstructed(
    http_client: httpx.Client,
    live_server: str,
    tmp_path: Path,
) -> None:
    """Unparseable Codex bytes survive in a timestamped backup."""
    session_id = _native_session(http_client, CODEX_NATIVE_AGENT_NAME)
    _patch_external_id(http_client, session_id, _CODEX_ID)
    _post_history(http_client, session_id)
    rollout = (
        tmp_path
        / "codex-home"
        / "sessions"
        / "2026"
        / "09"
        / "03"
        / f"rollout-2026-09-03T00-00-00-{_CODEX_ID}.jsonl"
    )
    rollout.parent.mkdir(parents=True)
    corrupt = b"\xffnot-json\n"
    rollout.write_bytes(corrupt)

    async with _async_client(live_server) as client:
        resolved = await codex_native._ensure_local_codex_resume_rollout(
            client,
            session_id=session_id,
            external_session_id=_CODEX_ID,
            codex_home=tmp_path / "codex-home",
            workspace=tmp_path.resolve(),
            model_provider="test-provider",
            codex_path=None,
        )

    backups = list(rollout.parent.glob(f"{rollout.name}.omnigent-backup-*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == corrupt
    assert resolved == rollout
    assert "ALBATROSS" in rollout.read_text()


@pytest.mark.asyncio
async def test_nonempty_unconvertible_claude_history_refuses_blank_launch(
    http_client: httpx.Client,
    live_server: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Server data that converts to zero Claude records is a hard stop."""
    session_id = _native_session(http_client, CLAUDE_NATIVE_AGENT_NAME)
    _patch_external_id(http_client, session_id, _CLAUDE_ID)
    _post_item(
        http_client,
        session_id,
        item_type="terminal_command",
        item_data={"kind": "input", "input": "pwd"},
        response_id="resp_unconvertible",
    )
    monkeypatch.setattr(claude_native, "_CLAUDE_PROJECTS_DIR", tmp_path / "projects")

    async with _async_client(live_server) as client:
        with pytest.raises(click.ClickException, match=r"(?i)(convert|resum|history)"):
            await claude_native._resolve_cold_resume_args(client, session_id)


@pytest.mark.asyncio
async def test_claude_native_compaction_shapes_survive_public_api_reconstruction(
    http_client: httpx.Client,
    live_server: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Native compacted message shapes survive server serialization."""
    session_id = _native_session(http_client, CLAUDE_NATIVE_AGENT_NAME)
    _patch_external_id(http_client, session_id, _CLAUDE_ID)
    _post_item(
        http_client,
        session_id,
        item_type="compaction",
        item_data={
            "summary": "compact boundary",
            "last_item_id": "msg_before",
            "token_count": 123,
            "compacted_messages": [
                {"type": "message", "role": "user", "content": "native compact summary"},
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "native reply"},
                        {
                            "type": "tool_use",
                            "id": "toolu_native",
                            "name": "Read",
                            "input": {"file_path": "README.md"},
                        },
                    ],
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_native",
                            "content": "native tool result",
                        }
                    ],
                },
            ],
        },
        response_id="resp_compaction",
    )
    projects = tmp_path / "projects"
    monkeypatch.setattr(claude_native, "_CLAUDE_PROJECTS_DIR", projects)

    async with _async_client(live_server) as client:
        args, resolution = await claude_native._resolve_cold_resume_args(client, session_id)

    assert args == ("--resume", _CLAUDE_ID)
    assert resolution.synthesized is True
    transcript = next(projects.rglob(f"{_CLAUDE_ID}.jsonl"))
    records = [json.loads(line) for line in transcript.read_text().splitlines()]
    assert [record["type"] for record in records] == ["system", "user", "assistant", "user"]
    assert records[0]["subtype"] == "compact_boundary"
    assert records[1]["message"]["content"] == "native compact summary"
    assert records[2]["message"]["content"][1]["type"] == "tool_use"
    assert records[3]["message"]["content"][0]["type"] == "tool_result"
    assert all(record["parentUuid"] == prior["uuid"] for prior, record in pairwise(records))


@pytest.mark.asyncio
async def test_codex_native_compaction_shape_survives_public_api_reconstruction(
    http_client: httpx.Client,
    live_server: str,
    tmp_path: Path,
) -> None:
    """Codex replacement_history survives the server/resolver boundary."""
    session_id = _native_session(http_client, CODEX_NATIVE_AGENT_NAME)
    _patch_external_id(http_client, session_id, _CODEX_ID)
    replacement = [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "native compact baseline"}],
        },
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "native compact reply"}],
        },
    ]
    _post_item(
        http_client,
        session_id,
        item_type="compaction",
        item_data={
            "summary": "codex compact boundary",
            "last_item_id": "msg_before",
            "token_count": 321,
            "compacted_messages": replacement,
            "window_id": 7,
        },
        response_id="resp_codex_compaction",
    )

    async with _async_client(live_server) as client:
        rollout = await codex_native._ensure_local_codex_resume_rollout(
            client,
            session_id=session_id,
            external_session_id=_CODEX_ID,
            codex_home=tmp_path / "codex-home",
            workspace=tmp_path.resolve(),
            model_provider="test-provider",
            codex_path=None,
        )

    records = [json.loads(line) for line in rollout.read_text().splitlines()]
    compacted = next(record for record in records if record["type"] == "compacted")
    assert compacted["payload"]["replacement_history"] == replacement
    assert compacted["payload"]["window_id"] == 7


@pytest.mark.asyncio
async def test_cross_workspace_claude_transcript_is_redirected_for_current_cwd(
    http_client: httpx.Client,
    live_server: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prior transcript is preserved and copied where cwd-scoped resume searches."""
    session_id = _native_session(http_client, CLAUDE_NATIVE_AGENT_NAME)
    _patch_external_id(http_client, session_id, _CLAUDE_ID)
    _post_history(http_client, session_id)
    projects = tmp_path / "projects"
    prior = projects / "prior-workspace" / f"{_CLAUDE_ID}.jsonl"
    before = _write_valid_claude(prior)
    current_workspace = tmp_path / "new-workspace"
    current_workspace.mkdir()
    current_target = projects / "new-workspace" / f"{_CLAUDE_ID}.jsonl"
    monkeypatch.setattr(claude_native, "_CLAUDE_PROJECTS_DIR", projects)
    monkeypatch.setattr(
        claude_native,
        "_claude_project_dir_for_cwd",
        lambda _cwd: current_target.parent,
    )

    async with _async_client(live_server) as client:
        args, resolution = await claude_native._resolve_cold_resume_args(client, session_id)

    assert args == ("--resume", _CLAUDE_ID)
    assert resolution.path == current_target
    assert resolution.reused_local is True
    assert prior.read_bytes() == before
    assert current_target.exists()


def test_valid_claude_cursor_is_preserved_for_resume(tmp_path: Path) -> None:
    """A valid path/offset/fingerprint cursor survives cold resolution."""
    transcript = tmp_path / f"{_CLAUDE_ID}.jsonl"
    _write_valid_claude(transcript)
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    offset = transcript.stat().st_size
    claude_forwarder._write_forward_state(
        bridge,
        claude_forwarder.TranscriptForwardState(
            transcript_path=transcript,
            line_cursor=1,
            byte_offset=offset,
            cursor_fingerprint=claude_forwarder._jsonl_cursor_fingerprint(transcript, offset),
            seen_source_ids=("already-posted",),
        ),
    )
    state_file = bridge / "transcript_forwarder.json"
    before = state_file.read_bytes()

    valid = claude_forwarder.prepare_transcript_forward_state_for_resume(
        bridge,
        transcript,
        session_id="conv_valid_cursor",
    )

    assert valid is True
    assert state_file.read_bytes() == before


@pytest.mark.asyncio
async def test_invalid_claude_cursor_surfaces_degraded_notice_at_prepare_boundary(
    http_client: httpx.Client,
    live_server: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Cold prepare warns users while preserving local runtime context."""
    session_id = _native_session(http_client, CLAUDE_NATIVE_AGENT_NAME)
    transcript = tmp_path / f"{_CLAUDE_ID}.jsonl"
    _write_valid_claude(transcript)
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    claude_forwarder._write_forward_state(
        bridge,
        claude_forwarder.TranscriptForwardState(
            transcript_path=transcript,
            line_cursor=99,
            byte_offset=1,
            cursor_fingerprint="invalid",
        ),
    )

    async def _not_running(_client: object, _session_id: str) -> None:
        return None

    async def _resume(
        _client: object,
        _session_id: str,
    ) -> tuple[tuple[str, str], claude_native.ClaudeResumeTranscriptResolution]:
        return (
            ("--resume", _CLAUDE_ID),
            claude_native.ClaudeResumeTranscriptResolution(
                transcript,
                reused_local=True,
                synthesized=False,
            ),
        )

    async def _launch(*_args: object, **_kwargs: object) -> str:
        return "terminal_claude_main"

    async def _tmux(_client: object, _session_id: str) -> Any:
        return claude_native._ClaudeTerminalTmux(socket=None, target=None)

    monkeypatch.setattr(claude_native, "_find_running_claude_terminal", _not_running)
    monkeypatch.setattr(claude_native, "_resolve_cold_resume_args", _resume)
    monkeypatch.setattr(claude_native, "_launch_claude_terminal", _launch)
    monkeypatch.setattr(claude_native, "_read_claude_terminal_tmux", _tmux)
    monkeypatch.setattr(claude_native, "prepare_bridge_dir", lambda *_args, **_kwargs: bridge)
    monkeypatch.setattr(
        claude_native,
        "_find_claude_resume_transcript",
        lambda *_args, **_kwargs: transcript,
        raising=False,
    )
    monkeypatch.setattr(
        claude_native,
        "prepare_transcript_forward_state_for_resume",
        claude_forwarder.prepare_transcript_forward_state_for_resume,
        raising=False,
    )

    prepared = await claude_native._prepare_claude_terminal(
        base_url=live_server,
        headers={},
        session_id=session_id,
        runner_id=None,
        session_bundle=None,
        claude_args=(),
        command="provider-must-not-run",
    )

    assert prepared.reattached is False
    assert prepared.cold_resumed is True
    assert "synchronization will continue from the transcript end" in capsys.readouterr().err
    assert transcript.exists()


def test_invalid_claude_cursor_degrades_to_eof_without_replay(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A stale cursor preserves runtime context but skips ambiguous history."""
    transcript = tmp_path / f"{_CLAUDE_ID}.jsonl"
    _write_valid_claude(transcript)
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    claude_forwarder._write_forward_state(
        bridge,
        claude_forwarder.TranscriptForwardState(
            transcript_path=transcript,
            line_cursor=99,
            byte_offset=1,
            cursor_fingerprint="invalid",
            seen_source_ids=("already-posted",),
        ),
    )

    valid = claude_forwarder.prepare_transcript_forward_state_for_resume(
        bridge,
        transcript,
        session_id="conv_degraded",
    )
    validated = claude_forwarder._read_forward_state(bridge)

    assert valid is False
    assert validated is not None
    assert validated.byte_offset == transcript.stat().st_size
    assert validated.seen_source_ids == ()
    assert "cursor fingerprint changed" in caplog.text
