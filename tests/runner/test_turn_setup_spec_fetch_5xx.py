"""Background turn setup vs. 5xx failures on the agent-bundle fetch.

When a user sends a message, the runner sets up the turn in the background
(``_run_turn_bg_setup_and_stream``). Turn setup resolves the session's agent
spec by fetching the bundle from the Omnigent server
(``GET /v1/sessions/{id}/agent/contents``). A server 5xx on that fetch is
usually transient — a restarting backend or a proxy blip — so the resolver
(``_resolve_agent_spec_from_server``) retries it with a short bounded backoff
before giving up.

Two contracts are pinned here, both driven through the production
background-turn path (``POST /v1/sessions/{id}/events`` with no
``?stream=true``) with the *real* resolver bound to a stubbed server client,
asserting off the runner's own per-session event queue — the same queue the
SSE ``/stream`` endpoint (and therefore the web UI) drains:

* A **transient** 5xx window (a few failures, then recovery) must self-heal:
  the turn proceeds to harness dispatch and settles without ever surfacing a
  ``failed`` status to the UI.
* A **persistent** 5xx must still fail the turn — after the bounded retry
  budget — with the structured ``runner_error`` / ``spec_resolver_failed``
  payload, the raw HTTP cause logged for operators but genericized out of the
  client-facing message (the log-and-genericize contract).
"""

from __future__ import annotations

import asyncio
import functools
import io
import logging
import tarfile
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import TracebackType
from typing import Any, cast

import httpx
import pytest
from fastapi import FastAPI

from omnigent.runner import create_runner_app
from omnigent.runner._entry import _resolve_agent_spec_from_server
from omnigent.runtime.harnesses.process_manager import HarnessProcessManager
from tests.runner.helpers import NullServerClient

_CONV = "conv_spec_fetch_5xx"
_AGENT_ID = "ag_spec_fetch_5xx"
# A real harness name: the bundle passes spec validation, while the recording
# process manager below intercepts the dispatch before any real spawn.
_BLIP_HARNESS = "claude-sdk"

# Zeroed backoff schedule so the resolver's retries run instantly in tests.
_NO_DELAYS = (0.0, 0.0, 0.0)


