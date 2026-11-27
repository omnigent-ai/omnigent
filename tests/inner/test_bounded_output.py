"""Bounded pipe capture without launching application or shell processes."""

from __future__ import annotations

import json
import os
import threading
import tracemalloc
from contextlib import ExitStack

import pytest

from omnigent.inner._bounded_output import BoundedPipeCapture, CapturedOutput, _TextPrefix


@pytest.mark.parametrize(
    "chunks,encoding,limit,text,omitted,nonblank",
    [
        ([b"first\r", b"\nsecond\rthird"], "utf-8", 8, "first\nse", 10, True),
        ([b"\xce", b"\xb1\xf0\x9f", b"\x98\x80z"], "utf-8", 2, "α😀", 1, True),
        ([b"caf\xe9\r", b"\n"], "iso8859-1", 4, "café", 1, True),
        ([b"   ", b"\t\n"], "utf-8", 2, "  ", 3, False),
        ([b"   ", b"message"], "utf-8", 2, "  ", 8, True),
    ],
)
def test_prefix_retains_exact_character_counts_across_encoding_and_newline_boundaries(
    chunks: list[bytes], encoding: str, limit: int, text: str, omitted: int, nonblank: bool
) -> None:
    capture = _TextPrefix(encoding, limit)
    for chunk in chunks:
        capture.feed(chunk)
    capture.feed(b"", final=True)
    assert capture.result() == CapturedOutput(text, omitted, nonblank=nonblank)


@pytest.mark.parametrize("data", [b"valid\xffsuffix", b"valid\xe2\x82"])
def test_decode_errors_are_retained_as_small_metadata_while_remaining_data_is_discarded(
    data: bytes,
) -> None:
    capture = _TextPrefix("utf-8", 4)
    capture.feed(data)
    capture.feed(b"", final=True)
    capture.feed(b"more output" * 100)
    result = capture.result()
    assert result.text == "" and result.omitted == 0
    assert result.decoding_error is not None and "decode" in result.decoding_error


def test_unfinished_stateful_output_discards_pending_bytes_at_the_bound() -> None:
    capture = _TextPrefix("utf-7", 4)
    capture.feed(b"safe+")
    for _ in range(3):
        capture.feed(b"A" * 32_768)
        assert capture.result() == CapturedOutput("safe", nonblank=True)
    capture.feed(b"A" * 32_768)
    failed = capture.result()
    assert failed.text == "" and failed.omitted == 0
    assert failed.decoding_error == "Output decoder retained an oversized incomplete sequence"
    assert capture._decoder.getstate()[0] == b""
    capture.feed(b"A" * 131_072)
    capture.feed(b"-", final=True)
    assert capture.result() == failed
    assert capture._decoder.getstate()[0] == b""


@pytest.mark.posix_only
@pytest.mark.timeout(20)
def test_both_pipes_are_fully_drained_with_bounded_heap_retention() -> None:
    chunk = b"x" * 16_384
    rounds = 512
    failures: list[Exception] = []
    started_tracing = not tracemalloc.is_tracing()
    with ExitStack() as resources:
        out_r, out_w = os.pipe()
        err_r, err_w = os.pipe()
        stdout = resources.enter_context(os.fdopen(out_r, "rb", buffering=0))
        stderr = resources.enter_context(os.fdopen(err_r, "rb", buffering=0))
        writers = [
            resources.enter_context(os.fdopen(out_w, "wb", buffering=0)),
            resources.enter_context(os.fdopen(err_w, "wb", buffering=0)),
        ]
        capture = BoundedPipeCapture(stdout, stderr, encoding="utf-8", max_chars=17)

        def produce() -> None:
            try:
                for _ in range(rounds):
                    for writer in writers:
                        remaining = memoryview(chunk)
                        while remaining:
                            written = os.write(writer.fileno(), remaining)
                            remaining = remaining[written:]
            except Exception as exc:
                failures.append(exc)
            finally:
                for writer in writers:
                    writer.close()

        producer = threading.Thread(target=produce, daemon=True)
        if started_tracing:
            tracemalloc.start()
        baseline, _peak = tracemalloc.get_traced_memory()
        tracemalloc.reset_peak()
        try:
            producer.start()
            capture.drain()
            results = capture.result()
            _current, peak = tracemalloc.get_traced_memory()
            print(
                json.dumps(
                    {
                        "output_memory_probe": {
                            "streamed_bytes": 2 * rounds * len(chunk),
                            "retained_characters": sum(len(result.text) for result in results),
                            "peak_heap_growth_bytes": peak - baseline,
                            "limit_per_stream": 17,
                        }
                    }
                )
            )
            assert peak - baseline < 2 * 1024 * 1024
            for result in results:
                assert result.text == "x" * 17
                assert result.omitted == rounds * len(chunk) - 17
                assert result.decoding_error is None
        finally:
            capture.close()
            stdout.close()
            stderr.close()
            producer.join(timeout=5)
            if started_tracing:
                tracemalloc.stop()
        assert not producer.is_alive() and not failures


@pytest.mark.posix_only
def test_capture_does_not_complete_until_the_last_output_holder_closes() -> None:
    with ExitStack() as resources:
        out_r, out_w = os.pipe()
        err_r, err_w = os.pipe()
        stdout = resources.enter_context(os.fdopen(out_r, "rb", buffering=0))
        stderr = resources.enter_context(os.fdopen(err_r, "rb", buffering=0))
        out_writer = resources.enter_context(os.fdopen(out_w, "wb", buffering=0))
        err_writer = resources.enter_context(os.fdopen(err_w, "wb", buffering=0))
        capture = BoundedPipeCapture(stdout, stderr, encoding="utf-8", max_chars=2)
        try:
            out_writer.write(b"first")
            out_writer.close()
            err_writer.write(b"second")
            assert not capture.poll(0.1)
            with pytest.raises(RuntimeError, match="not reached EOF"):
                capture.result()
            err_writer.write(b" tail")
            err_writer.close()
            capture.drain()
            assert capture.result() == (
                CapturedOutput("fi", 3, nonblank=True),
                CapturedOutput("se", 9, nonblank=True),
            )
        finally:
            capture.close()
