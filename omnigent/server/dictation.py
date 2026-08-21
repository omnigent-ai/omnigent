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
import platform
import re
import ssl
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from omnigent.server.dictation_metrics import metrics

_logger = logging.getLogger(__name__)

ENGINE_ENV = "OMNIGENT_DICTATION_ENGINE"
MODEL_DIR_ENV = "OMNIGENT_DICTATION_MODEL_DIR"
PUNCT_DIR_ENV = "OMNIGENT_DICTATION_PUNCT_DIR"
MODEL_ENV = "OMNIGENT_DICTATION_MODEL"
MODEL_CACHE_DIR_ENV = "OMNIGENT_DICTATION_MODEL_CACHE_DIR"
MAX_STREAMS_ENV = "OMNIGENT_DICTATION_MAX_STREAMS"
MAX_FRAME_BYTES_ENV = "OMNIGENT_DICTATION_MAX_FRAME_BYTES"
MAX_TAKE_SECONDS_ENV = "OMNIGENT_DICTATION_MAX_TAKE_SECONDS"
#: Worker stream URL for the ``remote`` engine, e.g.
#: ``ws://venus:8100/v1/dictation/stream``.
REMOTE_URL_ENV = "OMNIGENT_DICTATION_REMOTE_URL"
WORKER_TOKEN_ENV = "OMNIGENT_DICTATION_WORKER_TOKEN"
REMOTE_CA_FILE_ENV = "OMNIGENT_DICTATION_REMOTE_CA_FILE"
ALLOW_INSECURE_REMOTE_ENV = "OMNIGENT_DICTATION_ALLOW_INSECURE_REMOTE"

#: Built-in engine names. An empty ``OMNIGENT_DICTATION_ENGINE`` prefers
#: Parakeet MLX when installed on Apple Silicon, then falls back to sherpa.
ENGINE_SHERPA = "sherpa"
ENGINE_PARAKEET_MLX = "parakeet_mlx"
ENGINE_FAKE = "fake"
ENGINE_REMOTE = "remote"

#: Worker handshake budget: covers a cold model load on the worker side.
_REMOTE_READY_TIMEOUT_S = 30.0
_REMOTE_STOP_TIMEOUT_S = 10.0

#: The one PCM format the stream route accepts: 16 kHz mono s16le.
SAMPLE_RATE = 16000
_BYTES_PER_SECOND = SAMPLE_RATE * 2

#: Stable machine-readable unavailability reasons.
REASON_EXTRA_NOT_INSTALLED = "extra_not_installed"
REASON_MODELS_MISSING = "models_missing"
REASON_UNSUPPORTED_PLATFORM = "unsupported_platform"
REASON_UNKNOWN_ENGINE = "unknown_engine"
REASON_REMOTE_URL_MISSING = "remote_url_missing"
REASON_REMOTE_TOKEN_MISSING = "remote_token_missing"
REASON_INSECURE_REMOTE = "insecure_remote"
REASON_INVALID_REMOTE_URL = "invalid_remote_url"

DEFAULT_MAX_STREAMS = 2
DEFAULT_MAX_FRAME_BYTES = 256 * 1024
DEFAULT_MAX_TAKE_SECONDS = 300.0

# Endpoint rules mirror sherpa-onnx defaults tuned for dictation: a long
# hard stop (rule1, silence with no text yet), a shorter pause once
# something was said (rule2), and a max utterance length (rule3).
_RULE1_MIN_TRAILING_SILENCE_S = 3.5
_RULE2_MIN_TRAILING_SILENCE_S = 1.6
_RULE3_MIN_UTTERANCE_LENGTH_S = 30.0

_DEFAULT_MLX_MODEL = "mlx-community/parakeet-tdt-0.6b-v3"
_MLX_CONTEXT_SIZE = (256, 16)
_MLX_SPEECH_RMS = 0.01
_MLX_VAD_WINDOW_SAMPLES = SAMPLE_RATE // 10

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


