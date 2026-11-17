"""Adapt an existing HTTP controller's catalog and sandbox resource APIs."""

from __future__ import annotations

import logging
import os
from contextlib import suppress
from typing import Any, ClassVar
from urllib.parse import quote, urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from omnigent.onboarding.sandboxes.base import SandboxGoneError, SandboxHostLauncher
from omnigent.onboarding.sandboxes.profiles import (
    ProviderSandboxProfile,
    SandboxProfileAccessDenied,
    SandboxProfileChanged,
    SandboxProfileContext,
)
from omnigent.onboarding.sandboxes.types import SandboxConfigError

_logger = logging.getLogger(__name__)


def _parse_profile(value: Any) -> ProviderSandboxProfile:
    try:
        return ProviderSandboxProfile.model_validate(value)
    except ValueError:
        raise SandboxConfigError("Controller returned an invalid sandbox profile") from None


class ControllerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    url: str
    token_env: str = "SANDBOX_CONTROLLER_TOKEN"
    timeout_s: float = Field(default=10.0, gt=0, le=60)

    @model_validator(mode="after")
    def validate_url(self):
        parsed = urlparse(self.url)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Controller URL must not contain credentials, query or fragment")
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1")
        ):
            raise ValueError("Use HTTPS for controllers outside localhost")
        return self


