"""Tests for the remote dictation engine and the standalone worker.

Spins a real ``dictation_worker`` app (uvicorn on an ephemeral loopback
port, fake engine injected via env) and drives :class:`RemoteDictationEngine`
against it over actual TCP — the same relay path a beelink-class server
uses to borrow a beefier box's CPU. No sherpa dependency: the worker
runs the fake engine.
"""

from __future__ import annotations

import ssl
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
import uvicorn
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect

from omnigent.inner.egress.ca import ensure_ca
from omnigent.inner.egress.certs import HostCertCache
from omnigent.server import dictation


@pytest.fixture(autouse=True)
def _fake_engine_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The spawned worker (same process) must resolve the fake engine."""
    monkeypatch.setenv(dictation.ENGINE_ENV, dictation.ENGINE_FAKE)
    monkeypatch.setenv(dictation.MAX_STREAMS_ENV, "2")
    monkeypatch.delenv(dictation.REMOTE_URL_ENV, raising=False)
    monkeypatch.delenv(dictation.WORKER_TOKEN_ENV, raising=False)
    monkeypatch.setattr(dictation, "_engine", None)


_TOKEN = "fake-worker-token"


@pytest.fixture
def token() -> str | None:
    """Worker token; tests parametrize this to run against an authenticated worker."""
    return None


@pytest.fixture
def tls_files(request: pytest.FixtureRequest, tmp_path: Path) -> tuple[Path, Path] | None:
    """Server cert/key for ``localhost`` when parametrized indirectly; else plain ws://."""
    if not getattr(request, "param", False):
        return None
    ca_cert, ca_key = ensure_ca(cache_dir=tmp_path)
    cert_pem, key_pem = HostCertCache(ca_cert, ca_key)._generate("localhost")
    cert, key = tmp_path / "worker.pem", tmp_path / "worker.key"
    cert.write_bytes(cert_pem)
    key.write_bytes(key_pem)
    return cert, key


