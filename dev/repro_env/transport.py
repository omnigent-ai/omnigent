"""Stream HTTP and WebSockets between network namespaces over shared Unix sockets.

Only the two provisioned local services are exposed. No arbitrary destinations
or host command execution are accepted by the relay.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import threading
from pathlib import Path


class Relay:
    def __init__(
        self,
        *,
        unix_listener: Path | None = None,
        tcp_target: tuple[str, int] | None = None,
        unix_target: Path | None = None,
    ):
        self.unix_listener = unix_listener
        self.tcp_target = tcp_target
        self.unix_target = unix_target
        self.port = 0
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)

    async def _connect(self, reader, writer):
        remote = None
        tasks = []
        try:
            if self.unix_target:
                other, remote = await asyncio.open_unix_connection(
                    os.path.relpath(self.unix_target)
                )
            else:
                other, remote = await asyncio.open_connection(*self.tcp_target)

            async def copy(source, destination):
                while data := await source.read(65536):
                    destination.write(data)
                    await destination.drain()

            tasks = [
                asyncio.create_task(copy(reader, remote)),
                asyncio.create_task(copy(other, writer)),
            ]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except (OSError, ConnectionError):
            pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for stream in (writer, remote):
                if stream:
                    stream.close()
                    with contextlib.suppress(OSError):
                        await stream.wait_closed()

    async def _start(self):
        if self.unix_listener:
            self.server = await asyncio.start_unix_server(
                self._connect, os.path.relpath(self.unix_listener)
            )
            self.unix_listener.chmod(0o600)
        else:
            self.server = await asyncio.start_server(self._connect, "127.0.0.1", 0)
            self.port = self.server.sockets[0].getsockname()[1]

    async def _close(self):
        if hasattr(self, "server"):
            self.server.close()
            await self.server.wait_closed()
        tasks = asyncio.all_tasks() - {asyncio.current_task()}
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def __enter__(self):
        self.thread.start()
        try:
            asyncio.run_coroutine_threadsafe(self._start(), self.loop).result(timeout=10)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *_):
        try:
            asyncio.run_coroutine_threadsafe(self._close(), self.loop).result(timeout=10)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(timeout=10)
            self.loop.close()
            if self.unix_listener:
                self.unix_listener.unlink(missing_ok=True)
