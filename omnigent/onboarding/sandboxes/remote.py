"""Managed sandbox launcher backed by an external runtime controller."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping, Sequence
from typing import ClassVar
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import click

from omnigent.onboarding.sandboxes.base import RemoteCommandResult, SandboxLauncher

DEFAULT_TOKEN_ENV = "OMNIGENT_REMOTE_SANDBOX_TOKEN"
_RESUME_TIMEOUT_S = 15 * 60
_RESUME_POLL_INTERVAL_S = 2
_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_ERROR_BYTES = 4096
_RETRYABLE_STATUS_CODES = frozenset({429, 502, 503, 504})
_RETRY_DELAYS_S = (0.25, 1.0)


class RemoteSandboxLauncher(SandboxLauncher):
    """Delegate managed sandbox primitives to a versioned HTTP control plane."""

    provider: ClassVar[str] = "remote"
    supports_cli_bootstrap: ClassVar[bool] = False
    can_resume: ClassVar[bool] = True

    def __init__(
        self,
        *,
        url: str,
        token_env: str | None = None,
        env: Sequence[str] | None = None,
    ) -> None:
        self._url = url.rstrip("/")
        self._token_env = token_env or DEFAULT_TOKEN_ENV
        self._env_names = tuple(env or ())
        self._owner: str | None = None
        self._session_id: str | None = None

    def set_launch_context(self, *, owner: str, session_id: str | None) -> None:
        self._owner = owner
        self._session_id = session_id

    def prepare(self) -> None:
        if not self._url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
            raise click.ClickException(
                "remote sandbox controller URL must use HTTPS (or localhost for development)"
            )
        if not os.environ.get(self._token_env):
            raise click.ClickException(
                f"remote sandbox controller token is not set in {self._token_env}"
            )
        if self._owner is None or self._session_id is None:
            raise click.ClickException("remote sandbox launch context is incomplete")

    def provision(self, name: str) -> str:
        self.prepare()
        env: dict[str, str] = {}
        for env_name in self._env_names:
            value = os.environ.get(env_name)
            if value is None:
                raise click.ClickException(
                    f"sandbox.remote.env names '{env_name}' but it is not set"
                )
            env[env_name] = value
        body = self._request(
            "POST",
            "/api/v1/sandbox-runtimes",
            {
                "name": name,
                "owner": self._owner,
                "sessionId": self._session_id,
                "env": env,
            },
            timeout=15 * 60,
            retryable=True,
        )
        runtime = self._mapping(body.get("runtime"), "runtime")
        runtime_id = runtime.get("id")
        if not isinstance(runtime_id, str) or not runtime_id:
            raise click.ClickException("remote sandbox controller returned no runtime id")
        return runtime_id

    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        self._ensure_running(sandbox_id)
        body = self._request(
            "POST",
            f"/api/v1/sandbox-runtimes/{sandbox_id}/commands",
            {"command": command, "timeoutSeconds": 15 * 60},
            timeout=16 * 60,
        )
        result = self._mapping(body.get("result"), "command result")
        exit_code = result.get("exitCode")
        if exit_code is not None and not isinstance(exit_code, int):
            raise click.ClickException("remote sandbox controller returned an invalid exit code")
        stdout = result.get("stdout", "")
        stderr = result.get("stderr", "")
        if not isinstance(stdout, str) or not isinstance(stderr, str):
            raise click.ClickException("remote sandbox controller returned invalid command output")
        completed = RemoteCommandResult(returncode=exit_code or 0, stdout=stdout, stderr=stderr)
        if check and completed.returncode != 0:
            detail = stderr.strip() or stdout.strip() or "no output"
            raise click.ClickException(
                f"remote command failed in sandbox '{sandbox_id}' "
                f"(exit {completed.returncode}): {detail}"
            )
        return completed

    def terminate(self, sandbox_id: str) -> None:
        self._request("DELETE", f"/api/v1/sandbox-runtimes/{sandbox_id}", retryable=True)

    def set_activity(self, sandbox_id: str, *, active: bool) -> None:
        self._request(
            "POST",
            f"/api/v1/sandbox-runtimes/{sandbox_id}/activity",
            {"active": active},
            timeout=30,
            retryable=True,
        )

    def resume(self, sandbox_id: str) -> None:
        self._request(
            "POST",
            f"/api/v1/sandbox-runtimes/{sandbox_id}/resume",
            timeout=30,
            retryable=True,
        )
        deadline = time.monotonic() + _RESUME_TIMEOUT_S
        while time.monotonic() < deadline:
            runtime = self._runtime(sandbox_id)
            if runtime is None:
                raise click.ClickException(
                    f"remote sandbox runtime '{sandbox_id}' disappeared while waking"
                )
            state = runtime.get("state")
            if state == "running":
                return
            if state in {"deleted", "error"}:
                raise click.ClickException(
                    f"remote sandbox runtime '{sandbox_id}' could not wake (state: {state})"
                )
            time.sleep(_RESUME_POLL_INTERVAL_S)
        raise click.ClickException(
            f"remote sandbox runtime '{sandbox_id}' did not wake within 15 minutes"
        )

    def is_running(self, sandbox_id: str) -> bool | None:
        runtime = self._runtime(sandbox_id)
        return None if runtime is None else runtime.get("state") == "running"

    def exists(self, sandbox_id: str) -> bool | None:
        runtime = self._runtime(sandbox_id)
        return runtime is not None and runtime.get("state") != "deleted"

    def _ensure_running(self, sandbox_id: str) -> None:
        runtime = self._runtime(sandbox_id)
        if runtime is None:
            raise click.ClickException(f"remote sandbox runtime '{sandbox_id}' was not found")
        if runtime.get("state") != "running":
            self.resume(sandbox_id)

    def _runtime(self, sandbox_id: str) -> Mapping[str, object] | None:
        try:
            body = self._request("GET", f"/api/v1/sandbox-runtimes/{sandbox_id}", retryable=True)
        except click.ClickException as exc:
            if "(404)" in exc.message:
                return None
            raise
        return self._mapping(body.get("runtime"), "runtime")

    def _request(
        self,
        method: str,
        path: str,
        body: Mapping[str, object] | None = None,
        *,
        timeout: int = 90,
        retryable: bool = False,
    ) -> Mapping[str, object]:
        token = os.environ.get(self._token_env)
        if not token:
            raise click.ClickException(
                f"remote sandbox controller token is not set in {self._token_env}"
            )
        data = json.dumps(body).encode() if body is not None else None
        request = Request(
            f"{self._url}{path}",
            method=method,
            data=data,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "X-Sandbox-Runtime-API-Version": "1",
            },
        )
        attempts = len(_RETRY_DELAYS_S) + 1 if retryable else 1
        raw = b""
        for attempt in range(attempts):
            try:
                with urlopen(request, timeout=timeout) as response:
                    raw = self._read_bounded(response, _MAX_RESPONSE_BYTES)
                break
            except HTTPError as exc:
                if exc.code in _RETRYABLE_STATUS_CODES and attempt + 1 < attempts:
                    time.sleep(_RETRY_DELAYS_S[attempt])
                    continue
                detail = self._read_bounded(exc, _MAX_ERROR_BYTES).decode(
                    "utf-8", errors="replace"
                )
                raise click.ClickException(
                    f"remote sandbox controller request failed ({exc.code}): {detail}"
                ) from exc
            except (URLError, TimeoutError, OSError) as exc:
                if attempt + 1 < attempts:
                    time.sleep(_RETRY_DELAYS_S[attempt])
                    continue
                raise click.ClickException(
                    f"remote sandbox controller is unavailable: {exc}"
                ) from exc
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except ValueError as exc:
            raise click.ClickException("remote sandbox controller returned invalid JSON") from exc
        return self._mapping(value, "response")

    @staticmethod
    def _read_bounded(response: object, limit: int) -> bytes:
        read = getattr(response, "read", None)
        if not callable(read):
            raise click.ClickException("remote sandbox controller returned an invalid response")
        raw = read(limit + 1)
        if len(raw) > limit:
            raise click.ClickException(
                f"remote sandbox controller response exceeded {limit} bytes"
            )
        return raw

    @staticmethod
    def _mapping(value: object, name: str) -> Mapping[str, object]:
        if not isinstance(value, dict):
            raise click.ClickException(f"remote sandbox controller returned an invalid {name}")
        return value