@pytest.fixture
def worker_url(
    token: str | None,
    tls_files: tuple[Path, Path] | None,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[str]:
    """Run the real worker app on an ephemeral port; yield its stream URL."""
    from omnigent.server.dictation_worker import create_worker_app

    if token is not None:
        monkeypatch.setenv(dictation.WORKER_TOKEN_ENV, token)
    app = create_worker_app()
    monkeypatch.delenv(dictation.WORKER_TOKEN_ENV, raising=False)
    ssl_kwargs = (
        {"ssl_certfile": str(tls_files[0]), "ssl_keyfile": str(tls_files[1])} if tls_files else {}
    )
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", **ssl_kwargs)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("worker did not start")
        time.sleep(0.05)
    port = server.servers[0].sockets[0].getsockname()[1]
    if tls_files:
        yield f"wss://localhost:{port}/v1/dictation/stream"
    else:
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


@pytest.mark.parametrize("token", [None, _TOKEN], ids=["open", "token"])
def test_remote_engine_relays_partials_and_finish(worker_url: str, token: str | None) -> None:
    """PCM up, partial state down, stop-flush returns the tail."""
    engine = dictation.RemoteDictationEngine(worker_url, token=token)
    handle = engine.create_stream()
    handle.feed_pcm16(_WORD * 3)
    # The worker throttles partial emission (~150 ms); poll until the
    # 3-word partial arrives.
    _drain_partial(handle, " ".join(_WORDS[:3]))
    assert handle.finish() == " ".join(_WORDS[:3])


@pytest.mark.parametrize("token", [_TOKEN])
def test_handshake_debug_logs_omit_token(
    worker_url: str, token: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Root DEBUG logging must not capture the bearer from either handshake."""
    wrong_token = "fake-wrong-token-for-logs"
    with caplog.at_level("DEBUG"):
        handle = dictation.RemoteDictationEngine(worker_url, token=token).create_stream()
        handle.feed_pcm16(_WORD)
        handle.finish()
        with pytest.raises(InvalidStatus):
            dictation.RemoteDictationEngine(worker_url, token=wrong_token).create_stream()
    assert token not in caplog.text
    assert wrong_token not in caplog.text


@pytest.mark.parametrize("token", [None, _TOKEN], ids=["open", "token"])
def test_remote_engine_relays_finalized_utterances(worker_url: str, token: str | None) -> None:
    """A worker 'final' event surfaces as DictationUpdate.finalized."""
    engine = dictation.RemoteDictationEngine(worker_url, token=token)
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


@pytest.mark.parametrize("token", [None, _TOKEN], ids=["open", "token"])
def test_remote_stream_close_releases_worker_slot(worker_url: str, token: str | None) -> None:
    """close() frees the worker's capacity slot for later takes.

    Three sequential takes against a worker capped at two concurrent
    streams: without the close, the third handshake would be rejected
    with the 1013 at-capacity close.
    """
    engine = dictation.RemoteDictationEngine(worker_url, token=token)
    for _ in range(3):
        handle = engine.create_stream()
        handle.feed_pcm16(_WORD)
        handle.close()


def test_remote_engine_falls_back_when_worker_down(caplog: pytest.LogCaptureFixture) -> None:
    """Unreachable worker + local fallback → the take still serves, token unlogged."""
    engine = dictation.RemoteDictationEngine(
        "ws://127.0.0.1:9/v1/dictation/stream",  # port 9: nothing listens
        token=_TOKEN,
        fallback_factory=dictation.FakeDictationEngine,
    )
    with caplog.at_level("DEBUG"):
        handle = engine.create_stream()
    update = handle.feed_pcm16(_WORD * 2)
    assert update.partial == " ".join(_WORDS[:2])
    assert "unreachable" in caplog.text
    assert _TOKEN not in caplog.text


def test_remote_engine_raises_without_fallback() -> None:
    """Unreachable worker and no local models → the take fails loudly."""
    engine = dictation.RemoteDictationEngine("ws://127.0.0.1:9/v1/dictation/stream")
    with pytest.raises(OSError):
        engine.create_stream()


def test_remote_unavailable_without_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Selecting remote without a worker URL is unavailable, not a crash."""
    monkeypatch.setenv(dictation.ENGINE_ENV, dictation.ENGINE_REMOTE)
    monkeypatch.delenv(dictation.REMOTE_URL_ENV, raising=False)
    assert dictation.engine_availability() == (False, dictation.REASON_REMOTE_URL_MISSING)
    # A missing URL still wins over an invalid token.
    monkeypatch.setenv(dictation.WORKER_TOKEN_ENV, "")
    assert dictation.engine_availability() == (False, dictation.REASON_REMOTE_URL_MISSING)
    # With a token, a plaintext non-loopback URL is a config error.
    monkeypatch.setenv(dictation.WORKER_TOKEN_ENV, _TOKEN)
    monkeypatch.setenv(dictation.REMOTE_URL_ENV, "ws://worker.example/v1/dictation/stream")
    assert dictation.engine_availability() == (False, dictation.REASON_REMOTE_CONFIG_INVALID)


def test_remote_engine_selected_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """OMNIGENT_DICTATION_ENGINE=remote + a URL selects the relay engine."""
    monkeypatch.setenv(dictation.ENGINE_ENV, dictation.ENGINE_REMOTE)
    monkeypatch.setenv(dictation.REMOTE_URL_ENV, "ws://example:8100/v1/dictation/stream")
    monkeypatch.setattr(dictation, "_engine", None)
    assert dictation.engine_availability() == (True, None)
    engine = dictation.get_engine()
    assert isinstance(engine, dictation.RemoteDictationEngine)


def _handshake_status(url: str, headers: list[tuple[str, str]]) -> int:
    """Open a raw handshake with *headers*; return the rejection status."""
    with pytest.raises(InvalidStatus) as rejected:
        connect(url, open_timeout=5, additional_headers=headers).close()
    return rejected.value.response.status_code


@pytest.mark.parametrize("token", [_TOKEN])
@pytest.mark.parametrize(
    "headers",
    [
        [],
        [("Authorization", "Bearer wrong-token")],
        [("Authorization", f"Basic {_TOKEN}")],
        [("Authorization", f"Bearer{_TOKEN}")],
        [("Authorization", f"Bearer {_TOKEN} extra")],
        [("Authorization", f"Bearer {_TOKEN}"), ("Authorization", f"Bearer {_TOKEN}")],
    ],
    ids=["missing", "wrong", "basic", "no-space", "trailing", "duplicate"],
)
def test_worker_rejects_bad_credentials_before_engine(
    worker_url: str,
    token: str,
    headers: list[tuple[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rejected handshakes never reach the engine or a capacity slot."""
    resolved: list[dictation.DictationEngine] = []

    def _spy_factory() -> dictation.DictationEngine:
        resolved.append(dictation.FakeDictationEngine())
        return resolved[-1]

    # The worker's route resolves through get_engine, which reads this registry.
    monkeypatch.setattr(dictation, "_ENGINE_REGISTRY", dict(dictation._ENGINE_REGISTRY))
    dictation.register_engine("spy", _spy_factory)
    monkeypatch.setenv(dictation.ENGINE_ENV, "spy")
    assert _handshake_status(worker_url, headers) == 403
    assert resolved == []
    # Rejections did not consume slots: a full cap of valid takes still opens.
    engine = dictation.RemoteDictationEngine(worker_url, token=token)
    handles = [engine.create_stream() for _ in range(2)]
    for handle in handles:
        handle.close()
    assert len(resolved) == 1


@pytest.mark.parametrize("token", [_TOKEN])
def test_worker_accepts_case_insensitive_scheme(worker_url: str, token: str) -> None:
    """The bearer scheme is matched case-insensitively."""
    from omnigent.server.dictation_worker import _WorkerTokenAuth

    assert token not in repr(_WorkerTokenAuth(token))
    with connect(
        worker_url, open_timeout=5, additional_headers=[("Authorization", f"bEaReR {token}")]
    ) as websocket:
        assert websocket.protocol.state.name == "OPEN"


@pytest.mark.parametrize("token", [_TOKEN])
def test_rejected_token_does_not_fall_back(worker_url: str, token: str) -> None:
    """A 403 is misconfiguration: surface it instead of using local models."""
    del token
    engine = dictation.RemoteDictationEngine(
        worker_url, token="other-token", fallback_factory=dictation.FakeDictationEngine
    )
    with pytest.raises(InvalidStatus):
        engine.create_stream()


@pytest.mark.parametrize("token", [_TOKEN])
def test_tokenless_engine_against_token_worker_does_not_fall_back(
    worker_url: str, token: str
) -> None:
    """A missing server-side token is misconfiguration, not an outage."""
    del token
    engine = dictation.RemoteDictationEngine(
        worker_url, fallback_factory=dictation.FakeDictationEngine
    )
    with pytest.raises(InvalidStatus):
        engine.create_stream()


@pytest.mark.parametrize("token", [_TOKEN])
def test_env_factory_forwards_worker_token(
    worker_url: str, token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The env-built remote engine sends OMNIGENT_DICTATION_WORKER_TOKEN."""
    monkeypatch.setenv(dictation.ENGINE_ENV, dictation.ENGINE_REMOTE)
    monkeypatch.setenv(dictation.REMOTE_URL_ENV, worker_url)
    monkeypatch.setenv(dictation.WORKER_TOKEN_ENV, token)
    assert dictation.engine_availability() == (True, None)
    engine = dictation.get_engine()
    # The in-process worker shares the engine cache; keep it serving the fake.
    monkeypatch.setattr(dictation, "_engine", dictation.FakeDictationEngine())
    handle = engine.create_stream()
    handle.feed_pcm16(_WORD)
    _drain_partial(handle, _WORDS[0])
    handle.close()


_SECRET = "s3cr3t-value"


@pytest.mark.parametrize(
    "value",
    ["", " ", f"{_SECRET} x", f"{_SECRET}\n", f"{_SECRET}\u00e9"],
    ids=["empty", "blank", "space", "newline", "non-ascii"],
)
def test_invalid_worker_token_fails_startup_without_echo(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Set-but-invalid tokens fail closed on both sides and are not echoed."""
    from omnigent.server.dictation_worker import create_worker_app

    monkeypatch.setenv(dictation.WORKER_TOKEN_ENV, value)
    for build in (create_worker_app, dictation.worker_token):
        with pytest.raises(ValueError) as error:
            build()
        assert _SECRET not in str(error.value)
    with pytest.raises(ValueError) as error:
        dictation.RemoteDictationEngine("wss://worker.example/v1/dictation/stream", token=value)
    assert _SECRET not in str(error.value)


@pytest.mark.parametrize(
    "url",
    [
        f"ws://worker.example/v1/dictation/stream?k={_SECRET}",
        f"wss://user:{_SECRET}@worker.example/v1/dictation/stream",
        f"wss://worker.example/v1/dictation/stream?k={_SECRET}",
        f"wss://worker.example/v1/dictation/stream#{_SECRET}",
        f"http://worker.example/{_SECRET}",
        f"wss://worker.example:{_SECRET}/v1",
        f"ws://{_SECRET}.example/v1/dictation/stream",
        "ws://127.0.0.1.example/v1/dictation/stream",
    ],
    ids=[
        "ws-remote",
        "userinfo",
        "query",
        "fragment",
        "http",
        "bad-port",
        "ws-host",
        "ws-lookalike",
    ],
)
def test_authenticated_engine_rejects_unsafe_urls(url: str) -> None:
    """A token only travels over TLS or loopback, and errors hide the URL."""
    with pytest.raises(ValueError) as error:
        dictation.RemoteDictationEngine(
            url, token=_TOKEN, fallback_factory=dictation.FakeDictationEngine
        )
    assert _SECRET not in str(error.value)


@pytest.mark.parametrize(
    "url",
    [
        "wss://worker.example:8100/v1/dictation/stream",
        "ws://localhost:8100/v1/dictation/stream",
        "ws://127.0.0.2:8100/v1/dictation/stream",
        "ws://[::1]:8100/v1/dictation/stream",
    ],
)
def test_authenticated_engine_accepts_tls_or_loopback(url: str) -> None:
    """Construction validates without connecting."""
    dictation.RemoteDictationEngine(url, token=_TOKEN)


def test_invalid_env_token_is_not_swallowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bad token reports unavailable and fails the factory, never disabling auth."""
    monkeypatch.setenv(dictation.ENGINE_ENV, dictation.ENGINE_REMOTE)
    monkeypatch.setenv(dictation.REMOTE_URL_ENV, "wss://worker.example/v1/dictation/stream")
    monkeypatch.setenv(dictation.WORKER_TOKEN_ENV, f"{_SECRET} x")
    assert dictation.engine_availability() == (False, dictation.REASON_REMOTE_CONFIG_INVALID)
    with pytest.raises(RuntimeError) as error:
        dictation.get_engine()
    assert _SECRET not in str(error.value)


@pytest.mark.parametrize("tls_files", [True], indirect=True)
@pytest.mark.parametrize("token", [_TOKEN])
def test_authenticated_take_over_verified_tls(
    tmp_path: Path,
    worker_url: str,
    token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """wss:// with a trusted cert relays a take; an untrusted cert never falls back."""

    def _no_fallback() -> dictation.DictationEngine:
        raise AssertionError("fell back on an untrusted worker certificate")

    untrusted = dictation.RemoteDictationEngine(
        worker_url, token=token, fallback_factory=_no_fallback
    )
    with pytest.raises(ssl.SSLCertVerificationError):
        untrusted.create_stream()
    monkeypatch.setenv("SSL_CERT_FILE", str(ensure_ca(cache_dir=tmp_path)[0]))
    handle = dictation.RemoteDictationEngine(worker_url, token=token).create_stream()
    handle.feed_pcm16(_WORD * 2)
    _drain_partial(handle, " ".join(_WORDS[:2]))
    assert handle.finish() == " ".join(_WORDS[:2])
