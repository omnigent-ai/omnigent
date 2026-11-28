"""Drain process pipes while retaining only a bounded decoded prefix."""

from __future__ import annotations

import codecs
import io
import os
import selectors
from dataclasses import dataclass
from typing import IO

DEFAULT_OUTPUT_CHARS = 100_000
_READ_BYTES = 64 * 1024
_MAX_DECODER_PENDING = 2 * _READ_BYTES


@dataclass(frozen=True)
class CapturedOutput:
    text: str
    omitted: int = 0
    decoding_error: str | None = None
    nonblank: bool = False


class _TextPrefix:
    def __init__(self, encoding: str, limit: int) -> None:
        self._decoder = io.IncrementalNewlineDecoder(
            codecs.getincrementaldecoder(encoding)(errors="strict"), translate=True
        )
        self._limit = limit
        self._prefix = io.StringIO()
        self._kept = 0
        self._total = 0
        self._nonblank = False
        self._error: str | None = None

    def feed(self, data: bytes, *, final: bool = False) -> None:
        if self._error is not None:
            return
        try:
            decoded = self._decoder.decode(data, final=final)
            pending, _flags = self._decoder.getstate()
            if len(pending) > _MAX_DECODER_PENDING:
                raise UnicodeError("Output decoder retained an oversized incomplete sequence")
        except UnicodeError as exc:
            self._error = str(exc)
            self._prefix.close()
            self._prefix = io.StringIO()
            self._decoder.reset()
            return
        self._total += len(decoded)
        self._nonblank = self._nonblank or bool(decoded.strip())
        remaining = self._limit - self._kept
        if remaining > 0:
            prefix = decoded[:remaining]
            self._prefix.write(prefix)
            self._kept += len(prefix)

    def result(self) -> CapturedOutput:
        if self._error is not None:
            return CapturedOutput("", decoding_error=self._error)
        return CapturedOutput(
            self._prefix.getvalue(), self._total - self._kept, nonblank=self._nonblank
        )


class BoundedPipeCapture:
    """Read both pipes fairly through EOF, including bytes beyond the retained prefix."""

    def __init__(
        self,
        stdout: IO[bytes] | None,
        stderr: IO[bytes] | None,
        *,
        encoding: str,
        max_chars: int,
    ) -> None:
        if max_chars < 1:
            raise ValueError("Output retention must be positive")
        self._outputs = (_TextPrefix(encoding, max_chars), _TextPrefix(encoding, max_chars))
        self._selector = selectors.DefaultSelector()
        try:
            for stream, output in zip((stdout, stderr), self._outputs, strict=True):
                if stream is not None:
                    os.set_blocking(stream.fileno(), False)
                    self._selector.register(stream, selectors.EVENT_READ, output)
        except BaseException:
            self.close()
            raise

    def poll(self, timeout: float) -> bool:
        """Drain at most one fixed-size chunk per ready pipe; return whether both reached EOF."""
        if not self._selector.get_map():
            return True
        for key, _events in self._selector.select(timeout):
            try:
                chunk = os.read(key.fd, _READ_BYTES)
            except BlockingIOError:
                continue
            output: _TextPrefix = key.data
            output.feed(chunk, final=not chunk)
            if not chunk:
                self._selector.unregister(key.fileobj)
        return not self._selector.get_map()

    def drain(self) -> None:
        while not self.poll(0.05):
            pass

    def result(self) -> tuple[CapturedOutput, CapturedOutput]:
        if self._selector.get_map():
            raise RuntimeError("Output holders have not reached EOF")
        return self._outputs[0].result(), self._outputs[1].result()

    def close(self) -> None:
        self._selector.close()
