"""Benchmark the Parakeet MLX dictation adapter on a 16 kHz PCM WAV.

Run with:

    uv run --extra dictation-mlx dev/benchmarks/dictation_mlx.py sample.wav
"""

from __future__ import annotations

import argparse
import json
import time
import wave
from pathlib import Path

from omnigent.server import dictation


def run(path: Path, *, chunk_ms: int = 100) -> dict[str, float | str]:
    """Decode one WAV and return repeatable timing metrics."""
    with wave.open(str(path)) as wav:
        if (
            wav.getnchannels() != 1
            or wav.getsampwidth() != 2
            or wav.getframerate() != dictation.SAMPLE_RATE
        ):
            raise ValueError("input must be 16 kHz mono PCM16 WAV")
        pcm = wav.readframes(wav.getnframes())
    if not pcm:
        raise ValueError("input WAV must contain audio")

    load_started = time.perf_counter()
    engine = dictation.ParakeetMlxDictationEngine(
        dictation._mlx_model(), dictation._mlx_cache_dir()
    )
    load_seconds = time.perf_counter() - load_started

    decode_started = time.perf_counter()
    stream = engine.create_stream()
    texts: list[str] = []
    chunk_bytes = dictation.SAMPLE_RATE * 2 * chunk_ms // 1000
    for offset in range(0, len(pcm), chunk_bytes):
        update = stream.feed_pcm16(pcm[offset : offset + chunk_bytes])
        if update.finalized:
            texts.append(update.finalized)
    tail = stream.finish()
    if tail:
        texts.append(tail)
    decode_seconds = time.perf_counter() - decode_started
    audio_seconds = len(pcm) / (dictation.SAMPLE_RATE * 2)

    return {
        "model": dictation._mlx_model(),
        "audio_seconds": round(audio_seconds, 3),
        "load_seconds": round(load_seconds, 3),
        "decode_seconds": round(decode_seconds, 3),
        "real_time_factor": round(decode_seconds / audio_seconds, 4),
        "transcript": " ".join(texts),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wav", type=Path)
    parser.add_argument("--chunk-ms", type=int, default=100)
    args = parser.parse_args()
    if args.chunk_ms <= 0:
        parser.error("--chunk-ms must be positive")
    print(json.dumps(run(args.wav, chunk_ms=args.chunk_ms), indent=2))


if __name__ == "__main__":
    main()
