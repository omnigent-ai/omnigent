"""Unit tests for qwen-native runner-side helpers."""

from __future__ import annotations

import httpx

from omnigent.runner.app import _persist_qwen_external_session_id


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
