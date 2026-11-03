"""Serve the shared mock model so any gateway path prefix reaches its ``/v1`` routes.

A harness pinned to a gateway such as ``https://gw/ai-gateway/anthropic`` calls
``/ai-gateway/anthropic/v1/messages``; the prefix is stripped before the
request reaches ``tests/server/integration/mock_llm_server.py``.

Usage::

    python -m tests.e2e.resilience.lab.model_server 9999
"""

from __future__ import annotations

import sys
from typing import Any

import uvicorn

from tests.server.integration.mock_llm_server import app as mock_app


async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    """ASGI entry point that drops anything before the first ``/v1/`` segment."""
    if scope["type"] == "http":
        path = scope["path"]
        start = path.find("/v1/")
        if start > 0:
            scope = {**scope, "path": path[start:], "raw_path": path[start:].encode()}
    await mock_app(scope, receive, send)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=int(sys.argv[1]), log_level="warning")