def _mlx_model() -> str:
    return os.environ.get(MODEL_ENV, "").strip() or _DEFAULT_MLX_MODEL


def _mlx_cache_dir() -> Path:
    default = Path.home() / ".omnigent" / "models" / "dictation" / "parakeet-mlx"
    return Path(os.environ.get(MODEL_CACHE_DIR_ENV) or default).expanduser()


def max_streams() -> int:
    """Concurrent dictation connections allowed (decode is CPU-bound)."""
    raw = os.environ.get(MAX_STREAMS_ENV, "")
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_STREAMS
    return value if value > 0 else DEFAULT_MAX_STREAMS


def max_frame_bytes() -> int:
    """Maximum bytes accepted in one browser or relay audio frame."""
    return _positive_int_env(MAX_FRAME_BYTES_ENV, DEFAULT_MAX_FRAME_BYTES)


def max_take_seconds() -> float:
    """Maximum wall-clock and audio duration accepted for one take."""
    raw = os.environ.get(MAX_TAKE_SECONDS_ENV, "")
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_MAX_TAKE_SECONDS
    return value if value > 0 else DEFAULT_MAX_TAKE_SECONDS


def _positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


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


def _parakeet_mlx_available() -> tuple[bool, str | None]:
    """Availability probe for the Apple Silicon MLX engine (loads nothing)."""
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        return False, REASON_UNSUPPORTED_PLATFORM
    if importlib.util.find_spec("parakeet_mlx") is None:
        return False, REASON_EXTRA_NOT_INSTALLED
    model = Path(_mlx_model()).expanduser()
    if model.is_absolute() and not model.is_dir():
        return False, REASON_MODELS_MISSING
    return True, None


def _selected_engine_name() -> str:
    """Resolve an explicit engine or the best installed local default."""
    configured = os.environ.get(ENGINE_ENV, "").strip()
    if configured:
        return configured
    if (
        platform.system() == "Darwin"
        and platform.machine() == "arm64"
        and importlib.util.find_spec("parakeet_mlx") is not None
    ):
        return ENGINE_PARAKEET_MLX
    return ENGINE_SHERPA


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


_STATUS_ACTIONS = {
    REASON_EXTRA_NOT_INSTALLED: "Install the dictation extra.",
    REASON_MODELS_MISSING: "Install or configure the dictation ASR model files.",
    REASON_UNSUPPORTED_PLATFORM: "Use the MLX engine on an Apple Silicon Mac.",
    REASON_UNKNOWN_ENGINE: "Set a registered dictation engine name.",
    REASON_REMOTE_URL_MISSING: "Set the remote worker WebSocket URL.",
    REASON_REMOTE_TOKEN_MISSING: "Set the shared dictation worker token.",
    REASON_INSECURE_REMOTE: "Use wss, loopback, or explicitly allow insecure remote transport.",
    REASON_INVALID_REMOTE_URL: "Set a valid ws or wss remote worker URL.",
}


def engine_status() -> dict[str, object]:
    """Return stable diagnostics without secrets, paths, or exception text."""
    name = _selected_engine_name()
    available, reason = engine_availability()
    result: dict[str, object] = {
        "available": available,
        "engine": name,
        "reason": reason,
        "action": _STATUS_ACTIONS.get(reason) if reason is not None else None,
        "capacity": {"max_streams": max_streams()},
        "limits": {
            "max_frame_bytes": max_frame_bytes(),
            "max_take_seconds": max_take_seconds(),
        },
    }
    if name == ENGINE_PARAKEET_MLX and reason == REASON_EXTRA_NOT_INSTALLED:
        result["action"] = "Install the dictation-mlx extra."
    if name == ENGINE_REMOTE:
        url = _remote_url()
        parsed = urlsplit(url)
        result["remote"] = {
            "configured": bool(url),
            "secure": parsed.scheme == "wss",
            "token_configured": bool(_worker_token()),
            "fallback_available": _sherpa_available()[0],
            "connection_state": _get_remote_connection_state(),
        }
    elif name == ENGINE_PARAKEET_MLX:
        model = _mlx_model()
        result["model"] = "local" if Path(model).expanduser().is_absolute() else model
    return result


