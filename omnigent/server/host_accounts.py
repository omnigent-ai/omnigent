"""Operator-declared accounts for native host routes.

The registry names which account each host and harness already uses. It never
holds credentials or model entitlements. It is unrelated to the Codex ChatGPT
plan sign-in that Settings shows as a harness account.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harness_aliases import canonicalize_harness
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes._host_model_options import request_host_model_options

HOST_ACCOUNTS_FILE_ENV = "OMNIGENT_HOST_ACCOUNTS_FILE"
MANAGED_HARNESSES = frozenset({"claude-native", "codex-native"})


def require_no_account_transition(conv: Conversation, *, runner_id: str | None = None) -> None:
    """Refuse to start or address a runtime while an account move is pending.

    :param runner_id: The runner sending the request. Only the move's own
        launch runner, once bound to the session, is let through, so the
        destination can start while the move finishes.
    :raises OmnigentError: ``CONFLICT`` while a move is pending.
    """
    marker = conv.account_transition
    if marker is None:
        return
    if runner_id is not None and conv.runner_id == runner_id == marker.get("launch_runner_id"):
        return
    raise OmnigentError(
        "An account change is pending; finish or cancel it in Switch model or account",
        code=ErrorCode.CONFLICT,
    )


class HostAccountPlacement(BaseModel):
    """A fixed host route whose launcher already selects the declared account."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)

    host_id: str = Field(min_length=1)
    machine_id: str = Field(min_length=1)
    machine_label: str = Field(min_length=1)


class HostAccount(BaseModel):
    """One account, optionally available on several machines."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)

    id: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1)
    provider: Literal["anthropic", "openai"]
    harness: Literal["claude-native", "codex-native"]
    placements: list[HostAccountPlacement] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_provider(self) -> HostAccount:
        expected = "claude-native" if self.provider == "anthropic" else "codex-native"
        if self.harness != expected:
            raise ValueError("provider and harness must describe the same native route")
        return self


class HostAccountRegistry(BaseModel):
    """The accounts an operator declared for native host routes."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=True)

    version: Literal[1]
    accounts: list[HostAccount]

    @model_validator(mode="after")
    def validate_unique_routes(self) -> HostAccountRegistry:
        ids: set[str] = set()
        routes: set[tuple[str, str]] = set()
        for account in self.accounts:
            if account.id in ids:
                raise ValueError("duplicate account id")
            ids.add(account.id)
            for placement in account.placements:
                key = (placement.host_id, account.harness)
                if key in routes:
                    raise ValueError("host/harness route belongs to more than one placement")
                routes.add(key)
        return self

    def resolve(
        self, host_id: str | None, harness: str | None, account_id: str | None = None
    ) -> HostAccount | None:
        """Return the account for a native route; unregistered routes are refused."""
        harness = canonicalize_harness(harness) or harness
        if harness not in MANAGED_HARNESSES:
            if account_id is not None:
                raise HTTPException(400, "Accounts apply only to native Claude/Codex routes")
            return None
        for account in self.accounts:
            if account.harness == harness and any(
                placement.host_id == host_id for placement in account.placements
            ):
                if account_id is not None and account.id != account_id:
                    raise HTTPException(400, "Account does not match the requested route")
                return account
        raise HTTPException(400, "Native host route is not registered to an account")


def execution_topology(visible: dict[str, Any]) -> dict[str, Any]:
    """Normalize an ownership-filtered account view into machines and bindings."""
    machines: dict[str, dict[str, str]] = {}
    accounts = []
    bindings = []
    for account in visible["accounts"]:
        accounts.append({key: value for key, value in account.items() if key != "placements"})
        for placement in account["placements"]:
            machines.setdefault(
                placement["machine_id"],
                {"id": placement["machine_id"], "label": placement["machine_label"]},
            )
            bindings.append(
                {
                    "host_id": placement["host_id"],
                    "machine_id": placement["machine_id"],
                    "account_id": account["id"],
                    "harness": account["harness"],
                    "available": placement["available"],
                    "reason": placement.get("reason"),
                }
            )
    return {
        "configured": visible["configured"],
        "machines": list(machines.values()),
        "accounts": accounts,
        "bindings": bindings,
    }


def load_account_registry() -> HostAccountRegistry | None:
    """Read an explicit registry; a missing or invalid explicit file fails closed."""
    path = os.environ.get(HOST_ACCOUNTS_FILE_ENV)
    if path is None:
        return None
    try:
        if not path.strip():
            raise ValueError("empty registry path")
        return HostAccountRegistry.model_validate_json(Path(path).read_text())
    except (OSError, ValueError, ValidationError) as exc:
        # Never include file contents or local config paths in a client error.
        raise RuntimeError("Invalid explicit host account registry") from exc


