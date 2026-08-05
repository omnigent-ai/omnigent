"""Tests for the dictation stream WS and its ``/v1/info`` capability bit.

The WebSocket tests drive the real route against the deterministic
:class:`FakeDictationEngine` injected through ``engine_provider`` — no
sherpa-onnx dependency, no models, no microphone. The engine reveals one
word of ``FAKE_SCRIPT`` per 100 ms of audio fed, so tests control the
transcript by the number of PCM bytes they send.
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from omnigent.server import dictation as dictation_engine
from omnigent.server.dictation import FAKE_SCRIPT, MAX_STREAMS_ENV, FakeDictationEngine
from omnigent.server.routes.dictation import create_dictation_router

# One fake-engine "word" of audio: 100 ms of 16 kHz mono s16le.
_WORD_BYTES = b"\x00" * (16000 * 2 // 10)
_SCRIPT_WORDS = FAKE_SCRIPT.split()


class _NoIdentityAuthProvider:
    """Auth provider whose handshake yields no identity."""

    def get_user_id(self, request: object) -> None:
        """Always return ``None`` (unauthenticated)."""
        del request
        return


class _IdentityAuthProvider:
    def __init__(self) -> None:
        self.calls = 0

    def get_user_id(self, request: object) -> str:
        del request
        self.calls += 1
        return "test-user"


class _PermissionStore:
    def __init__(self, *, admin: bool) -> None:
        self.admin = admin

    def is_admin(self, user_id: str) -> bool:
        assert user_id == "test-user"
        return self.admin


def _fake_app(**router_kwargs: Any) -> FastAPI:
    """Bare app carrying only the dictation router with a fake engine."""
    app = FastAPI()
    router_kwargs.setdefault("engine_provider", FakeDictationEngine)
    app.include_router(create_dictation_router(**router_kwargs), prefix="/v1")
    return app


async def test_info_carries_dictation_capability(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /v1/info advertises dictation for the web UI capability probe."""
    monkeypatch.setenv(dictation_engine.ENGINE_ENV, dictation_engine.ENGINE_FAKE)
    resp = await client.get("/v1/info")
    assert resp.status_code == 200
    assert resp.json()["dictation_available"] is True


async def test_info_reports_dictation_unavailable(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: object,
) -> None:
    """Without an engine (no extra or no models) /v1/info advertises false."""
    monkeypatch.setenv(dictation_engine.MODEL_DIR_ENV, str(tmp_path))
    monkeypatch.setenv(dictation_engine.ENGINE_ENV, dictation_engine.ENGINE_SHERPA)
    resp = await client.get("/v1/info")
    assert resp.status_code == 200
    assert resp.json()["dictation_available"] is False


def test_stream_partial_final_stop_flow() -> None:
    """Audio in → ready, partial, final, stopped events out."""
    with TestClient(_fake_app()) as tc, tc.websocket_connect("/v1/dictation/stream") as ws:
        assert json.loads(ws.receive_text()) == {"type": "ready"}

        # Two words of audio → a partial with the first two script words.
        ws.send_bytes(_WORD_BYTES * 2)
        partial = json.loads(ws.receive_text())
        assert partial == {"type": "partial", "text": " ".join(_SCRIPT_WORDS[:2])}

        # The rest of the script → the fake finalizes the sentence.
        ws.send_bytes(_WORD_BYTES * (len(_SCRIPT_WORDS) - 2))
        final = json.loads(ws.receive_text())
        assert final == {"type": "final", "text": FAKE_SCRIPT}

        ws.send_text(json.dumps({"type": "stop"}))
        stopped = json.loads(ws.receive_text())
        assert stopped == {"type": "stopped", "text": ""}


def test_stream_stop_flushes_tail() -> None:
    """stop mid-utterance returns the un-finalized words as the tail."""
    with TestClient(_fake_app()) as tc, tc.websocket_connect("/v1/dictation/stream") as ws:
        assert json.loads(ws.receive_text())["type"] == "ready"
        ws.send_bytes(_WORD_BYTES * 3)
        assert json.loads(ws.receive_text())["type"] == "partial"
        ws.send_text(json.dumps({"type": "stop"}))
        stopped = json.loads(ws.receive_text())
        assert stopped == {"type": "stopped", "text": " ".join(_SCRIPT_WORDS[:3])}


def test_stream_ignores_unknown_control_messages() -> None:
    """Unknown text frames are ignored for forward compatibility."""
    with TestClient(_fake_app()) as tc, tc.websocket_connect("/v1/dictation/stream") as ws:
        assert json.loads(ws.receive_text())["type"] == "ready"
        ws.send_text(json.dumps({"type": "does-not-exist"}))
        ws.send_text("not json at all")
        # The stream is still alive and transcribing after both.
        ws.send_bytes(_WORD_BYTES)
        assert json.loads(ws.receive_text()) == {
            "type": "partial",
            "text": _SCRIPT_WORDS[0],
        }


def test_stream_closes_take_on_abrupt_disconnect() -> None:
    """A vanished client still releases the take (worker-slot safety).

    The remote relay engine holds a worker capacity slot until its
    handle is closed; the route must close handles on the disconnect
    path, not just on a clean stop.
    """
    engine = FakeDictationEngine()
    app = _fake_app(engine_provider=lambda: engine)
    with TestClient(app) as tc:
        with tc.websocket_connect("/v1/dictation/stream") as ws:
            assert json.loads(ws.receive_text())["type"] == "ready"
            ws.send_bytes(_WORD_BYTES)
            # Exit without stop: an abrupt browser disconnect.
        assert engine.last_stream is not None
        deadline = time.monotonic() + 5
        while not engine.last_stream.closed and time.monotonic() < deadline:
            time.sleep(0.02)
        assert engine.last_stream.closed


