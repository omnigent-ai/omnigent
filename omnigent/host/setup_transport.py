"""Host-local dispatcher for typed setup and ephemeral vendor operations."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from omnigent.host.frames import (
    HostSetupRequestFrame,
    HostSetupResultFrame,
    HostSetupTerminalFrame,
    SetupMethod,
)

if TYPE_CHECKING:
    from omnigent.host.setup_operations import SetupOperationManager


class HostSetupDispatcher:
    """Keep host configuration writes serialized across tunnel reconnects."""

    def __init__(self) -> None:
        self.write_lock = asyncio.Lock()
        self._manager: SetupOperationManager | None = None
        self._attachments: dict[str, str] = {}
        self._generation = 0

    def has_active_operation(self) -> bool:
        return self._manager is not None and self._manager.has_active_operation()

    def _operations(self) -> SetupOperationManager:
        if self._manager is None:
            from omnigent.host.setup_operations import SetupOperationManager

            self._manager = SetupOperationManager()
        return self._manager

    async def request(
        self,
        frame: HostSetupRequestFrame,
        send: Callable[[HostSetupTerminalFrame], Awaitable[None]],
    ) -> HostSetupResultFrame:
        """Validate again on the host and return only sanitized result models."""
        from omnigent.onboarding.setup_schema import SETUP_ACTION_ADAPTER, SetupDetectRequest
        from omnigent.onboarding.setup_service import (
            SetupPersistenceError,
            apply_setup_action,
            detect_setup_connections,
            get_setup_inventory,
        )

        try:
            if frame.method == SetupMethod.INVENTORY:
                result = await asyncio.to_thread(get_setup_inventory)
            elif frame.method == SetupMethod.DETECT:
                detect_request = SetupDetectRequest.model_validate(frame.secret_payload)
                async with self.write_lock:
                    if self.has_active_operation():
                        return HostSetupResultFrame(
                            frame.request_id,
                            error_status=409,
                            error="another setup operation is running",
                        )
                    result = await asyncio.to_thread(detect_setup_connections, detect_request)
            elif frame.method == SetupMethod.ACTION:
                action = SETUP_ACTION_ADAPTER.validate_python(frame.secret_payload)
                async with self.write_lock:
                    if self.has_active_operation():
                        return HostSetupResultFrame(
                            frame.request_id,
                            error_status=409,
                            error="another setup operation is running",
                        )
                    result = await asyncio.to_thread(apply_setup_action, action)
            elif frame.method == SetupMethod.START:
                from omnigent.host.setup_operations import SetupOperationRequest

                request = SetupOperationRequest.from_dict(frame.secret_payload)
                async with self.write_lock:
                    result = await self._operations().start(request)
            elif frame.method == SetupMethod.GET:
                result = await self._operations().get(frame.operation_id)
            elif frame.method == SetupMethod.VERIFY:
                async with self.write_lock:
                    result = await self._operations().verify(frame.operation_id)
            elif frame.method == SetupMethod.CANCEL:
                result = await self._operations().cancel(frame.operation_id)
            elif frame.method == SetupMethod.ATTACH:
                generation = self._generation

                async def output(payload: dict[str, Any]) -> None:
                    await send(
                        HostSetupTerminalFrame(
                            frame.operation_id,
                            frame.attachment_id,
                            payload,
                        )
                    )

                result = await self._operations().attach(
                    frame.operation_id,
                    frame.attachment_id,
                    output,
                )
                if generation != self._generation:
                    await self._operations().detach(frame.operation_id, frame.attachment_id)
                    return HostSetupResultFrame(
                        frame.request_id, error_status=409, error="host connection changed"
                    )
                self._attachments[frame.attachment_id] = frame.operation_id
            elif frame.method == SetupMethod.DETACH:
                await self.detach(frame.operation_id, frame.attachment_id)
                return HostSetupResultFrame(frame.request_id)
            else:
                return HostSetupResultFrame(
                    frame.request_id, error_status=400, error="unsupported setup request"
                )
            from pydantic import BaseModel

            payload = (
                result.model_dump(mode="json")
                if isinstance(result, BaseModel)
                else result.as_dict()
            )
            if frame.method in (SetupMethod.INVENTORY, SetupMethod.ACTION):
                inventory = (
                    payload if frame.method == SetupMethod.INVENTORY else payload.get("inventory")
                )
                if isinstance(inventory, dict):
                    inventory["supported_operations"] = [
                        action.value for action in self._operations().supported_actions()
                    ]
            return HostSetupResultFrame(frame.request_id, payload=payload)
        except SetupPersistenceError as exc:
            return HostSetupResultFrame(frame.request_id, error_status=502, error=str(exc))
        except ValueError:
            return HostSetupResultFrame(
                frame.request_id, error_status=400, error="invalid setup configuration"
            )
        except Exception as exc:  # noqa: BLE001 - never log credential-bearing exceptions
            from omnigent.host.setup_operations import SetupOperationError

            if isinstance(exc, SetupOperationError):
                status = {
                    "not_found": 404,
                    "invalid_request": 400,
                    "conflict": 409,
                    "unavailable": 503,
                }.get(exc.code, 502)
                return HostSetupResultFrame(
                    frame.request_id,
                    error_status=status,
                    error=exc.message,
                )
            # Exceptions can include keychain values or vendor output.
            return HostSetupResultFrame(
                frame.request_id, error_status=502, error="host setup failed"
            )

    async def terminal(self, frame: HostSetupTerminalFrame) -> None:
        """Ignore input for detached or mismatched operation channels."""
        if self._attachments.get(frame.attachment_id) != frame.operation_id:
            return
        try:
            await self._operations().handle_terminal(
                frame.operation_id,
                frame.attachment_id,
                frame.secret_payload,
            )
        except Exception:  # noqa: BLE001 - discard credential-bearing transport failures
            await self.detach(frame.operation_id, frame.attachment_id)

    async def detach(self, operation_id: str, attachment_id: str) -> None:
        if self._attachments.get(attachment_id) != operation_id:
            return
        self._attachments.pop(attachment_id, None)
        if self._manager is not None:
            await self._manager.detach(operation_id, attachment_id)

    async def disconnect(self) -> None:
        """Detach channels while retaining bounded operations across reconnect."""
        self._generation += 1
        for attachment_id, operation_id in list(self._attachments.items()):
            await self.detach(operation_id, attachment_id)

    async def shutdown(self) -> None:
        if self._manager is not None:
            await self._manager.shutdown()
