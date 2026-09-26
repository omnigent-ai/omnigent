"""Gensee managed sandbox launcher.

Gensee allocates one ephemeral sandbox runtime for each managed Omnigent host.
The runtime starts an Omnigent-compatible environment and connects back to the
server; lifecycle operations travel over Gensee's HTTPS control-plane API. This
is therefore a managed-only, provider-native host launcher: it does not expose
remote exec or the ``omnigent sandbox create`` CLI bootstrap primitives.

The controller credential is read from the server process environment. Host
launch tokens and explicitly configured environment values are uploaded to the
controller's short-lived secret store instead of being embedded in operation
records.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, ClassVar
from urllib.parse import quote, unquote, urlsplit

import click
import httpx

from omnigent.onboarding.sandboxes.base import SandboxHostLauncher
from omnigent.onboarding.sandboxes.types import RepoWorkspace, SandboxCapabilities

API_TOKEN_ENV_VAR: str = "GENSEE_CONTROLLER_API_TOKEN"
"""Environment variable containing the Gensee control-plane API token."""

CONTROLLER_URL_ENV_VAR: str = "GENSEE_CONTROLLER_URL"
"""Optional default controller URL override for direct launcher use."""

DEFAULT_CONTROLLER_URL: str = "https://sandbox.gensee.ai"
DEFAULT_WORKSPACE_ROOT: str = "/mnt/gensee-tclone/workspaces"
DEFAULT_OPERATION_TIMEOUT_S: int = 900
DEFAULT_POLL_INTERVAL_S: int = 2
DEFAULT_REQUEST_TIMEOUT_S: int = 40
DEFAULT_RETRY_TIMEOUT_S: int = 60
MANAGED_TOKEN_TTL_S: int = 7 * 24 * 3600

_SCHEME = "gensee+controller"
_TRANSIENT_HTTP_STATUS = frozenset({500, 502, 503, 504})
_TERMINAL_OPERATION_STATES = frozenset({"succeeded", "failed", "cancelled"})
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_BACKEND_NAME_RE = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?")
_MAX_ERROR_DETAIL_CHARS = 4096
_LOG = logging.getLogger(__name__)


class GenseeControlError(click.ClickException):
    """A Gensee control-plane request failed."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class _OperationDeadline:
    operation_id: str
    expires_at: float

    def remaining(self) -> float:
        remaining = self.expires_at - time.monotonic()
        if remaining <= 0:
            raise GenseeControlError(
                f"Gensee controller operation {self.operation_id} timed out"
            ) from None
        return remaining


