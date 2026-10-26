"""An explicit parent/CLI handshake gates the first stream without timing sleeps."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
from pathlib import Path
from typing import Any


class ReplStreamGate:
    def __init__(self, executable: str, env: dict[str, str], directory: Path, *, enabled: bool):
        self.executable = executable
        self.env = env.copy()
        self.listener: socket.socket | None = None
        self.observations = directory / "subscription-order.json"
        if enabled:
            self.listener = socket.socket()
            self.listener.bind(("127.0.0.1", 0))
            self.listener.listen(1)
            self.listener.settimeout(30)
            self.env["REPL_TEST_STREAM_GATE_PORT"] = str(self.listener.getsockname()[1])
            self.env["REPL_TEST_STREAM_OBSERVATIONS"] = str(self.observations)
            wrapper = directory / "gated-omnigent"
            wrapper.write_text(
                f"#!{sys.executable}\nfrom tests.e2e._repl_stream_gate import main\nmain()\n"
            )
            wrapper.chmod(0o700)
            self.executable = str(wrapper)

    def release(self) -> None:
        if self.listener is not None:
            connection, _ = self.listener.accept()
            with connection:
                connection.sendall(b"1")

    def verify(self) -> None:
        if self.listener is not None:
            assert json.loads(self.observations.read_text()) == [True, True], (
                "Both message POSTs must follow the actual stream acknowledgment"
            )

    def close(self) -> None:
        if self.listener is not None:
            self.listener.close()


def main() -> None:
    """Run the real CLI with a gate on its first SDK stream attempt."""
    from omnigent_client._sessions import SessionsNamespace

    original_stream = SessionsNamespace.stream
    original_post = SessionsNamespace.post_event
    first = True
    subscribed = False
    observations: list[bool] = []

    async def stream(self: SessionsNamespace, session_id: str):
        nonlocal first, subscribed
        if first:
            first = False
            reader, writer = await asyncio.open_connection(
                "127.0.0.1", int(os.environ["REPL_TEST_STREAM_GATE_PORT"])
            )
            try:
                await reader.readexactly(1)
            finally:
                writer.close()
                await writer.wait_closed()
        async for event in original_stream(self, session_id):
            subscribed = True
            yield event

    async def post(self: SessionsNamespace, session_id: str, event: Any, **kwargs: Any):
        if isinstance(event, dict) and event.get("type") == "message":
            observations.append(subscribed)
            Path(os.environ["REPL_TEST_STREAM_OBSERVATIONS"]).write_text(json.dumps(observations))
        return await original_post(self, session_id, event, **kwargs)

    SessionsNamespace.stream = stream
    SessionsNamespace.post_event = post
    from omnigent.cli import main as cli_main

    cli_main()
