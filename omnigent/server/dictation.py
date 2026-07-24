"""Local streaming speech-to-text engine for composer dictation.

Backs the ``WS /v1/dictation/stream`` route
(:mod:`omnigent.server.routes.dictation`) with an on-server recognizer
so dictation works where the browser Web Speech API does not (Electron,
Firefox/Chromium, self-hosted deployments) and audio never leaves the
operator's infrastructure. See ``designs/server-dictation.md``.

Engine selection
----------------

Engines are looked up by name in a small registry
(:func:`register_engine`), selected via ``OMNIGENT_DICTATION_ENGINE``:

- unset (default) — the sherpa-onnx engine. Requires the ``dictation``
  extra (``pip install omnigent[dictation]``) and a streaming transducer
  model on disk; both are checked lazily so the base install carries no
  new dependencies.
- ``sherpa`` — the same engine, named explicitly.
- ``remote`` — relays takes to a dictation worker on another machine
  (``OMNIGENT_DICTATION_REMOTE_URL``), so a small main server can borrow
  a beefier LAN box's CPU. Falls back to the local sherpa engine (when
  models are installed) if the worker is unreachable. See
  :class:`RemoteDictationEngine` and ``dictation_worker.py``.
- ``fake`` — a deterministic scripted engine used by tests and the
  Playwright e2e suite; no native dependency, no models, no microphone.

Adding an engine (e.g. Whisper) is one :func:`register_engine` call with
a factory and an availability probe — no edits to :func:`get_engine` or
:func:`engine_availability`. Third-party engines register themselves on
import.

sherpa-onnx engine
------------------

A process-wide ``OnlineRecognizer`` (streaming transducer:
``encoder/decoder/joiner + tokens.txt``) is shared across connections so
the model weights load once; each WebSocket gets its own recognizer
*stream*. Endpoint detection folds completed utterances into
``DictationUpdate.finalized`` and resets the stream. An optional online
punctuation model re-punctuates emitted text (the raw transducer output
is lowercased and stripped of punctuation first — the model wants clean
input) so live partials read like sentences. The recognizer returns
display-ready text directly; punctuation is an internal detail, not part
of the engine protocol (most models — Whisper, Parakeet — punctuate
themselves).

Recognizer calls are CPU-bound and sherpa streams are not documented
thread-safe, so every recognizer/punctuation call holds the engine's
``threading.Lock``; callers run them via ``asyncio.to_thread`` to keep
the event loop responsive.

Model layout
------------

======================================  ==========================================
Env var                                 Default
======================================  ==========================================
``OMNIGENT_DICTATION_MODEL_DIR``        ``~/.omnigent/models/dictation/asr``
``OMNIGENT_DICTATION_PUNCT_DIR``        ``~/.omnigent/models/dictation/punct``
======================================  ==========================================

The ASR dir must contain ``encoder*.onnx``, ``decoder*.onnx``,
``joiner*.onnx`` and ``tokens.txt`` (int8 variants preferred when both
are present). The punctuation dir (``model*.onnx`` + ``bpe.vocab``) is
optional — without it, raw recognizer output is emitted as-is.
``scripts/fetch-dictation-models.sh`` downloads a known-good pair into
the default locations.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import logging
import os
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

_logger = logging.getLogger(__name__)

ENGINE_ENV = "OMNIGENT_DICTATION_ENGINE"
MODEL_DIR_ENV = "OMNIGENT_DICTATION_MODEL_DIR"
PUNCT_DIR_ENV = "OMNIGENT_DICTATION_PUNCT_DIR"
MAX_STREAMS_ENV = "OMNIGENT_DICTATION_MAX_STREAMS"
#: Worker stream URL for the ``remote`` engine, e.g.
#: ``ws://venus:8100/v1/dictation/stream``.
REMOTE_URL_ENV = "OMNIGENT_DICTATION_REMOTE_URL"

#: Built-in engine names. The default (empty ``OMNIGENT_DICTATION_ENGINE``)
#: resolves to the sherpa engine.
ENGINE_SHERPA = "sherpa"
ENGINE_FAKE = "fake"
ENGINE_REMOTE = "remote"
_DEFAULT_ENGINE = ENGINE_SHERPA

#: Worker handshake budget: covers a cold model load on the worker side.
_REMOTE_READY_TIMEOUT_S = 30.0
_REMOTE_STOP_TIMEOUT_S = 10.0

#: The one PCM format the stream route accepts: 16 kHz mono s16le.
SAMPLE_RATE = 16000
_BYTES_PER_SECOND = SAMPLE_RATE * 2

#: Stable machine-readable unavailability reasons.
REASON_EXTRA_NOT_INSTALLED = "extra_not_installed"
REASON_MODELS_MISSING = "models_missing"
REASON_UNKNOWN_ENGINE = "unknown_engine"
REASON_REMOTE_URL_MISSING = "remote_url_missing"

DEFAULT_MAX_STREAMS = 2

# Endpoint rules mirror sherpa-onnx defaults tuned for dictation: a long
# hard stop (rule1, silence with no text yet), a shorter pause once
# something was said (rule2), and a max utterance length (rule3).
_RULE1_MIN_TRAILING_SILENCE_S = 3.5
_RULE2_MIN_TRAILING_SILENCE_S = 1.6
_RULE3_MIN_UTTERANCE_LENGTH_S = 30.0

_PUNCT_STRIP_RE = re.compile(r"[.,?!:;…]+")


@dataclass(frozen=True)
class DictationUpdate:
    """Result of feeding one audio chunk to a dictation stream.

    :param partial: The current in-progress utterance, display-ready
        (punctuated/cased by the engine if it does that). Revisable —
        later updates may rewrite earlier words as more context arrives.
    :param finalized: An utterance completed by endpoint detection (a
        pause), if one closed on this chunk, display-ready. The partial
        restarts empty after a finalized utterance.
    """

    partial: str
    finalized: str | None = None


class DictationStreamHandle(Protocol):
    """One dictation take: a stateful recognizer stream.

    All methods are synchronous and CPU-bound; call them via
    ``asyncio.to_thread`` from async code. Emitted text is display-ready:
    engines that need punctuation/casing apply it internally before
    returning (see the sherpa engine), so the route just forwards text.
    """

    def feed_pcm16(self, data: bytes) -> DictationUpdate:
        """Feed a chunk of 16 kHz mono s16le PCM and decode it."""
        ...

    def finish(self) -> str:
        """Flush trailing audio and return the final tail utterance."""
        ...

    def close(self) -> None:
        """Release the take's resources without flushing (client vanished).

        Idempotent, and safe after :meth:`finish`. A no-op for the
        in-process engines (the stream frees with the handle); the hook
        exists for engines holding an external resource.
        """
        ...


class DictationEngine(Protocol):
    """Factory for dictation streams; one engine is shared per process."""

    def create_stream(self) -> DictationStreamHandle:
        """Open a fresh recognizer stream for one connection."""
        ...


#: An engine's availability probe: ``() -> (available, reason)`` where
#: *reason* is ``None`` when available, else a machine-readable
#: ``REASON_*`` string. Called without loading any model.
AvailabilityProbe = Callable[[], "tuple[bool, str | None]"]
EngineFactory = Callable[[], DictationEngine]


@dataclass(frozen=True)
class _EngineEntry:
    factory: EngineFactory
    available: AvailabilityProbe


_ENGINE_REGISTRY: dict[str, _EngineEntry] = {}


def register_engine(
    name: str,
    factory: EngineFactory,
    *,
    available: AvailabilityProbe | None = None,
) -> None:
    """Register a dictation engine under *name*.

    Selected via ``OMNIGENT_DICTATION_ENGINE=<name>``. This is the whole
    swap-in surface: a new engine (Whisper, Parakeet, …) is one call with
    a factory and an optional availability probe — no edits to
    :func:`get_engine` or :func:`engine_availability`.

    :param name: Selector value, e.g. ``"whisper"``.
    :param factory: Builds the engine on first use (weights load here —
        keep it lazy).
    :param available: Probe returning ``(available, reason)`` without
        loading a model. Defaults to always-available (``(True, None)``)
        — right for engines with no optional dependency or model on disk.
    """
    _ENGINE_REGISTRY[name] = _EngineEntry(
        factory=factory,
        available=available or (lambda: (True, None)),
    )


def _asr_dir() -> Path:
    default = Path.home() / ".omnigent" / "models" / "dictation" / "asr"
    return Path(os.environ.get(MODEL_DIR_ENV) or default).expanduser()


def _punct_dir() -> Path:
    default = Path.home() / ".omnigent" / "models" / "dictation" / "punct"
    return Path(os.environ.get(PUNCT_DIR_ENV) or default).expanduser()


def max_streams() -> int:
    """Concurrent dictation connections allowed (decode is CPU-bound)."""
    raw = os.environ.get(MAX_STREAMS_ENV, "")
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_STREAMS
    return value if value > 0 else DEFAULT_MAX_STREAMS


def _pick_model_file(model_dir: Path, stem: str) -> Path | None:
    """Find ``<stem>*.onnx`` in *model_dir*, preferring int8 variants.

    Quantized files decode fastest on CPU and are what the fetch script
    installs; float fallbacks let operators drop in any upstream export.
    """
    candidates = sorted(model_dir.glob(f"{stem}*.onnx"))
    if not candidates:
        return None
    for candidate in candidates:
        if "int8" in candidate.name:
            return candidate
    return candidates[0]


def _asr_files(model_dir: Path) -> dict[str, Path] | None:
    """Resolve the transducer file set, or ``None`` if incomplete."""
    tokens = model_dir / "tokens.txt"
    encoder = _pick_model_file(model_dir, "encoder")
    decoder = _pick_model_file(model_dir, "decoder")
    joiner = _pick_model_file(model_dir, "joiner")
    if not tokens.is_file() or encoder is None or decoder is None or joiner is None:
        return None
    return {"tokens": tokens, "encoder": encoder, "decoder": decoder, "joiner": joiner}


def _punct_files(punct_dir: Path) -> dict[str, Path] | None:
    """Resolve the optional punctuation file set, or ``None``."""
    model = _pick_model_file(punct_dir, "model")
    vocab = punct_dir / "bpe.vocab"
    if model is None or not vocab.is_file():
        return None
    return {"model": model, "vocab": vocab}


def _sherpa_available() -> tuple[bool, str | None]:
    """Availability probe for the sherpa engine (loads nothing)."""
    if importlib.util.find_spec("sherpa_onnx") is None:
        return False, REASON_EXTRA_NOT_INSTALLED
    if _asr_files(_asr_dir()) is None:
        return False, REASON_MODELS_MISSING
    return True, None


def _selected_engine_name() -> str:
    """Resolve the configured engine name (default: sherpa)."""
    return os.environ.get(ENGINE_ENV, "").strip() or _DEFAULT_ENGINE


def engine_availability() -> tuple[bool, str | None]:
    """Report whether dictation can serve, without loading any model.

    Resolves the configured engine and calls its registered availability
    probe. Unknown engine names report unavailable.

    :returns: ``(available, reason)`` where *reason* is ``None`` when
        available, else a machine-readable ``REASON_*`` string.
    """
    entry = _ENGINE_REGISTRY.get(_selected_engine_name())
    if entry is None:
        return False, REASON_UNKNOWN_ENGINE
    return entry.available()


_engine_lock = threading.Lock()
_engine: DictationEngine | None = None


def get_engine() -> DictationEngine:
    """Return the process-wide engine, loading models on first use.

    The configured engine name is resolved once, on the first successful
    load — a failed load caches nothing, so a server that gains models
    later serves the next take without a restart. Tests never hit this:
    they inject an engine through the router's ``engine_provider``.

    :raises RuntimeError: When the configured engine is unknown or
        unavailable (check :func:`engine_availability` first), or the
        model fails to load.
    """
    global _engine
    with _engine_lock:
        if _engine is not None:
            return _engine
        name = _selected_engine_name()
        entry = _ENGINE_REGISTRY.get(name)
        if entry is None:
            raise RuntimeError(f"unknown dictation engine: {name!r}")
        available, reason = entry.available()
        if not available:
            raise RuntimeError(f"dictation unavailable: {reason}")
        _engine = entry.factory()
        return _engine


class SherpaDictationEngine:
    """Streaming sherpa-onnx transducer + optional online punctuation."""

    def __init__(self, asr_dir: Path, punct_dir: Path) -> None:
        """Load models eagerly; construction is slow (seconds).

        :param asr_dir: Directory holding the streaming transducer.
        :param punct_dir: Directory holding the optional punctuation
            model; silently skipped when absent or incomplete.
        :raises RuntimeError: If the ASR file set is incomplete.
        """
        import sherpa_onnx

        files = _asr_files(asr_dir)
        if files is None:
            raise RuntimeError(f"dictation ASR model incomplete in {asr_dir}")
        _logger.info("Loading dictation ASR model from %s", asr_dir)
        self._recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=str(files["tokens"]),
            encoder=str(files["encoder"]),
            decoder=str(files["decoder"]),
            joiner=str(files["joiner"]),
            num_threads=4,
            sample_rate=SAMPLE_RATE,
            feature_dim=80,
            enable_endpoint_detection=True,
            rule1_min_trailing_silence=_RULE1_MIN_TRAILING_SILENCE_S,
            rule2_min_trailing_silence=_RULE2_MIN_TRAILING_SILENCE_S,
            rule3_min_utterance_length=_RULE3_MIN_UTTERANCE_LENGTH_S,
            decoding_method="greedy_search",
            provider="cpu",
        )
        self._punct: Any = None
        punct_files = _punct_files(punct_dir)
        if punct_files is not None:
            try:
                self._punct = sherpa_onnx.OnlinePunctuation(
                    sherpa_onnx.OnlinePunctuationConfig(
                        model_config=sherpa_onnx.OnlinePunctuationModelConfig(
                            cnn_bilstm=str(punct_files["model"]),
                            bpe_vocab=str(punct_files["vocab"]),
                            num_threads=1,
                            provider="cpu",
                        )
                    )
                )
            except Exception:  # noqa: BLE001 - punctuation is best-effort
                _logger.warning(
                    "dictation punctuation model failed to load from %s; "
                    "emitting raw recognizer output",
                    punct_dir,
                    exc_info=True,
                )
        # Serializes all recognizer/punctuation calls: sherpa streams are
        # not documented thread-safe, and decode is CPU-bound anyway.
        self._lock = threading.Lock()

    def _beautify(self, text: str) -> str:
        """Re-punctuate and re-case *text* for display.

        Internal: the raw transducer emits lowercase, punctuation-free
        text, so the streams call this before returning so partials/finals
        read like sentences. Identity when no punctuation model loaded.
        """
        if self._punct is None or not text:
            return text
        # The model expects lowercase, punctuation-free input.
        cleaned = _PUNCT_STRIP_RE.sub("", text.lower())
        try:
            with self._lock:
                return self._punct.add_punctuation_with_case(cleaned)
        except Exception:  # noqa: BLE001 - never fail a take over cosmetics
            return text

    def create_stream(self) -> _SherpaStream:
        """Open a recognizer stream for one connection."""
        with self._lock:
            return _SherpaStream(self, self._recognizer.create_stream())


class _SherpaStream:
    """Per-connection recognizer stream (see :class:`DictationStreamHandle`)."""

    def __init__(self, engine: SherpaDictationEngine, stream: Any) -> None:
        self._engine = engine
        self._stream = stream

    def feed_pcm16(self, data: bytes) -> DictationUpdate:
        """Decode one PCM chunk; fold an endpoint into ``finalized``."""
        import numpy as np

        # Drop a trailing odd byte rather than crash the take; the next
        # frame realigns (client frames are always whole samples).
        usable = len(data) - (len(data) % 2)
        if usable <= 0:
            return DictationUpdate(partial="")
        samples = np.frombuffer(data[:usable], dtype=np.int16).astype(np.float32) / 32768.0
        engine = self._engine
        recognizer = engine._recognizer
        with engine._lock:
            self._stream.accept_waveform(SAMPLE_RATE, samples)
            while recognizer.is_ready(self._stream):
                recognizer.decode_stream(self._stream)
            partial = recognizer.get_result(self._stream).strip()
            finalized: str | None = None
            if recognizer.is_endpoint(self._stream):
                if partial:
                    finalized = partial
                partial = ""
                recognizer.reset(self._stream)
        # Punctuate outside the recognizer lock's decode section (beautify
        # takes the lock itself). Emit display-ready text so the route and
        # protocol stay engine-agnostic.
        return DictationUpdate(
            partial=engine._beautify(partial),
            finalized=engine._beautify(finalized) if finalized else None,
        )

    def finish(self) -> str:
        """Flush the tail: pad with silence, drain, return final text."""
        import numpy as np

        engine = self._engine
        recognizer = engine._recognizer
        with engine._lock:
            # One second of silence pushes trailing speech past the
            # feature window so the last words decode.
            self._stream.accept_waveform(SAMPLE_RATE, np.zeros(SAMPLE_RATE, dtype=np.float32))
            self._stream.input_finished()
            while recognizer.is_ready(self._stream):
                recognizer.decode_stream(self._stream)
            tail = recognizer.get_result(self._stream).strip()
        return engine._beautify(tail)

    def close(self) -> None:
        """No-op: the recognizer stream frees with the handle."""


class RemoteDictationEngine:
    """Relays dictation takes to a remote worker over WebSocket.

    The worker is anything speaking the ``/v1/dictation/stream`` wire
    protocol — another omnigent server or the standalone
    ``python -m omnigent.server.dictation_worker``. Lets a small main
    server (a mini-PC) borrow a beefier LAN box for recognition.

    Fallback happens per take, at stream creation: if the worker is
    unreachable, the lazily-built local engine (when models are
    installed) serves the take instead. A worker dying mid-take fails
    that take; the next one retries the worker.
    """

    def __init__(
        self,
        url: str,
        *,
        fallback_factory: Callable[[], DictationEngine] | None = None,
    ) -> None:
        """
        :param url: Worker stream URL, e.g.
            ``ws://venus:8100/v1/dictation/stream``.
        :param fallback_factory: Builds the local fallback engine on
            first use (lazy — its model weights cost ~real RAM), or
            ``None`` when no local model is installed.
        """
        self._url = url
        self._fallback_factory = fallback_factory
        self._fallback: DictationEngine | None = None
        self._fallback_lock = threading.Lock()

    def create_stream(self) -> DictationStreamHandle:
        """Connect a take to the worker, or to the local fallback."""
        try:
            return _RemoteStream(self._url)
        except Exception:
            if self._fallback_factory is None:
                raise
            _logger.warning(
                "dictation worker unreachable at %s; using local fallback engine",
                self._url,
                exc_info=True,
            )
            with self._fallback_lock:
                if self._fallback is None:
                    self._fallback = self._fallback_factory()
            return self._fallback.create_stream()


class _RemoteStream:
    """One relayed take: raw PCM up, transcript events down.

    A daemon reader thread folds the worker's ``partial``/``final``
    events into state that :meth:`feed_pcm16` returns on each call, so
    the relay presents the same synchronous handle interface the local
    engines do. The worker returns display-ready text already, so the
    relay just forwards it.
    """

    def __init__(self, url: str) -> None:
        from websockets.sync.client import connect

        self._ws = connect(url, open_timeout=5)
        try:
            deadline = time.monotonic() + _REMOTE_READY_TIMEOUT_S
            while True:
                message = self._ws.recv(timeout=max(0.1, deadline - time.monotonic()))
                if not isinstance(message, str):
                    continue
                event = json.loads(message)
                if event.get("type") == "ready":
                    break
                if event.get("type") == "error":
                    raise RuntimeError(f"dictation worker error: {event.get('message')}")
        except BaseException:
            self._ws.close()
            raise
        self._lock = threading.Lock()
        self._partial = ""
        self._finals: list[str] = []
        self._tail = ""
        self._dead = False
        self._stopped = threading.Event()
        threading.Thread(target=self._read_loop, daemon=True).start()

    def _read_loop(self) -> None:
        try:
            while True:
                message = self._ws.recv()
                if not isinstance(message, str):
                    continue
                try:
                    event = json.loads(message)
                except ValueError:
                    continue
                kind = event.get("type")
                with self._lock:
                    if kind == "partial":
                        self._partial = str(event.get("text", ""))
                    elif kind == "final":
                        self._finals.append(str(event.get("text", "")))
                        self._partial = ""
                    elif kind == "stopped":
                        self._tail = str(event.get("text", ""))
                        break
                    elif kind == "error":
                        self._dead = True
                        break
        except Exception:  # noqa: BLE001 - any transport failure kills the take
            with self._lock:
                self._dead = True
        self._stopped.set()

    def feed_pcm16(self, data: bytes) -> DictationUpdate:
        """Ship a chunk to the worker; return its latest transcript state."""
        with self._lock:
            if self._dead:
                raise RuntimeError("dictation worker connection lost")
        self._ws.send(data)
        with self._lock:
            finalized = " ".join(t for t in self._finals if t).strip() or None
            self._finals.clear()
            return DictationUpdate(partial=self._partial, finalized=finalized)

    def finish(self) -> str:
        """Ask the worker to flush; return its tail utterance."""
        with contextlib.suppress(Exception):
            self._ws.send(json.dumps({"type": "stop"}))
        self._stopped.wait(timeout=_REMOTE_STOP_TIMEOUT_S)
        self.close()
        with self._lock:
            return self._tail

    def close(self) -> None:
        """Close the worker socket, releasing its capacity slot.

        Also unblocks the reader thread's ``recv``. Idempotent — the
        sync websockets client tolerates repeated ``close`` calls.
        """
        with contextlib.suppress(Exception):
            self._ws.close()


def _remote_url() -> str:
    """The configured worker stream URL (may be empty)."""
    return os.environ.get(REMOTE_URL_ENV, "").strip()


def _remote_available() -> tuple[bool, str | None]:
    """Availability probe for the remote engine.

    A configured worker counts as available without probing it — the
    worker may be briefly down or still booting, and the stream route
    degrades cleanly (local fallback, or an error frame) when a take
    actually starts.
    """
    if not _remote_url():
        return False, REASON_REMOTE_URL_MISSING
    return True, None


def _build_remote_engine() -> RemoteDictationEngine:
    """Factory for the remote engine, with a lazy local fallback.

    Local models, when installed, back the worker up. The fallback
    factory is lazy so its ~650 MB of weights cost no RAM unless the
    worker actually goes down.
    """
    url = _remote_url()
    if not url:
        raise RuntimeError(f"dictation unavailable: {REASON_REMOTE_URL_MISSING}")
    fallback = (
        (lambda: SherpaDictationEngine(_asr_dir(), _punct_dir()))
        if _sherpa_available()[0]
        else None
    )
    return RemoteDictationEngine(url, fallback_factory=fallback)


#: Scripted transcript the fake engine reveals; asserted verbatim by the
#: server route tests and the Playwright e2e test.
FAKE_SCRIPT = "server dictation smoke test transcript"

# The fake reveals one word per this much audio, so tests control the
# transcript by the number of bytes they send.
_FAKE_BYTES_PER_WORD = _BYTES_PER_SECOND // 10


class FakeDictationEngine:
    """Deterministic engine for tests: audio bytes in, script words out.

    Reveals one word of :data:`FAKE_SCRIPT` per 100 ms of audio fed
    (regardless of content), finalizing the sentence when it completes.
    """

    def __init__(self) -> None:
        #: The most recently opened stream, for cleanup assertions.
        self.last_stream: _FakeStream | None = None

    def create_stream(self) -> _FakeStream:
        """Open a scripted stream."""
        self.last_stream = _FakeStream()
        return self.last_stream


class _FakeStream:
    """Per-connection scripted stream (see :class:`FakeDictationEngine`)."""

    def __init__(self) -> None:
        self._words = FAKE_SCRIPT.split()
        self._bytes_seen = 0
        self._done = False
        self.closed = False

    def feed_pcm16(self, data: bytes) -> DictationUpdate:
        """Reveal script words proportional to audio fed."""
        if self._done:
            return DictationUpdate(partial="")
        self._bytes_seen += len(data)
        revealed = self._bytes_seen // _FAKE_BYTES_PER_WORD
        if revealed >= len(self._words):
            self._done = True
            return DictationUpdate(partial="", finalized=" ".join(self._words))
        return DictationUpdate(partial=" ".join(self._words[:revealed]))

    def finish(self) -> str:
        """Return the words revealed so far as the tail utterance."""
        if self._done:
            return ""
        revealed = min(self._bytes_seen // _FAKE_BYTES_PER_WORD, len(self._words))
        self._done = True
        return " ".join(self._words[:revealed])

    def close(self) -> None:
        """Record the close so tests can assert take cleanup."""
        self.closed = True


# Built-in engines register themselves at import. The sherpa factory is
# lazy (weights load on first take), so importing this module costs no
# model RAM.
register_engine(
    ENGINE_SHERPA,
    lambda: SherpaDictationEngine(_asr_dir(), _punct_dir()),
    available=_sherpa_available,
)
register_engine(ENGINE_REMOTE, _build_remote_engine, available=_remote_available)
register_engine(ENGINE_FAKE, FakeDictationEngine)
