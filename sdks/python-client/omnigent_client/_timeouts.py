"""Timeout policies used by the Python SDK HTTP client."""

from __future__ import annotations

import httpx

# Tool execution can leave an SSE connection idle for several minutes. This
# timeout is applied explicitly by streaming endpoints; ordinary requests use
# the timeout configured on ``OmnigentClient``.
_SSE_TIMEOUT = httpx.Timeout(
    connect=30.0,
    read=600.0,
    write=30.0,
    pool=30.0,
)
