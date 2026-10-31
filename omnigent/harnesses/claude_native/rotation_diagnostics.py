"""Safe HTTP failure correlation for Claude session rotation."""

from __future__ import annotations

import re
import uuid
from contextlib import suppress

import httpx


def rotation_failure_attributes(error: httpx.HTTPError) -> dict[str, object]:
    """Describe the failed operation without bodies, URLs, identities, or credentials."""
    attributes: dict[str, object] = {}
    # Correlation must not replace the rotation's original failure.
    with suppress(Exception):
        request = error.request
        path = request.url.path.rstrip("/")
        stage = "unknown"
        if request.method == "GET" and re.search(r"/v1/sessions/[^/]+$", path):
            stage = "read_snapshot"
        elif request.method == "POST" and path.endswith("/v1/sessions"):
            stage = "create_session"
        elif request.method == "POST" and path.endswith("/transfer"):
            stage = "transfer_terminal"
        elif request.method == "PATCH" and re.search(r"/v1/sessions/[^/]+$", path):
            stage = "update_binding"
        attributes["rotation_stage"] = stage
        attributes["authorization_present"] = bool(request.headers.get("authorization"))
        if isinstance(error, httpx.HTTPStatusError):
            attributes["http_status"] = error.response.status_code
            correlation = error.response.headers.get("x-databricks-request-id", "")
            attributes["server_request_id_state"] = "unrecognized" if correlation else "missing"
            if len(correlation) in (32, 36):
                try:
                    attributes["server_request_id"] = str(uuid.UUID(correlation))
                    attributes["server_request_id_state"] = "uuid"
                except ValueError:
                    pass
    return attributes
