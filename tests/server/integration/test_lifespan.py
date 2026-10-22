"""Tests for the FastAPI app lifespan hook.

Exercises the ``_lifespan`` context manager in
``omnigent.server.app`` to verify shutdown wiring for the
:class:`TerminalRegistry`. Per ``designs/OMNIGENT_TERMINAL_BRIDGE.md``
§4.4, every live tmux session must be closed when the server's
lifespan exits.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from unittest.mock import Mock

import httpx
import pytest
from fastapi import FastAPI

from tests.budgets import budget

pytestmark = pytest.mark.asyncio


@pytest.mark.parametrize("ci", [True, False])
async def test_lifespan_usage_telemetry_uses_server_ci_environment(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    ci: bool,
) -> None:
    """Server CI suppresses usage telemetry without a runner or host opt-out."""
    import omnigent.telemetry.client as telemetry
    from omnigent.telemetry.environment import CI_ENV_VARS
    from omnigent.telemetry.events import SessionStoppedEvent

    for name in CI_ENV_VARS | {
        "OMNIGENT_ANALYTICS",
        "DISABLE_TELEMETRY",
        "OMNIGENT_DISABLE_TELEMETRY",
        "DO_NOT_TRACK",
    }:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path / "config"))
    if ci:
        monkeypatch.setenv("CI", "true")
    monkeypatch.setattr(telemetry, "_CLIENT", None)
    monkeypatch.setattr(telemetry, "_IS_DISABLED_CACHE", [None])
    client_factory = Mock()
    monkeypatch.setattr(telemetry, "TelemetryClient", client_factory)
    installation_id = Mock(return_value="server-installation")
    monkeypatch.setattr("omnigent.telemetry.installation_id.get_installation_id", installation_id)
    event = SessionStoppedEvent("server-installation", "test-session", None)

    async with app.router.lifespan_context(app):
        telemetry.emit(event)
        if ci:
            assert telemetry.get_client() is None
            client_factory.assert_not_called()
            installation_id.assert_not_called()
        else:
            client_factory.assert_called_once_with()
            client_factory.return_value._ensure_started.assert_called_once_with()
            client_factory.return_value.emit.assert_called_once_with(event)


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

    threads_before = set(threading.enumerate())
    async with app.router.lifespan_context(app):
        await asyncio.wait_for(publisher_started.wait(), timeout=1.0)
        watchdogs = [
            thread
            for thread in set(threading.enumerate()) - threads_before
            if thread.name == "omnigent-event-loop-watchdog"
        ]
        assert len(watchdogs) == 1
    watchdogs[0].join(timeout=budget(3))
    assert not watchdogs[0].is_alive()
