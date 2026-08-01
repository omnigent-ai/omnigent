"""Tests for the remote dictation engine and the standalone worker.

Spins a real ``dictation_worker`` app (uvicorn on an ephemeral loopback
port, fake engine injected via env) and drives :class:`RemoteDictationEngine`
against it over actual TCP — the same relay path a beelink-class server
uses to borrow a beefier box's CPU. No sherpa dependency: the worker
runs the fake engine.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator

import pytest
import uvicorn

from omnigent.server import dictation

_TOKEN = "test-worker-token"


@pytest.fixture(autouse=True)
def _fake_engine_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The spawned worker (same process) must resolve the fake engine."""
    monkeypatch.setenv(dictation.ENGINE_ENV, dictation.ENGINE_FAKE)
    monkeypatch.delenv(dictation.REMOTE_URL_ENV, raising=False)
    monkeypatch.setenv(dictation.WORKER_TOKEN_ENV, _TOKEN)
    monkeypatch.setattr(dictation, "_engine", None)


@pytest.fixture
def worker_url() -> Iterator[str]:
    """Run the real worker app on an ephemeral port; yield its stream URL."""
    from omnigent.server.dictation_worker import create_worker_app

    config = uvicorn.Config(
        create_worker_app(shared_token=_TOKEN), host="127.0.0.1", port=0, log_level="warning"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("worker did not start")
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"ws://127.0.0.1:{port}/v1/dictation/stream"
    server.should_exit = True
    thread.join(timeout=10)


_WORD = b"\x00" * (dictation.SAMPLE_RATE * 2 // 10)  # 100 ms per fake word
_WORDS = dictation.FAKE_SCRIPT.split()


def _drain_partial(handle: dictation.DictationStreamHandle, expected: str) -> None:
    """Poll feeds until the relayed partial catches up (reader is async)."""
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        update = handle.feed_pcm16(b"")
        if update.partial == expected:
            return
        time.sleep(0.05)
    raise AssertionError(f"partial never reached {expected!r}")


def test_remote_engine_relays_partials_and_finish(worker_url: str) -> None:
    """PCM up, partial state down, stop-flush returns the tail."""
    engine = dictation.RemoteDictationEngine(worker_url, token=_TOKEN)
    handle = engine.create_stream()
    handle.feed_pcm16(_WORD * 3)
    # The worker throttles partial emission (~150 ms); poll until the
    # 3-word partial arrives.
    _drain_partial(handle, " ".join(_WORDS[:3]))
    assert handle.finish() == " ".join(_WORDS[:3])


def test_remote_engine_relays_finalized_utterances(worker_url: str) -> None:
    """A worker 'final' event surfaces as DictationUpdate.finalized."""
    engine = dictation.RemoteDictationEngine(worker_url, token=_TOKEN)
    handle = engine.create_stream()
    handle.feed_pcm16(_WORD * len(_WORDS))
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        update = handle.feed_pcm16(b"")
        if update.finalized:
            assert update.finalized == dictation.FAKE_SCRIPT
            break
        time.sleep(0.05)
    else:
        raise AssertionError("finalized never arrived")
    assert handle.finish() == ""


def test_remote_stream_close_releases_worker_slot(worker_url: str) -> None:
    """close() frees the worker's capacity slot for later takes.

    Three sequential takes against a worker capped at two concurrent
    streams: without the close, the third handshake would be rejected
    with the 1013 at-capacity close.
    """
    engine = dictation.RemoteDictationEngine(worker_url, token=_TOKEN)
    for _ in range(3):
        handle = engine.create_stream()
        handle.feed_pcm16(_WORD)
        handle.close()


def test_remote_engine_falls_back_when_worker_down() -> None:
    """Unreachable worker + local fallback → the take still serves."""
    engine = dictation.RemoteDictationEngine(
        "ws://127.0.0.1:9/v1/dictation/stream",  # port 9: nothing listens
        token=_TOKEN,
        fallback_factory=dictation.FakeDictationEngine,
    )
    handle = engine.create_stream()
    update = handle.feed_pcm16(_WORD * 2)
    assert update.partial == " ".join(_WORDS[:2])


def test_remote_engine_raises_without_fallback() -> None:
    """Unreachable worker and no local models → the take fails loudly."""
    engine = dictation.RemoteDictationEngine("ws://127.0.0.1:9/v1/dictation/stream", token=_TOKEN)
    with pytest.raises(OSError):
        engine.create_stream()


def test_remote_unavailable_without_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Selecting remote without a worker URL is unavailable, not a crash."""
    monkeypatch.setenv(dictation.ENGINE_ENV, dictation.ENGINE_REMOTE)
    monkeypatch.delenv(dictation.REMOTE_URL_ENV, raising=False)
    assert dictation.engine_availability() == (False, dictation.REASON_REMOTE_URL_MISSING)


def test_remote_engine_selected_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """OMNIGENT_DICTATION_ENGINE=remote + a URL selects the relay engine."""
    monkeypatch.setenv(dictation.ENGINE_ENV, dictation.ENGINE_REMOTE)
    monkeypatch.setenv(dictation.REMOTE_URL_ENV, "ws://example:8100/v1/dictation/stream")
    monkeypatch.setenv(dictation.ALLOW_INSECURE_REMOTE_ENV, "1")
    monkeypatch.setattr(dictation, "_engine", None)
    assert dictation.engine_availability() == (True, None)
    engine = dictation.get_engine()
    assert isinstance(engine, dictation.RemoteDictationEngine)


def test_remote_engine_denies_non_loopback_plaintext() -> None:
    with pytest.raises(ValueError, match="plaintext"):
        dictation.RemoteDictationEngine(
            "ws://worker.example/v1/dictation/stream",
            token=_TOKEN,
        )


def test_remote_engine_allows_loopback_plaintext() -> None:
    engine = dictation.RemoteDictationEngine(
        "ws://localhost:9/v1/dictation/stream",
        token=_TOKEN,
    )
    with pytest.raises(OSError):
        engine.create_stream()


def test_wss_uses_verified_ssl_context(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_create_default_context(*, cafile: str | None = None) -> object:
        captured["cafile"] = cafile
        return object()

    monkeypatch.setattr(dictation.ssl, "create_default_context", fake_create_default_context)
    engine = dictation.RemoteDictationEngine(
        "wss://worker.example/v1/dictation/stream",
        token=_TOKEN,
        ca_file="/private/ca.pem",
    )
    assert captured == {"cafile": "/private/ca.pem"}
    assert engine._ssl_context is not None


def test_remote_stream_relays_bearer_token(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class _Connection:
        def recv(self, timeout: float | None = None) -> str:
            del timeout
            return '{"type":"ready"}'

        def close(self) -> None:
            return

    def fake_connect(url: str, **kwargs: object) -> _Connection:
        captured.update(url=url, **kwargs)
        return _Connection()

    monkeypatch.setattr("websockets.sync.client.connect", fake_connect)
    stream = dictation._RemoteStream("ws://localhost/stream", _TOKEN, None)
    stream.close()
    assert captured["additional_headers"] == {"Authorization": f"Bearer {_TOKEN}"}


def test_remote_finish_preserves_final_arriving_before_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages = iter(
        [
            '{"type":"ready"}',
            '{"type":"final","text":"complete utterance"}',
            '{"type":"stopped","text":"tail words"}',
        ]
    )

    class _Connection:
        def recv(self, timeout: float | None = None) -> str:
            del timeout
            return next(messages)

        def send(self, _: object) -> None:
            return

        def close(self) -> None:
            return

    monkeypatch.setattr(
        "websockets.sync.client.connect",
        lambda *_args, **_kwargs: _Connection(),
    )
    stream = dictation._RemoteStream("ws://localhost/stream", _TOKEN, None)
    assert stream.finish() == "complete utterance tail words"
