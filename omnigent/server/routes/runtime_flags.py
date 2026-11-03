"""``POST /v1/runtime-flags``: runtime flag values for hosts and runners."""

from __future__ import annotations

import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    model_validator,
)

from omnigent.host.identity import MANAGED_HOST_TOKEN_HEADER
from omnigent.runtime_flags import (
    DEFAULT_REFRESH_INTERVAL_S,
    MAX_RUNTIME_FLAGS_PER_REQUEST,
    RUNTIME_FLAG_NAME_PATTERN,
    RuntimeFlagType,
    runtime_flag_value_matches,
)
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.runtime_flags import RuntimeFlagCaller, RuntimeFlagProvider, RuntimeFlagQuery
from omnigent.stores.host_store import HostStore

_logger = logging.getLogger(__name__)

# Strict types keep pydantic from turning true into 1 or "1" into 1.
_WireValue = StrictBool | StrictInt | StrictFloat | StrictStr | list[StrictStr]


class RuntimeFlagRequestItem(BaseModel):
    """One flag a client declares."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(pattern=RUNTIME_FLAG_NAME_PATTERN)]
    type: RuntimeFlagType
    default: _WireValue

    @model_validator(mode="after")
    def _default_matches_type(self) -> RuntimeFlagRequestItem:
        if not runtime_flag_value_matches(self.type, self.default):
            raise ValueError(f"default does not match type {self.type}")
        return self


class RuntimeFlagsRequest(BaseModel):
    """The flags one client process declares."""

    model_config = ConfigDict(extra="forbid")

    flags: Annotated[list[RuntimeFlagRequestItem], Field(max_length=MAX_RUNTIME_FLAGS_PER_REQUEST)]
    # The host a launch token is bound to; required with that token, else unused.
    host_id: str | None = None

    @model_validator(mode="after")
    def _unique_names(self) -> RuntimeFlagsRequest:
        names = [flag.name for flag in self.flags]
        if len(names) != len(set(names)):
            raise ValueError("flag names must be unique")
        return self


class RuntimeFlagsResponse(BaseModel):
    """Values the server serves; a missing flag means "use your default"."""

    values: dict[str, _WireValue]
    refresh_after_s: int


def create_runtime_flags_router(
    provider: RuntimeFlagProvider | None,
    auth_provider: AuthProvider | None = None,
    host_store: HostStore | None = None,
) -> APIRouter:
    """Build the runtime-flag route, mounted under ``/v1``.

    :param provider: Evaluates flags; ``None`` serves none.
    :param auth_provider: Resolves the caller; ``None`` skips authentication.
    :param host_store: Resolves a managed host's launch token to the session
        owner; ``None`` rejects launch-token requests.
    :returns: The router.
    """
    router = APIRouter()

    async def resolve_caller(request: Request, body: RuntimeFlagsRequest) -> RuntimeFlagCaller:
        launch_token = request.headers.get(MANAGED_HOST_TOKEN_HEADER)
        if not launch_token:
            return RuntimeFlagCaller(user_id=require_user(request, auth_provider))
        # A sandbox host has no user credential; its launch token names the owner.
        managed = None
        if host_store is not None and body.host_id:
            managed = await asyncio.to_thread(
                host_store.resolve_launch_token, body.host_id, launch_token
            )
        if managed is None:
            raise HTTPException(status_code=401, detail="unauthenticated host")
        return RuntimeFlagCaller(user_id=managed.user_id)

    @router.post("/runtime-flags", response_model=RuntimeFlagsResponse)
    async def evaluate_runtime_flags(
        request: Request, body: RuntimeFlagsRequest
    ) -> RuntimeFlagsResponse:
        """Evaluate the caller's declared runtime flags."""
        caller = await resolve_caller(request, body)
        refresh_after_s = int(DEFAULT_REFRESH_INTERVAL_S)
        if provider is None or not body.flags:
            return RuntimeFlagsResponse(values={}, refresh_after_s=refresh_after_s)
        queries = tuple(
            RuntimeFlagQuery(name=flag.name, value_type=flag.type, default_value=flag.default)
            for flag in body.flags
        )
        try:
            served = await provider.evaluate(queries, caller)
        except Exception as exc:
            _logger.warning("runtime flag evaluation failed: %s", type(exc).__name__)
            raise HTTPException(503, "runtime flag evaluation is unavailable") from exc
        values = {
            query.name: served[query.name]
            for query in queries
            if query.name in served
            and runtime_flag_value_matches(query.value_type, served[query.name])
        }
        return RuntimeFlagsResponse(values=values, refresh_after_s=refresh_after_s)

    return router