def _bundle_tar_gz(harness: str) -> bytes:
    """Build a one-file agent bundle whose spec selects *harness*.

    :param harness: Harness name for the spec's executor config.
    :returns: A gzipped tarball with a single ``config.yaml``.
    """
    config_bytes = (
        f"spec_version: 1\nname: blip-agent\nexecutor:\n  config:\n    harness: {harness}\n"
    ).encode()
    bundle_buf = io.BytesIO()
    with tarfile.open(fileobj=bundle_buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(name="config.yaml")
        info.size = len(config_bytes)
        tf.addfile(info, io.BytesIO(config_bytes))
    return bundle_buf.getvalue()


class _Error503Response:
    """Stub HTTP response advertising a ``503`` status."""

    status_code = 503

    @staticmethod
    def json() -> dict[str, Any]:
        """Return an empty JSON body (never reached for a 503)."""
        return {}


class _BundleResponse:
    """Stub 200 response carrying a real agent-bundle payload."""

    status_code = 200

    def __init__(self, content: bytes) -> None:
        """Store the bundle bytes and version headers.

        :param content: Gzipped tarball bytes of the bundle.
        """
        self.content = content
        self.headers = {"X-Agent-Version": "1", "X-Agent-Session-Scoped": "true"}


class _TransientAgentContents5xxClient:
    """Server-client stub whose agent-contents fetch 5xxes, then recovers.

    Mirrors a server riding out a restart window: the first *failures* GETs
    return ``503``, every later GET serves a valid bundle. The runner's real
    resolver drives this stub, so the retry behavior under test is the
    production code path.
    """

    def __init__(self, bundle: bytes, *, failures: int) -> None:
        """Store the bundle payload and the size of the failure window.

        :param bundle: Bundle bytes served once the window has passed.
        :param failures: Number of leading GETs that return ``503``.
        """
        self._bundle = bundle
        self.failures = failures
        self.calls = 0

    async def get(self, url: str, **kwargs: Any) -> _Error503Response | _BundleResponse:
        """Serve the transient-failure window, then the bundle.

        :param url: Request URL (ignored).
        :param kwargs: Extra keyword arguments (ignored).
        :returns: A ``503`` inside the window, then a valid ``200`` bundle.
        """
        del url, kwargs
        self.calls += 1
        if self.calls <= self.failures:
            return _Error503Response()
        return _BundleResponse(self._bundle)


class _AgentContentsAlways503Client:
    """Server-client stub whose agent-contents fetch always returns ``503``."""

    async def get(self, url: str, **kwargs: Any) -> _Error503Response:
        """Return a ``503`` for any GET (the agent-contents fetch).

        :param url: Request URL (ignored).
        :param kwargs: Extra keyword arguments (ignored).
        :returns: A stub response reporting HTTP 503.
        """
        del url, kwargs
        return _Error503Response()


@pytest.fixture(autouse=True)
def _stub_harness_cli_preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    """Report every harness CLI as installed for dispatch preflights.

    The dispatched harness's CLI may be absent in CI; without this stub the
    transient-recovery turn would fail at the preflight instead of proving
    the spec-fetch behavior under test.

    :param monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setattr(
        "omnigent.onboarding.harness_install.missing_harness_cli",
        lambda harness: None,
    )


class _FakeHarnessStream:
    """Empty SSE stream so a dispatched background turn completes at once."""

    # HTTP status the runner's stream proxy reads before draining.
    status_code = 200

    async def __aenter__(self) -> _FakeHarnessStream:
        """Enter the stream context.

        :returns: This stream.
        """
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Exit the stream context.

        :param exc_type: Exception type from the context, if any.
        :param exc: Exception value from the context, if any.
        :param tb: Traceback from the context, if any.
        :returns: None.
        """
        del exc_type, exc, tb

    async def aiter_text(self) -> AsyncIterator[str]:
        """Yield nothing: the fake harness turn has no output.

        :returns: An async iterator that terminates immediately.
        """
        return
        yield  # pragma: no cover - makes this an async generator


class _FakeHarnessClient:
    """Harness client stub exposing an empty ``stream``."""

    def stream(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, object],
        timeout: float | None,
    ) -> _FakeHarnessStream:
        """Return an empty fake streaming response.

        :param method: HTTP method, e.g. ``"POST"``.
        :param url: Harness endpoint path.
        :param json: JSON body sent to the harness.
        :param timeout: Request timeout.
        :returns: Fake stream context manager.
        """
        del method, url, json, timeout
        return _FakeHarnessStream()


class _RecordingProcessManager:
    """Process-manager stub recording that harness dispatch was reached.

    :param captured: Dict the resolved harness name is written into under
        the ``"harness"`` key.
    """

    def __init__(self, captured: dict[str, str]) -> None:
        """Store the capture sink.

        :param captured: Dict the harness name is written into.
        """
        self._captured = captured

    async def get_client(
        self,
        conversation_id: str,
        harness_name: str,
        *,
        env: dict[str, str] | None = None,
    ) -> _FakeHarnessClient:
        """Record the harness name and return an empty fake harness client.

        :param conversation_id: Omnigent conversation id.
        :param harness_name: Harness name the runner resolved.
        :param env: Optional spawn environment (ignored).
        :returns: A fake harness client whose turn completes immediately.
        """
        del conversation_id, env
        self._captured["harness"] = harness_name
        return _FakeHarnessClient()

    def mark_in_flight(self, conversation_id: str, response_id: str) -> None:
        """Reaper in-flight marker — no-op for this stub."""
        del conversation_id, response_id

    def clear_in_flight(self, conversation_id: str) -> None:
        """Reaper in-flight clear — no-op for this stub."""
        del conversation_id

    async def release(self, conversation_id: str, **kwargs: object) -> None:
        """Agent-switch subprocess release — no-op for this stub."""
        del conversation_id, kwargs


