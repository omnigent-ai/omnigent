"""Runner registry and routes for chat code-block runs.

Each run gets its own sandbox helper so a long run never holds the shared
environment's lock. Output is coalesced into ``session.code_run.output``
events on the session's runner stream, and the ``finished`` item is posted to
the server through ``external_conversation_item`` with a deterministic
``source_id``, so a retried post cannot duplicate it.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, MutableSequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.entities.conversation import (
    CODE_RUN_OUTPUT_MAX_CHARS,
    CODE_RUN_START_REFUSALS,
    code_run_source_id,
)
from omnigent.inner.async_utils import run_sync_cleanup

if TYPE_CHECKING:
    from omnigent.inner.os_env import OpResult

_logger = logging.getLogger(__name__)

MAX_RUNNING_PER_SESSION = 4
CODE_RUN_LANGUAGES = frozenset({"shell", "python", "node"})
# Output is published at most this long after it arrives, or as soon as this
# many characters are pending.
_FLUSH_INTERVAL_S = 0.2
_FLUSH_CHARS = 16 * 1024
# A stopped or timed-out run whose helper has not ended by then is killed.
_STOP_GRACE_S = 5.0
_FINISH_POST_DEADLINE_S = 300.0
_FINISH_POST_MAX_BACKOFF_S = 30.0
_FINISH_POST_TIMEOUT_S = 30.0

_COPY_ON_WRITE_REFUSAL = {
    "code": CODE_RUN_START_REFUSALS[409],
    "reason": "Code runs are unavailable in sessions whose sandbox uses copy-on-write roots",
}


class CodeRunEnvironment(Protocol):
    """The slice of :class:`CallerProcessOSEnvironment` a run uses."""

    async def shell_stream(
        self,
        command: str,
        *,
        language: str,
        timeout: int,
        on_output: Callable[[str, str], None],
    ) -> OpResult: ...

    def stop_shell_stream(self) -> None: ...

    def kill_shell_stream(self) -> None: ...

    def close(self) -> None: ...


class CodeRunServerClient(Protocol):
    """The slice of the runner's server client a run uses."""

    async def post(
        self, url: str, *, json: object = None, timeout: float | None = None
    ) -> httpx.Response: ...


class CodeRunLimitError(Exception):
    """The session already has :data:`MAX_RUNNING_PER_SESSION` runs going."""


class CodeRunOutput:
    """Bounded copy of a run's output: the start and the end, up to *limit* chars."""

    def __init__(self, limit: int = CODE_RUN_OUTPUT_MAX_CHARS) -> None:
        self._head_limit = limit // 2
        self._tail_limit = limit - self._head_limit
        self._head: list[list[str]] = []
        self._head_chars = 0
        self._tail: collections.deque[list[str]] = collections.deque()
        self._tail_chars = 0
        self.total = 0

    @property
    def truncated(self) -> bool:
        """Whether output between the kept start and end was dropped."""
        return self.total > self._head_chars + self._tail_chars

    def append(self, stream: str, text: str) -> None:
        """Record *text* from *stream*, dropping the middle past the cap."""
        self.total += len(text)
        room = self._head_limit - self._head_chars
        if room > 0:
            _append_segment(self._head, stream, text[:room])
            self._head_chars += len(text[:room])
            text = text[room:]
        if not text:
            return
        _append_segment(self._tail, stream, text)
        self._tail_chars += len(text)
        while self._tail_chars > self._tail_limit:
            excess = self._tail_chars - self._tail_limit
            oldest = self._tail[0]
            if len(oldest[1]) <= excess:
                self._tail.popleft()
                self._tail_chars -= len(oldest[1])
            else:
                oldest[1] = oldest[1][excess:]
                self._tail_chars -= excess

    def segments(self) -> list[dict[str, str]]:
        """Return the kept output as ``{stream, text}`` segments in order."""
        merged: list[list[str]] = [list(segment) for segment in self._head]
        for stream, text in self._tail:
            if self.truncated:
                # Keep the cut visible: never join text across the dropped middle.
                merged.append([stream, text])
            else:
                _append_segment(merged, stream, text)
        return [{"stream": stream, "text": text} for stream, text in merged]


