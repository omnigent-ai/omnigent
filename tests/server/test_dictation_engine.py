"""Unit tests for the dictation engine layer (no route, no WebSocket).

Everything here runs without the ``dictation`` extra except the last
test, which exercises the real sherpa-onnx engine end-to-end and skips
itself unless the extra and a model are installed (developer machines).
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent.server import dictation


@pytest.fixture(autouse=True)
def _clean_engine_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate each test from ambient dictation env configuration."""
    monkeypatch.delenv(dictation.ENGINE_ENV, raising=False)
    monkeypatch.delenv(dictation.MODEL_DIR_ENV, raising=False)
    monkeypatch.delenv(dictation.PUNCT_DIR_ENV, raising=False)
    monkeypatch.delenv(dictation.MODEL_ENV, raising=False)
    monkeypatch.delenv(dictation.MODEL_CACHE_DIR_ENV, raising=False)
    monkeypatch.delenv(dictation.MAX_STREAMS_ENV, raising=False)
    monkeypatch.delenv(dictation.MAX_FRAME_BYTES_ENV, raising=False)
    monkeypatch.delenv(dictation.MAX_TAKE_SECONDS_ENV, raising=False)
    monkeypatch.delenv(dictation.WORKER_TOKEN_ENV, raising=False)
    monkeypatch.delenv(dictation.ALLOW_INSECURE_REMOTE_ENV, raising=False)


def _touch_asr_files(model_dir: Path) -> None:
    for name in ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt"):
        (model_dir / name).touch()


def test_availability_fake_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fake engine is always available, extra or not."""
    monkeypatch.setenv(dictation.ENGINE_ENV, dictation.ENGINE_FAKE)
    assert dictation.engine_availability() == (True, None)


def test_default_engine_prefers_installed_mlx_on_apple_silicon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dictation.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(dictation.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(dictation.importlib.util, "find_spec", lambda name: object())

    assert dictation._selected_engine_name() == dictation.ENGINE_PARAKEET_MLX


def test_default_engine_falls_back_to_sherpa_without_mlx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(dictation.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(dictation.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(dictation.importlib.util, "find_spec", lambda name: None)

    assert dictation._selected_engine_name() == dictation.ENGINE_SHERPA


def test_explicit_engine_overrides_apple_silicon_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(dictation.ENGINE_ENV, dictation.ENGINE_SHERPA)
    monkeypatch.setattr(dictation.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(dictation.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(dictation.importlib.util, "find_spec", lambda name: object())

    assert dictation._selected_engine_name() == dictation.ENGINE_SHERPA


def test_availability_extra_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the sherpa-onnx package the probe says extra_not_installed."""
    monkeypatch.setattr(dictation.importlib.util, "find_spec", lambda name: None)
    assert dictation.engine_availability() == (
        False,
        dictation.REASON_EXTRA_NOT_INSTALLED,
    )


