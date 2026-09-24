"""E2E regression: codex-native startup must recover an orphaned backfill lease.

The reported journey: a Codex native session whose private ``CODEX_HOME`` holds
a ``running`` state-db backfill lease left by an interrupted earlier launch
(the lease owner is dead, and its timestamp is younger than Codex's 900s
``BACKFILL_LEASE_SECONDS``). Relaunching/resuming the session cannot reclaim
that lease: Codex's ``codex app-server`` prints ``state db backfill is running
...; waiting up to 30s`` and exits 1 after ~30s, so the session's terminal
never comes up. Retrying does not help until the lease expires (~15 minutes).

Why raising the outer budget does not fix it: Omnigent's readiness budget is
already ``_APP_SERVER_READY_TIMEOUT_SECONDS = 60.0``, but Codex exits on its
own ~30s backfill gate well inside that window, so
``CodexNativeAppServer._wait_until_ready()`` observes an early exit and raises
``Codex app-server exited early: ... state db backfill ...`` regardless.

This drives the real product launch path end to end: ``build_codex_native_server``
is the exact factory the runner's ``_auto_create_codex_terminal`` uses, and
``CodexNativeAppServer.start()`` is the coroutine it awaits. No LLM and no model
credentials are needed -- the backfill gate blocks before the app-server binds
its listener, before any model traffic. The orphaned lease is established the
way the incident RCA established it: launch once to create the state db, then
mark the backfill row ``running`` with a current timestamp (a dead-owner,
unexpired lease).

The regression these guard:

* **control (expired lease)** -- a ``running`` lease aged past 900s is reclaimed
  and the launch reaches ready. Proves the seeding actually drives Codex's gate
  and distinguishes the bug from a harness failure.
* **the bug (orphaned unexpired lease)** -- a ``running`` lease at age 0 must
  still let the session start. While the bug is live the launch instead dies on
  the backfill gate at ~30s; a recovery fix must let it become ready (or fail
  fast with distinct, actionable, non-destructive guidance rather than the
  generic readiness error).

Runs with only the ``codex`` CLI installed (no login, no LLM key)::

    pytest tests/e2e/test_codex_native_orphaned_backfill_lease_e2e.py -v
"""

from __future__ import annotations

import asyncio
import socket
import sqlite3
import time
from pathlib import Path

import pytest

from omnigent.harnesses.codex_native.app_server import (
    _APP_SERVER_READY_TIMEOUT_SECONDS,
    build_codex_native_server,
)
from omnigent.inner.codex_executor import _find_codex_cli

pytestmark = pytest.mark.skipif(
    _find_codex_cli() is None,
    reason="codex-native backfill-lease e2e requires the `codex` CLI on PATH "
    "(or OMNIGENT_CODEX_PATH)",
)

# Codex's own backfill gate exits after ~30s; allow generous headroom above the
# 60s outer readiness budget for the fresh-launch + control launches too.
_LAUNCH_BUDGET_S = 90.0


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _mark_running_lease(codex_home: Path, *, age_seconds: int) -> None:
    """Mark the state-db backfill row as an owner-less ``running`` lease.

    Mirrors an earlier launch terminated mid-backfill: ``status=running`` with
    an ``updated_at`` ``age_seconds`` in the past and no watermark/success.
    """
    db = next(codex_home.glob("state_*.sqlite"))
    with sqlite3.connect(db) as conn:
        conn.execute(
            "update backfill_state set status='running', last_watermark=NULL, "
            "last_success_at=NULL, updated_at=? where id=1",
            (int(time.time()) - age_seconds,),
        )


async def _launch_until_ready_or_error(
    *, codex_home: Path, bridge_dir: Path
) -> tuple[str, float]:
    """Run the real codex-native launch path once against ``codex_home``.

    :returns: ``("ready", elapsed)`` when the app-server becomes ready, or
        ``(error_message, elapsed)`` when ``start()`` raises.
    """
    server = build_codex_native_server(
        socket_path=bridge_dir / "app-server.sock",
        codex_home=codex_home,
        cwd=bridge_dir,
        model=None,
        profile=None,
        bridge_dir=bridge_dir,
        ap_server_url=None,
        reconcile_process_registry=False,
    )
    server.listen_url = f"ws://127.0.0.1:{_free_port()}"
    started = time.monotonic()
    try:
        await server.start()
    except RuntimeError as exc:
        return str(exc), time.monotonic() - started
    finally:
        try:
            await server.close()
        except Exception:  # noqa: BLE001 - teardown must not mask the outcome
            pass
    return "ready", time.monotonic() - started


def test_codex_native_recovers_orphaned_backfill_lease(tmp_path: Path) -> None:
    """A codex-native launch must recover an orphaned, unexpired backfill lease.

    :param tmp_path: Per-test temp dir; hosts the isolated bridge + CODEX_HOME.
    :returns: None.
    """
    bridge_dir = tmp_path / "codex-native-bridge"
    bridge_dir.mkdir(mode=0o700)
    codex_home = bridge_dir / "codex-home"

    async def _drive() -> None:
        # Fresh launch: creates the state db and completes its backfill.
        outcome, elapsed = await _launch_until_ready_or_error(
            codex_home=codex_home, bridge_dir=bridge_dir
        )
        assert outcome == "ready", f"fresh codex-native launch failed in {elapsed:.1f}s: {outcome}"

        # Control: an expired (>900s) running lease is reclaimed on relaunch.
        _mark_running_lease(codex_home, age_seconds=901)
        outcome, elapsed = await _launch_until_ready_or_error(
            codex_home=codex_home, bridge_dir=bridge_dir
        )
        assert outcome == "ready", (
            f"expired backfill lease was not reclaimed on relaunch (failed in "
            f"{elapsed:.1f}s): {outcome}"
        )

        # The bug: an orphaned, unexpired running lease (dead owner) must still
        # let the session start. While the bug is live, Codex's backfill gate
        # exits at ~30s -- inside the 60s readiness budget -- and start() raises.
        _mark_running_lease(codex_home, age_seconds=0)
        outcome, elapsed = await _launch_until_ready_or_error(
            codex_home=codex_home, bridge_dir=bridge_dir
        )
        if outcome != "ready":
            assert "backfill" in outcome.lower(), (
                f"orphaned-lease relaunch failed on an unexpected error after "
                f"{elapsed:.1f}s (expected the codex backfill gate): {outcome}"
            )
            pytest.fail(
                f"codex-native launch could not recover an orphaned, "
                f"unexpired backfill lease (dead owner). start() died on the codex "
                f"backfill gate after {elapsed:.1f}s, within the "
                f"{_APP_SERVER_READY_TIMEOUT_SECONDS:g}s readiness budget, so raising "
                f"that budget cannot help: {outcome}"
            )

    asyncio.run(asyncio.wait_for(_drive(), timeout=_LAUNCH_BUDGET_S * 3))
