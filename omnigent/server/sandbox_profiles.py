"""Validated sandbox profile catalog and immutable launch selections."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import click
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError
from sqlalchemy import Engine

from omnigent.db.db_models import uuid_to_bytes
from omnigent.onboarding.sandboxes.profiles import ProviderSandboxProfile, validate_profile_config
from omnigent.server.managed_hosts import ManagedSandboxDeployment, build_owner_launcher
from omnigent.server.provider_sandbox_profiles import (
    ProviderProfileSnapshot,
    profile_context,
    provider_call,
    select_provider_profile,
)
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
        if entry is not None and provider_call(entry.launcher_factory).manages_profiles:
            raise HTTPException(
                status_code=403, detail="Sandbox profiles are managed by the provider"
            )
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
        self,
        *,
        include_disabled: bool = False,
        cursor: str | None = None,
        limit: int = 100,
        user_id: str | None = None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        managed = self.managed_providers()
        if not managed:
            return self._store.list_profiles(
                include_disabled=include_disabled, cursor=cursor, limit=limit
            )
        if not 1 <= limit <= 100:
            raise HTTPException(
                status_code=422, detail="Profile page size must be between 1 and 100"
            )
        after: tuple[str, str] | None = None
        if cursor is not None:
            try:
                parts = json.loads(cursor)
                if (
                    not isinstance(parts, list)
                    or len(parts) != 2
                    or not all(isinstance(p, str) for p in parts)
                ):
                    raise ValueError("Invalid cursor")
                after = (parts[0], parts[1])
            except (ValueError, TypeError):
                raise HTTPException(
                    status_code=422, detail="Invalid sandbox profile cursor"
                ) from None
        rows = []
        local_cursor = None
        while True:
            local, local_cursor = self._store.list_profiles(
                include_disabled=include_disabled, cursor=local_cursor, limit=100
            )
            rows.extend(p for p in local if p["provider"] not in managed)
            if local_cursor is None:
                break
        for provider in managed:
            rows.extend(self._provider_profiles(provider, user_id))
        rows.sort(key=lambda row: (row["provider"], row["id"]))
        rows = [row for row in rows if after is None or (row["provider"], row["id"]) > after]
        page = rows[:limit]
        next_cursor = (
            json.dumps([page[-1]["provider"], page[-1]["id"]]) if len(rows) > limit else None
        )
        return page, next_cursor

    def managed_providers(self) -> list[str]:
        if self._deployment is None:
            return []
        managed = []
        for entry in self._deployment.configs:
            if not entry.managed_launch_supported:
                continue
            if not provider_call(entry.launcher_factory).manages_profiles:
                continue
            if not entry.provider or not entry.provider.strip():
                raise HTTPException(
                    status_code=422,
                    detail="Provider-managed sandbox profiles require a configured provider name",
                )
            managed.append(entry.provider)
        return managed

    def _provider_profiles(self, provider: str, user_id: str | None) -> list[dict[str, Any]]:
        context = profile_context(user_id)
        assert self._deployment is not None
        entry = self._deployment.for_provider(provider)
        assert entry is not None

        def read() -> list[ProviderSandboxProfile]:
            profiles = TypeAdapter(list[ProviderSandboxProfile]).validate_python(
                build_owner_launcher(entry, self._deployment, context.user_id).list_profiles(
                    context
                )
            )
            if len(profiles) > 1000 or len({p.id for p in profiles}) != len(profiles):
                raise ValueError("Provider catalog must have at most 1000 unique profiles")
            return profiles

        return [
            {
                **profile.model_dump(),
                "provider": provider,
                "managed": True,
                "enabled": True,
                "config": {},
            }
            for profile in provider_call(read)
        ]

    def require_selection(self, provider: str | None, *, user_id: str | None = None) -> None:
        if self._deployment is None:
            return
        provider = provider or self._deployment.default.provider
        if provider in self.managed_providers():
            profile_context(user_id)
            raise HTTPException(
                status_code=422, detail="Choose a sandbox profile for this provider"
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
        if profile["provider"] in self.managed_providers():
            raise HTTPException(
                status_code=403, detail="Sandbox profiles are managed by the provider"
            )
        profile["enabled"] = False
        profile["revision"] = _revision(profile)
        return self._store.put(profile)

    def resolve(
        self,
        profile_id: str,
        expected_revision: str | None = None,
        provider: str | None = None,
        *,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        selected_provider = provider or (
            self._deployment.default.provider if self._deployment else None
        )
        if selected_provider in self.managed_providers():
            assert selected_provider is not None
            context = profile_context(user_id)
            profiles = self._provider_profiles(selected_provider, user_id)
            selected = next((p for p in profiles if p["id"] == profile_id), None)
            if selected is None:
                raise HTTPException(status_code=403, detail="Sandbox profile access denied")
            if expected_revision is not None and selected["revision"] != expected_revision:
                raise HTTPException(
                    status_code=409, detail="Sandbox profile changed. Choose a profile again."
                )
            snapshot = ProviderProfileSnapshot.model_validate(
                {
                    **selected,
                    "user_id": context.user_id,
                    "workspace_id": context.workspace_id,
                }
            ).model_dump()
            return self.validate_snapshot(snapshot)
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
        if snapshot.get("managed") is True:
            try:
                saved_reference = ProviderProfileSnapshot.model_validate(snapshot).model_dump()
                encode_snapshot(saved_reference)
            except ValueError as exc:
                raise _invalid_config(exc) from exc
            entry = (
                self._deployment.for_provider(saved_reference["provider"])
                if self._deployment
                else None
            )
            if entry is None:
                raise HTTPException(status_code=409, detail="Sandbox profile provider unavailable")
            launcher = provider_call(
                lambda: build_owner_launcher(entry, self._deployment, saved_reference["user_id"])
            )
            select_provider_profile(launcher, saved_reference)
            return saved_reference
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

    def inherit_snapshot(self, snapshot: dict[str, Any], user_id: str | None) -> dict[str, Any]:
        if snapshot.get("managed") is True:
            context = profile_context(user_id)
            snapshot = {**snapshot, "user_id": context.user_id}
        return self.validate_snapshot(snapshot)

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

    def require_session_host(
        self,
        session_id: str,
        host_id: str,
        *,
        bind: bool = True,
        sandbox_provider: str | None = None,
        user_id: str | None = None,
    ) -> None:
        """After host ownership checks, inherit or match the host's accepted profile."""
        host_snapshot = self.get_host_snapshot(host_id)
        session_snapshot = self.get_session_snapshot(session_id)
        if host_snapshot is not None:
            self.validate_snapshot(host_snapshot)
            if (
                host_snapshot.get("managed") is True
                and user_id is not None
                and host_snapshot["user_id"] != user_id
            ):
                raise HTTPException(
                    status_code=403, detail="Sandbox profile execution identity mismatch"
                )
        elif sandbox_provider is not None:
            self.require_selection(sandbox_provider, user_id=user_id)
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
