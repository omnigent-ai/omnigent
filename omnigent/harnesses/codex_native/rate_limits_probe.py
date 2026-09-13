from __future__ import annotations

import asyncio
import json

from omnigent._platform import resolve_cli_binary
from omnigent.harnesses.codex_native.rate_limits import normalize_rate_limits
from omnigent.inner import _proc
from omnigent.inner._subprocess_lifecycle import close_subprocess_transport
from omnigent.inner.agent_env import clean_agent_env
from omnigent.util.json_types import JsonObject

REFRESH_INTERVAL_S = 300.0
_INITIALIZE = (
    b'{"id":1,"method":"initialize","params":{"clientInfo":{"name":"omnigent-host",'
    b'"version":"0.1"},"capabilities":{"experimentalApi":true}}}\n'
)
_READ_LIMITS = (
    b'{"method":"initialized","params":{}}\n'
    b'{"id":2,"method":"account/rateLimits/read","params":{}}\n'
)


async def _response(stdout: asyncio.StreamReader, request_id: int) -> JsonObject:
    while line := await stdout.readline():
        try:
            message = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(message, dict) and message.get("id") == request_id:
            if message.get("error") is not None:
                raise RuntimeError("Codex rate-limit RPC unavailable")
            return message
    raise RuntimeError("Codex app-server closed before replying")


async def read_rate_limits(codex_path: str | None = None) -> JsonObject | None:
    """Read and sanitize the current Host user's subscription windows."""
    binary = codex_path or resolve_cli_binary("codex", env_var="OMNIGENT_CODEX_PATH")
    if binary is None:
        return None
    process = await asyncio.create_subprocess_exec(
        binary,
        "app-server",
        "--listen",
        "stdio://",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=clean_agent_env(allow_exact=("CODEX_HOME",), deny_exact=("OPENAI_API_KEY",)),
        **_proc.spawn_kwargs(),
    )
    assert process.stdin is not None and process.stdout is not None
    try:
        async with asyncio.timeout(12):
            process.stdin.write(_INITIALIZE)
            await process.stdin.drain()
            await _response(process.stdout, 1)
            process.stdin.write(_READ_LIMITS)
            await process.stdin.drain()
            return normalize_rate_limits(await _response(process.stdout, 2))
    # Record cancellation and re-raise after teardown so cleanup is not skipped.
    finally:
        cancelled: asyncio.CancelledError | None = None
        if process.returncode is None:
            _proc.terminate_tree(process)
            try:
                await asyncio.wait_for(asyncio.shield(process.wait()), 2)
            except TimeoutError:
                pass
            except asyncio.CancelledError as exc:
                cancelled = exc
            if process.returncode is None:
                _proc.kill_tree(process)
                try:
                    await asyncio.wait_for(asyncio.shield(process.wait()), 2)
                except asyncio.CancelledError as exc:
                    cancelled = cancelled or exc
                except Exception:  # noqa: BLE001 - teardown is best effort
                    pass
        process.stdin.close()
        try:
            await asyncio.wait_for(asyncio.shield(process.stdin.wait_closed()), 2)
        except asyncio.CancelledError as exc:
            cancelled = cancelled or exc
        except Exception:  # noqa: BLE001 - teardown is best effort
            pass
        close_subprocess_transport(process)
        if cancelled is not None:
            raise cancelled
