"""Machine-wide coordination for Databricks workspace credentials."""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import logging
import os
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx
from filelock import FileLock, Timeout

_logger = logging.getLogger(__name__)

_LOCK_TIMEOUT_S = 30.0
_REFRESH_SKEW_S = 60.0
_PROBE_INTERVAL_S = 15.0
_AUTH_COMMAND_TIMEOUT_S = 20.0
_MAX_STATE_BYTES = 64 * 1024


class AuthConfig(Protocol):
    """Small subset of the Databricks SDK config used by the broker."""

    def authenticate(self) -> dict[str, str]: ...


TokenValidator = Callable[[str, str], None]


class DatabricksCredentialError(OSError):
    """Base class for coordinated Databricks authentication failures."""


class DatabricksCredentialsDeadError(DatabricksCredentialError):
    """The profile must be repaired with ``databricks auth login``."""


@dataclass(frozen=True)
class AuthIdentity:
    """A Databricks profile and workspace pair."""

    profile: str
    workspace_host: str
    key: str

    @classmethod
    def create(cls, profile: str | None, workspace_host: str) -> AuthIdentity:
        normalized_host = normalize_workspace_host(workspace_host)
        normalized_profile = (profile or "DEFAULT").strip() or "DEFAULT"
        digest = hashlib.sha256(normalized_host.encode()).hexdigest()[:16]
        safe_profile = "".join(
            char if char.isalnum() or char in ("-", "_") else "-" for char in normalized_profile
        )[:64]
        return cls(
            profile=normalized_profile,
            workspace_host=normalized_host,
            key=f"{safe_profile}-{digest}",
        )


@dataclass(frozen=True)
class SharedToken:
    """Bearer token and publication metadata read from shared state."""

    token: str
    expires_at: float | None
    published_at: float

    def usable(self, now: float) -> bool:
        return self.expires_at is None or self.expires_at > now + _REFRESH_SKEW_S


def normalize_workspace_host(host: str) -> str:
    """Return a stable HTTPS workspace URL without a trailing slash."""
    value = host.strip()
    if "://" not in value:
        value = f"https://{value}"
    parsed = urlsplit(value)
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        raise ValueError(f"invalid Databricks workspace host: {host!r}")
    port = f":{parsed.port}" if parsed.port is not None else ""
    return urlunsplit((scheme, f"{hostname}{port}", "", "", ""))


def _state_root() -> Path:
    return Path.home() / ".omnigent" / "auth" / "databricks"


def _runtime_root() -> Path:
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
    if xdg_runtime:
        return Path(xdg_runtime) / "omnigent" / "auth" / "databricks"
    user_key = hashlib.sha256(getpass.getuser().encode()).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f"omnigent-{user_key}" / "auth" / "databricks"


def _current_uid() -> int | None:
    getuid = getattr(os, "getuid", None)
    return int(getuid()) if getuid is not None else None


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise DatabricksCredentialError(f"unsafe credential state directory: {path}")
    uid = _current_uid()
    if uid is not None and info.st_uid != uid:
        raise DatabricksCredentialError(
            f"credential state directory is not owned by this user: {path}"
        )
    if stat.S_IMODE(info.st_mode) & 0o077:
        path.chmod(0o700)


def _safe_read_json(path: Path) -> dict[str, Any] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise DatabricksCredentialError(f"unsafe credential state file: {path}")
    uid = _current_uid()
    if (uid is not None and info.st_uid != uid) or stat.S_IMODE(info.st_mode) & 0o077:
        raise DatabricksCredentialError(
            f"credential state file has unsafe ownership or mode: {path}"
        )
    if info.st_size > _MAX_STATE_BYTES:
        raise DatabricksCredentialError(f"credential state file is too large: {path}")
    try:
        raw = path.read_text(encoding="utf-8")
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DatabricksCredentialError(f"invalid credential state file: {path}") from exc
    if not isinstance(value, dict):
        raise DatabricksCredentialError(f"invalid credential state file: {path}")
    return value


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    _ensure_private_dir(path.parent)
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    if len(encoded) > _MAX_STATE_BYTES:
        raise DatabricksCredentialError("credential state exceeds size limit")
    fd, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(raw_temp)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=True) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        with suppress(FileNotFoundError):
            temp_path.unlink()


