"""Local streaming speech-to-text engine for composer dictation.

Backs the ``WS /v1/dictation/stream`` route
(:mod:`omnigent.server.routes.dictation`) with an on-server recognizer
so dictation works where the browser Web Speech API does not (Electron,
Firefox/Chromium, self-hosted deployments) and audio never leaves the
operator's infrastructure. See ``designs/server-dictation.md``.

Engine selection
----------------

:func:`engine_availability` / :func:`get_engine` resolve the engine from
``OMNIGENT_DICTATION_ENGINE``:

- unset (default) — the sherpa-onnx engine. Requires the ``dictation``
  extra (``pip install omnigent[dictation]``) and a streaming transducer
  model on disk; both are checked lazily so the base install carries no
  new dependencies.
- ``"fake"`` — a deterministic scripted engine used by tests and the
  Playwright e2e suite; no native dependency, no models, no microphone.

sherpa-onnx engine
------------------

A process-wide ``OnlineRecognizer`` (streaming transducer:
``encoder/decoder/joiner + tokens.txt``) is shared across connections so
the model weights load once; each WebSocket gets its own recognizer
*stream*. Endpoint detection folds completed utterances into
``DictationUpdate.finalized`` and resets the stream. An optional online
punctuation model re-punctuates emitted text (the raw transducer output
is lowercased and stripped of punctuation first — the model wants clean
input) so live partials read like sentences.

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

import importlib.util
import logging
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

_logger = logging.getLogger(__name__)

ENGINE_ENV = "OMNIGENT_DICTATION_ENGINE"
MODEL_DIR_ENV = "OMNIGENT_DICTATION_MODEL_DIR"
PUNCT_DIR_ENV = "OMNIGENT_DICTATION_PUNCT_DIR"
MAX_STREAMS_ENV = "OMNIGENT_DICTATION_MAX_STREAMS"

ENGINE_FAKE = "fake"

#: The one PCM format the stream route accepts: 16 kHz mono s16le.
SAMPLE_RATE = 16000
_BYTES_PER_SECOND = SAMPLE_RATE * 2

#: Stable machine-readable unavailability reasons for ``GET /v1/dictation``.
REASON_EXTRA_NOT_INSTALLED = "extra_not_installed"
REASON_MODELS_MISSING = "models_missing"

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

    :param partial: The current in-progress utterance. Revisable — later
        updates may rewrite earlier words as more context arrives.
    :param finalized: An utterance completed by endpoint detection (a
        pause), if one closed on this chunk. The partial restarts empty
        after a finalized utterance.
    """

    partial: str
    finalized: str | None = None


class DictationStreamHandle(Protocol):
    """One dictation take: a stateful recognizer stream.

    All methods are synchronous and CPU-bound; call them via
    ``asyncio.to_thread`` from async code.
    """

    def feed_pcm16(self, data: bytes) -> DictationUpdate:
        """Feed a chunk of 16 kHz mono s16le PCM and decode it."""
        ...

    def beautify(self, text: str) -> str:
        """Re-punctuate/case *text* for display. Identity when the
        engine has no punctuation model."""
        ...

    def finish(self) -> str:
        """Flush trailing audio and return the final tail utterance."""
        ...


class DictationEngine(Protocol):
    """Factory for dictation streams; one engine is shared per process."""

    def create_stream(self) -> DictationStreamHandle:
        """Open a fresh recognizer stream for one connection."""
        ...


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


def engine_availability() -> tuple[bool, str | None]:
    """Report whether dictation can serve, without loading any model.

    :returns: ``(available, reason)`` where *reason* is ``None`` when
        available, else one of :data:`REASON_EXTRA_NOT_INSTALLED` /
        :data:`REASON_MODELS_MISSING`.
    """
    if os.environ.get(ENGINE_ENV) == ENGINE_FAKE:
        return True, None
    if importlib.util.find_spec("sherpa_onnx") is None:
        return False, REASON_EXTRA_NOT_INSTALLED
    if _asr_files(_asr_dir()) is None:
        return False, REASON_MODELS_MISSING
    return True, None


_engine_lock = threading.Lock()
_engine: DictationEngine | None = None
_engine_key: tuple[str, ...] | None = None


def get_engine() -> DictationEngine:
    """Return the process-wide engine, loading models on first use.

    The engine is keyed on the resolved configuration so tests that
    flip env vars (fake ↔ sherpa, different model dirs) get a fresh
    engine instead of a stale singleton.

    :raises RuntimeError: When dictation is unavailable (check
        :func:`engine_availability` first) or the model fails to load.
    """
    global _engine, _engine_key
    key = (
        os.environ.get(ENGINE_ENV, ""),
        str(_asr_dir()),
        str(_punct_dir()),
    )
    with _engine_lock:
        if _engine is not None and _engine_key == key:
            return _engine
        if os.environ.get(ENGINE_ENV) == ENGINE_FAKE:
            _engine = FakeDictationEngine()
        else:
            available, reason = engine_availability()
            if not available:
                raise RuntimeError(f"dictation unavailable: {reason}")
            _engine = SherpaDictationEngine(_asr_dir(), _punct_dir())
        _engine_key = key
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

    def beautify(self, text: str) -> str:
        """Re-punctuate and re-case *text* for display."""
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
        return DictationUpdate(partial=partial, finalized=finalized)

    def beautify(self, text: str) -> str:
        """Delegate to the engine's punctuation model."""
        return self._engine.beautify(text)

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
        return self.beautify(tail)


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

    def create_stream(self) -> _FakeStream:
        """Open a scripted stream."""
        return _FakeStream()


class _FakeStream:
    """Per-connection scripted stream (see :class:`FakeDictationEngine`)."""

    def __init__(self) -> None:
        self._words = FAKE_SCRIPT.split()
        self._bytes_seen = 0
        self._done = False

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

    def beautify(self, text: str) -> str:
        """Identity — the fake has no punctuation model."""
        return text

    def finish(self) -> str:
        """Return the words revealed so far as the tail utterance."""
        if self._done:
            return ""
        revealed = min(self._bytes_seen // _FAKE_BYTES_PER_WORD, len(self._words))
        self._done = True
        return " ".join(self._words[:revealed])
