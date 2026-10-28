"""Durable liveness admission, independent of accepted request lifetimes."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid

from omnigent.stores.conversation_store import ConversationStore

_logger = logging.getLogger(__name__)
# Cleanup may outlive a cancelled handshake while its database worker finishes.
# custom-lint: disable-next=workspace-scoped-cache -- set of Task objects
_pending_cleanup: set[asyncio.Task[None]] = set()


async def admit_runner_connection(
    store: ConversationStore,
    runner_id: str,
    *,
    expected_epoch: str | None = None,
) -> str:
    """Claim once before admission; close partial claims after their worker finishes."""
    epoch = uuid.uuid4().hex
    closing = False

    async def claim_and_activate() -> None:
        await asyncio.to_thread(
            store.claim_runner_connection,
            runner_id,
            epoch,
            expected_epoch=expected_epoch,
        )
        if closing:
            return
        if not await asyncio.to_thread(store.activate_runner_connection, runner_id, epoch):
            raise RuntimeError("runner connection claim was superseded")

    write = asyncio.create_task(claim_and_activate(), name="runner-connection-claim")
    try:
        await asyncio.shield(write)
    except BaseException:
        closing = True

        async def close_after_write() -> None:
            with contextlib.suppress(Exception):
                await write
            await close_runner_connection(store, runner_id, epoch)

        cleanup = asyncio.create_task(close_after_write(), name="runner-connection-claim-cleanup")
        _pending_cleanup.add(cleanup)
        cleanup.add_done_callback(_pending_cleanup.discard)
        # A second cancellation can release the caller; the retained cleanup
        # still waits for the original worker before closing its exact epoch.
        await asyncio.shield(cleanup)
        raise
    return epoch


async def close_runner_connection(store: ConversationStore, runner_id: str, epoch: str) -> None:
    """Close this liveness epoch without retiring connections or accepted work."""
    try:
        await asyncio.to_thread(store.close_runner_connection, runner_id, epoch)
    except Exception:
        _logger.warning("Runner connection close failed for runner=%s", runner_id, exc_info=True)