class GenseeSandboxLauncher(SandboxHostLauncher):
    """Launch one ephemeral Gensee runtime per managed Omnigent host."""

    provider: ClassVar[str] = "gensee"

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        api_token_env: str = API_TOKEN_ENV_VAR,
        workspace_root: str = DEFAULT_WORKSPACE_ROOT,
        operation_timeout_s: float = DEFAULT_OPERATION_TIMEOUT_S,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S,
        retry_timeout_s: float = DEFAULT_RETRY_TIMEOUT_S,
        env: Sequence[str] | None = None,
        _transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.endpoint = _normalize_endpoint(
            endpoint or os.environ.get(CONTROLLER_URL_ENV_VAR, DEFAULT_CONTROLLER_URL)
        )
        self.api_token_env = _validate_env_name(api_token_env, field="api_token_env")
        self.workspace_root = _validate_workspace_root(workspace_root)
        self.operation_timeout_s = _positive_timeout(
            operation_timeout_s, field="operation_timeout_s"
        )
        self.poll_interval_s = _positive_timeout(poll_interval_s, field="poll_interval_s")
        self.request_timeout_s = _positive_timeout(request_timeout_s, field="request_timeout_s")
        self.retry_timeout_s = _nonnegative_timeout(retry_timeout_s, field="retry_timeout_s")
        self.env = _validate_env_names(env or ())
        self._transport = _transport
        self._secret_values: set[str] = set()

    @property
    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            managed_launch=True,
            programmatic_terminate=True,
        )

    def prepare(self) -> None:
        """Verify local credentials and control-plane readiness."""
        self._api_token()
        response = self._request("GET", "/readyz", authenticated=False)
        if response.get("ok") is not True:
            raise GenseeControlError("Gensee controller is not ready")

    def provision(self, name: str) -> str:
        """Allocate one ephemeral sandbox and return its opaque ID."""
        allocation_id = _allocation_name(name)
        try:
            response = self._request(
                "POST",
                "/v1/sandbox-allocations:allocate",
                {
                    "protocol_version": 1,
                    "user_id": allocation_id,
                    "allocation_id": allocation_id,
                    "idempotency_key": f"allocate-{allocation_id}",
                },
            )
            allocation = _mapping(response, "allocation")
            returned_id = _string(allocation, "allocation_id")
            if returned_id != allocation_id:
                raise GenseeControlError("Gensee controller returned an unexpected allocation ID")
            return _sandbox_id(returned_id)
        except (GenseeControlError, ValueError) as error:
            # Even a rejected retry can follow a creation whose response was lost.
            detail = (
                error.message
                if isinstance(error, GenseeControlError)
                else "Gensee controller returned an invalid allocation ID"
            )
            cleanup_detail = ""
            try:
                self._release(allocation_id)
            except GenseeControlError as cleanup_error:
                cleanup_detail = f"; cleanup failed: {cleanup_error.message}"
                _LOG.warning(
                    "Gensee allocation cleanup failed allocation_id=%s: %s",
                    allocation_id,
                    cleanup_error.message,
                )
            raise GenseeControlError(
                f"Gensee allocation {allocation_id} failed: {detail}{cleanup_detail}"
            ) from None

    def start_host(
        self,
        sandbox_id: str,
        *,
        token: str,
        host_id: str,
        host_name: str,
        server_url: str,
        repos: Sequence[RepoWorkspace] = (),
        host_config: dict[str, object] | None = None,
        on_stage: Callable[[str], None] | None = None,
    ) -> str:
        """Ask the sandbox agent to materialize a workspace and start its host."""
        if len(repos) > 1:
            raise GenseeControlError(
                "The 'gensee' provider supports at most one repository per session"
            )
        repo = repos[0] if repos else None
        if on_stage is not None:
            on_stage("cloning" if repo is not None else "starting")

        allocation_id = _allocation_id(sandbox_id)
        secret_id = f"secret-{secrets.token_hex(16)}"
        missing_env = [name for name in self.env if name not in os.environ]
        if missing_env:
            names = ", ".join(missing_env)
            raise GenseeControlError(
                f"configured Gensee environment variables are not set: {names}"
            )
        environment = {name: os.environ[name] for name in self.env}
        self._secret_values.update((token, *environment.values()))
        self._request(
            "POST",
            "/v1/sandbox-secrets:put",
            {
                "protocol_version": 1,
                "allocation_id": allocation_id,
                "secret_id": secret_id,
                "token": json.dumps(
                    {
                        "version": 1,
                        "host_token": token,
                        "environment": environment,
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            },
        )
        operation = self._submit_and_wait(
            allocation_id,
            "start_omnigent_host",
            {
                "secret_id": secret_id,
                "workspace": str(self.workspace_root / allocation_id),
                "host_id": host_id,
                "host_name": host_name,
                "server_url": server_url,
                "repo_url": repo.url if repo is not None else None,
                "repo_branch": repo.branch if repo is not None else None,
                "repo_name": repo.repo_name if repo is not None else None,
                "host_config": host_config,
            },
        )
        if repo is not None and on_stage is not None:
            on_stage("starting")
        response = _mapping(operation, "response")
        result = _mapping(response, "result")
        runtime = _mapping(result, "runtime")
        return _string(runtime, "workspace")

    def is_running(self, sandbox_id: str) -> bool | None:
        """Return runtime-agent readiness, preserving transitional states as unknown."""
        allocation_id = _allocation_id(sandbox_id)
        response = self._request(
            "GET",
            f"/v1/sandbox-allocations/{quote(allocation_id, safe='')}",
        )
        allocation = _mapping(response, "allocation")
        if allocation.get("state") != "active":
            return False
        runtime = _mapping(allocation, "runtime")
        observed = runtime.get("observed_state")
        if observed == "running" and runtime.get("ready") is True:
            return True
        if observed in {"absent", "deleting", "deleted", "stopped", "stopping"}:
            return False
        return None

    def terminate(self, sandbox_id: str) -> None:
        """Release an allocation; the controller treats repeats as success."""
        self._release(_allocation_id(sandbox_id))

    def _release(self, allocation_id: str) -> None:
        try:
            self._request(
                "POST",
                "/v1/sandbox-allocations:release",
                {
                    "protocol_version": 1,
                    "allocation_id": allocation_id,
                    "idempotency_key": f"release-{allocation_id}",
                },
            )
        except GenseeControlError as error:
            if error.status_code != 404:
                raise

    def _submit_and_wait(
        self,
        allocation_id: str,
        action: str,
        parameters: dict[str, object],
    ) -> dict[str, Any]:
        operation_id = f"operation-{secrets.token_hex(16)}"
        started_at = time.monotonic()
        deadline = _OperationDeadline(operation_id, started_at + self.operation_timeout_s)
        response = self._request(
            "POST",
            "/v1/sandbox-operations:submit",
            {
                "protocol_version": 1,
                "allocation_id": allocation_id,
                "operation_id": operation_id,
                "idempotency_key": operation_id,
                "action": action,
                "parameters": parameters,
            },
            deadline=deadline,
        )
        operation = _mapping(response, "operation")
        previous_state: object = None
        while operation.get("state") not in _TERMINAL_OPERATION_STATES:
            state = operation.get("state")
            if state != previous_state:
                _LOG.info(
                    "Gensee operation state operation_id=%s allocation_id=%s "
                    "action=%s state=%s elapsed_ms=%d",
                    operation_id,
                    allocation_id,
                    action,
                    self._safe_error_detail(str(state)),
                    round((time.monotonic() - started_at) * 1000),
                )
                previous_state = state
            time.sleep(min(self.poll_interval_s, deadline.remaining()))
            response = self._request(
                "GET",
                f"/v1/sandbox-operations/{quote(operation_id, safe='')}",
                deadline=deadline,
            )
            operation = _mapping(response, "operation")
        deadline.remaining()
        if operation.get("state") != "succeeded":
            response_value = operation.get("response")
            error = response_value.get("error") if isinstance(response_value, dict) else None
            detail = self._public_error_message(error)
            raise GenseeControlError(
                f"Gensee controller operation {operation_id} failed"
                + (f": {detail}" if detail else "")
            )
        return operation

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None = None,
        *,
        authenticated: bool = True,
        deadline: _OperationDeadline | None = None,
    ) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if authenticated:
            headers["Authorization"] = f"Bearer {self._api_token()}"
        retry_deadline = time.monotonic() + self.retry_timeout_s
        retry_delay = 1.0
        retrying = False
        while True:
            timeout = self.request_timeout_s
            if deadline is not None:
                timeout = min(timeout, deadline.remaining())
            if retrying:
                retry_remaining = retry_deadline - time.monotonic()
                if retry_remaining <= 0:
                    raise GenseeControlError("Gensee controller retry budget exhausted")
                timeout = min(timeout, retry_remaining)
            try:
                with httpx.Client(
                    base_url=f"{self.endpoint}/",
                    timeout=timeout,
                    transport=self._transport,
                ) as client:
                    response = client.request(
                        method,
                        path.lstrip("/"),
                        json=payload,
                        headers=headers,
                    )
            except (httpx.HTTPError, OSError) as error:
                if self._wait_to_retry(retry_deadline, retry_delay, deadline):
                    retry_delay = min(retry_delay * 2, 10)
                    retrying = True
                    continue
                detail = self._safe_error_detail(str(error)) or type(error).__name__
                raise GenseeControlError(f"Gensee controller request failed: {detail}") from None

            if deadline is not None:
                deadline.remaining()
            if response.status_code in _TRANSIENT_HTTP_STATUS and self._wait_to_retry(
                retry_deadline, retry_delay, deadline
            ):
                retry_delay = min(retry_delay * 2, 10)
                retrying = True
                continue
            if response.is_error:
                try:
                    body = response.json()
                except (UnicodeDecodeError, json.JSONDecodeError):
                    body = None
                detail = self._public_error_message(
                    body.get("error") if isinstance(body, dict) else None
                )
                raise GenseeControlError(
                    f"Gensee controller returned HTTP {response.status_code}"
                    + (f": {detail}" if detail else ""),
                    status_code=response.status_code,
                )
            try:
                value = response.json()
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise GenseeControlError("Gensee controller returned invalid JSON") from None
            if not isinstance(value, dict):
                raise GenseeControlError("Gensee controller response must be an object")
            if deadline is not None:
                deadline.remaining()
            return value

    def _public_error_message(self, error: object) -> str:
        if not isinstance(error, dict) or not isinstance(error.get("message"), str):
            return ""
        return self._safe_error_detail(error["message"])

    def _safe_error_detail(self, message: str) -> str:
        variants: set[str] = set()
        for secret in self._secret_values:
            if secret:
                variants.add(secret)
                # Error messages can contain JSON nested inside another JSON string.
                for ensure_ascii in (True, False):
                    escaped = json.dumps(secret, ensure_ascii=ensure_ascii)[1:-1]
                    variants.add(escaped)
                    variants.add(json.dumps(escaped, ensure_ascii=ensure_ascii)[1:-1])
        for secret in sorted(variants, key=len, reverse=True):
            message = message.replace(secret, "[redacted]")
        return message[:_MAX_ERROR_DETAIL_CHARS]

    def _api_token(self) -> str:
        token = os.environ.get(self.api_token_env)
        if token is None or not token.strip():
            raise GenseeControlError(
                f"environment variable {self.api_token_env!r} must contain the "
                "Gensee controller API token"
            )
        self._secret_values.add(token.strip())
        return token.strip()

    @staticmethod
    def _wait_to_retry(
        retry_deadline: float, delay: float, operation_deadline: _OperationDeadline | None = None
    ) -> bool:
        remaining = retry_deadline - time.monotonic()
        if operation_deadline is not None:
            remaining = min(remaining, operation_deadline.remaining())
        if remaining <= 0:
            return False
        time.sleep(min(delay, remaining))
        if operation_deadline is not None:
            operation_deadline.remaining()
        return time.monotonic() < retry_deadline


def _normalize_endpoint(value: str) -> str:
    endpoint = value.strip().rstrip("/")
    try:
        parsed = urlsplit(endpoint)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise ValueError(f"invalid Gensee controller endpoint: {error}") from error
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Gensee controller endpoint must be an HTTPS URL without "
            "credentials, a query, or a fragment"
        )
    if port is not None and not 1 <= port <= 65535:
        raise ValueError("Gensee controller endpoint port is out of range")
    return endpoint


