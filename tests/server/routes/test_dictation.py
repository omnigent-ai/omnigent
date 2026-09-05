"""Tests for the dictation stream WS and its ``/v1/info`` capability bit.

The WebSocket tests drive the real route against the deterministic
:class:`FakeDictationEngine` injected through ``engine_provider`` — no
sherpa-onnx dependency, no models, no microphone. The engine reveals one
word of ``FAKE_SCRIPT`` per 100 ms of audio fed, so tests control the
transcript by the number of PCM bytes they send.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from omnigent.errors import ErrorCode, OmnigentError
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


class _FakePunctuationRestorer:
    """Deterministic punctuation model for HTTP route tests."""

    def restore(self, text: str) -> str:
        assert text == "你好世界今天怎么样"
        return "你好，世界！今天怎么样？"


def _fake_app(**router_kwargs: object) -> FastAPI:
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
    monkeypatch.delenv(dictation_engine.ENGINE_ENV, raising=False)
    resp = await client.get("/v1/info")
    assert resp.status_code == 200
    assert resp.json()["dictation_available"] is False


async def test_info_carries_punctuation_capability(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """GET /v1/info advertises the independent final-punctuation model."""
    model = tmp_path / "model.int8.onnx"
    model.touch()
    monkeypatch.setenv(dictation_engine.FINAL_PUNCT_MODEL_ENV, str(model))
    monkeypatch.setattr(dictation_engine.importlib.util, "find_spec", lambda name: object())

    resp = await client.get("/v1/info")

    assert resp.status_code == 200
    assert resp.json()["dictation_punctuation_available"] is True


def test_punctuation_route_restores_completed_transcript() -> None:
    """The HTTP surface returns display-ready Chinese punctuation."""
    app = _fake_app(punctuation_provider=_FakePunctuationRestorer)
    with TestClient(app) as tc:
        resp = tc.post("/v1/dictation/punctuation", json={"text": "你好世界今天怎么样"})

    assert resp.status_code == 200
    assert resp.json() == {"text": "你好，世界！今天怎么样？"}


def test_punctuation_route_bounds_transcript_size() -> None:
    """Oversized free-form input is rejected before it reaches the CPU model."""
    app = _fake_app(punctuation_provider=_FakePunctuationRestorer)
    with TestClient(app) as tc:
        resp = tc.post("/v1/dictation/punctuation", json={"text": "字" * 501})

    assert resp.status_code == 422


async def test_punctuation_route_rejects_concurrent_inference() -> None:
    """A second request fails fast instead of occupying another worker thread."""
    entered = threading.Event()
    release = threading.Event()

    class BlockingRestorer:
        def restore(self, text: str) -> str:
            entered.set()
            assert release.wait(timeout=2)
            return f"{text}。"

    restorer = BlockingRestorer()
    app = _fake_app(punctuation_provider=lambda: restorer)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = asyncio.create_task(
            client.post("/v1/dictation/punctuation", json={"text": "第一句"})
        )
        assert await asyncio.to_thread(entered.wait, 1)
        second = await client.post("/v1/dictation/punctuation", json={"text": "第二句"})
        release.set()
        first_response = await first

    assert second.status_code == 429
    assert first_response.status_code == 200


def test_punctuation_route_requires_identity() -> None:
    """Accounts deployments do not expose the CPU model anonymously."""
    app = _fake_app(
        auth_provider=_NoIdentityAuthProvider(),
        punctuation_provider=_FakePunctuationRestorer,
    )
    with TestClient(app) as tc, pytest.raises(OmnigentError) as exc_info:
        tc.post("/v1/dictation/punctuation", json={"text": "你好世界今天怎么样"})

    assert exc_info.value.code == ErrorCode.UNAUTHORIZED


def test_punctuation_route_maps_model_failure_to_service_unavailable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Model failures return a fixed 503 without exposing transcript text."""

    private_text = "私密转写内容"

    def unavailable() -> _FakePunctuationRestorer:
        raise RuntimeError(f"model failed while processing {private_text}")

    app = _fake_app(punctuation_provider=unavailable)
    with caplog.at_level(logging.ERROR), TestClient(app) as tc:
        resp = tc.post("/v1/dictation/punctuation", json={"text": private_text})

    assert resp.status_code == 503
    assert resp.json() == {"detail": "dictation punctuation unavailable"}
    assert private_text not in resp.text
    assert private_text not in caplog.text


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