def account_registry(request: Request) -> HostAccountRegistry | None:
    """Return the registry loaded at server startup, if one is configured."""
    return getattr(request.app.state, "account_registry", None)


def require_account_route(
    request: Request,
    *,
    host_id: str | None,
    harness: str | None,
    account_id: str | None = None,
) -> HostAccount | None:
    """Validate the route identity before catalog probes or runtime side effects."""
    registry = account_registry(request)
    if registry is None:
        if account_id is not None:
            raise HTTPException(400, "Accounts are not configured on this server")
        return None
    return registry.resolve(host_id, harness, account_id)


async def validate_account_selection(
    request: Request,
    *,
    host_id: str | None,
    harness: str | None,
    model: str | None,
    effort: str | None,
    host_registry: HostRegistry | None,
    account_id: str | None = None,
) -> HostAccount | None:
    """Validate the route and its current host-provided model catalog."""
    registry = account_registry(request)
    if registry is None:
        if account_id is not None:
            raise HTTPException(400, "Accounts are not configured on this server")
        return None
    account = registry.resolve(host_id, harness, account_id)
    if account is None:
        return None
    conn = host_registry.get(host_id) if host_registry is not None and host_id else None
    if conn is None or host_registry is None:
        raise HTTPException(409, "Account machine is offline")
    try:
        result = await request_host_model_options(
            host_registry=host_registry,
            host_conn=conn,
            harness=account.harness,
            timeout_s=15.0,
        )
    except (TimeoutError, ConnectionError) as exc:
        raise HTTPException(503, "Account model catalog is unavailable") from exc
    if result.get("status") != "ok":
        raise HTTPException(503, "Account model catalog is unavailable")
    rows = result.get("models") or []
    chosen = (
        next(
            (
                row
                for row in rows
                if isinstance(row, dict) and model in (row.get("id"), row.get("model"))
            ),
            None,
        )
        if model is not None
        else next((row for row in rows if isinstance(row, dict) and row.get("isDefault")), None)
    )
    routable = result.get("routable_models") or []
    if model is not None and chosen is None and model not in routable:
        raise HTTPException(400, "Model is not offered by this account route")
    if effort is not None:
        if chosen is None:
            raise HTTPException(400, "Select a catalog model before choosing effort")
        supported = [
            entry.get("reasoningEffort")
            for entry in chosen.get("supportedReasoningEfforts", [])
            if isinstance(entry, dict)
        ]
        if not supported and account.harness == "claude-native":
            from omnigent.util.reasoning_effort import CLAUDE_EFFORTS

            supported = list(CLAUDE_EFFORTS)
        if effort not in supported:
            raise HTTPException(400, "Effort is not offered by this account model")
    return account


def validate_transition_launch(
    conv: Conversation,
    *,
    host_id: str,
    harness: str | None,
    account_id: str | None,
    transition_id: str | None,
    workspace: str,
) -> dict[str, Any] | None:
    """Only the recorded destination may finish a pending account move.

    :returns: The pending marker, or ``None`` when no move is pending.
    :raises HTTPException: 409 for a stale id or a launch that differs from the destination.
    """
    marker = conv.account_transition
    if marker is None:
        if transition_id is not None:
            raise HTTPException(409, "Account change is stale")
        return None
    if (
        transition_id != marker["id"]
        or account_id != marker["account_id"]
        or host_id != marker["host_id"]
        or harness != marker["harness"]
        or conv.model_override != marker["model"]
        or conv.reasoning_effort != marker["effort"]
        or (marker.get("workspace") is not None and workspace != marker["workspace"])
    ):
        raise HTTPException(409, "Launch does not match the pending account change")
    return marker


def validate_target_workspace(workspace: str | None) -> None:
    """Reject a destination directory that is not a plain absolute path.

    :raises HTTPException: 400 when ``workspace`` is relative, has a NUL byte or is too long.
    """
    if workspace is None:
        return
    absolute = workspace.startswith("/") or (
        len(workspace) > 2
        and workspace[0].isalpha()
        and workspace[1] == ":"
        and workspace[2] in "/\\"
    )
    if not absolute or "\x00" in workspace or len(workspace) > 1024:
        raise HTTPException(400, "target_workspace must be an absolute path")
