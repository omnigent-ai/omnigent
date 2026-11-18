"""Supported harness login and logout process integrations."""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import urlparse

_RPC_TIMEOUT_S = 10.0


@dataclass(frozen=True)
class CodexDeviceLogin:
    """Public device-code fields returned by Codex app-server."""

    login_id: str
    verification_url: str
    user_code: str


class CodexLoginSession:
    """One persistent Codex app-server used for a device login."""

    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task[None] | None = None
        self._stderr: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._events: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._next_id = 1

    @property
    def pid(self) -> int | None:
        """Return the app-server PID while this session owns one."""
        return self._process.pid if self._process is not None else None

    async def start(self) -> CodexDeviceLogin:
        """Spawn, initialize, and begin a ChatGPT device-code login."""
        await self.start_app_server()
        result = await self.request("account/login/start", {"type": "chatgptDeviceCode"})
        login_id = result.get("loginId")
        verification_url = result.get("verificationUrl")
        user_code = result.get("userCode")
        if (
            not isinstance(login_id, str)
            or not login_id
            or not isinstance(verification_url, str)
            or not verification_url
            or not isinstance(user_code, str)
            or not user_code
        ):
            raise RuntimeError("codex returned incomplete device login details")
        parsed_verification_url = urlparse(verification_url)
        if parsed_verification_url.scheme != "https" or not parsed_verification_url.netloc:
            raise RuntimeError("codex returned an invalid device login URL")
        return CodexDeviceLogin(login_id, verification_url, user_code)

    async def start_app_server(self) -> None:
        """Spawn and initialize the underlying Codex app-server."""
        codex = shutil.which("codex")
        if codex is None:
            raise RuntimeError("codex is not installed")
        self._process = await asyncio.create_subprocess_exec(
            codex,
            "app-server",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._reader = asyncio.create_task(self._reader_loop())
        self._stderr = asyncio.create_task(self._drain_stderr())
        await self.request(
            "initialize",
            {
                "clientInfo": {"name": "omnigent", "version": "0.1"},
                "capabilities": {"experimentalApi": True},
            },
        )
        await self.notify("initialized")

    async def wait_completed(self, login_id: str) -> tuple[bool, str | None]:
        """Wait for the matching account/login/completed notification."""
        while True:
            event = await self._events.get()
            if event is None:
                return False, "codex app-server disconnected"
            if event.get("method") != "account/login/completed":
                continue
            params = event.get("params")
            if not isinstance(params, dict) or params.get("loginId") != login_id:
                continue
            success = params.get("success") is True
            error = params.get("error")
            return success, error if isinstance(error, str) and error else None

    async def cancel(self, login_id: str) -> None:
        """Cancel this session's active login."""
        await self.request("account/login/cancel", {"loginId": login_id})

    async def request(self, method: str, params: dict[str, object]) -> dict[str, Any]:
        """Send one bounded JSON-RPC request."""
        process = self._process
        if process is None or process.stdin is None:
            raise RuntimeError("codex app-server is not running")
        request_id = self._next_id
        self._next_id += 1
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            process.stdin.write(
                (
                    json.dumps({"id": request_id, "method": method, "params": params}) + "\n"
                ).encode()
            )
            await process.stdin.drain()
            response = await asyncio.wait_for(future, timeout=_RPC_TIMEOUT_S)
        finally:
            self._pending.pop(request_id, None)
        if response.get("error"):
            raise RuntimeError(f"codex app-server rejected {method}")
        result = response.get("result")
        return cast(dict[str, Any], result) if isinstance(result, dict) else {}

    async def notify(self, method: str) -> None:
        """Send a JSON-RPC notification."""
        process = self._process
        if process is None or process.stdin is None:
            raise RuntimeError("codex app-server is not running")
        process.stdin.write((json.dumps({"method": method}) + "\n").encode())
        await process.stdin.drain()

    async def close(self) -> None:
        """Stop the app-server and settle every waiter."""
        process = self._process
        self._process = None
        if process is not None and process.returncode is None:
            process.terminate()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), timeout=2)
            if process.returncode is None:
                process.kill()
                await process.wait()
        for task in (self._reader, self._stderr):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._reader = None
        self._stderr = None

    async def _reader_loop(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            while line := await process.stdout.readline():
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(message, dict):
                    continue
                request_id = message.get("id")
                if isinstance(request_id, int):
                    future = self._pending.get(request_id)
                    if future is not None and not future.done():
                        future.set_result(cast(dict[str, Any], message))
                elif isinstance(message.get("method"), str):
                    self._events.put_nowait(cast(dict[str, Any], message))
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("codex app-server disconnected"))
            self._events.put_nowait(None)

    async def _drain_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        while await process.stderr.read(65536):
            pass


async def codex_logout() -> None:
    """Log Codex out through a short-lived initialized app-server."""
    session = CodexLoginSession()
    try:
        await session.start_app_server()
        await session.request("account/logout", {})
    finally:
        await session.close()


async def claude_logout() -> None:
    """Log Claude out through its supported CLI command."""
    claude = shutil.which("claude")
    if claude is None:
        raise RuntimeError("claude is not installed")
    process = await asyncio.create_subprocess_exec(
        claude,
        "auth",
        "logout",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        return_code = await asyncio.wait_for(process.wait(), timeout=_RPC_TIMEOUT_S)
    except TimeoutError:
        raise RuntimeError("claude logout timed out") from None
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if return_code != 0:
        raise RuntimeError("claude auth logout failed")
