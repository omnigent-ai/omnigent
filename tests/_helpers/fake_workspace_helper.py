"""A stand-in for the Rust workspace helper, for host proxy tests.

:func:`write_fake_helper` writes an executable that runs it with
``--connection-fd N --exit-on-stdin-eof``, as the host runs the real helper. It prints the
helper's ready line, then serves WorkspaceService and WorkspaceHelperService
(gRPC over HTTP/2) on the inherited connection, answering from the host's
Python :class:`~omnigent.workspace_fs.WorkspaceReader`. Environment switches
inject the faults the tests need:

- ``FAKE_HELPER_READY=never``: never print the ready line.
- ``FAKE_HELPER_PROTOCOL=<n>``: report another protocol version.
- ``FAKE_HELPER_CAPABILITIES``: comma-separated capabilities to report.
- ``FAKE_HELPER_HANG_CLOSE=1``: CloseWorkspace never answers.
- ``FAKE_HELPER_IGNORE_TERM=1``: ignore SIGTERM, so only SIGKILL stops it.
- ``FAKE_HELPER_EVENTS=<path>``: append ``start <pid>`` at start, and one
  line per Open/CloseWorkspace.
- ``FAKE_HELPER_OPEN_DELAY=<s>``: wait that long before answering OpenWorkspace.

SearchText answers one match per entry under the workspace root, in batches
of one, waiting ``FAKE_HELPER_TEXT_DELAY`` seconds before each, and exits
the process mid-stream when the query is ``"crash"``. With
``FAKE_HELPER_TEXT_GATE=<path>`` it sends the first batch, then waits for
that file to exist before the rest.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shlex
import signal
import socket
import stat
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pytest

FAKE_WORKSPACE_HELPER_SCRIPT = Path(__file__)
_REPOSITORY_ROOT = FAKE_WORKSPACE_HELPER_SCRIPT.parents[2]
DEFAULT_CAPABILITIES = (
    "ListDirectory",
    "ReadFile",
    "StatPath",
    "SearchText",
    "gitignore",
    # Fork-only names the host must not pass on.
    "WriteAttachment",
    "pty",
)


def write_fake_helper(directory: Path, **env: str) -> Path:
    """Write an executable that runs this fake with ``env`` set, for the host to start.

    :param directory: Where to write it, e.g. a test's ``tmp_path``.
    :param env: The fake's switches, e.g. ``FAKE_HELPER_READY="never"``.
    """
    exports = "".join(f"export {name}={shlex.quote(value)}\n" for name, value in env.items())
    path = directory / "workspace-helper"
    path.write_text(
        "#!/bin/sh\n"
        f"{exports}"
        f"export PYTHONPATH={shlex.quote(str(_REPOSITORY_ROOT))}\n"
        f"exec {shlex.quote(sys.executable)}"
        f' {shlex.quote(str(FAKE_WORKSPACE_HELPER_SCRIPT))} "$@"\n'
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def relax_helper_deadlines(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the fake the start and open time a loaded machine needs.

    The fake is a Python process that imports the host's reader, so it starts
    far slower than the real helper; tests of a deadline set their own.
    """
    from omnigent.host.workspace_helper import process, proxy

    monkeypatch.setattr(process, "_READY_TIMEOUT_S", 60.0)
    monkeypatch.setattr(proxy, "_OPEN_TIMEOUT_MS", 60_000)


