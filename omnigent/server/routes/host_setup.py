"""Owner-scoped setup transport to a selected host, without a runner session."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import secrets
from typing import Any

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, SecretStr

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.host.frames import (
    HostSetupRequestFrame,
    HostSetupTerminalFrame,
    SetupMethod,
    encode_host_frame,
)
from omnigent.onboarding.setup_schema import SETUP_ACTION_ADAPTER, SetupDetectRequest
from omnigent.server.auth import AuthProvider
from omnigent.server.feature_flags import Feature, FeatureFlags
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._host_launch import host_absent_error
from omnigent.server.routes._origin import require_trusted_origin
from omnigent.stores.host_store import HostStore

_SETUP_TIMEOUT_S = 30.0
_MAX_SETUP_BODY = 64 * 1024
_MAX_PENDING_TERMINAL_FRAMES = 32


def _wire_model(model: BaseModel) -> dict[str, Any]:
    """Serialize credentials only into the private, uncaptured host envelope."""
    result = model.model_dump(mode="json")
    for key in type(model).model_fields:
        value = getattr(model, key)
        if isinstance(value, SecretStr):
            result[key] = value.get_secret_value()
    return result


async def _body(request: Request, *, allow_empty: bool = False) -> object:
    """Reject invalid input without reflecting its contents in HTTP errors."""
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > _MAX_SETUP_BODY:
            raise HTTPException(413, "setup request is too large")
    if allow_empty and not data:
        return {}
    if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
        raise HTTPException(415, "setup requires application/json")
    try:
        return json.loads(data)
    except ValueError:
        raise HTTPException(422, "invalid setup request") from None


async def proxy_setup(
    registry: HostRegistry,
    conn: HostConnection,
    method: SetupMethod,
    *,
    payload: dict[str, Any] | None = None,
    operation_id: str = "",
    attachment_id: str = "",
) -> dict[str, Any]:
    """Correlate a response on the exact host connection that received it."""
    if conn.hello.setup_protocol_version != 1:
        raise HTTPException(501, "host does not support settings setup; update the host")
    request_id = secrets.token_hex(16)
    future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    conn.pending_setup[request_id] = future
    try:
        registry.send_text(
            conn,
            encode_host_frame(
                HostSetupRequestFrame(
                    request_id=request_id,
                    method=method,
                    secret_payload=payload or {},
                    operation_id=operation_id,
                    attachment_id=attachment_id,
                )
            ),
        )
        result = await asyncio.wait_for(future, _SETUP_TIMEOUT_S)
        if result.get("error_status"):
            raise HTTPException(result["error_status"], result.get("error") or "host setup failed")
        return result["payload"]
    except ConnectionError:
        raise HTTPException(502, "host connection lost") from None
    except TimeoutError:
        raise HTTPException(504, "host setup request timed out") from None
    finally:
        conn.pending_setup.pop(request_id, None)


def create_host_setup_router(
    host_registry: HostRegistry,
    host_store: HostStore,
    *,
    auth_provider: AuthProvider | None,
    flags: FeatureFlags,
) -> APIRouter:
    """Create host settings APIs and an ephemeral vendor-terminal attachment."""
    from omnigent.host.setup_logging import install_setup_server_log_filter

    install_setup_server_log_filter()
    router = APIRouter()

    async def resolve(host_id: str, user_id: str | None) -> HostConnection:
        host = await asyncio.to_thread(host_store.get_host, host_id)
        if host is None:
            raise HTTPException(404, "host not found")
        if user_id is not None and host.user_id != user_id:
            raise HTTPException(403, "not your host")
        conn = host_registry.get(host.host_id)
        if conn is None:
            raise host_absent_error(host)
        if user_id is not None and conn.owner != user_id:
            raise HTTPException(403, "not your host")
        if conn.hello.setup_protocol_version != 1:
            raise HTTPException(501, "host does not support settings setup; update the host")
        return conn

    def mutation_gate() -> None:
        if not flags.enabled(Feature.HARNESS_INSTALL):
            raise HTTPException(404, "not found")

    @router.get("/hosts/{host_id}/setup")
    async def inventory(request: Request, host_id: str) -> dict[str, Any]:
        conn = await resolve(host_id, require_user(request, auth_provider))
        result = await proxy_setup(host_registry, conn, SetupMethod.INVENTORY)
        result["mutations_enabled"] = flags.enabled(Feature.HARNESS_INSTALL)
        result["feature_enabled"] = flags.enabled(Feature.HARNESS_INSTALL)
        return result

    @router.post("/hosts/{host_id}/setup/actions")
    async def action(request: Request, host_id: str) -> dict[str, Any]:
        mutation_gate()
        require_trusted_origin(request)
        conn = await resolve(host_id, require_user(request, auth_provider))
        try:
            body = SETUP_ACTION_ADAPTER.validate_python(await _body(request))
        except ValueError:
            raise HTTPException(422, "invalid setup action") from None
        async with conn.credential_write_lock:
            return await proxy_setup(
                host_registry, conn, SetupMethod.ACTION, payload=_wire_model(body)
            )

    @router.post("/hosts/{host_id}/setup/detect")
    async def detect(request: Request, host_id: str) -> dict[str, Any]:
        mutation_gate()
        require_trusted_origin(request)
        conn = await resolve(host_id, require_user(request, auth_provider))
        try:
            body = SetupDetectRequest.model_validate(await _body(request, allow_empty=True))
        except ValueError:
            raise HTTPException(422, "invalid setup detection request") from None
        payload = _wire_model(body)
        if body.harness is None:
            payload.pop("harness", None)
        if not body.pi_default:
            payload.pop("pi_default", None)
        return await proxy_setup(host_registry, conn, SetupMethod.DETECT, payload=payload)

    @router.post("/hosts/{host_id}/setup-operations")
    async def start(request: Request, host_id: str) -> dict[str, Any]:
        mutation_gate()
        require_trusted_origin(request)
        from omnigent.host.setup_operations import SetupOperationError, SetupOperationRequest

        conn = await resolve(host_id, require_user(request, auth_provider))
        try:
            raw = await _body(request)
            if not isinstance(raw, dict):
                raise ValueError
            body = SetupOperationRequest.from_dict(raw)
        except (ValueError, SetupOperationError):
            raise HTTPException(422, "invalid setup operation") from None
        async with conn.credential_write_lock:
            return await proxy_setup(
                host_registry,
                conn,
                SetupMethod.START,
                payload={"action": body.action.value, "parameters": dict(body.parameters)},
            )

    @router.get("/hosts/{host_id}/setup-operations/{operation_id}")
    async def get_operation(request: Request, host_id: str, operation_id: str) -> dict[str, Any]:
        conn = await resolve(host_id, require_user(request, auth_provider))
        return await proxy_setup(host_registry, conn, SetupMethod.GET, operation_id=operation_id)

    @router.post("/hosts/{host_id}/setup-operations/{operation_id}/verify")
    async def verify(request: Request, host_id: str, operation_id: str) -> dict[str, Any]:
        mutation_gate()
        require_trusted_origin(request)
        conn = await resolve(host_id, require_user(request, auth_provider))
        async with conn.credential_write_lock:
            return await proxy_setup(
                host_registry, conn, SetupMethod.VERIFY, operation_id=operation_id
            )

    @router.delete("/hosts/{host_id}/setup-operations/{operation_id}")
    async def cancel(request: Request, host_id: str, operation_id: str) -> dict[str, Any]:
        mutation_gate()
        require_trusted_origin(request)
        conn = await resolve(host_id, require_user(request, auth_provider))
        return await proxy_setup(
            host_registry, conn, SetupMethod.CANCEL, operation_id=operation_id
        )

    @router.websocket("/hosts/{host_id}/setup-operations/{operation_id}/attach")
    async def attach(ws: WebSocket, host_id: str, operation_id: str) -> None:
        from omnigent.server.auth import local_single_user_enabled
        from omnigent.server.ws_origin import origin_allowed, parse_allowed_origins

        if not origin_allowed(
            ws.headers.get("origin"),
            local_mode=local_single_user_enabled(),
            extra_allowed=parse_allowed_origins(),
        ):
            await ws.close(code=4403, reason="untrusted origin")
            return
        try:
            mutation_gate()
            user_id = auth_provider.get_user_id(ws) if auth_provider else None
            if auth_provider is not None and user_id is None:
                raise HTTPException(401, "authentication required")
            conn = await resolve(host_id, user_id)
        except OmnigentError as exc:
            code = 4400 if exc.code == ErrorCode.WRONG_REPLICA else 1008
            await ws.close(code=code, reason=exc.message)
            return
        except HTTPException as exc:
            await ws.close(code=1008, reason=str(exc.detail))
            return
        attachment_id = secrets.token_hex(16)
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=32)
        conn.setup_attachments[attachment_id] = (operation_id, queue)
        await ws.accept()

        async def forward_output() -> None:
            while True:
                payload = await queue.get()
                if payload is None:
                    return
                if payload.get("type") == "output":
                    await ws.send_bytes(base64.b64decode(payload["data"], validate=True))
                elif payload.get("type") == "control":
                    await ws.send_text(payload["data"])
                elif payload.get("type") == "close":
                    await ws.close(code=1000)
                    return

        async def forward_input() -> None:
            while True:
                message = await ws.receive()
                if message["type"] == "websocket.disconnect":
                    return
                payload: dict[str, Any]
                raw = message.get("bytes")
                if raw is not None:
                    if len(raw) > _MAX_SETUP_BODY:
                        return
                    payload = {
                        "type": "input",
                        "encoding": "base64",
                        "data": base64.b64encode(raw).decode("ascii"),
                    }
                else:
                    text = message.get("text", "")
                    if len(text) > 1024:
                        return
                    try:
                        payload = json.loads(text)
                    except ValueError:
                        continue
                    if not isinstance(payload, dict) or payload.get("type") != "resize":
                        continue
                    if not all(
                        type(payload.get(key)) is int and 1 <= payload[key] <= 1000
                        for key in ("cols", "rows")
                    ):
                        continue
                    payload = {key: payload[key] for key in ("type", "cols", "rows")}
                if conn.outbound_queue.qsize() >= _MAX_PENDING_TERMINAL_FRAMES:
                    return
                host_registry.send_text(
                    conn,
                    encode_host_frame(
                        HostSetupTerminalFrame(operation_id, attachment_id, payload)
                    ),
                )

        tasks: list[asyncio.Task[None]] = []
        try:
            await proxy_setup(
                host_registry,
                conn,
                SetupMethod.ATTACH,
                operation_id=operation_id,
                attachment_id=attachment_id,
            )
            tasks = [asyncio.create_task(forward_output()), asyncio.create_task(forward_input())]
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except (HTTPException, ConnectionError, WebSocketDisconnect):
            pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            conn.setup_attachments.pop(attachment_id, None)
            with contextlib.suppress(ConnectionError):
                host_registry.send_text(
                    conn,
                    encode_host_frame(
                        HostSetupRequestFrame(
                            request_id=secrets.token_hex(16),
                            method=SetupMethod.DETACH,
                            operation_id=operation_id,
                            attachment_id=attachment_id,
                        )
                    ),
                )
            with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                await ws.close()

    return router
