"""
Minimal harness fixture for the process-manager / runner tests.

Exports ``create_app() -> FastAPI`` matching the contract from
``designs/SERVER_HARNESS_CONTRACT.md`` §Required harness package
shape, so the runner can import + serve it the same way it would
serve a real harness wrap.

The app implements just enough surface for the lifecycle tests to
verify spawn / health / round-trip behavior:

- ``GET /health`` returns ``{"status": "ok"}`` (the standard probe).
- ``GET /pid`` returns the runner subprocess's pid — useful for
  tests that need to verify the same subprocess persists across
  multiple ``get_client`` calls.
- ``GET /conversation-id`` returns the value the runner stashed
  on ``app.state.conversation_id`` — verifies the
  ``--conversation-id`` plumbing in the runner.
- ``GET /env/{name}`` returns the subprocess's value for the
  given env var (or ``null``). Used by per-spawn-env tests to
  verify ``HarnessProcessManager.get_client(env=...)`` actually
  threads through to the spawned process.
- ``GET /slow-stream`` emits a chunk every 0.5s for ``count``
  seconds. Used by the reaper-mid-stream regression test to
  hold a streaming response open across reaper passes and
  verify the active stream isn't torn down by an over-eager
  ``last_used_at`` cutoff.
- ``GET /stuck-shutdown`` returns immediately but its background
  task ignores cancellation forever. Used to verify a plain
  SIGTERM has a hard-exit backstop even if graceful shutdown
  wedges before lifespan teardown completes.
- ``POST /v1/sessions/{conversation_id}/events`` accepts
  interrupt events. Used by cancel-forwarding tests.

Lives under ``tests/`` so it doesn't ship as production code; the
test process registers the module path
(``"tests.unit.runtime.harnesses._test_harness"``) in
:data:`omnigent.runtime.harnesses._HARNESS_MODULES` per test.
"""

from __future__ import annotations

import asyncio
import os
import time

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse


def create_app() -> FastAPI:
    """
    Build the test fixture harness's FastAPI app.

    :returns: A bare-minimum :class:`FastAPI` instance with three
        introspection endpoints used by the test suite. NOT the
        full harness contract — production wraps implement
        ``/v1/responses`` etc.
    """
    app = FastAPI(title="harness-test-fixture")
    app.state.session_events = []

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Standard liveness probe used by spawn-readiness checks."""
        return {"status": "ok"}

    @app.get("/pid")
    async def pid() -> dict[str, int]:
        """
        Return the subprocess's OS pid for caching-verification tests.

        :returns: ``{"pid": <int>}`` where the int is the runner
            subprocess's own ``os.getpid()``. Tests compare this
            across two calls to confirm the process manager
            re-used the same subprocess.
        """
        return {"pid": os.getpid()}

    @app.get("/conversation-id")
    async def conversation_id(request: Request) -> dict[str, str]:
        """
        Echo back the conversation id the runner stashed on
        ``app.state.conversation_id``.

        :param request: FastAPI's request handle, used to reach
            ``request.app.state.conversation_id``.
        :returns: ``{"conversation_id": <str>}`` proving the
            runner CLI plumbing wired the value through.
        """
        return {"conversation_id": request.app.state.conversation_id}

    @app.get("/env/{name}")
    async def env_var(name: str) -> dict[str, str | None]:
        """
        Return the subprocess's value for an env var.

        Used by per-spawn-env tests to verify that
        ``HarnessProcessManager.get_client(env=...)`` actually
        threads the override into the spawned process's environment
        (rather than only the parent's ``os.environ``).

        :param name: The env var name to look up, e.g.
            ``"HARNESS_TEST_CUSTOM"``.
        :returns: ``{"value": <str>}`` if set, ``{"value": None}``
            otherwise.
        """
        return {"value": os.environ.get(name)}

    @app.get("/slow-stream")
    async def slow_stream(count: int = 5) -> StreamingResponse:
        """
        Stream ``count`` chunks, one per 0.5s, then close cleanly.

        Used by the reaper-mid-stream regression test to hold an
        AP→harness UDS connection open across at least one reaper
        pass. The body iterator yields ``"chunk-N\\n"`` lines so
        the consumer can verify the stream actually delivered all
        chunks (mid-tear-down would surface as a partial read or
        an httpx ``ReadError``). Default ``count=5`` × 0.5s = 2.5s
        — long enough that a 1.0s ``idle_timeout_s`` reaper has
        a clear window to fire mid-stream.

        :param count: Number of chunks to emit, e.g. ``5``.
        :returns: A :class:`StreamingResponse` whose body iterator
            yields one chunk per 0.5s.
        """

        async def _iter() -> object:
            for i in range(count):
                await asyncio.sleep(0.5)
                yield f"chunk-{i}\n".encode()

        return StreamingResponse(_iter(), media_type="text/plain")

    @app.get("/stuck-shutdown")
    async def stuck_shutdown() -> dict[str, str]:
        """Start a background task that ignores cancellation forever."""

        async def _ignore_cancellation() -> None:
            while True:
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    # Keep the task alive so uvicorn's graceful
                    # shutdown path wedges unless the runner's
                    # hard-exit backstop fires.
                    time.sleep(0.1)

        app.state.stuck_shutdown_task = asyncio.create_task(_ignore_cancellation())
        return {"status": "stuck_task_started"}

    @app.post("/v1/sessions/{conversation_id}/events")
    async def session_event(
        conversation_id: str,
        request: Request,
    ) -> Response:
        """
        Accept a harness session event.

        :param conversation_id: Omnigent conversation id, e.g.
            ``"conv_cancel"``.
        :param request: FastAPI request handle.
        :returns: Empty ``204 No Content`` response.
        :raises HTTPException: If the event body is not an
            ``interrupt`` event.
        """
        event = await request.json()
        if not isinstance(event, dict) or event.get("type") != "interrupt":
            raise HTTPException(status_code=400, detail="expected interrupt event")
        request.app.state.session_events.append(
            {"conversation_id": conversation_id, "event": event}
        )
        return Response(status_code=204)

    return app
