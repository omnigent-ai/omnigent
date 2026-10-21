"""Validated sandbox profile catalog and immutable launch selections."""

from __future__ import annotations

import hashlib
from typing import Any

import click
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import Engine

from omnigent.db.db_models import uuid_to_bytes
from omnigent.onboarding.sandboxes.profiles import validate_profile_config
from omnigent.server.managed_hosts import ManagedSandboxDeployment
from omnigent.stores.sandbox_profile_store import (
    PROFILE_ID_PATTERN,
    SandboxProfileStore,
    encode_snapshot,
)


class SandboxProfileWrite(BaseModel):
    """Customer-authored launch settings; secrets and provider internals are excluded."""

    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    provider: str = Field(min_length=1, max_length=32)
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class SandboxProfile(SandboxProfileWrite):
    id: str = Field(pattern=PROFILE_ID_PATTERN)
    revision: str = Field(pattern=r"^[a-f0-9]{64}$")


def _revision(profile: dict[str, Any]) -> str:
    return hashlib.sha256(
        encode_snapshot({k: v for k, v in profile.items() if k != "revision"})
    ).hexdigest()


def _invalid_config(error: ValueError | click.ClickException) -> HTTPException:
    if isinstance(error, ValidationError):
        detail = "; ".join(
            f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
            for item in error.errors(include_input=False)
        )
    else:
        detail = str(error)
    return HTTPException(status_code=422, detail=detail)


class SandboxProfileCatalog:
    """Resolve current permission to use a profile while retaining its saved configuration."""

    def __init__(self, engine: Engine, deployment: ManagedSandboxDeployment | None) -> None:
        self._store = SandboxProfileStore(engine)
        self._deployment = deployment

    def _validate_provider(self, provider: str, config: dict[str, Any]) -> None:
        entry = self._deployment.for_provider(provider) if self._deployment else None
        if (
            provider not in {"kubernetes", "agent_sandbox"}
            or entry is None
            or not entry.managed_launch_supported
        ):
            raise HTTPException(
                status_code=422,
                detail=f"Sandbox profiles are unavailable for provider {provider!r}",
            )
        try:
            entry.launcher_factory().apply_profile(config)
        except (ValueError, click.ClickException) as exc:
            raise _invalid_config(exc) from exc

    def providers(self) -> list[str]:
        offered = self._deployment.launchable_providers() if self._deployment else ()
        supported = []
        for provider in offered:
            try:
                self._validate_provider(provider, {})
            except HTTPException:
                continue
            supported.append(provider)
        return supported

    def list_profiles(
        self, *, include_disabled: bool = False, cursor: str | None = None, limit: int = 100
    ) -> tuple[list[dict[str, Any]], str | None]:
        return self._store.list_profiles(
            include_disabled=include_disabled, cursor=cursor, limit=limit
        )

    def put(self, profile_id: str, body: SandboxProfileWrite | dict[str, Any]) -> dict[str, Any]:
        try:
            validated = SandboxProfileWrite.model_validate(body)
            profile = {"id": profile_id, **validated.model_dump()}
            profile["config"] = validate_profile_config(profile["config"])
            profile["revision"] = _revision(profile)
            profile = SandboxProfile.model_validate(profile).model_dump()
            self._validate_provider(profile["provider"], profile["config"])
            return self._store.put(profile)
        except ValueError as exc:
            raise _invalid_config(exc) from exc

    def disable(self, profile_id: str) -> dict[str, Any]:
        profile = self._store.get(profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="Sandbox profile not found")
        profile["enabled"] = False
        profile["revision"] = _revision(profile)
        return self._store.put(profile)

    def resolve(
        self, profile_id: str, expected_revision: str | None = None, provider: str | None = None
    ) -> dict[str, Any]:
        profile = self._store.get(profile_id)
        if profile is None:
            raise HTTPException(status_code=404, detail="Sandbox profile not found")
        if not profile["enabled"]:
            raise HTTPException(status_code=409, detail="Sandbox profile is disabled")
        if expected_revision is not None and profile["revision"] != expected_revision:
            raise HTTPException(
                status_code=409, detail="Sandbox profile changed. Refresh the profile choices."
            )
        if provider is not None and provider != profile["provider"]:
            raise HTTPException(
                status_code=422, detail="Sandbox profile belongs to a different provider"
            )
        self._validate_provider(profile["provider"], profile["config"])
        return profile

    def validate_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Recheck current availability without replacing a session's accepted settings."""
        try:
            saved = SandboxProfile.model_validate(snapshot).model_dump()
            if saved["revision"] != _revision(saved):
                raise ValueError("Saved sandbox profile revision is invalid")
            validate_profile_config(saved["config"])
            encode_snapshot(saved)
        except ValueError as exc:
            raise _invalid_config(exc) from exc
        current = self._store.get(saved["id"])
        if current is None or not current["enabled"]:
            raise HTTPException(
                status_code=409, detail="The saved sandbox profile is no longer enabled"
            )
        if current["provider"] != saved["provider"]:
            raise HTTPException(
                status_code=409, detail="The saved sandbox profile's provider changed"
            )
        self._validate_provider(saved["provider"], saved["config"])
        return saved

    def bind_session(self, session_id: str, snapshot: dict[str, Any]) -> None:
        try:
            self._store.bind_session(session_id, self.validate_snapshot(snapshot))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    def get_session_snapshot(self, session_id: str) -> dict[str, Any] | None:
        return self._store.get_session_snapshot(session_id)

    def bind_host(self, session_id: str, host_id: str) -> None:
        try:
            self._store.bind_host(session_id, host_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    def get_host_snapshot(self, host_id: str) -> dict[str, Any] | None:
        return self._store.get_host_snapshot(host_id)

    def require_session_host(self, session_id: str, host_id: str, *, bind: bool = True) -> None:
        """After host ownership checks, inherit or match the host's accepted profile."""
        host_snapshot = self.get_host_snapshot(host_id)
        session_snapshot = self.get_session_snapshot(session_id)
        if host_snapshot is not None:
            self.validate_snapshot(host_snapshot)
        saved_host_id = self._store.get_session_host_id(session_id)
        if saved_host_id is not None and saved_host_id != uuid_to_bytes(host_id).hex():
            raise HTTPException(
                status_code=409, detail="A profiled session cannot be moved to a different host"
            )
        if session_snapshot is not None and (
            host_snapshot is None
            or any(
                session_snapshot[key] != host_snapshot[key]
                for key in ("id", "revision", "provider")
            )
        ):
            raise HTTPException(
                status_code=409, detail="Session and host sandbox profiles do not match"
            )
        if not bind:
            return
        if session_snapshot is None and host_snapshot is not None:
            self.bind_session(session_id, host_snapshot)
        self.bind_host(session_id, host_id)