def test_stream_rejects_unauthenticated_handshake() -> None:
    """With an auth provider and no identity, the handshake is refused."""
    app = _fake_app(auth_provider=_NoIdentityAuthProvider())
    with TestClient(app) as tc:
        with pytest.raises(WebSocketDisconnect):
            with tc.websocket_connect("/v1/dictation/stream") as ws:
                ws.receive_text()


def test_stream_capacity_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """Connections beyond the stream cap close with 1013 (try later)."""
    monkeypatch.setenv(MAX_STREAMS_ENV, "1")
    with TestClient(_fake_app()) as tc:
        with tc.websocket_connect("/v1/dictation/stream") as first:
            assert json.loads(first.receive_text())["type"] == "ready"
            with tc.websocket_connect("/v1/dictation/stream") as second:
                with pytest.raises(WebSocketDisconnect) as excinfo:
                    second.receive_text()
                assert excinfo.value.code == 1013


def test_worker_shared_token_is_required_before_accept() -> None:
    app = _fake_app(shared_token="secret")
    with TestClient(app) as tc:
        with pytest.raises(WebSocketDisconnect):
            with tc.websocket_connect("/v1/dictation/stream") as ws:
                ws.receive_text()
        with tc.websocket_connect(
            "/v1/dictation/stream", headers={"Authorization": "Bearer secret"}
        ) as ws:
            assert json.loads(ws.receive_text()) == {"type": "ready"}


def test_stream_rejects_oversized_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(dictation_engine.MAX_FRAME_BYTES_ENV, "8")
    with TestClient(_fake_app()) as tc, tc.websocket_connect("/v1/dictation/stream") as ws:
        assert json.loads(ws.receive_text())["type"] == "ready"
        ws.send_bytes(b"x" * 9)
        with pytest.raises(WebSocketDisconnect) as excinfo:
            ws.receive_text()
        assert excinfo.value.code == 1009


def test_stream_rejects_excess_audio_duration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(dictation_engine.MAX_TAKE_SECONDS_ENV, "0.001")
    with TestClient(_fake_app()) as tc, tc.websocket_connect("/v1/dictation/stream") as ws:
        assert json.loads(ws.receive_text())["type"] == "ready"
        ws.send_bytes(b"x" * 64)
        with pytest.raises(WebSocketDisconnect) as excinfo:
            ws.receive_text()
        assert excinfo.value.code == 1008


def test_disconnect_waits_for_in_flight_decode_before_close() -> None:
    decode_started = threading.Event()
    release_decode = threading.Event()

    class _Stream:
        closed_during_decode = False
        decoding = False

        def feed_pcm16(self, data: bytes) -> dictation_engine.DictationUpdate:
            del data
            self.decoding = True
            decode_started.set()
            release_decode.wait(timeout=5)
            self.decoding = False
            return dictation_engine.DictationUpdate(partial="")

        def finish(self) -> str:
            return ""

        def close(self) -> None:
            self.closed_during_decode = self.decoding

    class _Engine:
        def __init__(self) -> None:
            self.stream = _Stream()

        def create_stream(self) -> _Stream:
            return self.stream

    engine = _Engine()
    with TestClient(_fake_app(engine_provider=lambda: engine)) as tc:
        with tc.websocket_connect("/v1/dictation/stream") as ws:
            assert json.loads(ws.receive_text())["type"] == "ready"
            ws.send_bytes(_WORD_BYTES)
            assert decode_started.wait(timeout=5)
            threading.Timer(0.05, release_decode.set).start()
            tc.close()
            deadline = time.monotonic() + 5
            while engine.stream.decoding and time.monotonic() < deadline:
                time.sleep(0.01)
    assert not engine.stream.closed_during_decode


def test_status_requires_auth_and_sanitizes_remote_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dictation_engine, "_remote_connection_state", "not_attempted")
    monkeypatch.setattr(dictation_engine, "_sherpa_available", lambda: (False, None))
    monkeypatch.setenv(dictation_engine.ENGINE_ENV, dictation_engine.ENGINE_REMOTE)
    monkeypatch.setenv(
        dictation_engine.REMOTE_URL_ENV,
        "wss://user:password@worker.example/v1/dictation/stream?token=secret",
    )
    monkeypatch.setenv(dictation_engine.WORKER_TOKEN_ENV, "worker-secret")
    with TestClient(_fake_app(auth_provider=_NoIdentityAuthProvider())) as tc:
        assert tc.get("/v1/dictation/status").status_code == 401
    with TestClient(_fake_app(auth_provider=_IdentityAuthProvider())) as tc:
        response = tc.get("/v1/dictation/status")
        assert response.status_code == 200
        body = response.json()
        assert body["remote"] == {
            "configured": True,
            "secure": True,
            "token_configured": True,
            "fallback_available": False,
            "connection_state": "not_attempted",
        }
        assert body["capacity"] == {
            "max_streams": 2,
            "active_streams": 0,
            "available_streams": 2,
        }
        assert "password" not in response.text
        assert "worker-secret" not in response.text


def test_status_requires_admin_in_multi_user_mode() -> None:
    auth_provider = _IdentityAuthProvider()
    with TestClient(
        _fake_app(
            auth_provider=auth_provider,
            permission_store=_PermissionStore(admin=False),
        )
    ) as tc:
        assert tc.get("/v1/dictation/status").status_code == 403
        assert auth_provider.calls == 1

    with TestClient(
        _fake_app(
            auth_provider=_IdentityAuthProvider(),
            permission_store=_PermissionStore(admin=True),
        )
    ) as tc:
        assert tc.get("/v1/dictation/status").status_code == 200
