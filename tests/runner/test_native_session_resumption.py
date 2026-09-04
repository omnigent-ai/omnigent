"""Runner-side native session resumption policy tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnigent.claude_native import ClaudeResumeTranscriptResolution
from omnigent.runner.native.orchestration import (
    _post_claude_degraded_sync_notice,
    _prepare_claude_resume_forwarder,
    _resolve_missing_runner_claude_resume,
    _resolve_runner_claude_resume,
)


class _Response:
    status_code = 200

    def raise_for_status(self) -> None:
        return None


def _write_cursor(bridge_dir: Path, transcript: Path, *, fingerprint: str) -> bytes:
    payload = transcript.read_bytes()
    bridge_dir.mkdir(parents=True)
    (bridge_dir / "transcript_forwarder.json").write_text(
        json.dumps(
            {
                "transcript_path": str(transcript),
                "line_cursor": 1,
                "byte_offset": len(payload),
                "cursor_fingerprint": fingerprint,
                "seen_source_ids": ["old-item"],
            }
        ),
        encoding="utf-8",
    )
    return payload


@pytest.mark.asyncio
async def test_local_claude_resume_preserves_valid_forwarder_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid local transcript and cursor are reused without reset or fetch."""
    import omnigent.claude_native as claude_native
    from omnigent.claude_native_forwarder import _jsonl_cursor_fingerprint

    projects = tmp_path / ".claude" / "projects"
    workspace = tmp_path / "workspace"
    session_id = "conv-local"
    external_id = "02857840-6362-408f-b41f-309e396ed7c6"
    monkeypatch.setattr(claude_native, "_CLAUDE_PROJECTS_DIR", projects)
    transcript = claude_native._claude_project_dir_for_cwd(workspace) / f"{external_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "one",
                "parentUuid": None,
                "sessionId": external_id,
                "cwd": str(workspace.resolve()),
                "message": {"role": "user", "content": "remember me"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    bridge_dir = tmp_path / "bridge"
    payload = _write_cursor(
        bridge_dir,
        transcript,
        fingerprint=_jsonl_cursor_fingerprint(transcript, transcript.stat().st_size) or "",
    )
    state_before = (bridge_dir / "transcript_forwarder.json").read_bytes()

    resolution = await _resolve_runner_claude_resume(
        object(),  # type: ignore[arg-type]
        session_id=session_id,
        external_session_id=external_id,
        workspace=workspace,
    )

    assert resolution.reused_local is True
    assert _prepare_claude_resume_forwarder(
        bridge_dir,
        resolution,
        session_id=session_id,
    ) == (None, False)
    assert transcript.read_bytes() == payload
    assert (bridge_dir / "transcript_forwarder.json").read_bytes() == state_before


@pytest.mark.asyncio
async def test_missing_claude_cursor_degrades_and_is_atomically_seeded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing cursor is ambiguous, so seed EOF and require a notice."""
    import omnigent.claude_native as claude_native

    projects = tmp_path / ".claude" / "projects"
    workspace = tmp_path / "workspace"
    external_id = "02857840-6362-408f-b41f-309e396ed7c6"
    monkeypatch.setattr(claude_native, "_CLAUDE_PROJECTS_DIR", projects)
    transcript = claude_native._claude_project_dir_for_cwd(workspace) / f"{external_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps(
            {
                "type": "user",
                "sessionId": external_id,
                "cwd": str(workspace),
                "message": {"role": "user", "content": "local tail"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    bridge_dir = tmp_path / "bridge"

    resolution = await _resolve_runner_claude_resume(
        object(),  # type: ignore[arg-type]
        session_id="conv-missing-cursor",
        external_session_id=external_id,
        workspace=workspace,
    )
    assert _prepare_claude_resume_forwarder(
        bridge_dir,
        resolution,
        session_id="conv-missing-cursor",
    ) == (None, True)
    state = json.loads((bridge_dir / "transcript_forwarder.json").read_text())
    assert state["byte_offset"] == transcript.stat().st_size
    assert state["transcript_path"] == str(transcript)


@pytest.mark.asyncio
async def test_synthesized_claude_resume_resets_and_seeds_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Server reconstruction clears stale state and returns its exact prefix."""
    import omnigent.claude_native as claude_native

    projects = tmp_path / ".claude" / "projects"
    workspace = tmp_path / "workspace"
    external_id = "02857840-6362-408f-b41f-309e396ed7c6"
    monkeypatch.setattr(claude_native, "_CLAUDE_PROJECTS_DIR", projects)
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    state_file = bridge_dir / "transcript_forwarder.json"
    state_file.write_text('{"stale":true}', encoding="utf-8")
    transcript = projects / "current" / f"{external_id}.jsonl"

    async def _synthesize(*_args: Any, **_kwargs: Any) -> ClaudeResumeTranscriptResolution:
        transcript.parent.mkdir(parents=True)
        transcript.write_text('{"type":"user","uuid":"server"}\n', encoding="utf-8")
        return ClaudeResumeTranscriptResolution(
            transcript,
            reused_local=False,
            synthesized=True,
        )

    monkeypatch.setattr(
        claude_native,
        "_ensure_local_claude_resume_transcript",
        _synthesize,
    )
    resolution = await _resolve_runner_claude_resume(
        object(),  # type: ignore[arg-type]
        session_id="conv-server",
        external_session_id=external_id,
        workspace=workspace,
    )

    assert resolution.reused_local is False
    assert _prepare_claude_resume_forwarder(
        bridge_dir,
        resolution,
        session_id="conv-server",
    ) == (transcript.stat().st_size, False)
    assert state_file.exists()


@pytest.mark.asyncio
async def test_invalid_claude_cursor_starts_at_eof_and_posts_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale fingerprint degrades to EOF and surfaces one informational item."""
    import omnigent.claude_native as claude_native

    projects = tmp_path / ".claude" / "projects"
    workspace = tmp_path / "workspace"
    external_id = "02857840-6362-408f-b41f-309e396ed7c6"
    monkeypatch.setattr(claude_native, "_CLAUDE_PROJECTS_DIR", projects)
    transcript = claude_native._claude_project_dir_for_cwd(workspace) / f"{external_id}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "local",
                "parentUuid": None,
                "sessionId": external_id,
                "cwd": str(workspace.resolve()),
                "message": {"role": "user", "content": "remember me"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    bridge_dir = tmp_path / "bridge"
    _write_cursor(bridge_dir, transcript, fingerprint="stale")

    resolution = await _resolve_runner_claude_resume(
        object(),  # type: ignore[arg-type]
        session_id="conv-degraded",
        external_session_id=external_id,
        workspace=workspace,
    )
    assert _prepare_claude_resume_forwarder(
        bridge_dir,
        resolution,
        session_id="conv-degraded",
    ) == (None, True)

    posts: list[dict[str, Any]] = []

    class _Client:
        async def post(self, _url: str, **kwargs: Any) -> _Response:
            posts.append(kwargs["json"])
            return _Response()

    await _post_claude_degraded_sync_notice(
        session_id="conv-degraded",
        server_client=_Client(),  # type: ignore[arg-type]
    )
    assert len(posts) == 1
    item = posts[0]["data"]["item_data"]
    assert item["code"] == "claude_degraded_sync"
    assert item["level"] == "info"


@pytest.mark.asyncio
async def test_missing_claude_id_is_minted_synthesized_and_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Committed history with no native id gets a durable minted binding."""
    import omnigent.claude_native as claude_native

    transcript = tmp_path / "minted.jsonl"
    transcript.write_text('{"type":"user","uuid":"server"}\n', encoding="utf-8")
    synthesized_ids: list[str] = []

    async def _synthesize(
        _client: Any,
        *,
        external_session_id: str,
        **_kwargs: Any,
    ) -> ClaudeResumeTranscriptResolution:
        synthesized_ids.append(external_session_id)
        return ClaudeResumeTranscriptResolution(
            transcript,
            reused_local=False,
            synthesized=True,
        )

    monkeypatch.setattr(
        claude_native,
        "_ensure_local_claude_resume_transcript",
        _synthesize,
    )
    patches: list[dict[str, Any]] = []

    class _Client:
        async def patch(self, _url: str, **kwargs: Any) -> _Response:
            patches.append(kwargs["json"])
            return _Response()

    external_id, resolution = await _resolve_missing_runner_claude_resume(
        _Client(),  # type: ignore[arg-type]
        session_id="conv-missing",
        workspace=tmp_path,
    )

    assert external_id == synthesized_ids[0]
    assert resolution.path == transcript
    assert patches == [{"external_session_id": external_id}]


@pytest.mark.asyncio
async def test_missing_claude_id_does_not_hide_history_fetch_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unavailable history service must not be mistaken for empty history."""
    import omnigent.claude_native as claude_native

    async def _unavailable(*_args: Any, **_kwargs: Any) -> ClaudeResumeTranscriptResolution:
        raise RuntimeError("history unavailable")

    monkeypatch.setattr(
        claude_native,
        "_ensure_local_claude_resume_transcript",
        _unavailable,
    )

    with pytest.raises(RuntimeError, match="history unavailable"):
        await _resolve_missing_runner_claude_resume(
            object(),  # type: ignore[arg-type]
            session_id="conv-unavailable",
            workspace=tmp_path,
        )


@pytest.mark.asyncio
async def test_missing_claude_id_requires_persisted_binding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Do not launch a synthesized transcript under an uncommitted native id."""
    import omnigent.claude_native as claude_native

    transcript = tmp_path / "minted.jsonl"
    transcript.write_text('{"type":"user"}\n', encoding="utf-8")

    async def _synthesize(*_args: Any, **_kwargs: Any) -> ClaudeResumeTranscriptResolution:
        return ClaudeResumeTranscriptResolution(
            transcript,
            reused_local=False,
            synthesized=True,
        )

    monkeypatch.setattr(
        claude_native,
        "_ensure_local_claude_resume_transcript",
        _synthesize,
    )

    class _FailingResponse:
        def raise_for_status(self) -> None:
            raise RuntimeError("binding rejected")

    class _Client:
        async def patch(self, _url: str, **_kwargs: Any) -> _FailingResponse:
            return _FailingResponse()

    with pytest.raises(RuntimeError, match="binding rejected"):
        await _resolve_missing_runner_claude_resume(
            _Client(),  # type: ignore[arg-type]
            session_id="conv-patch-failed",
            workspace=tmp_path,
        )