def _validate_env_name(value: str, *, field: str) -> str:
    if not _ENV_NAME_RE.fullmatch(value):
        raise ValueError(f"Gensee {field} must be an environment variable name")
    return value


def _validate_env_names(values: Sequence[str]) -> tuple[str, ...]:
    result = tuple(_validate_env_name(value, field="env entry") for value in values)
    if len(result) != len(set(result)):
        raise ValueError("Gensee env must not contain duplicate names")
    return result


def _validate_workspace_root(value: str) -> PurePosixPath:
    if "\x00" in value:
        raise ValueError("Gensee workspace_root must not contain NUL bytes")
    path = PurePosixPath(value)
    if not path.is_absolute() or path == PurePosixPath("/") or ".." in path.parts:
        raise ValueError("Gensee workspace_root must be an absolute non-root path")
    return path


def _positive_timeout(value: float, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ValueError(f"Gensee {field} must be a positive number")
    return float(value)


def _nonnegative_timeout(value: float, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise ValueError(f"Gensee {field} must be a non-negative number")
    return float(value)


def _allocation_name(name: str) -> str:
    prefix = "".join(
        character.lower() if character.isascii() and character.isalnum() else "-"
        for character in name
    )
    prefix = prefix.strip("-")[:20] or "omnigent"
    return f"{prefix}-{secrets.token_hex(6)}"


def _sandbox_id(allocation_id: str) -> str:
    _validate_backend_identifier(allocation_id, kind="allocation")
    return f"{_SCHEME}:///{quote(allocation_id, safe='')}"


def _allocation_id(sandbox_id: str) -> str:
    parsed = urlsplit(sandbox_id)
    if parsed.scheme != _SCHEME or parsed.netloc or parsed.query or parsed.fragment:
        raise GenseeControlError(f"invalid Gensee sandbox ID: {sandbox_id!r}")
    allocation_id = unquote(parsed.path.removeprefix("/"))
    try:
        _validate_backend_identifier(allocation_id, kind="allocation")
    except ValueError as error:
        raise GenseeControlError(f"invalid Gensee sandbox ID: {sandbox_id!r}") from error
    return allocation_id


def _validate_backend_identifier(value: str, *, kind: str) -> None:
    if not _BACKEND_NAME_RE.fullmatch(value):
        raise ValueError(f"invalid Gensee {kind} ID")


def _mapping(value: dict[str, Any], key: str) -> dict[str, Any]:
    result = value.get(key)
    if not isinstance(result, dict):
        raise GenseeControlError(f"Gensee controller response is missing object {key!r}")
    return result


def _string(value: dict[str, Any], key: str) -> str:
    result = value.get(key)
    if not isinstance(result, str) or not result:
        raise GenseeControlError(f"Gensee controller response is missing string {key!r}")
    return result
