"""Work-aware suspension after an authenticated host has fully quiesced.

Only hosts with no remaining runner processes may quiesce. Launch, wake and
suspend share a durable gate; a lost coordinator never releases it by timeout.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import secrets
import time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from omnigent.db.db_models import workspace_scope
from omnigent.host.frames import (
    HostLaunchRunnerFrame,
    HostQuiesceFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.host.idle import CAP_IDLE_QUIESCE
from omnigent.onboarding.sandboxes.base import SandboxGoneError
from omnigent.runner.identity import token_bound_runner_id
from omnigent.stores.host_store import Host, HostStore, host_is_live

if TYPE_CHECKING:
    from omnigent.server.host_registry import HostConnection, HostRegistry
    from omnigent.server.managed_hosts import ManagedSandboxDeployment
    from omnigent.stores.conversation_store import ConversationStore

_LOG = logging.getLogger(__name__)
_OPERATION_WAIT_S = 180.0


def enabled(host: Host, config: ManagedSandboxDeployment | None) -> bool:
    if config is None or host.sandbox_provider is None:
        return False
    launcher = config.recorded(host.sandbox_provider).launcher_factory()
    return launcher.provider == host.sandbox_provider and getattr(
        launcher.capabilities, "managed_idle_suspend", False
    )


@contextlib.asynccontextmanager
async def operation(
    store: HostStore,
    host_id: str,
    purpose: str,
    *,
    expected_sandbox_id: str | None = None,
) -> AsyncIterator[dict[str, str]]:
    token = secrets.token_hex(16)
    deadline = time.monotonic() + _OPERATION_WAIT_S
    while True:
        try:
            record = await asyncio.to_thread(
                store.claim_sandbox_operation,
                host_id,
                token,
                purpose,
                expected_sandbox_id=expected_sandbox_id,
            )
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        if record is not None:
            break
        if time.monotonic() >= deadline:
            raise HTTPException(
                status_code=409,
                detail="Managed sandbox operation is still in progress; no new work was sent",
            )
        await asyncio.sleep(0.1)
    result = "unknown"
    try:
        yield record
        result = record.get("result", "active")
    except SandboxGoneError:
        result = "active"
        raise
    except BaseException:
        result = record.get("safe_failure", "unknown")
        raise
    finally:
        await asyncio.shield(
            asyncio.to_thread(store.finish_sandbox_operation, host_id, token, result)
        )


class ManagedIdleSuspend:
    def __init__(
        self,
        store: HostStore,
        conversations: ConversationStore,
        config: ManagedSandboxDeployment,
        registry: HostRegistry,
        *,
        interval_s: float = 5,
    ) -> None:
        self.store, self.conversations, self.config, self.registry = (
            store,
            conversations,
            config,
            registry,
        )
        self.interval_s = interval_s
        self.tasks: set[asyncio.Task[None]] = set()
        self.sweeper: asyncio.Task[None] | None = None
        self.closing = False

    def start(self) -> None:
        self.registry.idle_launch_dispatcher = self.dispatch
        self.sweeper = asyncio.create_task(self.run(), name="managed-idle-suspend")

    async def shutdown(self) -> None:
        self.closing = True
        if self.sweeper:
            self.sweeper.cancel()
            await asyncio.gather(self.sweeper, return_exceptions=True)
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    def dispatch(self, connection: HostConnection, data: str) -> bool:
        if (
            not connection.registered_with_managed_token
            or CAP_IDLE_QUIESCE not in connection.hello.capabilities
        ):
            return False
        if json.loads(data).get("kind") != "host.launch_runner":
            return False
        if self.closing:
            raise ConnectionError("Managed launch coordinator is shutting down")
        frame = decode_host_frame(data)
        if not isinstance(frame, HostLaunchRunnerFrame):
            return False
        original = connection.pending_launches.pop(frame.request_id, None)
        if original is None:
            raise ConnectionError("Managed runner launch must have an acknowledgement receiver")
        task = asyncio.create_task(self.send_launch(connection, frame, original))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return True

    async def send_launch(
        self,
        connection: HostConnection,
        frame: HostLaunchRunnerFrame,
        original: asyncio.Future[Any],
    ) -> None:
        target = None
        dispatched = False
        try:
            host = await asyncio.to_thread(self.store.get_host, connection.host_id)
            if host is None:
                raise ConnectionError("Managed host was deleted")
            if not enabled(host, self.config):
                connection.pending_launches[frame.request_id] = original
                self.registry.send_text_direct(connection, encode_host_frame(frame))
                return
            async with operation(self.store, connection.host_id, "launch") as record:
                record["safe_failure"] = record["previous"]
                host = await asyncio.to_thread(self.store.get_host, connection.host_id)
                if (
                    host is None
                    or host.user_id != connection.owner
                    or host.account_generation != connection.account_generation
                ):
                    raise ConnectionError("Managed host ownership changed before launch")
                if (
                    record["previous"] == "suspended"
                    or not host_is_live(host)
                    or self.registry.get(host.host_id) is None
                ):
                    from omnigent.server.managed_hosts import _resume_managed_host

                    record.pop("safe_failure", None)
                    await _resume_managed_host(host.host_id, self.store, self.config, force=True)
                    record["safe_failure"] = "active"
                deadline = time.monotonic() + _OPERATION_WAIT_S
                while (target := self.registry.get(host.host_id)) is None:
                    if time.monotonic() >= deadline:
                        raise ConnectionError("Managed host did not reconnect")
                    await asyncio.sleep(0.05)
                if frame.session_id is None:
                    raise ValueError("Managed runner launches require a session")
                conv = await asyncio.to_thread(
                    self.conversations.get_conversation, frame.session_id
                )
                if (
                    conv is None
                    or conv.host_id != host.host_id
                    or conv.runner_id != token_bound_runner_id(frame.binding_token)
                ):
                    record["result"] = "active"
                    raise SandboxGoneError("Runner binding changed before launch was sent")
                await self.registry.admit_launch(target, frame.session_id)
                acknowledgement = asyncio.get_running_loop().create_future()
                target.pending_guarded_launches[frame.request_id] = acknowledgement
                self.registry.send_text_direct(target, encode_host_frame(frame))
                dispatched = True
                record.pop("safe_failure", None)
                result = await asyncio.wait_for(acknowledgement, _OPERATION_WAIT_S)
                if not original.done():
                    original.set_result(result)
        except Exception as error:  # noqa: BLE001 -- detached launches must settle their callers
            if not dispatched and not original.done():
                original.set_result(
                    {
                        "status": "failed",
                        "error_code": "host_launch_failed",
                        "error": (
                            "Managed host could not accept the launch; "
                            "retry after resolving its lifecycle state"
                        ),
                    }
                )
            _LOG.warning("Managed runner launch did not settle: %s", error)
        finally:
            if target is not None:
                target.pending_guarded_launches.pop(frame.request_id, None)

    async def run(self) -> None:
        while True:
            for connection in self.registry.connections():
                if (
                    not connection.registered_with_managed_token
                    or CAP_IDLE_QUIESCE not in connection.hello.capabilities
                ):
                    continue
                with workspace_scope(connection.workspace_id):
                    try:
                        await self.suspend_if_idle(connection)
                    except Exception:  # noqa: BLE001 -- one provider must not stop the observer
                        _LOG.warning(
                            "Managed idle suspension did not settle for %s",
                            connection.host_id,
                            exc_info=True,
                        )
            await asyncio.sleep(self.interval_s)

    async def suspend_if_idle(self, connection: HostConnection) -> bool:
        host = await asyncio.to_thread(self.store.get_host, connection.host_id)
        if (
            host is None
            or not host.sandbox_id
            or not host_is_live(host)
            or not enabled(host, self.config)
        ):
            return False
        # Busy/unknown operations are not stolen by a periodic observer.
        state = await asyncio.to_thread(self.store.sandbox_operation, host.host_id)
        if state and state.get("phase") in {"busy", "unknown"}:
            return False
        from omnigent.server.managed_hosts import build_owner_launcher

        entry = self.config.recorded(host.sandbox_provider)
        launcher = build_owner_launcher(entry, self.config, host.user_id)
        async with operation(
            self.store, host.host_id, "suspend", expected_sandbox_id=host.sandbox_id
        ) as record:
            if self.registry.get(host.host_id) is not connection:
                record["result"] = "active"
                return False
            request_id = record["token"]
            future = asyncio.get_running_loop().create_future()
            connection.pending_quiesce[request_id] = future
            try:
                record["safe_failure"] = "active"
                self.registry.send_text_direct(
                    connection,
                    encode_host_frame(HostQuiesceFrame(request_id, record["generation"])),
                )
                record.pop("safe_failure", None)
                result = await asyncio.wait_for(future, 10)
            finally:
                connection.pending_quiesce.pop(request_id, None)
            if result["status"] == "busy":
                record["result"] = "active"
                return False
            if result["status"] != "ok":
                raise ValueError("Host returned an invalid quiescence acknowledgement")
            await asyncio.to_thread(launcher.wait_for_idle, host.sandbox_id, record["generation"])
            await asyncio.to_thread(launcher.stop, host.sandbox_id)
            record["result"] = "suspended"
            _LOG.info("Suspended idle managed host %s", host.host_id)
            return True