_engine_lock = threading.Lock()
_engine: DictationEngine | None = None
_remote_state_lock = threading.Lock()
_remote_connection_state = "not_attempted"


def _set_remote_connection_state(state: str) -> None:
    global _remote_connection_state
    with _remote_state_lock:
        _remote_connection_state = state


def _get_remote_connection_state() -> str:
    with _remote_state_lock:
        return _remote_connection_state


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
        import sherpa_onnx  # type: ignore[import-not-found]

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
                result = self._punct.add_punctuation_with_case(cleaned)
            return result if isinstance(result, str) else text
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


class ParakeetMlxDictationEngine:
    """Apple Silicon streaming transcription through parakeet-mlx."""

    def __init__(self, model_id: str, cache_dir: Path) -> None:
        if platform.system() != "Darwin" or platform.machine() != "arm64":
            raise RuntimeError("Parakeet MLX dictation requires an Apple Silicon Mac")
        from parakeet_mlx import from_pretrained  # type: ignore[import-not-found]

        cache_dir.mkdir(parents=True, exist_ok=True)
        model_label = "local model" if Path(model_id).expanduser().is_absolute() else model_id
        _logger.info("Loading Parakeet MLX dictation model %s", model_label)
        self._model = from_pretrained(model_id, cache_dir=cache_dir)
        sample_rate = int(self._model.preprocessor_config.sample_rate)
        if sample_rate != SAMPLE_RATE:
            raise RuntimeError(
                f"Parakeet MLX model sample rate must be {SAMPLE_RATE}, got {sample_rate}"
            )
        self._model.encoder.set_attention_model("rel_pos_local_attn", _MLX_CONTEXT_SIZE)
        self._hop_length = int(self._model.preprocessor_config.hop_length)
        self._flush_samples = (
            _MLX_CONTEXT_SIZE[1]
            * int(self._model.encoder_config.subsampling_factor)
            * self._hop_length
        )
        self._lock = threading.Lock()

    def create_stream(self) -> _ParakeetMlxStream:
        """Open a streaming Parakeet decoder for one connection."""
        return _ParakeetMlxStream(self)


