"""Tests for the FastAPI app lifespan hook.

Exercises the ``_lifespan`` context manager in
``omnigent.server.app`` to verify shutdown wiring for the
:class:`TerminalRegistry`. Per ``designs/OMNIGENT_TERMINAL_BRIDGE.md``
§4.4, every live tmux session must be closed when the server's
lifespan exits.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI

pytestmark = pytest.mark.asyncio


async def test_lifespan_shutdown_invokes_registry_shutdown(
    app: FastAPI,
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lifespan exit awaits ``registry.shutdown()``.

    Spies on the registry's ``shutdown`` method and asserts it
    was called exactly once during the lifespan exit. Catches
    the failure mode where the shutdown hook regresses to
    ``pass`` or skips the registry — every long-lived server
    would leak tmux subprocesses on restart.

    What breaks if this fails: deploy hosts accumulate orphan
    tmux sockets across restarts. Each restart adds another
    leaked socket directory. After enough restarts, /tmp fills
    up. We catch this here (in CI, in seconds) instead of in
    production after weeks of restarts.

    Doesn't actually launch any terminals — that requires a
    real tmux subprocess + real spec, which is overkill for
    verifying the *call*. The terminal-side cleanup behavior
    itself is covered by ``tests/terminals/test_registry.py``.
    """
    from omnigent.runtime import get_terminal_registry

    registry = get_terminal_registry()
    real_shutdown = registry.shutdown

    shutdown_calls = 0

    async def spy_shutdown() -> None:
        nonlocal shutdown_calls
        shutdown_calls += 1
        await real_shutdown()

    monkeypatch.setattr(registry, "shutdown", spy_shutdown)

    async with app.router.lifespan_context(app):
        # Inside the lifespan: shutdown shouldn't have run yet.
        assert shutdown_calls == 0

    # After lifespan exit: shutdown was called exactly once.
    # If 0, the lifespan dropped the call (regression).
    # If >1, something is double-invoking the hook (also wrong).
    assert shutdown_calls == 1, (
        f"Expected registry.shutdown() to be called exactly once on "
        f"lifespan exit, got {shutdown_calls}. If 0, the shutdown "
        f"hook is missing — every server restart will leak any tmux "
        f"sessions registered during the previous lifetime."
    )


async def test_lifespan_starts_periodic_metrics_otel_publisher(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The lifespan starts periodic OTEL publication for server metrics.

    If this wiring regresses, request/resource gauges stop exporting
    even though per-request duration histograms still work.
    """
    from omnigent.server import app as server_app
    from omnigent.server.performance_metrics import (
        ServerMetricsOtelPublisher,
        ServerPerformanceMetrics,
    )

    publisher_started = asyncio.Event()

    async def fake_publisher(
        metrics: ServerPerformanceMetrics,
        *,
        otel_publisher: ServerMetricsOtelPublisher,
        interval_seconds: float = 10.0,
    ) -> None:
        """
        Capture lifespan publisher arguments and wait for cancellation.

        :param metrics: Metrics tracker owned by the app lifespan.
        :param otel_publisher: OTEL publisher supplied by the app
            lifespan.
        :param interval_seconds: Publisher interval in seconds, e.g.
            ``10.0``.
        """
        assert isinstance(metrics, ServerPerformanceMetrics)
        assert isinstance(otel_publisher, ServerMetricsOtelPublisher)
        assert interval_seconds == 10.0
        publisher_started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        server_app,
        "publish_server_metrics_periodically",
        fake_publisher,
    )

    async with app.router.lifespan_context(app):
        await asyncio.wait_for(publisher_started.wait(), timeout=1.0)


async def test_lifespan_threads_tunnel_registry_to_subagent_block_notifier(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The real lifespan closure must pass its ``TunnelRegistry`` to
    ``configure_subagent_block_notifier``, not silently drop it.

    Regression coverage for the outer wiring, not just the inner function:
    ``tests/server/routes/test_subagent_block_wake.py`` already proves
    ``configure_subagent_block_notifier`` itself threads a
    ``tunnel_registry`` argument through to the dispatch call when given
    one — but that test calls the function directly, so it cannot catch
    the lifespan (``omnigent/server/app.py``) failing to pass its own
    ``tunnel_registry`` at the call site. This drives the REAL lifespan
    context manager (the same one the ASGI server runs on startup) and
    asserts the object it hands to ``configure_subagent_block_notifier``
    is the exact same ``TunnelRegistry`` instance stamped onto
    ``app.state.tunnel_registry`` — proving the wiring at app.py's actual
    call site, not a stand-in.
    """
    from omnigent.server.routes import sessions as sessions_module

    captured: dict[str, object] = {}
    real_configure = sessions_module.configure_subagent_block_notifier

    def _spy_configure(*args: object, **kwargs: object) -> object:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return real_configure(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(sessions_module, "configure_subagent_block_notifier", _spy_configure)

    async with app.router.lifespan_context(app):
        pass

    assert "args" in captured, (
        "configure_subagent_block_notifier was never called during lifespan "
        "startup — the sub-agent block-wake notifier wiring regressed."
    )
    # The real call site passes tunnel_registry positionally as the 3rd
    # argument (conversation_store, runner_router, tunnel_registry); accept
    # either positional or keyword so this doesn't false-fail on a
    # cosmetic refactor of the call style.
    call_args = captured["args"]
    call_kwargs = captured["kwargs"]
    assert isinstance(call_args, tuple)
    assert isinstance(call_kwargs, dict)
    passed_tunnel_registry = (
        call_args[2] if len(call_args) >= 3 else call_kwargs.get("tunnel_registry")
    )
    assert passed_tunnel_registry is app.state.tunnel_registry, (
        "the lifespan's configure_subagent_block_notifier call did not receive "
        "the SAME TunnelRegistry stamped onto app.state.tunnel_registry — a "
        "nested native sub-agent's blocked-parent wake would recover with no "
        "live-runner-reconnect wait."
    )
