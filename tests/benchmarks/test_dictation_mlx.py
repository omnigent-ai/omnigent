from __future__ import annotations

import wave
from pathlib import Path
from types import SimpleNamespace

import pytest

from dev.benchmarks import dictation_mlx


def test_run_reports_adapter_real_time_factor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    wav_path = tmp_path / "sample.wav"
    with wave.open(str(wav_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16_000)
        wav.writeframes(b"\x00\x00" * 16_000)

    stream = SimpleNamespace(
        feed_pcm16=lambda data: SimpleNamespace(finalized=None),
        finish=lambda: "benchmark transcript",
    )
    engine = SimpleNamespace(create_stream=lambda: stream)
    monkeypatch.setattr(
        dictation_mlx.dictation,
        "ParakeetMlxDictationEngine",
        lambda model, cache: engine,
    )

    result = dictation_mlx.run(wav_path)

    assert result["audio_seconds"] == 1.0
    assert isinstance(result["real_time_factor"], float)
    assert result["transcript"] == "benchmark transcript"


def test_run_rejects_incompatible_wav(tmp_path: Path) -> None:
    wav_path = tmp_path / "sample.wav"
    with wave.open(str(wav_path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(44_100)
        wav.writeframes(b"\x00\x00" * 2)

    with pytest.raises(ValueError, match="16 kHz mono PCM16"):
        dictation_mlx.run(wav_path)


def test_run_rejects_empty_wav(tmp_path: Path) -> None:
    wav_path = tmp_path / "empty.wav"
    with wave.open(str(wav_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16_000)

    with pytest.raises(ValueError, match="must contain audio"):
        dictation_mlx.run(wav_path)
