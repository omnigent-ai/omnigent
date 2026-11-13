"""Validated provider references and the shared authorization boundary."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, TypeVar

from fastapi import HTTPException
from pydantic import Field

from omnigent.db.db_models import current_workspace_id
from omnigent.onboarding.sandboxes.base import (
    OwnerCredentialMissingError,
    SandboxGoneError,
    SandboxHostLauncher,
)
from omnigent.onboarding.sandboxes.profiles import (
    ProviderSandboxProfile,
    SandboxProfileAccessDenied,
    SandboxProfileChanged,
    SandboxProfileContext,
)
from omnigent.server.auth import RESERVED_USER_LOCAL, RESERVED_USER_PUBLIC

_logger = logging.getLogger(__name__)
_Result = TypeVar("_Result")


def profile_context(user_id: str | None, sandbox_id: str | None = None) -> SandboxProfileContext:
    if not user_id or user_id in (RESERVED_USER_LOCAL, RESERVED_USER_PUBLIC):
        raise HTTPException(
            status_code=401, detail="Sandbox profiles require an authenticated user"
        )
    return SandboxProfileContext(
        user_id=user_id, workspace_id=current_workspace_id(), sandbox_id=sandbox_id
    )


def provider_call(operation: Callable[[], _Result], *, allow_gone: bool = False) -> _Result:
    """Provider errors may contain credentials or private catalog contents."""
    try:
        return operation()
    except OwnerCredentialMissingError:
        raise HTTPException(
            status_code=409,
            detail="Connect your sandbox provider account before choosing a profile",
        ) from None
    except SandboxProfileAccessDenied:
        raise HTTPException(status_code=403, detail="Sandbox profile access denied") from None
    except SandboxProfileChanged:
        raise HTTPException(
            status_code=409, detail="Sandbox profile changed. Choose a profile again."
        ) from None
    except SandboxGoneError:
        if allow_gone:
            raise
        raise HTTPException(
            status_code=409, detail="Sandbox profile resource no longer exists"
        ) from None
    except Exception as exc:  # noqa: BLE001 — external provider boundary
        _logger.warning("Sandbox profile provider failed (%s)", type(exc).__name__)
        raise HTTPException(
            status_code=503, detail="Sandbox profile provider unavailable"
        ) from None


class ProviderProfileSnapshot(ProviderSandboxProfile):
    """Server-accepted reference; configuration and policy remain with the provider."""

    provider: str = Field(min_length=1, max_length=32)
    user_id: str = Field(min_length=1, max_length=256)
    workspace_id: int

    def selection(self) -> ProviderSandboxProfile:
        return ProviderSandboxProfile.model_validate(
            self.model_dump(include={"id", "name", "description", "revision"})
        )


def select_provider_profile(
    launcher: SandboxHostLauncher, snapshot: dict[str, Any], *, sandbox_id: str | None = None
) -> None:
    saved = ProviderProfileSnapshot.model_validate(snapshot)
    if not launcher.manages_profiles:
        raise HTTPException(
            status_code=409, detail="Provider-managed sandbox profiles unavailable"
        )
    if saved.workspace_id != current_workspace_id():
        raise HTTPException(status_code=403, detail="Sandbox profile belongs to another workspace")
    context = profile_context(
        saved.user_id, sandbox_id if launcher.supports_sandbox_profile_lookup else None
    )
    provider_call(
        lambda: launcher.select_profile(context, saved.selection()),
        allow_gone=context.sandbox_id is not None,
    )