def _event(line: str) -> None:
    path = os.environ.get("FAKE_HELPER_EVENTS")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def _services() -> list[Any]:  # type: ignore[explicit-any]
    from connectrpc.code import Code
    from connectrpc.errors import ConnectError

    from omnigent.api.workspace.v1alpha1.workspace_connect import (
        WorkspaceService,
        WorkspaceServiceASGIApplication,
    )
    from omnigent.api.workspace.v1alpha1.workspace_helper_connect import (
        WorkspaceHelperService,
        WorkspaceHelperServiceASGIApplication,
    )
    from omnigent.api.workspace.v1alpha1.workspace_helper_pb2 import (
        CloseWorkspaceResponse,
        GetInfoResponse,
        OpenWorkspaceResponse,
    )
    from omnigent.api.workspace.v1alpha1.workspace_pb2 import (
        Entry,
        SearchTextResponse,
        StatPathResponse,
        TextSearchMatch,
    )
    from omnigent.host.workspace_service import WorkspaceService as PythonWorkspaceService
    from omnigent.workspace_fs import WorkspaceReader

    workspaces: dict[str, WorkspaceReader] = {}
    counter = 0

    def reader_for(ctx: Any) -> WorkspaceReader:  # type: ignore[explicit-any]
        workspace_id = ctx.request_headers.get("omnigent-workspace-id") if ctx else None
        if workspace_id not in workspaces:
            raise ConnectError(Code.NOT_FOUND, f"No open workspace {workspace_id!r}")
        return workspaces[workspace_id]

    class Control(WorkspaceHelperService):
        async def get_info(self, request: Any, ctx: Any) -> Any:  # type: ignore[explicit-any]
            return _info(GetInfoResponse)

        async def open_workspace(self, request: Any, ctx: Any) -> Any:  # type: ignore[explicit-any]
            nonlocal counter
            # Ids restart with the process, as the real helper's do.
            counter += 1
            workspace_id = f"w{counter}"
            _event(f"opening {workspace_id} {request.root}")
            await asyncio.sleep(float(os.environ.get("FAKE_HELPER_OPEN_DELAY", "0")))
            workspaces[workspace_id] = WorkspaceReader(Path(request.root))
            _event(f"open {workspace_id} {request.root}")
            return OpenWorkspaceResponse(workspace_id=workspace_id, root=request.root)

        async def close_workspace(self, request: Any, ctx: Any) -> Any:  # type: ignore[explicit-any]
            if os.environ.get("FAKE_HELPER_HANG_CLOSE"):
                await asyncio.Event().wait()
            workspaces.pop(request.workspace_id, None)
            _event(f"close {request.workspace_id}")
            return CloseWorkspaceResponse()

    class Workspace(PythonWorkspaceService, WorkspaceService):
        def __init__(self) -> None:
            super().__init__(reader_for)

        async def stat_path(self, request: Any, ctx: Any) -> Any:  # type: ignore[explicit-any]
            reader = reader_for(ctx)
            # The root's name in the answer lets a test see which workspace answered.
            return StatPathResponse(entry=Entry(name=reader.root.name, path=request.path))

        async def search_text(  # type: ignore[explicit-any]
            self, request: Any, ctx: Any
        ) -> AsyncIterator[Any]:
            reader = reader_for(ctx)
            delay = float(os.environ.get("FAKE_HELPER_TEXT_DELAY", "0"))
            gate = os.environ.get("FAKE_HELPER_TEXT_GATE")
            for index, name in enumerate(sorted(os.listdir(reader.root))):
                await asyncio.sleep(delay)
                while gate and index and not os.path.exists(gate):
                    await asyncio.sleep(0.05)
                if request.query == "crash":
                    os._exit(3)
                match = TextSearchMatch(path=name, line=1, column=1, text=request.query)
                yield SearchTextResponse(matches=[match])
            yield SearchTextResponse(done=True)

    return [
        WorkspaceServiceASGIApplication(Workspace()),
        WorkspaceHelperServiceASGIApplication(Control()),
    ]


def _info(response_type: Any) -> Any:  # type: ignore[explicit-any]
    raw = os.environ.get("FAKE_HELPER_CAPABILITIES")
    capabilities = raw.split(",") if raw is not None else list(DEFAULT_CAPABILITIES)
    return response_type(
        protocol_version=int(os.environ.get("FAKE_HELPER_PROTOCOL", "1")),
        version="fake",
        capabilities=capabilities,
    )