class _UnusedProcessManager:
    """Process-manager stub asserting the harness is never spawned.

    Used by the persistent-failure case: spec resolution fails *before*
    harness selection, so no harness subprocess should be spawned.
    ``get_client`` raising makes an unexpected spawn a loud failure rather
    than a silent divergence from the path under test.
    """

    async def get_client(
        self,
        conversation_id: str,
        harness_name: str,
        *,
        env: dict[str, str] | None = None,
    ) -> Any:
        """Fail loudly: a harness must not be spawned on a spec-resolve failure.

        :param conversation_id: Omnigent conversation id.
        :param harness_name: Harness name requested by the runner.
        :param env: Optional spawn environment.
        :raises AssertionError: Always — the turn should abort before spawn.
        """
        del conversation_id, harness_name, env
        raise AssertionError("harness spawn attempted despite spec_resolver_failed during setup")

    def mark_in_flight(self, conversation_id: str, response_id: str) -> None:
        """Reaper in-flight marker — no-op for this stub."""
        del conversation_id, response_id

    def clear_in_flight(self, conversation_id: str) -> None:
        """Reaper in-flight clear — no-op for this stub."""
        del conversation_id

    async def release(self, conversation_id: str, **kwargs: object) -> None:
        """Agent-switch subprocess release — no-op for this stub."""
        del conversation_id, kwargs


@asynccontextmanager
async def _runner_test_client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    """Yield an ``httpx.AsyncClient`` wired to the runner ASGI app under test.

    :param app: Runner app under test.
    :returns: Async context manager yielding an ``httpx.AsyncClient``.
    """
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
        yield client


async def _await_bg_turn_task(conv: str, *, timeout: float = 10.0) -> None:
    """Await the fire-and-forget ``turn-{conv}`` background task before draining.

    The ``POST /events`` background path returns 202 before its turn task
    finishes publishing the terminal ``session.status``. A task that already
    finished is absent from ``asyncio.all_tasks()``, so a ``None`` lookup is a
    safe no-op.

    :param conv: Session/conversation id.
    :param timeout: Hard cap in seconds for awaiting the task.
    """
    turn_task = next(
        (t for t in asyncio.all_tasks() if t.get_name() == f"turn-{conv}"),
        None,
    )
    if turn_task is not None:
        await asyncio.wait_for(turn_task, timeout=timeout)


def _drain_status_events(queues: dict[str, Any], conv: str) -> list[dict[str, Any]]:
    """Return every ``session.status`` event currently queued for *conv*.

    Reads the runner's per-session event queue
    (``app.state.session_event_queues``) — the same queue the SSE ``/stream``
    endpoint drains — after the background turn task has been awaited, so the
    terminal status is already present.

    :param queues: The app's per-session event-queue dict.
    :param conv: Session/conversation id.
    :returns: The ordered ``session.status`` event dicts published for *conv*.
    """
    events: list[dict[str, Any]] = []
    queue = queues.get(conv)
    while queue is not None and not queue.empty():
        event = queue.get_nowait()
        if isinstance(event, dict) and event.get("type") == "session.status":
            events.append(event)
    return events


async def _post_bg_turn(http: httpx.AsyncClient, conv: str) -> None:
    """Dispatch a background turn the way the Omnigent server does.

    :param http: Client wired to the runner app under test.
    :param conv: Session/conversation id.
    """
    # No ``?stream=true`` -> the background turn path the Omnigent server
    # uses in production; failures surface on the event stream rather than
    # as a synchronous body.
    response = await http.post(
        f"/v1/sessions/{conv}/events",
        json={
            "type": "message",
            "role": "user",
            "agent_id": _AGENT_ID,
            "model": "x",
            "content": [],
        },
    )
    assert response.status_code == 202
    await _await_bg_turn_task(conv)