class _ParakeetMlxStream:
    """Per-take Parakeet stream with Omnigent-owned utterance endpointing."""

    def __init__(self, engine: ParakeetMlxDictationEngine) -> None:
        self._engine = engine
        self._transcriber: Any = None
        self._utterance_samples = 0
        self._silence_samples = 0
        self._speech_seen = False
        self._pending_pcm = b""
        self._pending: Any = None
        self._vad_pending: Any = None
        self._closed = False
        self._open_transcriber()

    def _open_transcriber(self) -> None:
        context = self._engine._model.transcribe_stream(
            context_size=_MLX_CONTEXT_SIZE,
            keep_original_attention=True,
        )
        self._transcriber = context.__enter__()

    def _close_transcriber(self) -> None:
        if self._transcriber is not None:
            self._transcriber.__exit__(None, None, None)
            self._transcriber = None

    def _text(self) -> str:
        return str(self._transcriber.result.text).strip()

    def _add_audio(self, samples: Any, *, flush: bool = False) -> None:
        import mlx.core as mx  # type: ignore[import-not-found]
        import numpy as np

        pending = samples if self._pending is None else np.concatenate((self._pending, samples))
        usable = (
            len(pending) if flush else len(pending) - (len(pending) % self._engine._hop_length)
        )
        if usable:
            self._transcriber.add_audio(mx.array(pending[:usable]))
        self._pending = pending[usable:]

    def _feed_window(self, samples: Any) -> str | None:
        import numpy as np

        self._add_audio(samples)
        self._utterance_samples += int(samples.size)
        rms = float(np.sqrt(np.mean(np.square(samples)))) if samples.size else 0.0
        if rms >= _MLX_SPEECH_RMS:
            self._speech_seen = True
            self._silence_samples = 0
        elif self._speech_seen:
            self._silence_samples += int(samples.size)

        silence_endpoint = self._speech_seen and self._silence_samples >= int(
            _RULE2_MIN_TRAILING_SILENCE_S * SAMPLE_RATE
        )
        duration_endpoint = self._utterance_samples >= int(
            _RULE3_MIN_UTTERANCE_LENGTH_S * SAMPLE_RATE
        )
        if not silence_endpoint and not duration_endpoint:
            return None
        if duration_endpoint and not silence_endpoint:
            silence = np.zeros(self._engine._flush_samples, dtype=np.float32)
            self._add_audio(silence, flush=True)
        text = self._text()
        self._close_transcriber()
        self._open_transcriber()
        self._utterance_samples = 0
        self._silence_samples = 0
        self._speech_seen = False
        self._pending = None
        return text or None

    def feed_pcm16(self, data: bytes) -> DictationUpdate:
        """Decode PCM and finalize only at an utterance endpoint."""
        import numpy as np

        pcm = self._pending_pcm + data
        usable = len(pcm) - (len(pcm) % 2)
        self._pending_pcm = pcm[usable:]
        if usable <= 0:
            return DictationUpdate(partial=self._text())
        samples = np.frombuffer(pcm[:usable], dtype="<i2").astype(np.float32) / 32768.0

        with self._engine._lock:
            pending = (
                samples
                if self._vad_pending is None
                else np.concatenate((self._vad_pending, samples))
            )
            finals: list[str] = []
            offset = 0
            while len(pending) - offset >= _MLX_VAD_WINDOW_SAMPLES:
                finalized = self._feed_window(pending[offset : offset + _MLX_VAD_WINDOW_SAMPLES])
                if finalized:
                    finals.append(finalized)
                offset += _MLX_VAD_WINDOW_SAMPLES
            self._vad_pending = pending[offset:]
            return DictationUpdate(
                partial=self._text(),
                finalized=" ".join(finals).strip() or None,
            )

    def finish(self) -> str:
        """Flush right context with silence and return one final tail."""
        if self._closed:
            return ""
        import numpy as np

        with self._engine._lock:
            if self._vad_pending is not None and len(self._vad_pending):
                self._add_audio(self._vad_pending)
                self._utterance_samples += len(self._vad_pending)
                self._vad_pending = None
            if self._utterance_samples:
                silence = np.zeros(self._engine._flush_samples, dtype=np.float32)
                self._add_audio(silence, flush=True)
            text = self._text()
            self._close_transcriber()
            self._closed = True
            return text

    def close(self) -> None:
        """Release MLX stream state without flushing."""
        if self._closed:
            return
        with self._engine._lock:
            self._close_transcriber()
            self._closed = True


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
        token: str | None = None,
        ca_file: str | None = None,
        allow_insecure: bool = False,
        fallback_factory: Callable[[], DictationEngine] | None = None,
    ) -> None:
        """
        :param url: Worker stream URL, e.g.
            ``ws://venus:8100/v1/dictation/stream``.
        :param fallback_factory: Builds the local fallback engine on
            first use (lazy — its model weights cost ~real RAM), or
            ``None`` when no local model is installed.
        """
        _validate_remote_url(url, allow_insecure=allow_insecure)
        if not token:
            raise ValueError("dictation worker token is required")
        self._url = url
        self._token = token
        self._ssl_context = _remote_ssl_context(url, ca_file)
        self._fallback_factory = fallback_factory
        self._fallback: DictationEngine | None = None
        self._fallback_lock = threading.Lock()

    def create_stream(self) -> DictationStreamHandle:
        """Connect a take to the worker, or to the local fallback."""
        try:
            stream = _RemoteStream(self._url, self._token, self._ssl_context)
            _set_remote_connection_state("connected")
            metrics.remote_connection("connected")
            return stream
        except Exception:
            _set_remote_connection_state("unavailable")
            metrics.remote_connection("failed")
            if self._fallback_factory is None:
                raise
            _logger.warning(
                "dictation worker unreachable; using local fallback engine",
                exc_info=True,
            )
            with self._fallback_lock:
                if self._fallback is None:
                    self._fallback = self._fallback_factory()
            _set_remote_connection_state("local_fallback")
            metrics.fallback()
            return self._fallback.create_stream()


