"""Tests for pi-native fork/resume wiring in ``omnigent/runner/app.py``.

Covers reading the fork labels into ``_PiNativeLaunchConfig`` and the
``_resolve_pi_resume_session`` decision (cold resume, fork rebuild, fresh).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.runner.app import (
    _pi_native_launch_config,
    _PiNativeLaunchConfig,
    _resolve_pi_resume_session,
)
from omnigent.stores.conversation_store import (
    FORK_CARRY_HISTORY_LABEL_KEY,
    FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY,
)

_EXTERNAL_ID = "019efdb8-54c8-7c02-be27-875eb2620635"


def _user_item(text: str, item_id: str = "u1") -> dict[str, Any]:
    return {
        "id": item_id,
        "type": "message",
        "role": "user",
        "response_id": "r1",
        "content": [{"type": "input_text", "text": text}],
    }


# --------------------------------------------------------------------------
# _pi_native_launch_config reads fork labels
# --------------------------------------------------------------------------


class _Resp:
    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class _GetClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    async def get(self, url: str, timeout: float | None = None) -> _Resp:
        return _Resp(200, self._payload)


@pytest.mark.asyncio
async def test_launch_config_reads_fork_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", "/repo")
    client = _GetClient(
        {
            "workspace": "/repo",
            "labels": {
                FORK_SOURCE_EXTERNAL_SESSION_LABEL_KEY: "src-pi-id",
                FORK_CARRY_HISTORY_LABEL_KEY: "1",
            },
        }
    )
    config = await _pi_native_launch_config(session_id="conv_1", server_client=client)
    assert config.fork_source_external_id == "src-pi-id"
    assert config.fork_carry_history is True


@pytest.mark.asyncio
async def test_launch_config_defaults_no_fork(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", "/repo")
    client = _GetClient({"workspace": "/repo"})
    config = await _pi_native_launch_config(session_id="conv_1", server_client=client)
    assert config.fork_source_external_id is None
    assert config.fork_carry_history is False


# --------------------------------------------------------------------------
# _resolve_pi_resume_session
# --------------------------------------------------------------------------


def _config(
    *,
    workspace: Path,
    external_session_id: str | None = None,
    fork_source_external_id: str | None = None,
    fork_carry_history: bool = False,
) -> _PiNativeLaunchConfig:
    return _PiNativeLaunchConfig(
        workspace=workspace,
        server_url="http://server",
        terminal_launch_args=None,
        external_session_id=external_session_id,
        fork_source_external_id=fork_source_external_id,
        fork_carry_history=fork_carry_history,
    )


def _items_only_client(items: list[dict[str, Any]]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/items"):
            return httpx.Response(200, json={"data": items, "has_more": False})
        if request.method == "PATCH":
            return httpx.Response(200, json={})
        return httpx.Response(404, json={})

    return httpx.AsyncClient(base_url="http://server", transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_resolve_no_client_returns_captured_id(tmp_path: Path) -> None:
    config = _config(workspace=tmp_path, external_session_id=_EXTERNAL_ID)
    out = await _resolve_pi_resume_session(
        session_id="conv_1",
        launch_config=config,
        session_dir=tmp_path,
        workspace=tmp_path,
        server_client=None,
    )
    assert out == _EXTERNAL_ID


@pytest.mark.asyncio
async def test_resolve_cold_resume_builds_and_returns_captured_id(tmp_path: Path) -> None:
    config = _config(workspace=tmp_path, external_session_id=_EXTERNAL_ID)
    async with _items_only_client([_user_item("hello there")]) as client:
        out = await _resolve_pi_resume_session(
            session_id="conv_1",
            launch_config=config,
            session_dir=tmp_path,
            workspace=tmp_path,
            server_client=client,
        )
    assert out == _EXTERNAL_ID
    # A session file for the captured id should now exist.
    files = list(tmp_path.glob(f"*_{_EXTERNAL_ID}.jsonl"))
    assert len(files) == 1


@pytest.mark.asyncio
async def test_resolve_fork_rebuild_mints_id_and_patches(tmp_path: Path) -> None:
    config = _config(workspace=tmp_path, fork_carry_history=True)
    patched: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path.endswith("/items"):
            return httpx.Response(
                200, json={"data": [_user_item("forked context")], "has_more": False}
            )
        if request.method == "PATCH":
            import json as _json

            patched.update(_json.loads(request.content.decode()))
            return httpx.Response(200, json={})
        return httpx.Response(404, json={})

    async with httpx.AsyncClient(
        base_url="http://server", transport=httpx.MockTransport(handler)
    ) as client:
        out = await _resolve_pi_resume_session(
            session_id="conv_1",
            launch_config=config,
            session_dir=tmp_path,
            workspace=tmp_path,
            server_client=client,
        )
    # A new id was minted, a file written, and the server patched with it.
    assert out is not None
    assert out != _EXTERNAL_ID
    assert patched.get("external_session_id") == out
    assert list(tmp_path.glob(f"*_{out}.jsonl"))


@pytest.mark.asyncio
async def test_resolve_fork_rebuild_empty_history_returns_none(tmp_path: Path) -> None:
    config = _config(workspace=tmp_path, fork_carry_history=True)
    async with _items_only_client([]) as client:
        out = await _resolve_pi_resume_session(
            session_id="conv_1",
            launch_config=config,
            session_dir=tmp_path,
            workspace=tmp_path,
            server_client=client,
        )
    # No history to carry -> launch fresh (no minted id, no file).
    assert out is None
    assert not list(tmp_path.glob("*.jsonl"))


@pytest.mark.asyncio
async def test_resolve_fresh_session_returns_none(tmp_path: Path) -> None:
    config = _config(workspace=tmp_path)  # no captured id, no fork marker
    async with _items_only_client([_user_item("ignored")]) as client:
        out = await _resolve_pi_resume_session(
            session_id="conv_1",
            launch_config=config,
            session_dir=tmp_path,
            workspace=tmp_path,
            server_client=client,
        )
    assert out is None
    assert not list(tmp_path.glob("*.jsonl"))