async def _serve(connection_fd: int) -> None:
    import h2.config
    import h2.connection
    import h2.events

    apps = _services()
    sock = socket.socket(fileno=connection_fd)
    reader, writer = await asyncio.open_unix_connection(sock=sock)
    conn = h2.connection.H2Connection(
        h2.config.H2Configuration(client_side=False, header_encoding="utf-8")
    )
    conn.initiate_connection()
    writer.write(conn.data_to_send())
    bodies: dict[int, asyncio.Queue[bytes | None]] = {}
    windows: dict[int, asyncio.Event] = {}
    tasks: set[asyncio.Task[None]] = set()

    def flush() -> None:
        writer.write(conn.data_to_send())

    async def send_data(stream_id: int, data: bytes) -> None:
        view = memoryview(data)
        while view:
            window = min(conn.local_flow_control_window(stream_id), conn.max_outbound_frame_size)
            if window <= 0:
                event = windows.setdefault(stream_id, asyncio.Event())
                event.clear()
                await event.wait()
                continue
            conn.send_data(stream_id, bytes(view[:window]))
            view = view[window:]
            flush()

    async def run(stream_id: int, headers: list[tuple[str, str]]) -> None:
        pseudo = dict(item for item in headers if item[0].startswith(":"))
        path = pseudo[":path"]
        app = (
            next((a for a in apps if path.startswith("/" + a.path.strip("/") + "/")), None)
            or apps[0]
        )
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "2",
            "method": pseudo[":method"],
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [(k.encode(), v.encode()) for k, v in headers if not k.startswith(":")],
            "server": ("helper", 80),
            "client": ("helper", 0),
            "extensions": {"http.response.trailers": {}},
        }

        async def receive() -> dict[str, Any]:  # type: ignore[explicit-any]
            chunk = await bodies[stream_id].get()
            if chunk is None:
                return {"type": "http.request", "body": b"", "more_body": False}
            return {"type": "http.request", "body": chunk, "more_body": True}

        expects_trailers = False

        async def send(message: dict[str, Any]) -> None:  # type: ignore[explicit-any]
            nonlocal expects_trailers
            if message["type"] == "http.response.start":
                # With trailers announced, they, not the last body, end the stream.
                expects_trailers = bool(message.get("trailers"))
                response = [(":status", str(message["status"]))] + [
                    (k.decode(), v.decode()) for k, v in message.get("headers", [])
                ]
                conn.send_headers(stream_id, response)
                flush()
            elif message["type"] == "http.response.body":
                await send_data(stream_id, message.get("body", b""))
                if not message.get("more_body") and not expects_trailers:
                    conn.end_stream(stream_id)
                    flush()
            elif message["type"] == "http.response.trailers":
                trailers = [(k.decode(), v.decode()) for k, v in message.get("headers", [])]
                conn.send_headers(stream_id, trailers, end_stream=True)
                flush()

        with contextlib.suppress(Exception):
            await app(scope, receive, send)

    while data := await reader.read(65536):
        for event in conn.receive_data(data):
            if isinstance(event, h2.events.RequestReceived):
                bodies[event.stream_id] = asyncio.Queue()
                task = asyncio.create_task(run(event.stream_id, list(event.headers or [])))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
            elif isinstance(event, h2.events.DataReceived):
                bodies[event.stream_id].put_nowait(event.data or b"")
                conn.acknowledge_received_data(event.flow_controlled_length or 0, event.stream_id)
            elif isinstance(event, h2.events.StreamEnded):
                bodies[event.stream_id].put_nowait(None)
            elif isinstance(event, h2.events.StreamReset):
                bodies.pop(event.stream_id, None)
            elif isinstance(event, h2.events.WindowUpdated):
                for window in windows.values():
                    window.set()
        flush()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--connection-fd", type=int, required=True)
    parser.add_argument("--exit-on-stdin-eof", action="store_true")
    arguments = parser.parse_args()
    _event(f"start {os.getpid()}")
    if os.environ.get("FAKE_HELPER_IGNORE_TERM"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if os.environ.get("FAKE_HELPER_READY") != "never":
        info = _info(dict)
        ready = {
            "ready": True,
            "protocolVersion": info["protocol_version"],
            "version": info["version"],
            "capabilities": info["capabilities"],
        }
        print(json.dumps(ready), flush=True)

    async def serve_until_eof() -> None:
        serving = asyncio.ensure_future(_serve(arguments.connection_fd))
        stdin_closed = asyncio.ensure_future(asyncio.to_thread(sys.stdin.buffer.read))
        await asyncio.wait({serving, stdin_closed}, return_when=asyncio.FIRST_COMPLETED)
        os._exit(0)

    asyncio.run(serve_until_eof())


if __name__ == "__main__":
    main()