def test_availability_models_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """With the package but an empty model dir the probe says models_missing."""
    monkeypatch.setenv(dictation.ENGINE_ENV, dictation.ENGINE_SHERPA)
    monkeypatch.setattr(dictation.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setenv(dictation.MODEL_DIR_ENV, str(tmp_path))
    assert dictation.engine_availability() == (
        False,
        dictation.REASON_MODELS_MISSING,
    )


def test_availability_with_models(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A populated model dir plus the package reports available."""
    monkeypatch.setenv(dictation.ENGINE_ENV, dictation.ENGINE_SHERPA)
    monkeypatch.setattr(dictation.importlib.util, "find_spec", lambda name: object())
    _touch_asr_files(tmp_path)
    monkeypatch.setenv(dictation.MODEL_DIR_ENV, str(tmp_path))
    assert dictation.engine_availability() == (True, None)


def test_parakeet_mlx_availability_requires_apple_silicon(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(dictation.ENGINE_ENV, dictation.ENGINE_PARAKEET_MLX)
    monkeypatch.setattr(dictation.platform, "system", lambda: "Linux")
    monkeypatch.setattr(dictation.platform, "machine", lambda: "x86_64")
    assert dictation.engine_availability() == (
        False,
        dictation.REASON_UNSUPPORTED_PLATFORM,
    )


def test_parakeet_mlx_constructor_rejects_other_platforms(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(dictation.platform, "system", lambda: "Linux")
    monkeypatch.setattr(dictation.platform, "machine", lambda: "x86_64")

    with pytest.raises(RuntimeError, match="Apple Silicon"):
        dictation.ParakeetMlxDictationEngine("model", tmp_path)


def test_parakeet_mlx_availability_checks_extra_and_local_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(dictation.ENGINE_ENV, dictation.ENGINE_PARAKEET_MLX)
    monkeypatch.setattr(dictation.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(dictation.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(dictation.importlib.util, "find_spec", lambda name: None)
    assert dictation.engine_availability() == (
        False,
        dictation.REASON_EXTRA_NOT_INSTALLED,
    )

    monkeypatch.setattr(dictation.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setenv(dictation.MODEL_ENV, str(tmp_path / "missing"))
    assert dictation.engine_availability() == (False, dictation.REASON_MODELS_MISSING)
    monkeypatch.setenv(dictation.MODEL_ENV, "mlx-community/parakeet-tdt-0.6b-v3")
    assert dictation.engine_availability() == (True, None)


def test_parakeet_mlx_status_sanitizes_local_model_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(dictation.ENGINE_ENV, dictation.ENGINE_PARAKEET_MLX)
    monkeypatch.setenv(dictation.MODEL_ENV, str(tmp_path))
    monkeypatch.setattr(dictation.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(dictation.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(dictation.importlib.util, "find_spec", lambda name: object())

    status = dictation.engine_status()

    assert status["model"] == "local"
    assert str(tmp_path) not in str(status)


class _FakeMlxTranscriber:
    def __init__(self) -> None:
        self.result = SimpleNamespace(text="")
        self.audio: list[object] = []
        self.closed = False

    def __enter__(self) -> _FakeMlxTranscriber:
        return self

    def __exit__(self, *args: object) -> None:
        self.closed = True

    def add_audio(self, audio: object) -> None:
        self.audio.append(audio)
        self.result.text = "stable draft"


class _FakeMlxModel:
    def __init__(self) -> None:
        self.streams: list[_FakeMlxTranscriber] = []

    def transcribe_stream(self, **kwargs: object) -> _FakeMlxTranscriber:
        assert kwargs == {
            "context_size": dictation._MLX_CONTEXT_SIZE,
            "keep_original_attention": True,
        }
        stream = _FakeMlxTranscriber()
        self.streams.append(stream)
        return stream


@pytest.fixture
def fake_mlx(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    mlx_core = SimpleNamespace(array=lambda value: value)
    monkeypatch.setitem(sys.modules, "mlx", SimpleNamespace(core=mlx_core))
    monkeypatch.setitem(sys.modules, "mlx.core", mlx_core)


def _fake_mlx_engine() -> tuple[dictation.ParakeetMlxDictationEngine, _FakeMlxModel]:
    engine = object.__new__(dictation.ParakeetMlxDictationEngine)
    model = _FakeMlxModel()
    engine._model = model
    engine._lock = dictation.threading.Lock()
    engine._hop_length = 160
    engine._flush_samples = 1280
    return engine, model


def test_parakeet_mlx_stream_normalizes_pcm_and_keeps_tokens_partial(
    fake_mlx: None,
) -> None:
    engine, model = _fake_mlx_engine()
    stream = engine.create_stream()

    update = stream.feed_pcm16(b"\x00@\x00\xc0")

    assert update == dictation.DictationUpdate(partial="")
    assert model.streams[0].audio == []

    update = stream.feed_pcm16(b"\x00@" * (dictation._MLX_VAD_WINDOW_SAMPLES - 2))

    assert update == dictation.DictationUpdate(partial="stable draft")
    assert model.streams[0].audio[0].tolist()[:2] == pytest.approx([0.5, -0.5])


def test_parakeet_mlx_stream_preserves_split_pcm_sample(fake_mlx: None) -> None:
    engine, model = _fake_mlx_engine()
    engine._hop_length = 1
    stream = engine.create_stream()

    assert stream.feed_pcm16(b"\x00").partial == ""
    assert stream.feed_pcm16(b"@").partial == ""
    assert (
        stream.feed_pcm16(b"\x00@" * (dictation._MLX_VAD_WINDOW_SAMPLES - 1)).partial
        == "stable draft"
    )
    assert model.streams[0].audio[0].tolist()[0] == pytest.approx(0.5)


def test_parakeet_mlx_stream_finalizes_whole_utterance_after_silence(
    fake_mlx: None,
) -> None:
    engine, model = _fake_mlx_engine()
    stream = engine.create_stream()
    speech = b"\xff\x7f" * dictation.SAMPLE_RATE
    silence = b"\x00\x00" * int(dictation._RULE2_MIN_TRAILING_SILENCE_S * dictation.SAMPLE_RATE)

    assert stream.feed_pcm16(speech).finalized is None
    update = stream.feed_pcm16(silence)

    assert update == dictation.DictationUpdate(partial="", finalized="stable draft")
    assert model.streams[0].closed
    assert len(model.streams) == 2


def test_parakeet_mlx_endpoint_is_frame_segmentation_invariant(fake_mlx: None) -> None:
    speech = b"\xff\x7f" * dictation.SAMPLE_RATE
    silence = b"\x00\x00" * int(dictation._RULE2_MIN_TRAILING_SILENCE_S * dictation.SAMPLE_RATE)
    resumed = b"\xff\x7f" * dictation._MLX_VAD_WINDOW_SAMPLES
    audio = speech + silence + resumed

    combined_engine, _ = _fake_mlx_engine()
    combined = combined_engine.create_stream().feed_pcm16(audio)

    split_engine, _ = _fake_mlx_engine()
    split_stream = split_engine.create_stream()
    split_finals: list[str] = []
    for offset in range(0, len(audio), 317):
        update = split_stream.feed_pcm16(audio[offset : offset + 317])
        if update.finalized:
            split_finals.append(update.finalized)

    assert combined.finalized == "stable draft"
    assert " ".join(split_finals) == combined.finalized
    assert split_stream._text() == combined.partial


def test_parakeet_mlx_stream_flushes_before_duration_endpoint(fake_mlx: None) -> None:
    engine, model = _fake_mlx_engine()
    stream = engine.create_stream()
    speech = b"\xff\x7f" * int(dictation._RULE3_MIN_UTTERANCE_LENGTH_S * dictation.SAMPLE_RATE)

    update = stream.feed_pcm16(speech)

    assert update.finalized == "stable draft"
    assert len(model.streams[0].audio[-1]) == engine._flush_samples
    assert model.streams[0].closed


def test_parakeet_mlx_stream_finish_flushes_and_closes(fake_mlx: None) -> None:
    engine, model = _fake_mlx_engine()
    stream = engine.create_stream()
    stream.feed_pcm16(b"\xff\x7f" * dictation.SAMPLE_RATE)

    assert stream.finish() == "stable draft"
    assert model.streams[0].closed
    assert len(model.streams[0].audio[-1]) == engine._flush_samples
    assert stream.finish() == ""
    stream.close()


def test_pick_model_file_prefers_int8(tmp_path: Path) -> None:
    """int8 quantizations win over float exports of the same stem."""
    (tmp_path / "encoder.onnx").touch()
    (tmp_path / "encoder.int8.onnx").touch()
    picked = dictation._pick_model_file(tmp_path, "encoder")
    assert picked is not None and picked.name == "encoder.int8.onnx"


def test_max_streams_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bad or non-positive values fall back to the default."""
    assert dictation.max_streams() == dictation.DEFAULT_MAX_STREAMS
    monkeypatch.setenv(dictation.MAX_STREAMS_ENV, "5")
    assert dictation.max_streams() == 5
    monkeypatch.setenv(dictation.MAX_STREAMS_ENV, "0")
    assert dictation.max_streams() == dictation.DEFAULT_MAX_STREAMS
    monkeypatch.setenv(dictation.MAX_STREAMS_ENV, "lots")
    assert dictation.max_streams() == dictation.DEFAULT_MAX_STREAMS


def test_get_engine_is_a_singleton_and_failure_caches_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One engine per process; a failed load leaves the slot empty for retry."""
    monkeypatch.setattr(dictation, "_engine", None)
    # Unavailable (empty model dir) → raises and caches nothing.
    monkeypatch.setenv(dictation.ENGINE_ENV, dictation.ENGINE_SHERPA)
    monkeypatch.setattr(dictation.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setenv(dictation.MODEL_DIR_ENV, str(tmp_path))
    with pytest.raises(RuntimeError):
        dictation.get_engine()
    assert dictation._engine is None
    # Becomes available (fake engine) → loads once, then reuses.
    monkeypatch.setenv(dictation.ENGINE_ENV, dictation.ENGINE_FAKE)
    first = dictation.get_engine()
    assert isinstance(first, dictation.FakeDictationEngine)
    assert dictation.get_engine() is first


def test_get_engine_rejects_unknown_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unregistered engine name is unavailable and raises on load."""
    monkeypatch.setattr(dictation, "_engine", None)
    monkeypatch.setenv(dictation.ENGINE_ENV, "does-not-exist")
    assert dictation.engine_availability() == (False, dictation.REASON_UNKNOWN_ENGINE)
    with pytest.raises(RuntimeError):
        dictation.get_engine()


def test_register_engine_is_selectable(monkeypatch: pytest.MonkeyPatch) -> None:
    """A registered engine is selected by name with no core edits.

    Mirrors what adding Whisper looks like: one register_engine call, then
    OMNIGENT_DICTATION_ENGINE picks it up.
    """
    monkeypatch.setattr(dictation, "_engine", None)
    monkeypatch.setitem(
        dictation._ENGINE_REGISTRY,
        "probe-engine",
        dictation._EngineEntry(
            factory=dictation.FakeDictationEngine,
            available=lambda: (True, None),
        ),
    )
    monkeypatch.setenv(dictation.ENGINE_ENV, "probe-engine")
    assert dictation.engine_availability() == (True, None)
    assert isinstance(dictation.get_engine(), dictation.FakeDictationEngine)


def test_fake_stream_reveals_script_by_bytes() -> None:
    """One script word per 100 ms of audio; sentence finalizes when done."""
    word = b"\x00" * (dictation.SAMPLE_RATE * 2 // 10)
    words = dictation.FAKE_SCRIPT.split()
    stream = dictation.FakeDictationEngine().create_stream()

    update = stream.feed_pcm16(word * 2)
    assert update.partial == " ".join(words[:2])
    assert update.finalized is None

    update = stream.feed_pcm16(word * (len(words) - 2))
    assert update.partial == ""
    assert update.finalized == dictation.FAKE_SCRIPT

    # After the script completes, the stream stays quiet.
    assert stream.feed_pcm16(word).partial == ""
    assert stream.finish() == ""


def test_fake_stream_finish_returns_tail() -> None:
    """finish() mid-script returns the revealed words."""
    word = b"\x00" * (dictation.SAMPLE_RATE * 2 // 10)
    words = dictation.FAKE_SCRIPT.split()
    stream = dictation.FakeDictationEngine().create_stream()
    stream.feed_pcm16(word * 3)
    assert stream.finish() == " ".join(words[:3])


def test_sherpa_engine_transcribes_test_wav() -> None:
    """Real-model smoke test; skips unless the extra + models are installed.

    Hermetic on CI (always skipped there); on a developer machine with
    models fetched via ``scripts/fetch-dictation-models.sh`` it exercises
    the true engine: PCM in → partial/finalized text out.
    """
    pytest.importorskip("sherpa_onnx")
    asr_dir = dictation._asr_dir()
    if dictation._asr_files(asr_dir) is None:
        pytest.skip(f"no dictation ASR model in {asr_dir}")
    wavs = sorted(asr_dir.glob("test_wavs/*.wav"))
    if not wavs:
        pytest.skip("model dir has no test_wavs to decode")

    import wave

    engine = dictation.SherpaDictationEngine(asr_dir, dictation._punct_dir())
    stream = engine.create_stream()
    with wave.open(str(wavs[0])) as wav:
        assert wav.getframerate() == dictation.SAMPLE_RATE
        pcm = wav.readframes(wav.getnframes())

    texts: list[str] = []
    chunk = dictation.SAMPLE_RATE * 2 // 10  # 100 ms
    for i in range(0, len(pcm), chunk):
        update = stream.feed_pcm16(pcm[i : i + chunk])
        if update.finalized:
            texts.append(update.finalized)
    tail = stream.finish()
    if tail:
        texts.append(tail)
    transcript = " ".join(texts)
    assert len(transcript.split()) >= 3, transcript


def test_parakeet_mlx_engine_transcribes_test_wav() -> None:
    """Opt-in real-model smoke for an operator-provided 16 kHz PCM WAV."""
    wav_path = os.environ.get("OMNIGENT_DICTATION_MLX_TEST_WAV", "").strip()
    if not wav_path:
        pytest.skip("set OMNIGENT_DICTATION_MLX_TEST_WAV to run the Parakeet MLX smoke")
    pytest.importorskip("parakeet_mlx")

    import wave

    engine = dictation.ParakeetMlxDictationEngine(
        dictation._mlx_model(), dictation._mlx_cache_dir()
    )
    stream = engine.create_stream()
    with wave.open(wav_path) as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == dictation.SAMPLE_RATE
        pcm = wav.readframes(wav.getnframes())

    texts: list[str] = []
    chunk = dictation.SAMPLE_RATE * 2 // 10
    for i in range(0, len(pcm), chunk):
        update = stream.feed_pcm16(pcm[i : i + chunk])
        if update.finalized:
            texts.append(update.finalized)
    tail = stream.finish()
    if tail:
        texts.append(tail)
    transcript = " ".join(texts)
    assert len(transcript.split()) >= 3, transcript