class ControllerSandboxLauncher(SandboxHostLauncher):
    """The controller owns configuration, authorization and durable assignments."""

    provider: ClassVar[str] = "controller_demo"
    manages_profiles: ClassVar[bool] = True
    supports_sandbox_profile_lookup: ClassVar[bool] = True
    supports_cli_bootstrap: ClassVar[bool] = False
    can_resume: ClassVar[bool] = True

    def __init__(self, *, config: ControllerConfig) -> None:
        self.config = config
        self._context: SandboxProfileContext | None = None
        self._profile: ProviderSandboxProfile | None = None

    def _request(
        self,
        method: str,
        path: str,
        *,
        context: SandboxProfileContext | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        token = os.environ.get(self.config.token_env)
        if not token:
            raise SandboxConfigError("Configure the controller service credential")
        headers = {"Authorization": f"Bearer {token}"}
        if context is not None:
            headers.update(
                {
                    "X-Sandbox-User": context.user_id,
                    "X-Sandbox-Workspace": str(context.workspace_id),
                }
            )
        try:
            # A streamed read bounds memory even when a controller sends an oversized body.
            with (
                httpx.Client(timeout=self.config.timeout_s, follow_redirects=False) as client,
                client.stream(
                    method, self.config.url.rstrip("/") + path, headers=headers, json=body
                ) as response,
            ):
                if response.status_code == 403:
                    raise SandboxProfileAccessDenied("Controller denied sandbox access")
                if response.status_code == 409:
                    raise SandboxProfileChanged("Controller rejected the saved revision")
                if response.status_code == 404:
                    raise SandboxGoneError("The controller resource no longer exists")
                if not response.is_success:
                    raise SandboxConfigError("Controller request failed")
                payload = bytearray()
                for chunk in response.iter_bytes():
                    payload.extend(chunk)
                    if len(payload) > 1024 * 1024:
                        raise SandboxConfigError("Controller response exceeds 1 MiB")
                if not payload:
                    return {}
                return TypeAdapter(dict[str, Any]).validate_json(payload)
        except (httpx.HTTPError, ValueError):
            raise SandboxConfigError(
                "Controller request failed or returned invalid data"
            ) from None

    def list_profiles(self, context: SandboxProfileContext) -> list[ProviderSandboxProfile]:
        if context.sandbox_id is not None:
            record = self._request(
                "GET", f"/sandboxes/{quote(context.sandbox_id, safe='')}", context=context
            )
            return [_parse_profile(record.get("sandbox_profile"))]
        result = self._request("GET", "/profiles", context=context)
        try:
            profiles = TypeAdapter(list[ProviderSandboxProfile]).validate_python(
                result.get("profiles")
            )
        except ValueError:
            raise SandboxConfigError("Controller returned an invalid profile catalog") from None
        if len(profiles) > 1000:
            raise SandboxConfigError("Controller catalog exceeds 1000 profiles")
        return profiles

    def select_profile(
        self, context: SandboxProfileContext, profile: ProviderSandboxProfile
    ) -> None:
        if context.sandbox_id is not None:
            [accepted] = self.list_profiles(context)
        else:
            record = self._request(
                "GET",
                f"/profiles/{quote(profile.id, safe='')}"
                f"?revision={quote(profile.revision, safe='')}",
                context=context,
            )
            accepted = _parse_profile(record.get("profile"))
        if (accepted.id, accepted.revision) != (profile.id, profile.revision):
            raise SandboxProfileChanged("Controller returned a different profile revision")
        self._context, self._profile = context, accepted

    def prepare(self) -> None:
        if self._context is None or self._profile is None:
            raise SandboxConfigError("Select a sandbox profile before provisioning")

    def _selection(self) -> dict[str, str]:
        self.prepare()
        assert self._profile is not None
        return {"id": self._profile.id, "revision": self._profile.revision}

    def provision(self, name: str) -> str:
        result = self._request(
            "POST",
            "/sandboxes",
            context=self._context,
            body={"name": name, "sandbox_profile": self._selection()},
        )
        sandbox_id = result.get("sandbox_id")
        if not isinstance(sandbox_id, str) or not sandbox_id:
            raise SandboxConfigError("Controller returned no sandbox id")
        try:
            profile = _parse_profile(result.get("sandbox_profile"))
            assert self._profile is not None
            if (profile.id, profile.revision) != (self._profile.id, self._profile.revision):
                raise SandboxProfileChanged("Controller allocated a different profile revision")
        except (SandboxConfigError, SandboxProfileChanged):
            try:
                self.terminate(sandbox_id)
            except Exception:  # noqa: BLE001 — retain the allocation validation error
                # Keep the admission failure; record an escaped ID for manual cleanup.
                _logger.error("Controller cleanup failed for sandbox %r", sandbox_id[:256])
            raise
        return sandbox_id

    def resume(self, sandbox_id: str) -> None:
        self._request(
            "POST",
            f"/sandboxes/{quote(sandbox_id, safe='')}/resume",
            context=self._context,
            body={"sandbox_profile": self._selection()},
        )

    def start_host(
        self,
        sandbox_id: str,
        *,
        token: str,
        host_id: str,
        host_name: str,
        server_url: str,
        repos=(),
        host_config=None,
        on_stage=None,
    ) -> str:
        if on_stage is not None:
            on_stage("starting")
        # Map this payload to the controller's existing host/bootstrap operation.
        result = self._request(
            "POST",
            f"/sandboxes/{quote(sandbox_id, safe='')}/host",
            context=self._context,
            body={
                "sandbox_profile": self._selection(),
                "token": token,
                "host_id": host_id,
                "host_name": host_name,
                "server_url": server_url,
                "host_config": host_config,
                "repos": [{"url": repo.url, "branch": repo.branch} for repo in repos],
            },
        )
        workspace = result.get("workspace")
        if not isinstance(workspace, str) or not workspace.strip():
            raise SandboxConfigError("Controller returned an invalid workspace")
        return workspace

    def terminate(self, sandbox_id: str) -> None:
        with suppress(SandboxGoneError):
            self._request("DELETE", f"/sandboxes/{quote(sandbox_id, safe='')}")

    def is_running(self, sandbox_id: str) -> bool | None:
        try:
            result = self._request("GET", f"/sandboxes/{quote(sandbox_id, safe='')}")
        except SandboxGoneError:
            return False
        running = result.get("running")
        if running is not None and not isinstance(running, bool):
            raise SandboxConfigError("Controller returned an invalid running status")
        return running
