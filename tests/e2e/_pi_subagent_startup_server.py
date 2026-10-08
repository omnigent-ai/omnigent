"""Run the real server with an observable delay in Pi's startup HTTP response.

Usage::

    PI_STARTUP_TEST_EVIDENCE_DIR=/tmp/pi-evidence \
      python -m tests.e2e._pi_subagent_startup_server \
      --port 18767 --database-uri sqlite:////tmp/pi.db \
      --artifact-location /tmp/pi-artifacts

All arguments go to the normal ``omnigent server`` CLI. The app factory is
wrapped only to install HTTP timing/observation middleware; the real stores,
routes, tunnel handling, and lifecycle logic remain intact. After the first
child's external-session-id PATCH succeeds, its response headers are withheld
until ``<evidence-dir>/release-startup`` exists. Other requests continue normally.

``http-events.jsonl`` contains only event names, timestamps, session identifiers,
HTTP status codes, lifecycle states, response identifiers, and tool-call
identifiers. It never contains headers, prompts, tool arguments, or results.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi import FastAPI
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from omnigent.stores import ConversationStore

_SESSION_PATH = re.compile(r"^/v1/sessions/([^/]+)(/events)?$")
_OBSERVED_EVENTS = frozenset(
    {"external_session_status", "external_model_change", "external_conversation_item"}
)


class PiStartupObservationMiddleware:
    """Delay one real startup response and record sanitized accepted events."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        evidence_dir: Path,
        conversation_store: ConversationStore,
    ) -> None:
        self.app = app
        self.evidence_dir = evidence_dir
        self.conversation_store = conversation_store
        self._startup_gated = False
        self.evidence_dir.mkdir(parents=True, exist_ok=True)

    def _record(self, event: str, session_id: str, http_status: int, **fields: str) -> None:
        row = {
            "event": event,
            "time": time.time(),
            "session_id": session_id,
            "http_status": http_status,
            **fields,
        }
        with (self.evidence_dir / "http-events.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        match = _SESSION_PATH.fullmatch(scope.get("path", ""))
        method = scope.get("method", "")
        if (
            scope["type"] != "http"
            or match is None
            or (method, bool(match[2])) not in {("PATCH", False), ("POST", True)}
        ):
            await self.app(scope, receive, send)
            return

        session_id = match[1]
        body = bytearray()

        async def observed_receive() -> Message:
            message = await receive()
            if message["type"] == "http.request":
                body.extend(message.get("body", b""))
            return message

        async def observed_send(message: Message) -> None:
            if message["type"] == "http.response.start" and 200 <= message["status"] < 300:
                try:
                    payload = json.loads(body)
                except (ValueError, UnicodeDecodeError):
                    payload = None
                if isinstance(payload, dict):
                    await self._observe_response(session_id, method, payload, message["status"])
            await send(message)

        await self.app(scope, observed_receive, observed_send)

    async def _observe_response(
        self, session_id: str, method: str, payload: dict[str, Any], http_status: int
    ) -> None:
        if method == "PATCH":
            if self._startup_gated or not isinstance(payload.get("external_session_id"), str):
                return
            conversation = await asyncio.to_thread(
                self.conversation_store.get_conversation, session_id
            )
            if (
                self._startup_gated
                or conversation is None
                or not conversation.parent_conversation_id
            ):
                return
            self._startup_gated = True
            self._record("startup_patch_pending", session_id, http_status)
            while not (self.evidence_dir / "release-startup").exists():
                await asyncio.sleep(0.05)
            self._record("startup_patch_released", session_id, http_status)
            return

        event = payload.get("type")
        data = payload.get("data")
        if (
            not isinstance(event, str)
            or event not in _OBSERVED_EVENTS
            or not isinstance(data, dict)
        ):
            return
        fields = {}
        if isinstance(data.get("response_id"), str):
            fields["response_id"] = data["response_id"]
        if event == "external_session_status" and isinstance(data.get("status"), str):
            fields["status"] = data["status"]
        if event == "external_conversation_item":
            if isinstance(data.get("item_type"), str):
                fields["item_type"] = data["item_type"]
            item_data = data.get("item_data")
            if isinstance(item_data, dict) and isinstance(item_data.get("call_id"), str):
                fields["call_id"] = item_data["call_id"]
        self._record(event, session_id, http_status, **fields)


def main() -> None:
    """Add observation middleware, then delegate startup to the real server CLI."""
    import omnigent.server.app as server_app
    from omnigent.cli import cli

    evidence_dir = Path(os.environ["PI_STARTUP_TEST_EVIDENCE_DIR"])
    real_create_app = server_app.create_app

    def create_observed_app(*args: Any, **kwargs: Any) -> FastAPI:
        app = real_create_app(*args, **kwargs)
        app.add_middleware(
            PiStartupObservationMiddleware,
            evidence_dir=evidence_dir,
            conversation_store=kwargs["conversation_store"],
        )
        return app

    with patch.object(server_app, "create_app", create_observed_app):
        cli(args=["server", *sys.argv[1:]], prog_name="pi-startup-test-server")


if __name__ == "__main__":
    main()