def _append_segment(segments: MutableSequence[list[str]], stream: str, text: str) -> None:
    if not text:
        return
    if segments and segments[-1][0] == stream:
        segments[-1][1] += text
    else:
        segments.append([stream, text])


@dataclass
class _CodeRun:
    session_id: str
    run_id: str
    language: str
    command: str
    timeout: int
    started_at: float = field(default_factory=time.monotonic)
    output: CodeRunOutput = field(default_factory=CodeRunOutput)
    environment: CodeRunEnvironment | None = None
    stop_requested: bool = False
    task: asyncio.Task[None] | None = None
    stop_task: asyncio.Task[None] | None = None
    pending: list[list[str]] = field(default_factory=list)
    pending_chars: int = 0
    published: int = 0
    flush_handle: asyncio.TimerHandle | None = None


class CodeRunRegistry:
    """Active code runs keyed by ``(session_id, run_id)``.

    :param publish_event: Puts an event on a session's runner stream.
    :param server_client: Client used to post the ``finished`` item.
    :param environment_factory: Builds a fresh sandbox helper for a session's run.
    :param sleep: Backoff sleep between ``finished`` post attempts.
    """

    def __init__(
        self,
        *,
        publish_event: Callable[[str, Mapping[str, object]], None],
        server_client: CodeRunServerClient,
        environment_factory: Callable[[str], Awaitable[CodeRunEnvironment | None]],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._publish_event = publish_event
        self._server_client = server_client
        self._environment_factory = environment_factory
        self._sleep = sleep
        self._runs: dict[tuple[str, str], _CodeRun] = {}

    def start(
        self, session_id: str, *, run_id: str, language: str, command: str, timeout: int
    ) -> None:
        """Start a run in the background; a known ``run_id`` is a no-op.

        :raises CodeRunLimitError: When the session already has the maximum running.
        """
        if (session_id, run_id) in self._runs:
            return
        if len(self._session_runs(session_id)) >= MAX_RUNNING_PER_SESSION:
            raise CodeRunLimitError(f"At most {MAX_RUNNING_PER_SESSION} code runs per session")
        run = _CodeRun(session_id, run_id, language, command, timeout)
        self._runs[(session_id, run_id)] = run
        run.task = asyncio.create_task(self._execute(run), name=f"code-run-{run_id}")

    def stop(self, session_id: str, run_id: str) -> bool:
        """Ask a run to stop. Returns ``False`` for unknown or finished runs."""
        run = self._runs.get((session_id, run_id))
        if run is None or run.task is None or run.task.done():
            return False
        if not run.stop_requested:
            run.stop_requested = True
            if run.environment is not None:
                run.environment.stop_shell_stream()
            run.stop_task = asyncio.create_task(
                self._enforce_stop(run), name=f"code-run-stop-{run_id}"
            )
        return True

    def running(self, session_id: str) -> list[dict[str, object]]:
        """Snapshot every active run of *session_id* with its output so far."""
        return [
            {"run_id": run.run_id, "offset": run.output.total, "output": run.output.segments()}
            for run in self._session_runs(session_id)
        ]

    async def stop_session(self, session_id: str) -> None:
        """Stop every run of *session_id* and wait briefly for them to end."""
        runs = self._session_runs(session_id)
        for run in runs:
            self.stop(session_id, run.run_id)
        tasks = {run.task for run in runs if run.task is not None}
        if tasks:
            await asyncio.wait(tasks, timeout=_STOP_GRACE_S + 1.0)

    async def wait(self, session_id: str, run_id: str) -> None:
        """Wait until the run has ended and its ``finished`` post settled."""
        run = self._runs.get((session_id, run_id))
        if run is not None and run.task is not None:
            await asyncio.shield(run.task)

    def _session_runs(self, session_id: str) -> list[_CodeRun]:
        return [run for (owner, _), run in self._runs.items() if owner == session_id]

    async def _enforce_stop(self, run: _CodeRun) -> None:
        # Re-send stop until the helper reads it (it may still be starting),
        # then kill the helper once the grace runs out.
        assert run.task is not None
        deadline = time.monotonic() + _STOP_GRACE_S
        while True:
            await asyncio.wait({run.task}, timeout=0.5)
            environment = run.environment
            if run.task.done():
                return
            if environment is None:
                continue
            if time.monotonic() < deadline:
                environment.stop_shell_stream()
            else:
                environment.kill_shell_stream()

    async def _execute(self, run: _CodeRun) -> None:
        result: OpResult = {}
        try:
            try:
                run.environment = await self._environment_factory(run.session_id)
                if run.environment is None:
                    raise RuntimeError("Unable to create an OS environment for the run")
                if not run.stop_requested:
                    result = await self._stream(run)
            except Exception as exc:  # noqa: BLE001 - a run that cannot start is saved as failed
                _logger.warning(
                    "code run %s failed: %s",
                    run.run_id,
                    exc,
                    extra={"session_id": run.session_id},
                )
                result = {"error": str(exc)}
            finally:
                if run.environment is not None:
                    await run_sync_cleanup(
                        run.environment.close,
                        component="runner_code_run",
                        session_id=run.session_id,
                    )
            error = result.get("error")
            if isinstance(error, str) and not run.stop_requested:
                self._on_output(run, "stderr", error + "\n")
            self._flush(run)
            await self._post_finished(run, result)
        finally:
            self._runs.pop((run.session_id, run.run_id), None)

    async def _stream(self, run: _CodeRun) -> OpResult:
        assert run.environment is not None
        loop = asyncio.get_running_loop()

        def on_output(stream: str, text: str) -> None:
            loop.call_soon_threadsafe(self._on_output, run, stream, text)

        stream_task = asyncio.ensure_future(
            run.environment.shell_stream(
                run.command, language=run.language, timeout=run.timeout, on_output=on_output
            )
        )
        # The helper enforces the timeout; this backstops a helper that stopped answering.
        done, _ = await asyncio.wait({stream_task}, timeout=run.timeout + _STOP_GRACE_S)
        if not done:
            run.environment.kill_shell_stream()
        result = await stream_task
        # Let output the helper thread scheduled land before the result is saved.
        await asyncio.sleep(0)
        return result

    def _on_output(self, run: _CodeRun, stream: str, text: str) -> None:
        run.output.append(stream, text)
        _append_segment(run.pending, stream, text)
        run.pending_chars += len(text)
        if run.pending_chars >= _FLUSH_CHARS:
            self._flush(run)
        elif run.flush_handle is None:
            run.flush_handle = asyncio.get_running_loop().call_later(
                _FLUSH_INTERVAL_S, self._flush, run
            )

    def _flush(self, run: _CodeRun) -> None:
        if run.flush_handle is not None:
            run.flush_handle.cancel()
            run.flush_handle = None
        for stream, text in run.pending:
            for start in range(0, len(text), _FLUSH_CHARS):
                piece = text[start : start + _FLUSH_CHARS]
                self._publish_event(
                    run.session_id,
                    {
                        "type": "session.code_run.output",
                        "run_id": run.run_id,
                        "offset": run.published,
                        "stream": stream,
                        "text": piece,
                    },
                )
                run.published += len(piece)
        run.pending = []
        run.pending_chars = 0

    async def _post_finished(self, run: _CodeRun, result: OpResult) -> None:
        raw_exit = result.get("exit_code")
        if run.stop_requested:
            status, exit_code = "stopped", None
        elif "error" in result:
            status, exit_code = "failed", None
        elif result.get("timed_out"):
            status, exit_code = "timed_out", None
        else:
            status, exit_code = "exited", raw_exit if isinstance(raw_exit, int) else None
        body = {
            "type": "external_conversation_item",
            "data": {
                "item_type": "code_run",
                "item_data": {
                    "run_id": run.run_id,
                    "phase": "finished",
                    "status": status,
                    "exit_code": exit_code,
                    "duration_ms": int((time.monotonic() - run.started_at) * 1000),
                    "output": run.output.segments(),
                    "truncated": run.output.truncated or bool(result.get("truncated")),
                },
                "response_id": run.run_id,
                "source_id": code_run_source_id(run.run_id, "finished"),
            },
        }
        url = f"/v1/sessions/{run.session_id}/events"
        waited = 0.0
        delay = 1.0
        while True:
            outcome = "unreachable"
            with contextlib.suppress(httpx.HTTPError, OSError):
                response = await self._server_client.post(
                    url, json=body, timeout=_FINISH_POST_TIMEOUT_S
                )
                if response.status_code < 400:
                    return
                if response.status_code < 500 and response.status_code != 429:
                    _logger.warning(
                        "server refused finished code run %s: %s",
                        run.run_id,
                        response.status_code,
                        extra={"session_id": run.session_id},
                    )
                    return
                outcome = f"status {response.status_code}"
            if waited + delay > _FINISH_POST_DEADLINE_S:
                _logger.warning(
                    "giving up on saving finished code run %s (%s)",
                    run.run_id,
                    outcome,
                    extra={"session_id": run.session_id},
                )
                return
            await self._sleep(delay)
            waited += delay
            delay = min(delay * 2, _FINISH_POST_MAX_BACKOFF_S)


def register_code_run_routes(
    app: FastAPI,
    *,
    registry: CodeRunRegistry,
    uses_copy_on_write: Callable[[str], Awaitable[bool]],
) -> None:
    """Register the ``/v1/sessions/{id}/code-runs`` routes on *app*.

    :param registry: The runner's run registry.
    :param uses_copy_on_write: Whether a session's sandbox has copy-on-write
        roots; those sessions share the primary helper with the agent.
    """

    @app.post("/v1/sessions/{session_id}/code-runs")
    async def start_code_run(session_id: str, request: Request) -> JSONResponse:
        body = await request.json()
        fields = body if isinstance(body, dict) else {}
        run_id = fields.get("run_id")
        language = fields.get("language")
        command = fields.get("command")
        timeout = fields.get("timeout")
        if (
            not isinstance(run_id, str)
            or not run_id
            or not isinstance(language, str)
            or language not in CODE_RUN_LANGUAGES
            or not isinstance(command, str)
            or not command.strip()
            or not isinstance(timeout, int)
            or isinstance(timeout, bool)
            or timeout < 1
        ):
            return JSONResponse(
                status_code=400,
                content={
                    "code": CODE_RUN_START_REFUSALS[400],
                    "reason": "Malformed code run request",
                },
            )
        if await uses_copy_on_write(session_id):
            return JSONResponse(status_code=409, content=_COPY_ON_WRITE_REFUSAL)
        try:
            registry.start(
                session_id, run_id=run_id, language=language, command=command, timeout=timeout
            )
        except CodeRunLimitError as exc:
            return JSONResponse(
                status_code=429,
                content={"code": CODE_RUN_START_REFUSALS[429], "reason": str(exc)},
            )
        return JSONResponse(status_code=202, content={"run_id": run_id})

    @app.post("/v1/sessions/{session_id}/code-runs/{run_id}/stop")
    async def stop_code_run(session_id: str, run_id: str) -> JSONResponse:
        return JSONResponse(
            status_code=200, content={"stopped": registry.stop(session_id, run_id)}
        )

    @app.get("/v1/sessions/{session_id}/code-runs")
    async def list_code_runs(session_id: str, status: str = "running") -> JSONResponse:
        del status  # Only active runs are kept; finished runs live on the server.
        return JSONResponse(
            status_code=200,
            content={
                "object": "list",
                "data": registry.running(session_id),
                "copy_on_write": await uses_copy_on_write(session_id),
            },
        )
