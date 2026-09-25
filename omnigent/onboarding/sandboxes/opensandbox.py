"""OpenSandbox managed-host provider.

Runs Omnigent's prebuilt host image in an OpenSandbox sandbox through the
official synchronous SDK. Provider credentials stay in the server process;
only explicitly named harness credentials are copied into child sandboxes.
"""

from __future__ import annotations

import logging
import math
import os
import shlex
import threading
import time
from collections.abc import Mapping, Sequence
from datetime import timedelta
from typing import TYPE_CHECKING, ClassVar, Protocol

import click

from omnigent.onboarding.sandboxes.base import (
    DEFAULT_HOST_IMAGE,
    ExecModelHostLauncher,
    RemoteCommandResult,
)
from omnigent.onboarding.sandboxes.types import SandboxCapabilities

if TYPE_CHECKING:
    from opensandbox import SandboxManagerSync, SandboxSync
    from opensandbox.config import ConnectionConfigSync

logger = logging.getLogger(__name__)

API_KEY_ENV_VAR = "OPEN_SANDBOX_API_KEY"
DOMAIN_ENV_VAR = "OPEN_SANDBOX_DOMAIN"
PROTOCOL_ENV_VAR = "OPEN_SANDBOX_PROTOCOL"
REQUEST_TIMEOUT_ENV_VAR = "OPEN_SANDBOX_REQUEST_TIMEOUT"
SERVER_PROXY_ENV_VAR = "OPEN_SANDBOX_USE_SERVER_PROXY"
HOST_IMAGE_ENV_VAR = "OMNIGENT_OPENSANDBOX_IMAGE"
SNAPSHOT_ID_ENV_VAR = "OMNIGENT_OPENSANDBOX_SNAPSHOT_ID"
SANDBOX_ENV_PASSTHROUGH_ENV_VAR = "OMNIGENT_OPENSANDBOX_SANDBOX_ENV"
MAX_LIFETIME_ENV_VAR = "OMNIGENT_OPENSANDBOX_MAX_LIFETIME_S"
READY_TIMEOUT_ENV_VAR = "OMNIGENT_OPENSANDBOX_READY_TIMEOUT_S"

_DEFAULT_MAX_LIFETIME_S = 24 * 60 * 60
_DEFAULT_READY_TIMEOUT_S = 300
_DEFAULT_REQUEST_TIMEOUT_S = 30
_MIN_LIFETIME_S = 60
_TOKEN_TTL_SLACK_S = 3600
_HEALTH_POLL_S = 1
_DELETE_TIMEOUT_S = 30
_DELETE_POLL_S = 1
_MAX_COMMAND_OUTPUT_BYTES = 4 * 1024 * 1024
_CONTROL_CREDENTIAL_ENV_VARS = frozenset({API_KEY_ENV_VAR})


class _Closable(Protocol):
    def close(self) -> None: ...


def _ensure_sdk() -> None:
    """Require the optional OpenSandbox SDK with an actionable install hint."""
    try:
        import opensandbox  # noqa: F401
    except ImportError as exc:
        raise click.ClickException(
            "The OpenSandbox SDK is required for the 'opensandbox' sandbox provider. "
            "Install it with `pip install 'omnigent[opensandbox]'`, then set "
            "OPEN_SANDBOX_DOMAIN and OPEN_SANDBOX_API_KEY."
        ) from exc


def _close_quietly(
    resource: _Closable,
    *,
    description: str,
    secret_values: Sequence[str] = (),
) -> None:
    try:
        resource.close()
    except Exception as exc:
        logger.warning(
            "Could not close %s: %s",
            description,
            _safe_provider_error(exc, secret_values),
        )


