"""Yield slow shell calls without restarting their execution."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Coroutine
from typing import Any

from omnigent.util.json_types import JsonObject

_logger = logging.getLogger(__name__)


async def execute_shell_with_yield(
    execution: Coroutine[Any, Any, str],
    *,
    after: float,
    arguments: JsonObject,
    inbox: asyncio.Queue[JsonObject],
    tasks: dict[str, tuple[asyncio.Task[str], asyncio.Event]],
) -> tuple[str, bool]:
    """Return quick output or an inbox handle for the same running command.

    :param execution: Already-authorized shell execution, started once here.
    :param after: Seconds to wait for a foreground result.
    :param arguments: Policy-approved shell arguments for delayed result checks.
    :param inbox: Owning session's completion queue.
    :param tasks: Owning session's cancellable background tasks.
    :returns: Output and whether its actual result is deferred to the inbox.
    """
    execution_task = asyncio.create_task(execution)
    try:
        await asyncio.wait({execution_task}, timeout=after)
    except asyncio.CancelledError:
        execution_task.cancel()
        await asyncio.gather(execution_task, return_exceptions=True)
        raise
    if execution_task.done():
        return execution_task.result(), False

    handle_id = f"async_{uuid.uuid4().hex[:12]}"
    cancel_event = asyncio.Event()

    async def _collect() -> str:
        cancellation = asyncio.create_task(cancel_event.wait())
        status = "completed"
        output = ""
        try:
            await asyncio.wait({execution_task, cancellation}, return_when=asyncio.FIRST_COMPLETED)
            if execution_task.done():
                output = execution_task.result()
            else:
                status = "cancelled"
                execution_task.cancel()
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except Exception as exc:
            _logger.exception("background shell failed")
            status, output = "failed", str(exc)
        finally:
            cancellation.cancel()
            if not execution_task.done():
                execution_task.cancel()
            # Coroutine cancellation does not necessarily stop a shell helper's
            # worker thread; retain ownership until its cleanup has finished.
            await asyncio.gather(execution_task, cancellation, return_exceptions=True)
            inbox.put_nowait(
                {
                    "type": "tool_result",
                    "handle_id": handle_id,
                    "tool_name": "sys_os_shell",
                    "status": status,
                    "output": output,
                    "request_data": {"name": "sys_os_shell", "arguments": arguments},
                }
            )
            tasks.pop(handle_id, None)
        return output

    tasks[handle_id] = (asyncio.create_task(_collect(), name=f"shell-{handle_id}"), cancel_event)
    return (
        json.dumps(
            {
                "handle_id": handle_id,
                "task_id": handle_id,
                "tool_name": "sys_os_shell",
                "status": "in_progress",
                "message": (
                    "The command is still running; do not run it again. "
                    "Call sys_read_inbox to collect its result, or "
                    f"sys_cancel_async with handle_id={handle_id!r} to cancel."
                ),
            }
        ),
        True,
    )