@pytest.mark.asyncio
async def test_bg_turn_setup_rides_out_transient_spec_fetch_5xx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient 5xx window on the bundle fetch must not fail the turn.

    The resolver client 503s twice — a restart/proxy blip — then serves a
    valid bundle. The turn must ride the window out: spec resolution
    succeeds, harness dispatch is reached with the bundle's harness, and no
    ``failed`` status is ever surfaced to the UI.

    :param monkeypatch: Used to zero the resolver backoff delays for speed.
        ``raising=False`` keeps the failure mode behavioral on trees where
        the retry schedule does not exist yet.
    :returns: None.
    """
    monkeypatch.setattr(
        "omnigent.runner._entry._SPEC_FETCH_RETRY_DELAYS_S", _NO_DELAYS, raising=False
    )
    resolver_client = _TransientAgentContents5xxClient(_bundle_tar_gz(_BLIP_HARNESS), failures=2)
    captured: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="spec-fetch-blip-cache-") as cache_dir:
        resolver = functools.partial(
            _resolve_agent_spec_from_server,
            cast(httpx.AsyncClient, resolver_client),
            Path(cache_dir),
        )
        app = create_runner_app(
            process_manager=cast(HarnessProcessManager, _RecordingProcessManager(captured)),
            spec_resolver=resolver,
            # A working general server client so turn setup reaches spec
            # resolution rather than failing earlier on an unrelated call.
            server_client=NullServerClient(),  # type: ignore[arg-type]
        )

        async with _runner_test_client(app) as http:
            await _post_bg_turn(http, _CONV)
            events = _drain_status_events(app.state.session_event_queues, _CONV)

    failed = [event for event in events if event.get("status") == "failed"]
    # The corrected behavior: the blip never reaches the UI as a failed turn.
    assert failed == [], f"transient 5xx surfaced as a failed turn: {failed}"
    # The turn genuinely proceeded past spec resolution into harness dispatch.
    assert captured.get("harness") == _BLIP_HARNESS
    # The transient window was actually consumed: the resolver saw the 503s
    # and kept going until the fetch recovered.
    assert resolver_client.calls > resolver_client.failures


@pytest.mark.asyncio
async def test_bg_turn_setup_persistent_spec_fetch_5xx_surfaces_failed_to_ui(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A persistent 5xx on the bundle fetch still fails the turn, structured.

    Drives the real resolver against a server client that 503s every
    agent-contents fetch. Once the bounded retry budget is exhausted the turn
    aborts before any harness is selected and is surfaced to the UI as
    ``failed`` with ``code=runner_error`` /
    ``message=spec_resolver_failed: ...``. The raw cause (the HTTP status)
    is logged for operators but genericized out of the client-facing message.

    :param monkeypatch: Used to zero the resolver backoff delays for speed.
        ``raising=False`` keeps the failure mode behavioral on trees where
        the retry schedule does not exist yet.
    :param caplog: Pytest log capture, used to confirm the canonical
        broken-turn log lines emit and the raw cause is logged, not relayed.
    :returns: None.
    """
    monkeypatch.setattr(
        "omnigent.runner._entry._SPEC_FETCH_RETRY_DELAYS_S", _NO_DELAYS, raising=False
    )
    with tempfile.TemporaryDirectory(prefix="spec-fetch-down-cache-") as cache_dir:
        resolver = functools.partial(
            _resolve_agent_spec_from_server,
            cast(httpx.AsyncClient, _AgentContentsAlways503Client()),
            Path(cache_dir),
        )
        app = create_runner_app(
            process_manager=cast(HarnessProcessManager, _UnusedProcessManager()),
            spec_resolver=resolver,
            server_client=NullServerClient(),  # type: ignore[arg-type]
        )

        async with _runner_test_client(app) as http:
            with caplog.at_level(logging.INFO, logger="omnigent.runner.app"):
                await _post_bg_turn(http, _CONV)
                events = _drain_status_events(app.state.session_event_queues, _CONV)

    failed = [event for event in events if event.get("status") == "failed"]
    assert failed, (
        "no terminal session.status: failed event was published; the turn "
        "either hung or was surfaced with a different status"
    )
    error = failed[0]["error"]
    # The canonical broken-turn signal: setup-phase failures wear runner_error.
    assert error["code"] == "runner_error"
    # The structured failure reason is preserved through to the UI event.
    assert error["message"].startswith("spec_resolver_failed: "), error["message"]
    assert "Request failed on the runner; see the runner log for details:" in (error["message"])
    # Log-and-genericize contract: the raw resolver cause (the HTTP status the
    # server returned) must NOT leak into the client-facing message.
    assert "failed with HTTP 503" not in error["message"]

    # The canonical broken-turn log lines emit on this single failure.
    assert f"turn bg error for {_CONV}" in caplog.text
    assert "spec_resolver_failed" in caplog.text
    assert f"turn surfaced to UI as failed for {_CONV}" in caplog.text
    # harness=None: setup aborted before any harness was selected.
    assert "harness=None" in caplog.text
    # The raw cause is logged for operators even though it is not relayed.
    assert "failed with HTTP 503" in caplog.text