def _positive_seconds(value: object, *, name: str, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise click.ClickException(f"{name} must be a positive number; got {value!r}.")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise click.ClickException(f"{name} must be a positive number; got {value!r}.") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise click.ClickException(f"{name} must be a positive finite number; got {value!r}.")
    return parsed


def _env_seconds(name: str, default: float) -> float:
    return _positive_seconds(os.environ.get(name), name=name, default=default)


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise click.ClickException(
        f"{name} must be one of true/false, 1/0, yes/no, or on/off; got {value!r}."
    )


def resolve_max_lifetime_s(configured: float | None = None) -> int:
    """Resolve the requested sandbox lifetime from config or environment."""
    raw: object = configured if configured is not None else os.environ.get(MAX_LIFETIME_ENV_VAR)
    lifetime = _positive_seconds(raw, name=MAX_LIFETIME_ENV_VAR, default=_DEFAULT_MAX_LIFETIME_S)
    if lifetime < _MIN_LIFETIME_S:
        raise click.ClickException(
            f"OpenSandbox lifetime must be at least {_MIN_LIFETIME_S} seconds."
        )
    return int(lifetime)


def managed_token_ttl_s(configured_lifetime_s: float | None = None) -> int:
    """Keep the managed launch token alive slightly longer than the sandbox."""
    return resolve_max_lifetime_s(configured_lifetime_s) + _TOKEN_TTL_SLACK_S


def _safe_provider_error(exc: Exception, secret_values: Sequence[str] = ()) -> str:
    message = str(exc) or type(exc).__name__
    values = [os.environ.get(API_KEY_ENV_VAR), *secret_values]
    for secret in values:
        if secret:
            message = message.replace(secret, "[redacted]")
    return message


def _state_name(state: object) -> str:
    return str(getattr(state, "value", state)).casefold()


def _is_not_found(exc: BaseException) -> bool:
    return getattr(exc, "status_code", None) == 404


def _join_output(messages: Sequence[object]) -> str:
    return "\n".join(
        str(text).rstrip("\r\n")
        for message in messages
        if (text := getattr(message, "text", None)) is not None
    )


def _output_size(stdout: str, stderr: str) -> int:
    return len(stdout.encode("utf-8")) + len(stderr.encode("utf-8"))


def _echo_output(text: str, *, err: bool = False) -> None:
    if text:
        click.echo(text, err=err, nl=not text.endswith("\n"))


class OpenSandboxLauncher(ExecModelHostLauncher):
    """Launch managed Omnigent hosts in OpenSandbox sandboxes."""

    provider: ClassVar[str] = "opensandbox"

    def __init__(
        self,
        *,
        connection_config: ConnectionConfigSync | None = None,
        image: str | None = None,
        snapshot_id: str | None = None,
        env: Sequence[str] | None = None,
        max_lifetime_s: float | None = None,
        ready_timeout_s: float | None = None,
        metadata: Mapping[str, str] | None = None,
    ) -> None:
        _ensure_sdk()
        from opensandbox.config import ConnectionConfigSync

        configured_image = image or os.environ.get(HOST_IMAGE_ENV_VAR) or None
        configured_snapshot = snapshot_id or os.environ.get(SNAPSHOT_ID_ENV_VAR) or None
        if configured_image is not None and configured_snapshot is not None:
            raise click.ClickException(
                "Configure only one of sandbox.opensandbox.image and "
                "sandbox.opensandbox.snapshot_id."
            )

        self._connection_config = connection_config or ConnectionConfigSync(
            api_key=os.environ.get(API_KEY_ENV_VAR),
            domain=os.environ.get(DOMAIN_ENV_VAR),
            protocol=os.environ.get(PROTOCOL_ENV_VAR, "http"),
            request_timeout=timedelta(
                seconds=_env_seconds(REQUEST_TIMEOUT_ENV_VAR, _DEFAULT_REQUEST_TIMEOUT_S)
            ),
            use_server_proxy=_env_bool(SERVER_PROXY_ENV_VAR, False),
        )
        self._secret_values: set[str] = set()
        connection_api_key = getattr(self._connection_config, "api_key", None)
        if isinstance(connection_api_key, str) and connection_api_key:
            self._secret_values.add(connection_api_key)
        self._image = configured_image or (
            None if configured_snapshot is not None else DEFAULT_HOST_IMAGE
        )
        self._snapshot_id = configured_snapshot
        self._env_names = tuple(env) if env is not None else None
        self._max_lifetime_s = resolve_max_lifetime_s(max_lifetime_s)
        self._ready_timeout_s = _positive_seconds(
            ready_timeout_s
            if ready_timeout_s is not None
            else os.environ.get(READY_TIMEOUT_ENV_VAR),
            name=READY_TIMEOUT_ENV_VAR,
            default=_DEFAULT_READY_TIMEOUT_S,
        )
        self._metadata = dict(metadata or {})
        self._clients: dict[str, SandboxSync] = {}
        self._lock = threading.RLock()

    @property
    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            cli_bootstrap=False,
            managed_launch=True,
            local_port_forward=False,
            resume_stopped=False,
            programmatic_terminate=True,
            file_copy=False,
            streaming_exec=False,
            foreground_exec=False,
        )

    def _resolve_sandbox_env(self) -> dict[str, str]:
        names: Sequence[str]
        if self._env_names is not None:
            names = self._env_names
        else:
            names = tuple(
                name.strip()
                for name in os.environ.get(SANDBOX_ENV_PASSTHROUGH_ENV_VAR, "").split(",")
                if name.strip()
            )
        resolved: dict[str, str] = {}
        for name in names:
            if name in _CONTROL_CREDENTIAL_ENV_VARS:
                raise click.ClickException(
                    f"sandbox env passthrough must not inject OpenSandbox control "
                    f"credential {name}; keep it in the Omnigent server process only."
                )
            value = os.environ.get(name)
            if value is None:
                raise click.ClickException(
                    f"sandbox env passthrough names {name!r} but it is not set in the "
                    "server environment — set it or remove it from "
                    f"sandbox.opensandbox.env / {SANDBOX_ENV_PASSTHROUGH_ENV_VAR}."
                )
            self._secret_values.add(value)
            resolved[name] = value
        return resolved

    def _remember(self, client: SandboxSync) -> SandboxSync:
        with self._lock:
            existing = self._clients.get(str(client.id))
            if existing is None:
                self._clients[str(client.id)] = client
                return client
        if existing is not client:
            _close_quietly(
                client,
                description=f"duplicate sandbox client {client.id}",
                secret_values=tuple(self._secret_values),
            )
        return existing

    def _resolve(self, sandbox_id: str) -> SandboxSync:
        from opensandbox import SandboxSync

        with self._lock:
            cached = self._clients.get(sandbox_id)
        if cached is not None:
            return cached
        try:
            connected = SandboxSync.connect(
                sandbox_id,
                connection_config=self._connection_config,
                connect_timeout=timedelta(seconds=self._ready_timeout_s),
                health_check_polling_interval=timedelta(seconds=_HEALTH_POLL_S),
            )
        except Exception as exc:
            raise click.ClickException(
                f"Could not connect to OpenSandbox sandbox {sandbox_id!r}: "
                f"{_safe_provider_error(exc, tuple(self._secret_values))}"
            ) from exc
        return self._remember(connected)

    def prepare(self) -> None:
        from opensandbox import SandboxManagerSync
        from opensandbox.models.sandboxes import SandboxFilter

        manager = None
        try:
            manager = SandboxManagerSync.create(connection_config=self._connection_config)
            manager.list_sandbox_infos(SandboxFilter(page=1, page_size=1))
        except Exception as exc:
            raise click.ClickException(
                "OpenSandbox preflight failed. Check OPEN_SANDBOX_DOMAIN, "
                "OPEN_SANDBOX_API_KEY, protocol, and network access. "
                f"({_safe_provider_error(exc, tuple(self._secret_values))})"
            ) from exc
        finally:
            if manager is not None:
                _close_quietly(
                    manager,
                    description="OpenSandbox preflight manager",
                    secret_values=tuple(self._secret_values),
                )

    def provision(self, name: str) -> str:
        from opensandbox import SandboxSync

        sandbox = None
        click.echo(f"▸ Creating OpenSandbox sandbox {name!r}")
        try:
            sandbox = SandboxSync.create(
                self._image,
                snapshot_id=self._snapshot_id,
                timeout=timedelta(seconds=self._max_lifetime_s),
                ready_timeout=timedelta(seconds=self._ready_timeout_s),
                env=self._resolve_sandbox_env() or None,
                metadata={
                    **self._metadata,
                    "omnigent-name": name,
                    "omnigent-provider": self.provider,
                },
                connection_config=self._connection_config,
                skip_health_check=True,
            )
            sandbox.check_ready(
                timeout=timedelta(seconds=self._ready_timeout_s),
                polling_interval=timedelta(seconds=_HEALTH_POLL_S),
            )
        except BaseException as exc:
            if sandbox is not None:
                self._destroy_after_failed_create(sandbox)
            if isinstance(exc, Exception):
                raise click.ClickException(
                    "OpenSandbox provisioning failed: "
                    f"{_safe_provider_error(exc, tuple(self._secret_values))}"
                ) from exc
            raise

        remembered = self._remember(sandbox)
        sandbox_id = str(remembered.id)
        click.echo(f"  → created {sandbox_id}")
        return sandbox_id

    def _destroy_after_failed_create(self, sandbox: SandboxSync) -> None:
        try:
            sandbox.kill()
        except Exception as exc:
            logger.warning(
                "Could not destroy OpenSandbox sandbox %s after a readiness failure: %s",
                getattr(sandbox, "id", None),
                _safe_provider_error(exc, tuple(self._secret_values)),
            )
        _close_quietly(
            sandbox,
            description=f"failed OpenSandbox client {getattr(sandbox, 'id', None)}",
            secret_values=tuple(self._secret_values),
        )

    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        from opensandbox.models.execd import RunCommandOpts

        client = self._resolve(sandbox_id)
        try:
            wrapped = f"bash -lc {shlex.quote(command)}"
            execution = client.commands.run(wrapped, opts=RunCommandOpts())
            returncode = execution.exit_code
            if returncode is None and execution.id:
                returncode = client.commands.get_command_status(execution.id).exit_code
            execution_error = execution.error
            if returncode is None:
                if execution_error is None:
                    raise RuntimeError("OpenSandbox did not report a command exit code")
                returncode = 1
            stdout = _join_output(execution.logs.stdout)
            stderr = _join_output(execution.logs.stderr)
            if execution_error is not None:
                detail = f"{execution_error.name}: {execution_error.value}"
                stderr = f"{stderr}\n{detail}" if stderr else detail
        except Exception as exc:
            raise click.ClickException(
                f"Remote command could not run on OpenSandbox sandbox {sandbox_id!r}: "
                f"{_safe_provider_error(exc, tuple(self._secret_values))}"
            ) from exc

        if _output_size(stdout, stderr) > _MAX_COMMAND_OUTPUT_BYTES:
            raise click.ClickException(
                f"Remote command output exceeded {_MAX_COMMAND_OUTPUT_BYTES} bytes."
            )
        _echo_output(stdout)
        _echo_output(stderr, err=True)
        if check and returncode != 0:
            raise click.ClickException(
                f"Remote command failed on OpenSandbox sandbox {sandbox_id!r} (exit {returncode})."
            )
        return RemoteCommandResult(returncode=returncode, stdout=stdout, stderr=stderr)

    def terminate(self, sandbox_id: str) -> None:
        from opensandbox import SandboxManagerSync

        with self._lock:
            client = self._clients.pop(sandbox_id, None)
        manager = None
        try:
            termination_error: Exception | None = None
            try:
                if client is not None:
                    client.kill()
                else:
                    manager = SandboxManagerSync.create(connection_config=self._connection_config)
                    manager.kill_sandbox(sandbox_id)
            except Exception as exc:
                if _is_not_found(exc):
                    return
                termination_error = exc

            try:
                self._wait_for_terminal(sandbox_id, manager=manager)
            except Exception:
                if termination_error is not None:
                    raise termination_error from None
                raise
        except Exception as exc:
            if not _is_not_found(exc):
                raise click.ClickException(
                    f"Could not terminate OpenSandbox sandbox {sandbox_id!r}: "
                    f"{_safe_provider_error(exc, tuple(self._secret_values))}"
                ) from exc
        finally:
            if manager is not None:
                _close_quietly(
                    manager,
                    description="OpenSandbox termination manager",
                    secret_values=tuple(self._secret_values),
                )
            if client is not None:
                _close_quietly(
                    client,
                    description=f"terminated OpenSandbox client {sandbox_id}",
                    secret_values=tuple(self._secret_values),
                )

    def _wait_for_terminal(
        self,
        sandbox_id: str,
        *,
        manager: SandboxManagerSync | None,
    ) -> None:
        from opensandbox import SandboxManagerSync

        active_manager = manager or SandboxManagerSync.create(
            connection_config=self._connection_config
        )
        owns_manager = manager is None
        try:
            deadline = time.monotonic() + _DELETE_TIMEOUT_S
            while True:
                try:
                    info = active_manager.get_sandbox_info(sandbox_id)
                except Exception as exc:
                    if _is_not_found(exc):
                        return
                    raise
                if _state_name(info.status.state) in {"terminated", "failed"}:
                    return
                if time.monotonic() >= deadline:
                    raise click.ClickException(
                        f"sandbox remained {_state_name(info.status.state)!r} for "
                        f"{_DELETE_TIMEOUT_S} seconds after delete"
                    )
                time.sleep(_DELETE_POLL_S)
        finally:
            if owns_manager:
                _close_quietly(
                    active_manager,
                    description="OpenSandbox delete poll manager",
                    secret_values=tuple(self._secret_values),
                )

    def is_running(self, sandbox_id: str) -> bool | None:
        from opensandbox import SandboxManagerSync

        manager = None
        try:
            manager = SandboxManagerSync.create(connection_config=self._connection_config)
            info = manager.get_sandbox_info(sandbox_id)
        except Exception as exc:
            if _is_not_found(exc):
                return False
            logger.warning(
                "Could not read OpenSandbox sandbox %s state: %s",
                sandbox_id,
                _safe_provider_error(exc, tuple(self._secret_values)),
            )
            return None
        finally:
            if manager is not None:
                _close_quietly(
                    manager,
                    description="OpenSandbox status manager",
                    secret_values=tuple(self._secret_values),
                )
        state = _state_name(info.status.state)
        if state == "running":
            return True
        if state in {"paused", "terminated", "failed"}:
            return False
        return None

    def close(self) -> None:
        with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            _close_quietly(
                client,
                description=f"OpenSandbox client {getattr(client, 'id', None)}",
                secret_values=tuple(self._secret_values),
            )
