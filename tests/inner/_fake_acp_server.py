"""A tiny fake ACP server over stdio, used by the rovo harness unit tests.

Run as ``python -m tests.inner._fake_acp_server`` (the tests spawn it via the
:class:`~omnigent.inner.rovo_acp.AcpClient` command vector). It speaks just
enough of ACP (JSON-RPC 2.0, newline-framed) to exercise the client/executor:

- ``initialize`` → returns a capabilities result.
- ``session/new`` → returns a ``sessionId`` + ``availableModels``.
- ``session/prompt`` → emits two ``agent_message_chunk`` updates ("PO", "NG")
  then resolves with ``stopReason: end_turn``.
- ``session/cancel`` (notification) → resolves the active prompt with
  ``stopReason: cancelled``.
"""

from __future__ import annotations

import json
import os
import sys


def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _read_msg() -> dict | None:
    line = sys.stdin.readline()
    if not line:
        return None
    line = line.strip()
    if not line:
        return {}
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return {}


def main() -> None:
    session_counter = 0
    permission_request_id = 1000
    # Set FAKE_ACP_PERMISSION=1 to make session/prompt emit a tool_call that
    # first requires a session/request_permission round-trip (regression for
    # the auto-allow handler).
    want_permission = os.environ.get("FAKE_ACP_PERMISSION") == "1"
    while True:
        msg = _read_msg()
        if msg is None:
            break
        if not msg:
            continue
        method = msg.get("method")
        msg_id = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "protocolVersion": 1,
                        "agentCapabilities": {
                            "loadSession": True,
                            "mcpCapabilities": {"http": True, "sse": True},
                        },
                        "authMethods": [{"id": "product-login", "name": "Login"}],
                    },
                }
            )
        elif method == "session/new":
            session_counter += 1
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "sessionId": f"sess-{session_counter}",
                        # Real Rovo shape: a nested ``models`` object.
                        "models": {
                            "availableModels": [
                                {"modelId": "Claude Sonnet 4.6", "name": "Claude Sonnet 4.6"},
                                {"modelId": "Claude Haiku 4.5", "name": "Claude Haiku 4.5"},
                            ],
                            "currentModelId": "Claude Sonnet 4.6",
                        },
                    },
                }
            )
        elif method == "session/set_model":
            # Echo the selected model to stderr so tests can assert on it.
            sys.stderr.write(f"SET_MODEL={params.get('modelId')}\n")
            sys.stderr.flush()
            _send({"jsonrpc": "2.0", "id": msg_id, "result": {}})
        elif method == "session/prompt":
            session_id = params.get("sessionId")
            # Optionally do a permission round-trip first: emit a tool_call
            # update, then request permission, then wait for the client's
            # response before continuing.
            if want_permission:
                _send(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "sessionId": session_id,
                            "update": {
                                "sessionUpdate": "tool_call",
                                "toolCallId": "tc-1",
                                "title": "read_file",
                                "rawInput": {"path": "x"},
                            },
                        },
                    }
                )
                permission_request_id += 1
                pid = permission_request_id
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": pid,
                        "method": "session/request_permission",
                        "params": {
                            "sessionId": session_id,
                            "toolCallId": "tc-1",
                            "options": [
                                {"optionId": "allow", "kind": "allow_once"},
                                {"optionId": "deny", "kind": "reject_once"},
                            ],
                        },
                    }
                )
                # Block until the client answers our permission request.
                resp = _read_msg()
                if resp is None:
                    break
                outcome = (resp.get("result") or {}).get("outcome", {})
                # Echo what we received so the test can assert on it.
                sys.stderr.write(f"PERMISSION_OUTCOME={json.dumps(outcome)}\n")
                sys.stderr.flush()
                _send(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "sessionId": session_id,
                            "update": {
                                "sessionUpdate": "tool_call_update",
                                "toolCallId": "tc-1",
                                "status": "completed",
                                "rawOutput": "file contents",
                            },
                        },
                    }
                )
            # Stream a thought, then two text chunks.
            _send(
                {
                    "jsonrpc": "2.0",
                    "method": "session/update",
                    "params": {
                        "sessionId": session_id,
                        "update": {
                            "sessionUpdate": "agent_thought_chunk",
                            "content": {"type": "text", "text": "thinking"},
                        },
                    },
                }
            )
            for piece in ("PO", "NG"):
                _send(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "sessionId": session_id,
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": piece},
                            },
                        },
                    }
                )
            _send({"jsonrpc": "2.0", "id": msg_id, "result": {"stopReason": "end_turn"}})
        # notifications (no id) are ignored except for clean shutdown on EOF.


if __name__ == "__main__":
    main()
