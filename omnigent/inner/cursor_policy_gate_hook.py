"""Cursor preToolUse hook backed by the harness's policy socket.

The SDK bridge inherits no server credentials. Its hook sends the native tool
request to the harness, which holds the current turn's policy evaluator.
"""

from __future__ import annotations

import json
import os
import socket
import sys

_MAX_REQUEST_BYTES = 1024 * 1024
_MAX_RESPONSE_BYTES = 16 * 1024
_HOOK_APPROVAL_TIMEOUT_S = 86400


def _deny(message: str) -> dict[str, str]:
    return {"permission": "deny", "agent_message": message}


def _evaluate(socket_path: str | None) -> dict[str, str]:
    if not socket_path:
        return _deny("Cursor policy gate unavailable")
    try:
        raw = sys.stdin.read(_MAX_REQUEST_BYTES + 1)
        if len(raw.encode("utf-8")) > _MAX_REQUEST_BYTES:
            return _deny("Malformed Cursor tool request")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return _deny("Malformed Cursor tool request")
        tool_name = payload.get("tool_name") or payload.get("toolName")
        tool_input = payload.get("tool_input", payload.get("toolInput", {}))
        if not isinstance(tool_name, str) or not tool_name or not isinstance(tool_input, dict):
            return _deny("Malformed Cursor tool request")
        request = {"name": tool_name, "arguments": tool_input}
        # The path can exceed a socket address's length; connect by name from its directory.
        os.chdir(os.path.dirname(socket_path))
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(_HOOK_APPROVAL_TIMEOUT_S)
            client.connect(os.path.basename(socket_path))
            client.sendall((json.dumps(request) + "\n").encode("utf-8"))
            with client.makefile("rb") as response_stream:
                raw_response = response_stream.readline(_MAX_RESPONSE_BYTES + 1)
        if len(raw_response) > _MAX_RESPONSE_BYTES or not raw_response.endswith(b"\n"):
            return _deny("Cursor policy gate unavailable")
        response = json.loads(raw_response)
        if not isinstance(response, dict) or response.get("permission") not in {"allow", "deny"}:
            return _deny("Cursor policy gate unavailable")
        message = response.get("agent_message")
        if message is not None and not isinstance(message, str):
            return _deny("Cursor policy gate unavailable")
        return response
    except Exception:  # noqa: BLE001 -- any hook or transport error must block execution
        return _deny("Cursor policy gate unavailable")


def main(socket_path: str | None = None) -> None:
    path = (
        socket_path if socket_path is not None else (sys.argv[1] if len(sys.argv) == 2 else None)
    )
    json.dump(_evaluate(path), sys.stdout)


if __name__ == "__main__":
    main()
