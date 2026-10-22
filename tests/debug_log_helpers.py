"""Capture debug rows synchronously, before ambient logging context unwinds."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from omnigent.debug_logging import record_to_row


@contextmanager
def capture_debug_rows(source: str) -> Iterator[list[dict[str, object]]]:
    rows: list[dict[str, object]] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            rows.append(record_to_row(record, source))

    logger = logging.getLogger("omnigent")
    previous_level = logger.level
    handler = Capture()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield rows
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
