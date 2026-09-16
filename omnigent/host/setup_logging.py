"""Exclude setup credentials and terminal bytes from protocol debug logging."""

from __future__ import annotations

import logging
import re

_SETUP_SOCKET_PATH = re.compile(r"/hosts/[^/]+/(?:tunnel|setup-operations/[^/]+/attach)$")


class SetupWireLogFilter(logging.Filter):
    """Keep protocol diagnostics while omitting sensitive frame payloads."""

    def __init__(self, *, host_tunnel: bool = False) -> None:
        super().__init__()
        self.host_tunnel = host_tunnel

    def filter(self, record: logging.LogRecord) -> bool:
        if record.msg not in ("< %s", "> %s") or not isinstance(record.args, tuple):
            return True
        if len(record.args) != 1:
            return True
        frame = record.args[0]
        opcode = getattr(frame, "opcode", None)
        data = getattr(frame, "data", None)
        if opcode not in (0, 1, 2) or not isinstance(data, (bytes, bytearray, memoryview)):
            return True
        connection = getattr(record, "websocket", None)
        scope = getattr(connection, "scope", None)
        path = scope.get("path") if isinstance(scope, dict) else getattr(connection, "path", None)
        if (
            not self.host_tunnel
            and isinstance(path, str)
            and not _SETUP_SOCKET_PATH.search(path.partition("?")[0])
        ):
            return True
        # Sans-I/O loggers omit connection identity. Retain opcode and size,
        # but never print an unscoped payload that could belong to setup.
        direction = record.msg[0]
        record.msg = "%s opcode=%s [payload omitted, %d bytes]"
        record.args = (direction, opcode, len(data))
        return True


def setup_host_wire_logger() -> logging.Logger:
    """Return a host-only protocol logger whose payloads cannot be captured."""
    logger = logging.getLogger("omnigent.host.wire")
    if not any(isinstance(item, SetupWireLogFilter) for item in logger.filters):
        logger.addFilter(SetupWireLogFilter(host_tunnel=True))
    return logger


def install_setup_server_log_filter() -> None:
    """Protect the protocol loggers used by Uvicorn's WebSocket backends."""
    for name in ("uvicorn.error", "websockets.server", "websockets.protocol"):
        logger = logging.getLogger(name)
        if not any(isinstance(item, SetupWireLogFilter) for item in logger.filters):
            logger.addFilter(SetupWireLogFilter())
