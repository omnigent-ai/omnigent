"""Managed-host launcher for a pre-provisioned CoDA Databricks App."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Mapping
from http.client import HTTPMessage
from typing import IO, ClassVar
from urllib import error, request
from urllib.parse import urlsplit

import click

from omnigent.onboarding.sandboxes.base import SandboxHostLauncher
from omnigent.onboarding.sandboxes.types import SandboxCapabilities

CODA_WORKSPACE_PATH = "/app/python/source_code"
_CODA_APPS_HOST_SUFFIX = ".databricksapps.com"
_CONTROL_ERROR_MAX_BYTES = 8192


class _NoRedirectHandler(request.HTTPRedirectHandler):
    """Turn upstream redirects into errors instead of following them."""

    def redirect_request(
        self,
        req: request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> request.Request | None:
        del newurl
        raise error.HTTPError(req.full_url, code, "redirects disabled", headers, fp)


_NO_REDIRECT_OPENER = request.build_opener(_NoRedirectHandler)


def validate_coda_app_url(app_url: str) -> str:
    """Validate and return a trusted CoDA Databricks Apps URL."""
    if not isinstance(app_url, str) or not app_url.strip():
        raise ValueError("CoDA app_url must be a non-empty HTTPS Databricks Apps URL")
    candidate = app_url.strip()
    try:
        parsed = urlsplit(candidate)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(f"CoDA app_url is malformed: {exc}") from exc
    if parsed.scheme != "https" or hostname is None:
        raise ValueError("CoDA app_url must use HTTPS and include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("CoDA app_url must not contain userinfo")
    if ":" in parsed.netloc.rsplit("@", 1)[-1]:
        raise ValueError("CoDA app_url must not contain an explicit port")
    if parsed.query or parsed.fragment:
        raise ValueError("CoDA app_url must not contain a query or fragment")
    if parsed.path not in ("", "/"):
        raise ValueError("CoDA app_url may contain only an empty or root path")
    hostname = hostname.lower()
    prefix = hostname[: -len(_CODA_APPS_HOST_SUFFIX)]
    if not hostname.endswith(_CODA_APPS_HOST_SUFFIX) or not prefix:
        raise ValueError("CoDA app_url hostname must have a subdomain under .databricksapps.com")
    if any(not label for label in prefix.split(".")):
        raise ValueError("CoDA app_url hostname contains an empty subdomain label")
    return candidate


def _safe_control_error_detail(_raw: object) -> str:
    """Return a fixed error without exposing any upstream response body."""
    return "upstream control request failed"


def _parse_sandbox_id(sandbox_id: str) -> tuple[str, str]:
    """Return the app and lease identifiers from a CoDA sandbox id."""
    if not sandbox_id.startswith("coda:") or "#" not in sandbox_id:
        raise click.ClickException(f"invalid CoDA sandbox id: {sandbox_id!r}")
    app_name, lease_id = sandbox_id[5:].rsplit("#", 1)
    if not app_name or not lease_id:
        raise click.ClickException(f"invalid CoDA sandbox id: {sandbox_id!r}")
    return app_name, lease_id


class CodaProvider(SandboxHostLauncher):
    """Lease and control one already-running CoDA Databricks App over HTTP."""

    provider: ClassVar[str] = "coda"

    @property
    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            cli_bootstrap=False,
            managed_launch=True,
            local_port_forward=False,
            resume_stopped=False,
            programmatic_terminate=True,
        )

    def __init__(
        self,
        *,
        app_name: str,
        app_url: str,
        workspace_path: str = CODA_WORKSPACE_PATH,
        request_fn: Callable[[str, str, Mapping[str, object] | None], Mapping[str, object]]
        | None = None,
        app_getter: Callable[[str], object] | None = None,
    ) -> None:
        self._app_name = app_name
        self._app_url = validate_coda_app_url(app_url).rstrip("/")
        self._workspace_path = workspace_path
        self._request_fn = request_fn or self._request
        self._app_getter = app_getter or self._get_app

    @staticmethod
    def _get_app(app_name: str) -> object:
        """Resolve the configured App lazily so the SDK stays optional."""
        from databricks.sdk import WorkspaceClient

        return WorkspaceClient().apps.get(app_name)

    def _request(
        self, method: str, path: str, body: Mapping[str, object] | None
    ) -> Mapping[str, object]:
        """Make an authenticated control request without exposing raw bodies."""
        from databricks.sdk.core import Config

        headers = dict(Config().authenticate())
        data = json.dumps(body).encode() if body is not None else None
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = request.Request(f"{self._app_url}{path}", data=data, headers=headers, method=method)
        try:
            with _NO_REDIRECT_OPENER.open(req, timeout=30) as response:
                payload = response.read()
        except error.HTTPError as exc:
            raw_detail = exc.read(_CONTROL_ERROR_MAX_BYTES).decode(errors="replace")
            if exc.code == 409:
                raise click.ClickException("CoDA app has no available lease capacity") from exc
            detail = _safe_control_error_detail(raw_detail)
            raise click.ClickException(
                f"CoDA control request failed ({exc.code}): {detail}"
            ) from exc
        except error.URLError:
            raise click.ClickException(
                "CoDA control request failed: upstream unavailable"
            ) from None
        try:
            decoded = json.loads(payload or b"{}")
        except (UnicodeDecodeError, json.JSONDecodeError, TypeError):
            raise click.ClickException("CoDA control response was not valid JSON") from None
        if not isinstance(decoded, dict):
            raise click.ClickException("CoDA control response must be a JSON object")
        return decoded

    def prepare(self) -> None:
        """Validate App readiness and control-plane authentication without mutation."""
        app = self._app_getter(self._app_name)
        compute = getattr(getattr(app, "compute_status", None), "state", None)
        if str(compute).upper().split(".")[-1] != "ACTIVE":
            raise click.ClickException(f"CoDA app {self._app_name!r} compute is not ACTIVE")
        status = self._request_fn("GET", "/api/omnigent-host/status", None)
        if status.get("ready") is not True:
            raise click.ClickException(f"CoDA app {self._app_name!r} is not ready")

    def provision(self, name: str) -> str:
        """Acquire a fenced CoDA lease without creating infrastructure."""
        lease_id = uuid.uuid4().hex
        self._request_fn(
            "POST",
            "/api/omnigent-host/lease",
            {
                "action": "acquire",
                "app_name": self._app_name,
                "host_name": name,
                "lease_id": lease_id,
            },
        )
        return f"coda:{self._app_name}#{lease_id}"

    def start_host(
        self,
        sandbox_id: str,
        *,
        token: str,
        host_id: str,
        host_name: str,
        server_url: str,
        repo_url: str | None = None,
        repo_branch: str | None = None,
        repo_name: str | None = None,
        host_config: dict[str, object] | None = None,
        on_stage: Callable[[str], None] | None = None,
    ) -> str:
        """Ask CoDA to start its managed host and return its workspace."""
        app_name, lease_id = _parse_sandbox_id(sandbox_id)
        if app_name != self._app_name:
            raise click.ClickException("CoDA sandbox id targets a different app")
        if on_stage is not None:
            on_stage("cloning")
            on_stage("starting")
        result = self._request_fn(
            "POST",
            "/api/omnigent-host/connect",
            {
                "server_url": server_url,
                "host_token": token,
                "host_id": host_id,
                "host_name": host_name,
                "host_config": host_config,
                "repo_url": repo_url,
                "repo_branch": repo_branch,
                "repo_name": repo_name,
                "lease_id": lease_id,
            },
        )
        workspace = result.get("workspace") or self._workspace_path
        if not isinstance(workspace, str) or not workspace.startswith("/"):
            raise click.ClickException(
                "CoDA connect response did not contain an absolute workspace"
            )
        return workspace

    def terminate(self, sandbox_id: str) -> None:
        """Release and scrub the lease; never mutate the Databricks App."""
        app_name, lease_id = _parse_sandbox_id(sandbox_id)
        if app_name != self._app_name:
            return
        try:
            self._request_fn(
                "POST",
                "/api/omnigent-host/disconnect",
                {"lease_id": lease_id, "scrub": True},
            )
        except click.ClickException:
            return