class _RemoteStream:
    """One relayed take: raw PCM up, transcript events down.

    A daemon reader thread folds the worker's ``partial``/``final``
    events into state that :meth:`feed_pcm16` returns on each call, so
    the relay presents the same synchronous handle interface the local
    engines do. The worker returns display-ready text already, so the
    relay just forwards it.
    """

    def __init__(self, url: str, token: str, ssl_context: ssl.SSLContext | None) -> None:
        from websockets.sync.client import connect

        self._ws = connect(
            url,
            additional_headers={"Authorization": f"Bearer {token}"},
            ssl=ssl_context,
            open_timeout=5,
            max_size=max_frame_bytes(),
        )
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
        """Ask the worker to flush; return pending finals plus its tail."""
        stopped = False
        try:
            self._ws.send(json.dumps({"type": "stop"}))
            stopped = self._stopped.wait(timeout=_REMOTE_STOP_TIMEOUT_S)
        finally:
            self.close()
        if not stopped:
            raise TimeoutError("dictation worker did not finish the take")
        with self._lock:
            if self._dead:
                raise RuntimeError("dictation worker connection lost while stopping")
            parts = [*self._finals, self._tail]
            self._finals.clear()
            return " ".join(part for part in parts if part).strip()

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


def _worker_token() -> str:
    return os.environ.get(WORKER_TOKEN_ENV, "").strip()


def _allow_insecure_remote() -> bool:
    return os.environ.get(ALLOW_INSECURE_REMOTE_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    import ipaddress

    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_remote_url(url: str, *, allow_insecure: bool) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"ws", "wss"} or not parsed.hostname:
        raise ValueError("dictation remote URL must use ws:// or wss://")
    if parsed.scheme == "ws" and not _is_loopback_host(parsed.hostname) and not allow_insecure:
        raise ValueError("plaintext remote dictation is denied for non-loopback hosts")


def _remote_ssl_context(url: str, ca_file: str | None) -> ssl.SSLContext | None:
    if urlsplit(url).scheme != "wss":
        return None
    return ssl.create_default_context(cafile=ca_file or None)


def _remote_available() -> tuple[bool, str | None]:
    """Availability probe for the remote engine.

    A configured worker counts as available without probing it — the
    worker may be briefly down or still booting, and the stream route
    degrades cleanly (local fallback, or an error frame) when a take
    actually starts.
    """
    if not _remote_url():
        return False, REASON_REMOTE_URL_MISSING
    if not _worker_token():
        return False, REASON_REMOTE_TOKEN_MISSING
    try:
        _validate_remote_url(_remote_url(), allow_insecure=_allow_insecure_remote())
    except ValueError as exc:
        if "plaintext" in str(exc):
            return False, REASON_INSECURE_REMOTE
        return False, REASON_INVALID_REMOTE_URL
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
    return RemoteDictationEngine(
        url,
        token=_worker_token(),
        ca_file=os.environ.get(REMOTE_CA_FILE_ENV, "").strip() or None,
        allow_insecure=_allow_insecure_remote(),
        fallback_factory=fallback,
    )


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
register_engine(
    ENGINE_PARAKEET_MLX,
    lambda: ParakeetMlxDictationEngine(_mlx_model(), _mlx_cache_dir()),
    available=_parakeet_mlx_available,
)
register_engine(ENGINE_REMOTE, _build_remote_engine, available=_remote_available)
register_engine(ENGINE_FAKE, FakeDictationEngine)
