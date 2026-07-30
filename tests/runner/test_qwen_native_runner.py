"""Unit tests for qwen-native runner-side helpers."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from omnigent import qwen_native_bridge as qnb
from omnigent.runner.app import _build_qwen_fork_recording, _persist_qwen_external_session_id


class _RecordingClient:
    """Async httpx-client stub recording PATCHes; returns a chosen status."""

    def __init__(self, status: int = 200) -> None:
        self.patches: list[tuple[str, dict]] = []
        self._status = status

    async def patch(self, url: str, *, json: dict, timeout: float | None = None) -> httpx.Response:
        self.patches.append((url, json))
        return httpx.Response(self._status, request=httpx.Request("PATCH", url))


async def test_persist_external_session_id_patches_session() -> None:
    client = _RecordingClient()
    await _persist_qwen_external_session_id(client, "conv_abc", "qsid-1")  # type: ignore[arg-type]
    assert client.patches == [("/v1/sessions/conv_abc", {"external_session_id": "qsid-1"})]


async def test_persist_external_session_id_noop_without_client() -> None:
    # No server client (e.g. embedded/test runner) → silent no-op, no raise.
    await _persist_qwen_external_session_id(None, "conv_abc", "qsid-1")


async def test_persist_external_session_id_swallows_errors() -> None:
    # Best-effort: a rejected PATCH or transport error must not raise (only
    # resume/fork carry-over degrades, never the live turn).
    rejected = _RecordingClient(status=500)
    await _persist_qwen_external_session_id(rejected, "conv_abc", "qsid-1")  # type: ignore[arg-type]

    class _Boom:
        async def patch(self, *_a: object, **_k: object) -> httpx.Response:
            raise httpx.ConnectError("down")

    await _persist_qwen_external_session_id(_Boom(), "conv_abc", "qsid-1")  # type: ignore[arg-type]


class _ItemsClient:
    """Async httpx-client stub serving one page of session items from GET /items."""

    def __init__(self, items: list[dict]) -> None:
        self._items = items

    async def get(self, url: str, *, params: dict | None = None, **_k: object) -> httpx.Response:
        body = {"data": self._items, "has_more": False}
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json=body,
        )


async def test_build_qwen_fork_recording_writes_recording(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Isolate ~/.qwen at a temp HOME so the synthesized recording lands there.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    items = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "hello"}],
        },
    ]
    client = _ItemsClient(items)

    qsid = await _build_qwen_fork_recording(
        client,  # type: ignore[arg-type]
        session_id="conv_fork",
        workspace=str(workspace),
    )

    # Returns the clone's deterministic id, and a resumable recording now exists.
    assert qsid == qnb.qwen_session_id_for_conversation("conv_fork")
    assert qnb.qwen_session_recording_exists(qsid, workspace)
    recording = qnb.qwen_session_recording_path(qsid, workspace)
    types = [json.loads(line)["type"] for line in recording.read_text().splitlines()]
    assert types == ["user", "assistant"]


async def test_build_qwen_fork_recording_returns_none_when_no_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Nothing carryable → None so the caller launches fresh (no recording written).
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    qsid = await _build_qwen_fork_recording(
        _ItemsClient([]),  # type: ignore[arg-type]
        session_id="conv_empty",
        workspace=str(workspace),
    )
    assert qsid is None
    assert not qnb.qwen_session_recording_exists(
        qnb.qwen_session_id_for_conversation("conv_empty"), workspace
    )


async def test_build_qwen_fork_recording_does_not_clobber_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # B2: a relaunch (e.g. after a failed external_session_id persist) re-enters
    # the fork path. If qwen has already built and since appended live, full-
    # fidelity turns, the rebuild must NOT overwrite them — it resumes as-is.
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    qsid = qnb.qwen_session_id_for_conversation("conv_fork")
    # Simulate qwen's live recording already on disk (a richer transcript than a
    # text-only rebuild would produce).
    recording = qnb.qwen_session_recording_path(qsid, workspace)
    recording.parent.mkdir(parents=True, exist_ok=True)
    sentinel = '{"type":"assistant","message":{"role":"model","parts":[{"text":"LIVE"}]}}\n'
    recording.write_text(sentinel)

    returned = await _build_qwen_fork_recording(
        _ItemsClient(
            [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "rebuilt"}],
                }
            ]
        ),  # type: ignore[arg-type]
        session_id="conv_fork",
        workspace=str(workspace),
    )

    # Returns the id to resume, and the live recording is untouched.
    assert returned == qsid
    assert recording.read_text() == sentinel
