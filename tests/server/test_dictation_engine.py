"""Unit tests for the dictation engine layer (no route, no WebSocket).

Everything here runs without the ``dictation`` extra except the last
test, which exercises the real sherpa-onnx engine end-to-end and skips
itself unless the extra and a model are installed (developer machines).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.server import dictation


@pytest.fixture(autouse=True)
def _clean_engine_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate each test from ambient dictation env configuration."""
    monkeypatch.delenv(dictation.ENGINE_ENV, raising=False)
    monkeypatch.delenv(dictation.MODEL_DIR_ENV, raising=False)
    monkeypatch.delenv(dictation.PUNCT_DIR_ENV, raising=False)
    monkeypatch.delenv(dictation.MAX_STREAMS_ENV, raising=False)


def _touch_asr_files(model_dir: Path) -> None:
    for name in ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt"):
        (model_dir / name).touch()


def test_availability_fake_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """The fake engine is always available, extra or not."""
    monkeypatch.setenv(dictation.ENGINE_ENV, dictation.ENGINE_FAKE)
    assert dictation.engine_availability() == (True, None)


def test_availability_extra_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the sherpa-onnx package the probe says extra_not_installed."""
    monkeypatch.setattr(dictation.importlib.util, "find_spec", lambda name: None)
    assert dictation.engine_availability() == (
        False,
        dictation.REASON_EXTRA_NOT_INSTALLED,
    )


def test_availability_models_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """With the package but an empty model dir the probe says models_missing."""
    monkeypatch.setattr(dictation.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setenv(dictation.MODEL_DIR_ENV, str(tmp_path))
    assert dictation.engine_availability() == (
        False,
        dictation.REASON_MODELS_MISSING,
    )


def test_availability_with_models(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A populated model dir plus the package reports available."""
    monkeypatch.setattr(dictation.importlib.util, "find_spec", lambda name: object())
    _touch_asr_files(tmp_path)
    monkeypatch.setenv(dictation.MODEL_DIR_ENV, str(tmp_path))
    assert dictation.engine_availability() == (True, None)


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
