"""Regression: sandboxed claude-sdk sessions must not hang on
"Starting up…" forever when REPL-terminal auto-create stalls.

User journey (web surface)
--------------------------
1. Configure a runner-hosted, sandboxed (``os_env.sandbox: linux_bwrap``)
   claude-sdk agent.
2. Open the app and create a new top-level session for that agent — the web
   client POSTs ``/v1/sessions`` to the runner to initialize it.
3. Observe: the session view is stuck on the "Starting up…" spinner forever;
   the session never becomes usable.

Root cause (reproduced here)
----------------------------
During session init the runner auto-creates the omnigent REPL terminal for
non-native harnesses. It publishes ``terminal_pending=True``, ``await``s
``_auto_create_repl_terminal(...)`` *with no timeout*, and only publishes
``terminal_pending=False`` in the ``finally``. If that auto-create call never
completes (e.g. the sandboxed launch stalls), the unbounded ``await`` never
returns, the ``finally`` never runs, ``terminal_pending`` stays ``True``
forever, and the ``POST /v1/sessions`` init never completes — so the web UI
shows "Starting up…" indefinitely. See the REPL auto-create block in
``omnigent/runner/app.py``; ``terminalPending`` gates the spinner in the web SPA.

Why this is reproduced at the runner-route level
------------------------------------------------
The user-visible surface is ``web`` (the stuck spinner), but the web/subprocess
harness cannot reach the hung state on CI: the kernel forbids unprivileged user
namespaces, so bwrap fails fast (auto-create *errors*, and the existing
``finally`` clears the flag) instead of hanging, and ``tmux_start_on_attach``
defers the sandboxed command. So this test faithfully injects the report's exact
trigger — "that call never completes" — by making ``_auto_create_repl_terminal``
hang, and drives the *real* ``POST /v1/sessions`` init route the web client uses.

Fail→pass contract
-------------------
* Buggy build: the POST never returns (init blocked on the unbounded await) and
  ``terminal_pending`` is stuck ``True`` → assertions fail.
* Fixed build (bound the auto-create ``await`` with ``asyncio.wait_for``): the
  hang is aborted, the ``finally`` publishes ``terminal_pending=False``, and the
  POST returns → assertions pass.

The fix must bound REPL auto-create well under ``_INIT_BOUND_S`` below. If the
fix reads ``OMNIGENT_REPL_TERMINAL_AUTOCREATE_TIMEOUT_S`` (mirroring the
``OMNIGENT_SUBAGENT_LAUNCH_TIMEOUT_S`` pattern already in the same module) this
test's fixed path also completes near-instantly.
"""

from __future__ import annotations

import asyncio

import pytest

import omnigent.runner.app as runner_app
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.runner import create_runner_app
from omnigent.spec.types import AgentSpec, ExecutorSpec
from omnigent.terminals import TerminalRegistry
from tests.runner.conftest import _FakeProcessManager, _runner_client, _ScriptedHarnessClient
from tests.runner.helpers import NullServerClient

# Outer bound on the whole init POST. Must exceed the fix's internal auto-create
# timeout (so a *fixed* build passes) yet stay finite (so the *buggy* build fails
# with a clean assertion instead of hanging). ~30s (_CLAUDE_PANE_READY_TIMEOUT_S)
# is the analogous terminal-readiness budget, so 60s leaves comfortable margin.
_INIT_BOUND_S = 60.0


@pytest.mark.asyncio
@pytest.mark.timeout(150)
async def test_repl_autocreate_hang_does_not_wedge_session_init(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Hermetic: satisfy the ambient runner config's model-gateway credential
    # reference (present only when a config home declaring it is loaded); a dummy
    # value is fine because the harness is a fake process manager.
    monkeypatch.setenv("DATABRICKS_REPRO_GATEWAY_TOKEN", "test-dummy-token")
    monkeypatch.setenv("OMNIGENT_DATABRICKS_REPRO_GATEWAY_TOKEN", "test-dummy-token")
    # Advisory: a fix that makes the timeout env-configurable will complete the
    # fixed path near-instantly. Ignored (harmless) if the fix hardcodes.
    monkeypatch.setenv("OMNIGENT_REPL_TERMINAL_AUTOCREATE_TIMEOUT_S", "2")

    # Record every terminal_pending publish so we can assert the flag is cleared.
    events: list[tuple[str, bool]] = []
    real_publish = runner_app._publish_terminal_pending

    def _recording_publish(publish_event, session_id, pending):  # type: ignore[no-untyped-def]
        events.append((session_id, pending))
        return real_publish(publish_event, session_id, pending)

    monkeypatch.setattr(runner_app, "_publish_terminal_pending", _recording_publish)

    # Inject the report's exact trigger: REPL auto-create "never completes".
    never = asyncio.Event()

    async def _hanging_auto_create(session_id, resource_registry, publish_event, **kwargs):  # type: ignore[no-untyped-def]
        del session_id, resource_registry, publish_event, kwargs
        await never.wait()

    monkeypatch.setattr(runner_app, "_auto_create_repl_terminal", _hanging_auto_create)

    # Runner-hosted, sandboxed, non-native (claude-sdk), top-level session — the
    # exact shape that takes the REPL auto-create branch during init.
    spec = AgentSpec(
        spec_version=1,
        name="repl-sdk",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=".",
            sandbox=OSEnvSandboxSpec(type="linux_bwrap"),
        ),
    )

    async def _resolver(agent_id, session_id=None):  # type: ignore[no-untyped-def]
        del agent_id, session_id
        return spec

    app = create_runner_app(
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
        terminal_registry=TerminalRegistry(),
    )

    payload = {
        "session_id": "2d1b1a96e3e08f2cd43c0cc4b695ac5d",
        "agent_id": "880b5afda28ad55ff74cbeb9b5fc67fb",
        "host_id": "a8fd87d1ec915a4d95a0cab76f9dc4bb",
    }

    posted = False
    async with _runner_client(app) as client:
        try:
            try:
                await asyncio.wait_for(
                    client.post("/v1/sessions", json=payload), timeout=_INIT_BOUND_S
                )
                posted = True
            except asyncio.TimeoutError:
                posted = False
            # Snapshot BEFORE unblocking the parked task, so a fixed build's
            # cleanup can't retroactively add the cleared flag we check for.
            pendings = [pending for (_sid, pending) in events]
        finally:
            # Let any parked auto-create unwind so the ASGI app tears down cleanly.
            never.set()

    saw_pending_true = True in pendings
    saw_pending_false = False in pendings

    # Sanity: the session actually took the REPL auto-create branch and published
    # terminal_pending=True (otherwise the test isn't exercising the bug).
    assert saw_pending_true, (
        "expected the session to enter REPL auto-create and publish "
        f"terminal_pending=True; got {events!r}"
    )

    # The bug: with auto-create hung, the unbounded await wedges init so the POST
    # never returns and the web UI is stuck on 'Starting up…' forever. A fixed
    # build bounds the await and returns.
    assert posted, (
        f"POST /v1/sessions never returned within {_INIT_BOUND_S}s — session init "
        "is wedged on the unbounded _auto_create_repl_terminal await "
        "(web UI stuck on 'Starting up…')"
    )

    # The invariant the fix restores: terminal_pending is always cleared, even
    # when auto-create stalls (the finally must run).
    assert saw_pending_false, (
        "terminal_pending was published True but never cleared to False — the "
        f"'finally' that clears it never ran; got {events!r}"
    )
