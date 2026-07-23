"""Tests for wake-post retries and native transcript forwarder lifecycle."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent import (
    claude_native_bridge,
    codex_native_bridge,
)
from omnigent.entities.session_resources import SessionResourceView
from omnigent.runner import app as runner_app_mod
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.helpers import NullServerClient


@dataclass
class _WakePost:
    """
    A single recorded POST made by ``_QueuedResponseServerClient``.

    :param url: The path the wake notice was POSTed to, e.g.
        ``"/v1/sessions/0349c7f62dcaa06b868e9c088c39f062/events"``.
    :param notice: The injected notice text pulled out of the request body.
    """

    url: str
    notice: str


class _QueuedResponseServerClient:
    """
    Omnigent HTTP client stub that returns a fixed queue of real responses.

    A real stub (NOT ``MagicMock``) so that an unexpected attribute access or
    an extra POST beyond the queue fails the test loudly instead of silently
    returning a truthy mock. Each ``post`` pops the next pre-built
    :class:`httpx.Response` (so ``raise_for_status`` runs its real logic —
    a 503 raises, a 200 does not) and records the call for assertions.

    :param responses: Responses to return in order, one per ``post`` call,
        e.g. ``[httpx.Response(503, ...), httpx.Response(200, ...)]``.
    """

    def __init__(self, responses: list[httpx.Response]) -> None:
        """
        Store the response queue and an empty call log.

        :param responses: Responses to return in order, one per POST.
        :returns: None.
        """
        self._responses = list(responses)
        self.calls: list[_WakePost] = []

    async def post(self, url: str, *, json: dict[str, Any], timeout: float) -> httpx.Response:
        """
        Record the POST and return the next queued response.

        :param url: Target path, e.g. ``"/v1/sessions/b460374fc8e697b296708f52dc9d8179/events"``.
        :param json: Wake-notice request body in the ingest message shape.
        :param timeout: Per-request timeout (recorded only, not enforced).
        :returns: The next pre-built response from the queue.
        :raises AssertionError: If more POSTs are made than responses queued.
        """
        notice = json["data"]["content"][0]["text"]
        self.calls.append(_WakePost(url=url, notice=notice))
        assert self._responses, (
            f"Wake POST made {len(self.calls)} call(s) but only "
            f"{len(self.calls) - 1} response(s) were queued — the retry "
            f"loop exceeded its bound."
        )
        return self._responses.pop(0)


def _wake_response(status_code: int, parent_id: str) -> httpx.Response:
    """
    Build a real ``httpx.Response`` for a wake POST to ``parent_id``.

    A request is attached so ``raise_for_status`` can construct a proper
    ``HTTPStatusError`` on non-2xx, matching what httpx does in production.

    :param status_code: HTTP status to simulate, e.g. ``503``.
    :param parent_id: Parent session id used to build the request URL.
    :returns: A response carrying a representative JSON body.
    """
    request = httpx.Request("POST", f"http://test/v1/sessions/{parent_id}/events")
    body = (
        {"error": {"code": "RUNNER_UNAVAILABLE", "message": "runner reconnecting"}}
        if status_code >= 400
        else {"id": "evt_1", "object": "session.event"}
    )
    return httpx.Response(status_code, request=request, json=body)


async def test_wake_post_retries_transient_503_then_succeeds(
    _no_wake_backoff: list[float],
) -> None:
    """
    A transient 503 wake response is retried and the next 200 succeeds.

    Guards the core bug: Omnigent returns a genuine 503 ``RUNNER_UNAVAILABLE``
    *response* (not a transport exception) while the parent's runner tunnel
    reconnects. The wake POST must treat that as a failure and retry, not
    accept it as delivered.
    """
    parent_id = "dd17997e050fc080efac96bc9ec22b55"
    client = _QueuedResponseServerClient(
        [_wake_response(503, parent_id), _wake_response(200, parent_id)]
    )

    delivered = await runner_app_mod._deliver_subagent_wake_post(
        client,  # type: ignore[arg-type]
        parent_id,
        "[System: worker completed]",
    )

    # Returns True only because the retry re-POSTed after the 503 and got a
    # 200. If the status check were missing, the first 503 would be treated
    # as success and there would be exactly one call with delivered already
    # True — so both the count and the value below pin the fix.
    assert delivered is True
    # Exactly two POSTs: the 503 attempt + the 200 retry. One call would mean
    # the 503 was silently accepted; three would mean it retried past success.
    assert len(client.calls) == 2, (
        f"Expected 2 wake POSTs (503 then 200 retry), got {len(client.calls)}."
    )
    # Both POSTs targeted the parent's events endpoint with the same notice.
    assert client.calls[0].url == f"/v1/sessions/{parent_id}/events"
    assert client.calls[1].notice == "[System: worker completed]"
    # Exactly one backoff slept (between the two attempts) — proves the retry
    # path ran rather than the call being retried zero or two+ times.
    assert len(_no_wake_backoff) == 1, (
        f"Expected one backoff before the single retry, got {_no_wake_backoff}."
    )


async def test_wake_post_persistent_503_returns_failure(
    _no_wake_backoff: list[float],
) -> None:
    """
    A 503 on every attempt exhausts the retry budget and reports failure.

    This is the regression guard for the silent-strand bug: a 503 must be
    surfaced as a delivery failure (so the caller releases the debounce flag
    and logs), never swallowed as a success.
    """
    parent_id = "a25887ef53cb74bba721c20edf204d10"
    client = _QueuedResponseServerClient(
        [_wake_response(503, parent_id) for _ in range(runner_app_mod._WAKE_POST_MAX_ATTEMPTS)]
    )

    delivered = await runner_app_mod._deliver_subagent_wake_post(
        client,  # type: ignore[arg-type]
        parent_id,
        "[System: worker completed]",
    )

    # False = the non-2xx response was treated as a failure. Before the fix
    # this returned (implicitly) success and the wake was considered delivered.
    assert delivered is False
    # Attempted exactly the bounded budget — not once (no retry) and not
    # unbounded. The stub would have asserted on a call past the queue.
    assert len(client.calls) == runner_app_mod._WAKE_POST_MAX_ATTEMPTS, (
        f"Expected {runner_app_mod._WAKE_POST_MAX_ATTEMPTS} attempts on persistent 503, "
        f"got {len(client.calls)}."
    )
    # One backoff fewer than attempts: we don't sleep after the final attempt.
    assert len(_no_wake_backoff) == runner_app_mod._WAKE_POST_MAX_ATTEMPTS - 1, (
        f"Expected {runner_app_mod._WAKE_POST_MAX_ATTEMPTS - 1} backoffs between "
        f"{runner_app_mod._WAKE_POST_MAX_ATTEMPTS} attempts, got {_no_wake_backoff}."
    )


async def test_wake_post_permanent_4xx_not_retried(
    _no_wake_backoff: list[float],
) -> None:
    """
    A permanent 4xx wake rejection fails immediately without retrying.

    A 400 is a client-side rejection that retrying cannot fix, so the loop
    must give up after one attempt rather than burn the whole budget.
    """
    parent_id = "43cc3eccd350fed1b91854b2adf5ec3e"
    client = _QueuedResponseServerClient([_wake_response(400, parent_id)])

    delivered = await runner_app_mod._deliver_subagent_wake_post(
        client,  # type: ignore[arg-type]
        parent_id,
        "[System: worker completed]",
    )

    # Permanent rejection => failure, no delivery.
    assert delivered is False
    # Exactly one attempt: a permanent 4xx is not retried. Two+ would mean the
    # classifier wrongly treated 400 as transient.
    assert len(client.calls) == 1, (
        f"Expected a single attempt on permanent 400, got {len(client.calls)}."
    )
    # No backoff at all — the loop exited before any sleep.
    assert _no_wake_backoff == []


@pytest.mark.parametrize(
    "status_code,expected_retryable",
    [
        (503, True),  # RUNNER_UNAVAILABLE — the routine reconnect case
        (500, True),  # generic server error
        (429, True),  # rate limit — explicitly transient
        (409, True),  # conflict — explicitly transient
        (400, False),  # bad request — permanent
        (404, False),  # not found — permanent
    ],
)
def test_wake_post_is_retryable_status_classification(
    status_code: int, expected_retryable: bool
) -> None:
    """
    The status classifier retries 5xx + transient 4xx, not permanent 4xx.

    :param status_code: Simulated HTTP status on the wake response.
    :param expected_retryable: Whether that status should be retried.
    """
    request = httpx.Request("POST", "http://test/v1/sessions/p/events")
    exc = httpx.HTTPStatusError(
        "wake rejected",
        request=request,
        response=httpx.Response(status_code, request=request),
    )
    # Pins which statuses cost a retry vs. fail fast; a wrong verdict here
    # would either waste the budget on permanent errors or give up on a 503.
    assert runner_app_mod._wake_post_is_retryable(exc) is expected_retryable


def test_wake_post_transport_error_is_retryable() -> None:
    """
    A transport-level error (no response) is always retryable.

    A ``ConnectError`` carries no HTTP response — the POST may never have
    reached Omnigent — so the wake should be retried.
    """
    request = httpx.Request("POST", "http://test/v1/sessions/p/events")
    exc = httpx.ConnectError("connection refused", request=request)
    # True because a transport failure is not a definitive server rejection.
    assert runner_app_mod._wake_post_is_retryable(exc) is True


@dataclass
class _ForwarderRun:
    """
    One spawned transcript-forwarder stub run.

    :param task: The asyncio task executing this run, captured via
        ``asyncio.current_task()`` when the stub body starts. Used for
        registry-independent cleanup.
    :param cancelled: ``True`` once the parked run observed
        :class:`asyncio.CancelledError`.
    """

    task: asyncio.Task[Any] | None = None
    cancelled: bool = False


async def _drain_forwarder_runs(runs: list[_ForwarderRun]) -> None:
    """
    Cancel and await any still-parked forwarder stub runs.

    Test cleanup helper so a failed assertion never leaks a parked task
    (or a registry entry) into the next test.

    :param runs: Stub runs recorded by a parking forwarder fake.
    :returns: None.
    """
    leftovers = [run.task for run in runs if run.task is not None and not run.task.done()]
    for task in leftovers:
        task.cancel()
    if leftovers:
        await asyncio.wait(leftovers)


@pytest.mark.asyncio
async def test_cancel_auto_forwarder_task_cancels_and_awaits_registered_task() -> None:
    """
    Cancelling a session's registered forwarder awaits its completion.

    This is the ordering guarantee the claude re-create path relies on:
    ``_cancel_auto_forwarder_task`` must not return while the old task can
    still post items (it runs right before the bridge's forward-cursor
    state is wiped).
    """
    session_id = "f98115a89870f7e364064c9d06c52ee7"
    run = _ForwarderRun()

    async def _parked() -> None:
        """Park forever like the restart-forever supervisor."""
        run.task = asyncio.current_task()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            run.cancelled = True
            raise

    try:
        task = asyncio.create_task(_parked())
        runner_app_mod._register_auto_forwarder_task(session_id, task)
        # Yield so the coroutine body starts (a never-started task would be
        # dropped without ever entering the except branch).
        await asyncio.sleep(0)
        assert not task.done()

        await runner_app_mod._cancel_auto_forwarder_task(session_id)

        # cancelled() is only True for a FINISHED cancelled task, proving
        # the helper awaited completion rather than fire-and-forgetting.
        assert task.cancelled(), (
            "Registered forwarder task must be finished-cancelled after "
            "_cancel_auto_forwarder_task returns; a live task here means the "
            "helper did not await the cancellation."
        )
        # The coroutine body observed the cancel — the parked await was
        # actually interrupted, not skipped.
        assert run.cancelled is True
        # The slot is freed for the successor registration.
        assert session_id not in runner_app_mod._AUTO_FORWARDER_TASKS
        # Idempotent: a second cancel with no registered task is a no-op.
        await runner_app_mod._cancel_auto_forwarder_task(session_id)
    finally:
        runner_app_mod._AUTO_FORWARDER_TASKS.pop(session_id, None)
        await _drain_forwarder_runs([run])


@pytest.mark.asyncio
async def test_register_auto_forwarder_task_replaces_incumbent_and_survives_stale_evict() -> None:
    """
    Re-registration cancels the incumbent; its done-callback can't evict the successor.

    Two claims:

    1. Registering task B for a session that already holds live task A
       cancels A (no session ever runs two forwarders).
    2. A's done-callback fires AFTER B occupies the slot; the eviction is
       identity-checked, so the stale callback must leave B registered.
       Without the identity check, B would lose its strong reference and
       the registry would report no forwarder for a session that has one.
    """
    session_id = "f14ef86c47cc555be7e4c5eb00e88a9a"
    run_a = _ForwarderRun()
    run_b = _ForwarderRun()

    async def _parked(run: _ForwarderRun) -> None:
        """Park forever; record cancellation on the given run."""
        run.task = asyncio.current_task()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            run.cancelled = True
            raise

    try:
        task_a = asyncio.create_task(_parked(run_a))
        runner_app_mod._register_auto_forwarder_task(session_id, task_a)
        await asyncio.sleep(0)

        task_b = asyncio.create_task(_parked(run_b))
        runner_app_mod._register_auto_forwarder_task(session_id, task_b)

        # Claim 1: the incumbent was cancelled by the replacement.
        await asyncio.wait({task_a})
        assert run_a.cancelled is True, (
            "Registering a successor must cancel the live incumbent; a "
            "surviving incumbent is exactly the double-mirror bug."
        )
        # Let task_a's done-callback (the stale evict) run.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # Claim 2: the stale callback did not evict the successor.
        assert runner_app_mod._AUTO_FORWARDER_TASKS.get(session_id) is task_b, (
            "Task A's done-callback evicted task B — eviction must be "
            "identity-checked so a predecessor's completion cannot drop the "
            "live successor's registration."
        )
        # The successor must still be running — done here means A's cancel hit B.
        assert not task_b.done()
    finally:
        runner_app_mod._AUTO_FORWARDER_TASKS.pop(session_id, None)
        await _drain_forwarder_runs([run_a, run_b])


@pytest.mark.asyncio
async def test_auto_forwarder_registry_isolates_sessions_and_evicts_completed() -> None:
    """
    Per-session keying: cancelling one session leaves another's forwarder running.

    Also pins the natural-completion eviction: a registered task that
    finishes on its own removes its entry (the dict must not leak entries
    the way the old set relied on ``discard`` for).
    """
    run_a = _ForwarderRun()
    run_b = _ForwarderRun()

    async def _parked(run: _ForwarderRun) -> None:
        """Park forever; record cancellation on the given run."""
        run.task = asyncio.current_task()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            run.cancelled = True
            raise

    try:
        task_a = asyncio.create_task(_parked(run_a))
        task_b = asyncio.create_task(_parked(run_b))
        runner_app_mod._register_auto_forwarder_task("4263b99f5e92593cafda836bdb6b7690", task_a)
        runner_app_mod._register_auto_forwarder_task("414de30f273a0a21428e869a1d7a2a3d", task_b)
        await asyncio.sleep(0)

        await runner_app_mod._cancel_auto_forwarder_task("4263b99f5e92593cafda836bdb6b7690")

        assert run_a.cancelled is True
        # Session B's forwarder is untouched by session A's cancel — keying
        # by session id must not regress to whole-registry cancellation.
        assert run_b.cancelled is False
        assert not task_b.done()
        assert (
            runner_app_mod._AUTO_FORWARDER_TASKS.get("414de30f273a0a21428e869a1d7a2a3d") is task_b
        )

        # Natural completion evicts the entry (no leak for finished tasks).
        task_b.cancel()
        await asyncio.wait({task_b})
        await asyncio.sleep(0)
        assert "414de30f273a0a21428e869a1d7a2a3d" not in runner_app_mod._AUTO_FORWARDER_TASKS
    finally:
        runner_app_mod._AUTO_FORWARDER_TASKS.pop("4263b99f5e92593cafda836bdb6b7690", None)
        runner_app_mod._AUTO_FORWARDER_TASKS.pop("414de30f273a0a21428e869a1d7a2a3d", None)
        await _drain_forwarder_runs([run_a, run_b])


@pytest.mark.asyncio
async def test_auto_create_claude_terminal_recreate_cancels_prior_forwarder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Re-running claude terminal auto-create leaves exactly one live forwarder.

    Regression for the recovery path: ``create_session_terminal``'s
    ensure branch re-runs ``_auto_create_claude_terminal`` after a bridge
    closure, but the prior ``supervise_forwarder`` task is restart-forever
    and survives pane death (it re-resolves the transcript path each loop).
    Before the per-session registry, the second create wiped the shared
    forward cursor and spawned a second forwarder, so both tasks mirrored
    every post-recovery transcript record into the session — each item
    persisted twice (the server has no external-item dedup).

    :param tmp_path: Pytest-provided temporary directory.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "root")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8000")

    session_id = "f3e241b72bac7d5e33d7a9819e0fa865"
    runs: list[_ForwarderRun] = []

    async def _parking_forwarder(**kwargs: Any) -> None:
        """Park forever like the real restart-forever supervisor."""
        del kwargs
        run = _ForwarderRun(task=asyncio.current_task())
        runs.append(run)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            run.cancelled = True
            raise

    monkeypatch.setattr(
        "omnigent.claude_native_forwarder.supervise_forwarder",
        _parking_forwarder,
    )

    class _FakeResourceRegistry:
        """Captures terminal launches; no live terminal registry."""

        terminal_registry = None

        async def launch_required_terminal(
            self,
            *,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            """Return a terminal resource view without launching tmux."""
            del terminal_name, session_key, spec, resource_role
            return SessionResourceView(
                id="terminal_claude_main",
                type="terminal",
                session_id=session_id,
                name="claude:main",
                metadata={"terminal_name": "claude", "session_key": "main", "running": True},
            )

    try:
        await runner_app_mod._auto_create_claude_terminal(
            session_id,
            _FakeResourceRegistry(),  # type: ignore[arg-type]
            lambda _sid, _evt: None,
            server_client=NullServerClient(),  # type: ignore[arg-type]
        )
        # Let forwarder A start and park — in production the recovery
        # re-create fires long after the original create's task is running.
        await asyncio.sleep(0)

        # The recovery path: terminal resource gone, ensure re-creates.
        await runner_app_mod._auto_create_claude_terminal(
            session_id,
            _FakeResourceRegistry(),  # type: ignore[arg-type]
            lambda _sid, _evt: None,
            server_client=NullServerClient(),  # type: ignore[arg-type]
        )
        await asyncio.sleep(0)

        # Both creates spawned a forwarder; the recovery one is the survivor.
        assert len(runs) == 2, (
            f"Expected 2 forwarder spawns (one per auto-create), got {len(runs)}."
        )
        # The first forwarder was cancelled by the re-create — a False here
        # is the production bug: two live tasks double-posting every record.
        assert runs[0].cancelled is True, (
            "Re-creating the claude terminal must cancel the prior session "
            "forwarder; it survived, so every post-recovery transcript "
            "record would be mirrored twice."
        )
        # The recovery's own forwarder survives — cancelled here means the
        # re-create killed its replacement and the session mirrors nothing.
        assert runs[1].cancelled is False
        live_runs = [run for run in runs if not run.cancelled]
        # Exactly one live forwarder mirrors the transcript for the session.
        assert len(live_runs) == 1
        # The registry holds exactly the live task for this session, keyed
        # by session id — this is the strong reference that keeps it alive.
        registered = runner_app_mod._AUTO_FORWARDER_TASKS.get(session_id)
        assert registered is live_runs[0].task
        # Still running: a done survivor would leave the session unmirrored.
        assert not registered.done()
    finally:
        runner_app_mod._AUTO_FORWARDER_TASKS.pop(session_id, None)
        await _drain_forwarder_runs(runs)


@pytest.mark.asyncio
async def test_auto_create_codex_terminal_recreate_cancels_prior_forwarder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Re-running codex terminal auto-create leaves exactly one live forwarder.

    Codex flavor of the claude double-mirror regression: the codex spawn
    registered its forwarder task in the same unkeyed set, so an ensure
    re-create for an existing session leaked the prior known-thread
    forwarder alongside the new one.

    :param tmp_path: Temporary directory for isolated bridge state.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    import omnigent.codex_native_app_server as codex_app_mod

    session_id = "a3f4361a350851cfb9eb3db2bf2b0380"
    thread_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    monkeypatch.setattr(codex_native_bridge, "_BRIDGE_ROOT", tmp_path / "codex-bridge")
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(tmp_path / "workspace"))
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://ap.example")
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    monkeypatch.setattr("omnigent.runner._entry._make_auth_token_factory", lambda: None)

    class _SnapshotServerClient:
        """Server client returning a persisted resume thread + one item."""

        async def get(self, url: str, **kwargs: Any) -> httpx.Response:
            """
            Return the session snapshot / items consumed by the helper.

            :param url: Request path, e.g. ``"/v1/sessions/conv_..."``.
            :param kwargs: Request keyword arguments (ignored).
            :returns: HTTP 200 response carrying launch config or items.
            """
            del kwargs
            if url == f"/v1/sessions/{session_id}/items":
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "id": "b1649a5cbfec3f92bec12275c14f4b5f",
                                "response_id": "codex_turn_1",
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": "remember this"}],
                            }
                        ],
                        "has_more": False,
                    },
                    request=httpx.Request("GET", url),
                )
            return httpx.Response(
                200,
                json={"external_session_id": thread_id},
                request=httpx.Request("GET", url),
            )

    class _FakeCodexAppServer:
        """Minimal app-server object used by ``codex_terminal_env``."""

        codex_path = "/opt/codex/bin/codex"

        def __init__(self) -> None:
            """:returns: None."""
            self.env = {"OPENAI_API_KEY": "sk-test"}
            self.codex_home = tmp_path / "unconfigured-codex-home"
            self.listen_url: str | None = None
            self.config_overrides: list[str] = []

        async def start(self) -> None:
            """:returns: None."""

        async def close(self) -> None:
            """:returns: None."""

    def _fake_build_codex_native_server(**kwargs: Any) -> _FakeCodexAppServer:
        """
        Build a fresh fake app-server per create call.

        :param kwargs: Keyword arguments passed by the runner helper.
        :returns: Fake app-server bound to the requested CODEX_HOME.
        """
        app_server = _FakeCodexAppServer()
        app_server.codex_home = kwargs["codex_home"]
        return app_server

    class _UnexpectedDiscoveryClient:
        """App-server client that must not connect on a known-thread resume."""

        def __init__(self, *, ws_url: str, client_name: str) -> None:
            """
            :param ws_url: App-server WebSocket URL.
            :param client_name: JSON-RPC client name.
            """
            self.ws_url = ws_url
            self.client_name = client_name

        async def connect(self) -> None:
            """Fail if the resume path tries to discover a fresh thread."""
            raise AssertionError("resume path must not connect discovery client")

        async def close(self) -> None:
            """:returns: None."""

    async def _fake_preload_thread(transport: str, loaded_thread_id: str) -> None:
        """
        No-op thread preload.

        :param transport: App-server transport URL.
        :param loaded_thread_id: Thread id passed to ``thread/resume``.
        :returns: None.
        """
        del transport, loaded_thread_id

    runs: list[_ForwarderRun] = []

    async def _parking_forward_known_thread(**kwargs: Any) -> None:
        """Park forever in place of the known-thread forwarder."""
        del kwargs
        run = _ForwarderRun(task=asyncio.current_task())
        runs.append(run)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            run.cancelled = True
            raise

    class _FakeResourceRegistry:
        """Returns a terminal resource view without launching tmux."""

        async def launch_auxiliary_terminal(
            self,
            *,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            """Return a terminal resource view for the codex TUI."""
            del terminal_name, session_key, spec, resource_role
            return SessionResourceView(
                id="terminal_codex_main",
                type="terminal",
                session_id=session_id,
                name="Codex",
            )

    monkeypatch.setattr(
        codex_app_mod,
        "build_codex_native_server",
        _fake_build_codex_native_server,
    )
    monkeypatch.setattr(codex_app_mod, "CodexAppServerClient", _UnexpectedDiscoveryClient)
    monkeypatch.setattr(codex_app_mod, "preload_codex_thread_for_resume", _fake_preload_thread)
    monkeypatch.setattr(
        runner_app_mod,
        "_codex_forward_known_thread",
        _parking_forward_known_thread,
    )

    agent_spec = AgentSpec(
        spec_version=1,
        name="codex",
        executor=ExecutorSpec(
            type="omnigent",
            config={"harness": "codex-native", "model": "gpt-5-default"},
        ),
    )

    try:
        await runner_app_mod._auto_create_codex_terminal(
            session_id,
            _FakeResourceRegistry(),  # type: ignore[arg-type]
            lambda _sid, _evt: None,
            agent_spec=agent_spec,
            server_client=_SnapshotServerClient(),  # type: ignore[arg-type]
        )
        await asyncio.sleep(0)

        await runner_app_mod._auto_create_codex_terminal(
            session_id,
            _FakeResourceRegistry(),  # type: ignore[arg-type]
            lambda _sid, _evt: None,
            agent_spec=agent_spec,
            server_client=_SnapshotServerClient(),  # type: ignore[arg-type]
        )
        await asyncio.sleep(0)

        assert len(runs) == 2, (
            f"Expected 2 forwarder spawns (one per auto-create), got {len(runs)}."
        )
        # The first forwarder was cancelled by the re-create — a False here
        # means two live tasks mirror the same codex thread into the session.
        assert runs[0].cancelled is True, (
            "Re-creating the codex terminal must cancel the prior session "
            "forwarder; it survived, so transcript records would be "
            "double-posted."
        )
        # The recovery's own forwarder survives — cancelled here means the
        # re-create killed its replacement and the session mirrors nothing.
        assert runs[1].cancelled is False
        live_runs = [run for run in runs if not run.cancelled]
        # Exactly one live forwarder mirrors the thread for the session.
        assert len(live_runs) == 1
        registered = runner_app_mod._AUTO_FORWARDER_TASKS.get(session_id)
        assert registered is live_runs[0].task
        # Still running: a done survivor would leave the session unmirrored.
        assert not registered.done()
    finally:
        runner_app_mod._AUTO_FORWARDER_TASKS.pop(session_id, None)
        runner_app_mod._AUTO_CODEX_APP_SERVERS.pop(session_id, None)
        await _drain_forwarder_runs(runs)