def _token_expiry(token: str) -> float | None:
    """Read an unverified JWT ``exp`` claim; opaque PATs have no expiry."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(payload).decode())
        expiry = decoded.get("exp")
        return float(expiry) if isinstance(expiry, (int, float)) else None
    except (ValueError, UnicodeError, json.JSONDecodeError):
        return None


def _permanent_auth_failure(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(
        marker in text
        for marker in (
            "invalid_grant",
            "refresh token is invalid",
            "refresh token has expired",
            "refresh token reuse",
        )
    )


def _credential_source_fingerprint() -> str:
    digest = hashlib.sha256()
    paths = (
        Path(os.environ.get("DATABRICKS_CONFIG_FILE") or Path.home() / ".databrickscfg"),
        Path.home() / ".databricks" / "token-cache.json",
    )
    for path in paths:
        digest.update(str(path).encode())
        try:
            info = path.stat()
        except OSError:
            digest.update(b"missing")
        else:
            digest.update(f"{info.st_ino}:{info.st_size}:{info.st_mtime_ns}".encode())
    for name in ("DATABRICKS_CONFIG_PROFILE", "DATABRICKS_HOST", "DATABRICKS_TOKEN"):
        digest.update(name.encode())
        digest.update(os.environ.get(name, "").encode())
    return digest.hexdigest()


def _validate_workspace_token(workspace_host: str, token: str) -> None:
    """Prove that a bearer is accepted by the intended workspace."""
    response = httpx.get(
        f"{workspace_host}/api/2.0/workspace/get-status",
        params={"path": "/"},
        headers={"Authorization": f"Bearer {token}"},
        timeout=10.0,
        follow_redirects=False,
    )
    response.raise_for_status()


class DatabricksAuthBroker:
    """Publish and consume one bearer per user, profile, and workspace."""

    def __init__(
        self,
        config: AuthConfig,
        *,
        profile: str | None,
        workspace_host: str,
        failure_message: str | None = None,
        token_validator: TokenValidator | None = None,
    ) -> None:
        self._config = config
        # SDK-backed service-principal and cloud-provider exchanges honor this
        # setting. OAuth-U2M uses the separately bounded CLI child below.
        sdk_timeout = getattr(config, "http_timeout_seconds", None)
        if isinstance(sdk_timeout, (int, float)) and sdk_timeout > _AUTH_COMMAND_TIMEOUT_S:
            config_with_timeout: Any = config
            config_with_timeout.http_timeout_seconds = _AUTH_COMMAND_TIMEOUT_S
        self.identity = AuthIdentity.create(profile, workspace_host)
        self._failure_message = failure_message
        self._token_validator = token_validator or _validate_workspace_token

    @property
    def _token_path(self) -> Path:
        return _state_root() / f"{self.identity.key}.json"

    @property
    def _dead_path(self) -> Path:
        return _state_root() / f"{self.identity.key}.dead"

    @property
    def _lock_path(self) -> Path:
        return _runtime_root() / f"{self.identity.key}.lock"

    @contextmanager
    def _locked(self) -> Iterator[None]:
        _ensure_private_dir(self._lock_path.parent)
        try:
            info = self._lock_path.lstat()
        except FileNotFoundError:
            pass
        else:
            uid = _current_uid()
            if (
                stat.S_ISLNK(info.st_mode)
                or not stat.S_ISREG(info.st_mode)
                or (uid is not None and info.st_uid != uid)
                or stat.S_IMODE(info.st_mode) & 0o077
            ):
                raise DatabricksCredentialError(
                    f"credential lock file has unsafe ownership or mode: {self._lock_path}"
                )
        lock = FileLock(self._lock_path, mode=0o600)
        started = time.monotonic()
        try:
            with lock.acquire(timeout=_LOCK_TIMEOUT_S):
                waited = time.monotonic() - started
                if waited >= 0.05:
                    _logger.info(
                        "Waited %.3fs for Databricks credential broker lock (profile=%r host=%s)",
                        waited,
                        self.identity.profile,
                        self.identity.workspace_host,
                    )
                yield
        except Timeout as exc:
            raise DatabricksCredentialError(
                f"timed out waiting for Databricks credential lock for {self.identity.profile!r}"
            ) from exc

    def current_token(self) -> str:
        """Return a shared bearer, refreshing once under the machine lock."""
        with self._locked():
            now = time.time()
            monotonic_now = time.monotonic()
            dead = self._read_dead()
            last_probe_monotonic = float(dead.get("last_probe_monotonic", -1.0)) if dead else -1.0
            elapsed = monotonic_now - last_probe_monotonic
            if dead is not None and 0.0 <= elapsed < _PROBE_INTERVAL_S:
                raise DatabricksCredentialsDeadError(self._dead_message(dead))

            shared = self._read_token()
            if dead is None and shared is not None and shared.usable(now):
                return shared.token

            if dead is not None:
                # Claim this machine-wide probe slot before doing network work.
                # A failed probe must not allow every waiting process to probe.
                dead["last_probe_at"] = now
                dead["last_probe_monotonic"] = monotonic_now
                _atomic_write_json(self._dead_path, dead)
            return self._refresh_locked(now, recovering=dead is not None)

    def invalidate(self, rejected_token: str | None = None) -> None:
        """Discard a rejected shared bearer without marking login dead."""
        with self._locked():
            shared = self._read_token()
            if shared is None or rejected_token is None or shared.token == rejected_token:
                with suppress(FileNotFoundError):
                    self._token_path.unlink()

    def _refresh_locked(self, now: float, *, recovering: bool) -> str:
        refresh_started = time.monotonic()
        before = _credential_source_fingerprint()
        try:
            headers = self._authenticate()
            after = _credential_source_fingerprint()
            if before != after:
                # A concurrent `databricks auth login` changed the credential
                # source. Discard the result that may have used the old source.
                headers = self._authenticate()
            elif str(getattr(self._config, "auth_type", "") or "").lower() in {
                "databricks-cli",
                "oauth-u2m",
            }:
                # OAuth-U2M credentials may live in Keychain/credential
                # manager, whose changes are not visible through file stats.
                # A second ordinary lookup is cheap (the first populated the
                # CLI cache) and closes the common login-during-refresh race.
                second = self._authenticate()
                if second != headers:
                    headers = second
        except Exception as exc:
            if _permanent_auth_failure(exc):
                dead = self._write_dead(now, exc)
                raise DatabricksCredentialsDeadError(self._dead_message(dead)) from exc
            raise DatabricksCredentialError(
                self._failure_message or self._login_message()
            ) from exc

        auth_value = headers.get("Authorization", "")
        if not auth_value.startswith("Bearer "):
            raise DatabricksCredentialError("Databricks authentication returned no bearer token")
        token = auth_value.removeprefix("Bearer ")
        if recovering:
            try:
                self._token_validator(self.identity.workspace_host, token)
            except Exception:  # noqa: BLE001 -- validators may use any HTTP client.
                # A still-valid cached access token may have been revoked. The
                # ordinary probe deliberately avoids rotation; only after the
                # workspace rejects it do we ask OAuth-U2M for one fresh token.
                try:
                    headers = self._authenticate(force_refresh=True)
                    auth_value = headers.get("Authorization", "")
                    if not auth_value.startswith("Bearer "):
                        raise DatabricksCredentialError(
                            "Databricks authentication returned no bearer token"
                        )
                    token = auth_value.removeprefix("Bearer ")
                    self._token_validator(self.identity.workspace_host, token)
                except Exception as retry_exc:
                    raise DatabricksCredentialsDeadError(
                        self._dead_message(self._read_dead() or {})
                    ) from retry_exc
        try:
            self._publish(token, now)
        except Exception:
            _logger.exception(
                "Databricks credential publication failed (profile=%r host=%s)",
                self.identity.profile,
                self.identity.workspace_host,
            )
            raise
        _logger.info(
            "Published Databricks credential in %.3fs (profile=%r host=%s recovery=%s)",
            time.monotonic() - refresh_started,
            self.identity.profile,
            self.identity.workspace_host,
            recovering,
        )
        if recovering:
            with suppress(FileNotFoundError):
                self._dead_path.unlink()
            _logger.info(
                "Databricks authentication recovered for profile %r", self.identity.profile
            )
        return token

    def _authenticate(self, *, force_refresh: bool = False) -> dict[str, str]:
        """Authenticate with a bounded CLI child for OAuth-U2M profiles."""
        auth_type = str(getattr(self._config, "auth_type", "") or "").lower()
        if auth_type not in {"databricks-cli", "oauth-u2m"}:
            return self._config.authenticate()
        command = ["databricks", "auth", "token"]
        if self.identity.profile == "DEFAULT":
            command.extend(("--host", self.identity.workspace_host))
        else:
            command.extend(("--profile", self.identity.profile))
        if force_refresh:
            command.append("--force-refresh")
        command.extend(("--output", "json"))
        try:
            result = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=_AUTH_COMMAND_TIMEOUT_S,
            )
            payload = json.loads(result.stdout)
            token = payload.get("access_token")
            if not isinstance(token, str) or not token:
                raise DatabricksCredentialError("Databricks CLI returned no bearer token")
            return {"Authorization": f"Bearer {token}"}
        except subprocess.TimeoutExpired as exc:
            raise DatabricksCredentialError("Databricks credential command timed out") from exc
        except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            detail = (
                exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
            )
            raise DatabricksCredentialError(
                detail or "Databricks credential command failed"
            ) from exc

    def _read_token(self) -> SharedToken | None:
        value = _safe_read_json(self._token_path)
        if value is None:
            return None
        if (
            value.get("profile") != self.identity.profile
            or value.get("workspace_host") != self.identity.workspace_host
            or not isinstance(value.get("token"), str)
            or not isinstance(value.get("published_at"), (int, float))
        ):
            raise DatabricksCredentialError(
                f"credential state identity mismatch: {self._token_path}"
            )
        expires_at = value.get("exp")
        if expires_at is not None and not isinstance(expires_at, (int, float)):
            raise DatabricksCredentialError(f"invalid token expiry: {self._token_path}")
        return SharedToken(
            value["token"], float(expires_at) if expires_at else None, value["published_at"]
        )

    def _read_dead(self) -> dict[str, Any] | None:
        value = _safe_read_json(self._dead_path)
        if value is None:
            return None
        if (
            value.get("profile") != self.identity.profile
            or value.get("workspace_host") != self.identity.workspace_host
        ):
            raise DatabricksCredentialError(f"dead marker identity mismatch: {self._dead_path}")
        return value

    def _publish(self, token: str, now: float) -> None:
        _atomic_write_json(
            self._token_path,
            {
                "version": 1,
                "profile": self.identity.profile,
                "workspace_host": self.identity.workspace_host,
                "token": token,
                "exp": _token_expiry(token),
                "published_at": now,
            },
        )

    def _write_dead(self, now: float, exc: BaseException) -> dict[str, Any]:
        marker = {
            "version": 1,
            "profile": self.identity.profile,
            "workspace_host": self.identity.workspace_host,
            "detected_at": now,
            "last_probe_at": now,
            "last_probe_monotonic": time.monotonic(),
            "source": "databricks-auth-broker",
            "error": type(exc).__name__,
            "remedy": self._login_command(),
        }
        _atomic_write_json(self._dead_path, marker)
        _logger.error(
            "Databricks credentials are invalid for profile %r; run `%s`",
            self.identity.profile,
            marker["remedy"],
        )
        return marker

    def _login_command(self) -> str:
        if self.identity.profile == "DEFAULT":
            return f"databricks auth login --host {self.identity.workspace_host}"
        return f"databricks auth login --profile {self.identity.profile}"

    def _login_message(self) -> str:
        return (
            f"Databricks authentication failed for profile {self.identity.profile!r}. "
            f"Run: {self._login_command()}"
        )

    def _dead_message(self, marker: dict[str, Any]) -> str:
        return (
            f"Databricks credentials are invalid for profile {self.identity.profile!r}. "
            f"Run: {marker.get('remedy', self._login_command())}"
        )


def token_for_config(
    config: AuthConfig,
    *,
    profile: str | None = None,
    workspace_host: str | None = None,
    failure_message: str | None = None,
) -> str:
    """Public entry point for every in-process Databricks auth consumer."""
    host = workspace_host or str(getattr(config, "host", "") or "")
    resolved_profile = profile or getattr(config, "profile", None)
    if not host:
        raise DatabricksCredentialError("Databricks authentication resolved no workspace host")
    return DatabricksAuthBroker(
        config,
        profile=resolved_profile,
        workspace_host=host,
        failure_message=failure_message,
    ).current_token()


def _main(argv: list[str] | None = None) -> int:
    """Print one broker-coordinated token for harness helper commands."""
    parser = argparse.ArgumentParser()
    selector = parser.add_mutually_exclusive_group(required=True)
    selector.add_argument("--profile")
    selector.add_argument("--host")
    parser.add_argument("--workspace-host", required=True)
    args = parser.parse_args(argv)
    try:
        from databricks.sdk.config import Config

        if args.profile:
            config = Config(profile=args.profile)
            profile = args.profile
        else:
            config = Config(host=args.host, auth_type="databricks-cli")
            profile = None
        token = DatabricksAuthBroker(
            config,
            profile=profile,
            workspace_host=args.workspace_host,
        ).current_token()
    except Exception as exc:  # noqa: BLE001 -- CLI boundary prints a safe summary.
        print(str(exc), file=sys.stderr)
        return 1
    print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
