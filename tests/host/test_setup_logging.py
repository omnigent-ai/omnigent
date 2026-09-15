"""Protocol DEBUG must not expose setup credentials or terminal payloads."""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest
from websockets.frames import Frame, Opcode
from websockets.legacy.framing import Frame as LegacyFrame

from omnigent.host.setup_logging import install_setup_server_log_filter, setup_host_wire_logger


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize(
    "path", ["/v1/hosts/host-a/tunnel", "/v1/hosts/host-a/setup-operations/op/attach", None]
)
def test_protocol_debug_omits_setup_bytes(caplog, path, legacy):
    install_setup_server_log_filter()
    logger = logging.getLogger("uvicorn.error")
    extra = {"websocket": SimpleNamespace(scope={"path": path})} if path else {}
    frame = (
        LegacyFrame(True, Opcode.BINARY, b"fixture-secret")
        if legacy
        else Frame(Opcode.BINARY, b"fixture-secret")
    )
    with caplog.at_level(logging.DEBUG, logger="uvicorn.error"):
        logger.debug("< %s", frame, extra=extra)
        logger.debug("= connection is OPEN", extra=extra)
    assert "fixture-secret" not in caplog.text
    assert "payload omitted, 14 bytes" in caplog.text
    assert "connection is OPEN" in caplog.text


def test_host_protocol_debug_omits_credential_json(caplog):
    logger = setup_host_wire_logger()
    with caplog.at_level(logging.DEBUG, logger=logger.name):
        logger.debug("> %s", Frame(Opcode.TEXT, b'{"secret":"fixture-secret"}'))
    assert "fixture-secret" not in caplog.text
    assert "payload omitted" in caplog.text


def test_unrelated_identified_websocket_diagnostics_remain(caplog):
    install_setup_server_log_filter()
    logger = logging.getLogger("uvicorn.error")
    with caplog.at_level(logging.DEBUG, logger=logger.name):
        logger.debug(
            "< %s",
            Frame(Opcode.TEXT, b"ordinary-message"),
            extra={"websocket": SimpleNamespace(scope={"path": "/v1/sessions/fixture/events"})},
        )
    assert "ordinary-message" in caplog.text
