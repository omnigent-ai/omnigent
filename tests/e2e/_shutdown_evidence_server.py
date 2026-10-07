"""Observe production shutdown events and incoming wake requests without changing them."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from omnigent.debug_logging import debug_event

_EVENTS = {
    "session_shutdown_requested",
    "session_shutdown_applied",
    "session_lifecycle_started",
    "runner_stream_disconnected",
    "shutdown_test_message_received",
}
_logger = logging.getLogger("omnigent.server.shutdown_observer")


class _Evidence(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        if getattr(record, "event_name", None) not in _EVENTS:
            return
        row = {
            "event_name": record.event_name,
            "at_ms": int(record.created * 1000),
            "session_id": getattr(record, "session_id", None),
            "attributes": record.attributes,
        }
        with Path(os.environ["OMNIGENT_E2E_SHUTDOWN_EVIDENCE"]).open("a") as stream:
            stream.write(json.dumps(row) + "\n")


class _ObserveMessages:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        observe = (
            scope["type"] == "http"
            and scope["method"] == "POST"
            and path.startswith("/v1/sessions/")
            and path.endswith("/events")
        )
        chunks = []

        async def observed_receive():
            message = await receive()
            if observe and message["type"] == "http.request":
                chunks.append(message.get("body", b""))
                if not message.get("more_body"):
                    body = json.loads(b"".join(chunks))
                    if body.get("type") == "message":
                        _logger.info(
                            "Observed test message",
                            extra=debug_event(
                                "shutdown_test_message_received",
                                session_id=path.split("/")[-2],
                                payload=body,
                            ),
                        )
            return message

        await self.app(scope, observed_receive, send)


def main() -> None:
    import omnigent.server.app as server_app
    from omnigent.cli import main as cli_main

    create_app = server_app.create_app

    def observed_app(*args, **kwargs):
        app = create_app(*args, **kwargs)
        logging.getLogger("omnigent.server").addHandler(_Evidence())
        app.add_middleware(_ObserveMessages)
        return app

    server_app.create_app = observed_app
    cli_main()
