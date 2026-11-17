"""Parent-owned, inference-only credentials for contained Claude processes."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import secrets
import shlex
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Collection, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlsplit

from omnigent.databricks_ai_gateway import (
    CLAUDE_CODE_AGENT,
    is_databricks_ai_gateway_url,
    is_ug_route,
    with_gateway_attribution_pairs,
)
from omnigent.telemetry.surface import harness_client_surface
from omnigent.util.env_credentials import env_names_with_omnigent_prefix

from .credential_proxy import CredentialRewriteRule
from .datamodel import CredentialSourceSpec
from .egress.proxy import EgressProxy, RequestBodyTransform
from .egress.rules import EgressRule, is_dns_safe_host, parse_rules
from .model_auth import (
    ProviderAuthRequired,
    _pin_auth_python_runtime,
    _resolve_auth_executable,
    _TrustedExecutable,
    _validate_auth_runtime,
    mint_profile_token,
)
from .model_egress import FrozenModelRoute
from .model_signer_service import (
    _parse_strict_header_lines,
    _parse_strict_request_headers,
    _SignerRelay,
)
from .model_signing import (
    SignedModelRequest,
    SigningRejected,
    _validate_headers,
    _validate_json_body,
    _validate_secret_value,
)
from .sandbox import SandboxPolicy, protect_credential_source

HELPER_ENV = "OMNIGENT_CLAUDE_API_KEY_HELPER"
_SECRET_ENV = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_CUSTOM_HEADERS",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
        "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR",
        HELPER_ENV,
    }
)
_BYPASS_ENV = frozenset({"ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy"})
_BETA = re.compile(r"[a-z0-9][a-z0-9.-]{0,100}(?:,[a-z0-9][a-z0-9.-]{0,100})*\Z")
# Match the Messages API request limit so attachments the provider accepts still pass.
_MAX_REQUEST_BODY_BYTES = 32 * 1024 * 1024


def _is_claude_credential_env_name(name: str) -> bool:
    """Exclude alternate transport secrets as well as the selected credential."""
    canonical = name.removeprefix("OMNIGENT_")
    return name in _SECRET_ENV or (
        canonical.startswith(("ANTHROPIC_", "CLAUDE_"))
        and any(
            part
            in {
                "KEY",
                "TOKEN",
                "SECRET",
                "PASSWORD",
                "PASSWD",
                "PASSPHRASE",
                "CREDENTIAL",
                "CREDENTIALS",
            }
            for part in canonical.split("_")
        )
    )


def _managed_config_object_validator(
    forbidden_env_names: Collection[str],
) -> Callable[[list[tuple[str, object]]], dict[str, object]]:
    forbidden = {
        alias
        for name in (*_SECRET_ENV, *forbidden_env_names)
        for alias in env_names_with_omnigent_prefix(name)
    }

    def validate_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        # Check every pair before duplicate keys can hide an earlier credential.
        for key, value in pairs:
            if key == "apiKeyHelper" and value not in (None, ""):
                raise ValueError("managed apiKeyHelper is unsupported with parent-owned auth")
            if key == "env":
                if not isinstance(value, dict):
                    raise ValueError("managed env must be an object")
                if forbidden.intersection(value) or any(
                    not isinstance(name, str) or _is_claude_credential_env_name(name)
                    for name in value
                ):
                    raise ValueError("managed credentials must remain parent-owned")
        return dict(pairs)

    return validate_object


def _validate_claude_managed_config(
    managed_config_dirs: Sequence[Path], forbidden_env_names: Collection[str]
) -> None:
    """Reject managed credentials before any contained Claude process starts."""
    validate_object = _managed_config_object_validator(forbidden_env_names)

    for directory in managed_config_dirs:
        try:
            entries = {entry.name: entry for entry in directory.iterdir()}
            paths = [
                entries[name]
                for name in ("managed-mcp.json", "managed-settings.json")
                if name in entries
            ]
            dropins = entries.get("managed-settings.d")
            if dropins is not None:
                paths.extend(sorted(path for path in dropins.iterdir() if path.suffix == ".json"))
        except OSError:
            raise ValueError("cannot inspect managed Claude configuration directory") from None
        for path in paths:
            try:
                payload = json.loads(
                    path.read_text(encoding="utf-8"), object_pairs_hook=validate_object
                )
                if not isinstance(payload, dict):
                    raise ValueError("managed configuration must be an object")
            except (OSError, ValueError, RecursionError):
                raise ValueError(
                    "managed Claude configuration is unreadable, invalid, or contains "
                    "unsupported credentials"
                ) from None


def _claude_managed_preferences_paths() -> tuple[Path, ...]:
    """Match Claude's device and OS-account MDM preferences on macOS."""
    if sys.platform != "darwin":
        return ()
    import pwd

    root = Path("/Library/Managed Preferences")
    name = "com.anthropic.claudecode.plist"
    try:
        return (root / pwd.getpwuid(os.geteuid()).pw_name / name, root / name)
    except (KeyError, OSError):
        raise ValueError("cannot resolve managed Claude preferences account") from None


class _UniqueManagedDict(dict[str, object]):
    def __setitem__(self, key: str, value: object) -> None:
        if key in self:
            raise ValueError("duplicate managed preference key")
        super().__setitem__(key, value)


def _validate_claude_managed_preferences(
    paths: Sequence[Path], forbidden_env_names: Collection[str]
) -> None:
    if not paths:
        return
    import plistlib
    from xml.parsers.expat import ExpatError

    validate_object = _managed_config_object_validator(forbidden_env_names)

    def validate_value(value: object) -> None:
        if isinstance(value, dict):
            validate_object(list(value.items()))
            for child in value.values():
                validate_value(child)
        elif isinstance(value, list):
            for child in value:
                validate_value(child)

    for path in paths:
        try:
            raw = path.read_bytes()
        except FileNotFoundError:
            continue
        except OSError:
            raise ValueError("cannot read managed Claude preferences") from None
        try:
            payload = plistlib.loads(raw, dict_type=_UniqueManagedDict)
            if not isinstance(payload, dict):
                raise ValueError("managed preferences must be an object")
            validate_value(payload)
        except (ValueError, TypeError, OverflowError, RecursionError, ExpatError):
            raise ValueError(
                "managed Claude preferences are invalid or contain unsupported credentials"
            ) from None


class ClaudeAuthRequired(ProviderAuthRequired):
    """Credential failures without secret-bearing command output."""

    title = "Claude authentication required"
    cause = "The selected Claude credential is missing or expired."

    def __init__(self) -> None:
        super().__init__("Claude model authentication is unavailable; sign in again and retry.")


class _CredentialResolver:
    """Parent-only resolver shared by equal callback access and the running relay."""

    def __init__(self, resolve: Callable[[], str]) -> None:
        self._resolve = resolve
        self._generation = 0
        self._lock = threading.Lock()

    def __call__(self) -> str:
        with self._lock:
            resolve = self._resolve
        return resolve()

    def replace(self, replacement: _CredentialResolver) -> None:
        with replacement._lock:
            resolve = replacement._resolve
        with self._lock:
            self._resolve = resolve
            self._generation += 1


@dataclass(frozen=True)
class ClaudeModelAccess:
    """Trusted authority, never serialized into a sandbox policy or launcher.

    Equality covers the endpoint and credential sources, not per-instance secrets.
    """

    base_url: str
    auth_header: str
    credential: Callable[[], str] = field(repr=False, compare=False)
    source_paths: tuple[Path, ...] = ()
    oauth: bool = False
    placeholder: str = field(
        default_factory=lambda: "oa_cred_" + secrets.token_urlsafe(32), compare=False
    )
    auth_executable: _TrustedExecutable | None = field(default=None, repr=False)
    source_env_names: frozenset[str] = frozenset()
    _attribution_headers: tuple[tuple[str, str], ...] = field(init=False, default=(), repr=False)
    _credential_resolver: _CredentialResolver = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or not is_dns_safe_host(parsed.hostname)
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or any(c in parsed.path for c in "%?#\\")
            or any(part in (".", "..") for part in parsed.path.split("/"))
            or any(ord(c) < 0x21 or ord(c) > 0x7E for c in self.base_url)
        ):
            raise ValueError("sandboxed Claude requires an exact HTTPS model endpoint on port 443")
        if self.auth_header not in ("authorization", "x-api-key"):
            raise ValueError("unsupported Claude credential type")
        resolver = self.credential
        if not isinstance(resolver, _CredentialResolver):
            resolver = _CredentialResolver(resolver)
        object.__setattr__(self, "_credential_resolver", resolver)
        object.__setattr__(self, "credential", resolver)
        if is_databricks_ai_gateway_url(self.base_url):
            object.__setattr__(
                self,
                "_attribution_headers",
                tuple(
                    with_gateway_attribution_pairs(
                        (),
                        CLAUDE_CODE_AGENT,
                        via_ug=is_ug_route(self.base_url),
                        client_surface=harness_client_surface(),
                    )
                ),
            )

    def _adopt_credential(self, access: ClaudeModelAccess) -> None:
        if self != access:
            raise ValueError("model credential replacement requires unchanged authority")
        self._credential_resolver.replace(access._credential_resolver)

    @property
    def host(self) -> str:
        return urlsplit(self.base_url).hostname or ""

    @property
    def paths(self) -> tuple[str, ...]:
        prefix = urlsplit(self.base_url).path.rstrip("/")
        return (prefix + "/v1/messages", prefix + "/v1/messages/count_tokens")

    def with_bundle_sources(self, bundle_dir: Path | None) -> ClaudeModelAccess:
        """Protect original bundle configuration as well as model credential stores."""
        if bundle_dir is None:
            return self
        root = bundle_dir.resolve()
        sources = [root]
        sources.extend(
            path
            for path in root.rglob("*")
            if (path.is_file() or path.is_symlink())
            and path.relative_to(root).parts[0] != "skills"
        )
        return replace(self, source_paths=(*self.source_paths, *sources))

    def protect_sources(self, policy: SandboxPolicy, cwd: Path) -> SandboxPolicy:
        """Reject credential sources reachable through any sandbox grant."""
        from omnigent.onboarding.ambient import claude_code_managed_mcp_paths

        managed_dirs = []
        for directory in dict.fromkeys(path.parent for path in claude_code_managed_mcp_paths()):
            try:
                directory.stat()
            except FileNotFoundError:
                continue
            except OSError:
                raise ValueError("cannot inspect managed Claude configuration directory") from None
            managed_dirs.append(directory)
        _validate_claude_managed_config(managed_dirs, self.source_env_names)
        _validate_claude_managed_preferences(
            _claude_managed_preferences_paths(), self.source_env_names
        )
        policy = replace(
            policy, credential_source_paths=list(policy.credential_source_paths or [])
        )
        if self.auth_executable is not None:
            _validate_auth_runtime(self.auth_executable, policy)
        for path in self.source_paths:
            # A child grant would otherwise re-expose part of a protected directory.
            sources = (path.absolute(), path.resolve())
            for grant in (
                *(policy.read_roots or []),
                *policy.write_roots,
                *policy.write_files,
                cwd,
            ):
                if any(
                    candidate.is_relative_to(source)
                    for candidate in (grant.absolute(), grant.resolve())
                    for source in sources
                ):
                    raise ValueError("protected credential paths must stay outside sandbox grants")
            protect_credential_source(
                CredentialSourceSpec(kind="file", path=str(path)), policy, cwd=cwd
            )
        return policy

    def worker_environment(self, env: MutableMapping[str, str], home: Path) -> None:
        """Install only a session placeholder and isolated Claude state."""
        for key in (*_SECRET_ENV, *self.source_env_names):
            for name in env_names_with_omnigent_prefix(key):
                env.pop(name, None)
        for key in _BYPASS_ENV:
            env.pop(key, None)
        for key in list(env):
            if _is_claude_credential_env_name(key) or key.removeprefix("OMNIGENT_").startswith(
                ("HARNESS_", "DATABRICKS_")
            ):
                env.pop(key)
        env["HOME"] = str(home)
        env["CLAUDE_CONFIG_DIR"] = str(home / ".claude")
        env["ANTHROPIC_BASE_URL"] = self.base_url
        # A Bearer placeholder also works with apiKeyHelper-style gateways.
        env["CLAUDE_CODE_OAUTH_TOKEN" if self.oauth else "ANTHROPIC_AUTH_TOKEN"] = self.placeholder
        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"

    def reconstruct(
        self,
        *,
        bearer_token: str,
        method: str,
        connect_host: str,
        sni_host: str,
        request_host: str,
        target: str,
        headers: Sequence[tuple[str, str]],
        body: bytes,
    ) -> SignedModelRequest:
        """Rebuild the complete upstream request from a narrow allowlist."""
        _validate_secret_value(bearer_token, label="model credential")
        if (
            method != "POST"
            or target not in (*self.paths, *(path + "?beta=true" for path in self.paths))
            or any(host.lower() != self.host for host in (connect_host, sni_host, request_host))
        ):
            raise SigningRejected("request is outside the Claude inference routes")
        auth = [
            (name.lower(), value)
            for name, value in headers
            if name.lower() in ("authorization", "x-api-key")
        ]
        if not auth or any(
            entry
            not in (
                ("authorization", f"Bearer {self.placeholder}"),
                ("x-api-key", self.placeholder),
            )
            for entry in auth
        ):
            raise SigningRejected("model credential headers require exact session placeholders")
        # Claude can send both placeholder forms. Collapse only validated auth;
        # shared validation still rejects placeholders in all other headers.
        names = [name.lower() for name, _ in headers]
        if len(names) != len(set(names)):
            raise SigningRejected("duplicate model request header")
        validated = _validate_headers(
            [
                (name, value)
                for name, value in headers
                if name.lower() not in ("authorization", "x-api-key")
            ]
            + [("Authorization", f"Bearer {self.placeholder}")],
            self.placeholder,
        )
        normalized = {name: values[0] for name, values in validated.items()}
        if "content-encoding" in normalized:
            raise SigningRejected("unsupported model request credential or encoding")
        if normalized.get("content-type") != "application/json":
            raise SigningRejected("model requests require JSON")
        if len(body) > _MAX_REQUEST_BODY_BYTES:
            raise SigningRejected("model request body is too large")
        _validate_json_body(body)
        payload = json.loads(body)
        if not isinstance(payload, dict) or not isinstance(payload.get("model"), str):
            raise SigningRejected("model requests require a model identifier")
        rebuilt = [
            ("Host", self.host),
            (
                self.auth_header,
                f"Bearer {bearer_token}" if self.auth_header == "authorization" else bearer_token,
            ),
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(body))),
            ("anthropic-version", "2023-06-01"),
        ]
        if self.host.endswith(".databricks.com") or self.host.endswith(".azuredatabricks.net"):
            rebuilt.append(("x-databricks-use-coding-agent-mode", "true"))
        if self.oauth:
            rebuilt.append(("x-app", "cli"))
            agent = normalized.get("user-agent", "")
            if agent.startswith("claude-cli/") and len(agent) <= 256:
                rebuilt.append(("User-Agent", agent))
        beta = normalized.get("anthropic-beta", "")
        if beta and _BETA.fullmatch(beta) is None:
            raise SigningRejected("invalid Anthropic beta header")
        if self.oauth and "oauth-2025-04-20" not in beta.split(","):
            beta = ",".join(filter(None, (beta, "oauth-2025-04-20")))
        if beta:
            rebuilt.append(("anthropic-beta", beta))
        if "accept" in normalized:
            if normalized["accept"] not in ("application/json", "text/event-stream", "*/*"):
                raise SigningRejected("unsupported model response format")
            rebuilt.append(("Accept", normalized["accept"]))
        rebuilt.extend(self._attribution_headers)
        return SignedModelRequest("POST", self.host, target, body, tuple(rebuilt))

    def create_proxy(
        self,
        rules: list[EgressRule],
        ca_cert_path: Path,
        ca_key_path: Path,
        *,
        upstream_ca_bundle: Path | None = None,
        block_private_destinations: bool = True,
        auth_token: str | None = None,
        credential_rewrites: list[CredentialRewriteRule] | None = None,
        request_body_transform: RequestBodyTransform | None = None,
    ) -> EgressProxy:
        if upstream_ca_bundle is None:
            raise ValueError("Claude model relay requires an upstream CA bundle")
        return _ClaudeRelay(
            access=self,
            rules=rules,
            ca_cert_path=ca_cert_path,
            ca_key_path=ca_key_path,
            ca_bundle_path=upstream_ca_bundle,
            block_private_destinations=block_private_destinations,
            auth_token=auth_token,
            credential_rewrites=credential_rewrites,
            request_body_transform=request_body_transform,
        )


class _ClaudeCredential:
    def __init__(self, resolve: Callable[[], str]) -> None:
        self._resolver = (
            resolve if isinstance(resolve, _CredentialResolver) else _CredentialResolver(resolve)
        )
        self._cached: str | None = None
        self._until = 0.0
        self._generation = -1
        self._lock = asyncio.Lock()

    async def token_for_request(self) -> str:
        async with self._lock:
            while True:
                with self._resolver._lock:
                    generation = self._resolver._generation
                    if generation != self._generation:
                        self._cached = None
                    if self._cached is not None and time.monotonic() < self._until:
                        return self._cached
                    resolve = self._resolver._resolve
                try:
                    token = await asyncio.to_thread(resolve)
                    _validate_secret_value(token, label="model credential")
                except Exception:  # noqa: BLE001 — credential source errors must not expose secrets
                    with self._resolver._lock:
                        if generation != self._resolver._generation:
                            continue
                    raise ClaudeAuthRequired() from None
                with self._resolver._lock:
                    if generation != self._resolver._generation:
                        continue
                    self._cached = token
                    self._until = time.monotonic() + 30
                    self._generation = generation
                    return token

    def invalidate_after_unauthorized(self) -> None:
        with self._resolver._lock:
            self._until = 0


class _ClaudeRelay(_SignerRelay):
    def __init__(
        self,
        *,
        access: ClaudeModelAccess,
        rules: list[EgressRule],
        ca_cert_path: Path,
        ca_key_path: Path,
        ca_bundle_path: Path,
        block_private_destinations: bool,
        auth_token: str | None,
        credential_rewrites: list[CredentialRewriteRule] | None,
        request_body_transform: RequestBodyTransform | None,
    ) -> None:
        super().__init__(
            route=FrozenModelRoute("POST", access.host, access.paths[0]),
            placeholder=access.placeholder,
            credential_source=_ClaudeCredential(access.credential),
            ca_cert_path=ca_cert_path,
            ca_key_path=ca_key_path,
            ca_bundle_path=ca_bundle_path,
            provider_port=None,
        )
        self._access = access
        self._rules = [
            *rules,
            *parse_rules(
                [
                    f"POST {access.host}{path}{query}"
                    for path in access.paths
                    for query in ("", "?beta=true")
                ]
            ),
        ]
        self._block_private_destinations = block_private_destinations
        self._auth_token = auth_token
        self._expected_auth_value = (
            b"Basic " + base64.b64encode(f"omnigent:{auth_token}".encode())
            if auth_token is not None
            else None
        )
        self._request_body_transform = request_body_transform
        for rule in credential_rewrites or []:
            if rule.host.lower() == access.host:
                raise ValueError("generic credential rules cannot overlap the Claude model host")
            self._cred_by_host[rule.host.lower()] = rule
            if rule.synthetic is not None:
                self._cred_by_synthetic.setdefault(rule.synthetic, {})[rule.host.lower()] = rule

    def _allows_http2_passthrough(self, host: str) -> bool:
        return host.lower() != self._access.host and super()._allows_http2_passthrough(host)

    def _parse_tunnel_request_line(self, host: str, raw: bytes) -> tuple[str, str]:
        if host.lower() == self._access.host:
            return self._parse_inner_request_line(raw)
        return EgressProxy._parse_inner_request_line(raw)

    def _parse_tunnel_content_length(self, host: str, headers_raw: bytes) -> int:
        if host.lower() == self._access.host:
            return self._parse_inner_content_length(headers_raw)
        return EgressProxy._parse_inner_content_length(headers_raw)

    def _parse_inner_request_line(self, raw: bytes) -> tuple[str, str]:
        method, path = EgressProxy._parse_inner_request_line(raw)
        if raw != f"{method} {path} HTTP/1.1\r\n".encode("ascii"):
            raise SigningRejected("noncanonical request line")
        return method, path

    @staticmethod
    def _parse_inner_content_length(headers_raw: bytes) -> int:
        parsed = _parse_strict_header_lines(headers_raw)
        if not any(name.lower() == "content-length" for name, _ in parsed):
            headers_raw = headers_raw[:-2] + b"Content-Length: 0\r\n\r\n"
        _, length = _parse_strict_request_headers(
            headers_raw, allow_connection_close=True, max_body_bytes=_MAX_REQUEST_BODY_BYTES
        )
        return length

    @staticmethod
    def _request_headers(raw: bytes) -> tuple[list[tuple[str, str]], int]:
        return _parse_strict_request_headers(
            raw, allow_connection_close=True, max_body_bytes=_MAX_REQUEST_BODY_BYTES
        )

    async def _assert_destination_allowed(self, host: str, port: int) -> str | None:
        if host.lower() == self._access.host and port != 443:
            raise PermissionError("model access requires HTTPS on port 443")
        return await EgressProxy._assert_destination_allowed(self, host, port)

    def _reconstruct_request(
        self,
        *,
        bearer_token: str,
        method: str,
        connect_host: str,
        sni_host: str,
        request_host: str,
        target: str,
        headers: Sequence[tuple[str, str]],
        body: bytes,
    ) -> SignedModelRequest:
        signed = self._access.reconstruct(
            bearer_token=bearer_token,
            method=method,
            connect_host=connect_host,
            sni_host=sni_host,
            request_host=request_host,
            target=target,
            headers=headers,
            body=body,
        )
        if self._request_body_transform is not None:
            transformed = self._request_body_transform(method, connect_host, 443, target, body)
            signed = replace(
                signed,
                body=transformed,
                headers=tuple(
                    (name, str(len(transformed)) if name.lower() == "content-length" else value)
                    for name, value in signed.headers
                ),
            )
        return signed

    async def _forward_https(
        self,
        client_writer: asyncio.StreamWriter,
        host: str,
        port: int,
        method: str,
        path: str,
        request_line: bytes,
        headers_raw: bytes,
        body: bytes,
    ) -> bool:
        forward = super()._forward_https if host.lower() == self._access.host else None
        if forward is None:
            return await EgressProxy._forward_https(
                self, client_writer, host, port, method, path, request_line, headers_raw, body
            )
        try:
            self._request_headers(headers_raw)
        except SigningRejected:
            await self._send_forbidden(client_writer, "")
            return False
        return await forward(
            client_writer, host, port, method, path, request_line, headers_raw, body
        )

    async def _handle_http(
        self,
        writer: asyncio.StreamWriter,
        reader: asyncio.StreamReader,
        method: str,
        url: str,
        request_line: bytes,
        headers_raw: bytes,
    ) -> None:
        if urlsplit(url).hostname == self._access.host:
            await self._send_forbidden(writer, "")
            return
        await super()._handle_http(writer, reader, method, url, request_line, headers_raw)


def resolve_claude_model_access(
    env: Mapping[str, str],
    *,
    credential: Callable[[], str] | None = None,
    source_paths: tuple[Path, ...] = (),
    source_env_names: frozenset[str] = frozenset(),
    databricks_profile: str | None = None,
) -> ClaudeModelAccess:
    """Resolve credentials in the trusted parent, before starting any worker."""
    for flag in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"):
        if env.get(flag) not in (None, "", "0", "false"):
            raise ValueError("sandboxed Claude model relay requires an Anthropic-compatible API")
    base_url = env.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com"
    config_dir = Path(env.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))
    private_paths = (
        config_dir,
        config_dir / ".credentials.json",
        Path.home() / ".claude",
        Path.home() / ".claude/.credentials.json",
        Path.home() / ".claude.json",
        Path.home() / "Library/Keychains",
        Path.home() / ".cache/omnigent-egress",
        Path.home() / ".omnigent/config.yaml",
        Path.home() / ".omnigent/secrets.json",
        Path(env.get("OMNIGENT_CONFIG_HOME") or (Path.home() / ".omnigent")) / "config.yaml",
        Path(env.get("OMNIGENT_CONFIG_HOME") or (Path.home() / ".omnigent")) / "secrets.json",
        *source_paths,
    )
    if credential is not None:
        return ClaudeModelAccess(
            base_url, "authorization", credential, private_paths, source_env_names=source_env_names
        )
    helper = env.get(HELPER_ENV)
    if helper:
        # Static keys generated by the provider layer require no shell execution.
        words = shlex.split(helper)
        if len(words) == 3 and words[:2] == ["printf", "%s"]:
            token = words[2]
            header = (
                "x-api-key"
                if urlsplit(base_url).hostname == "api.anthropic.com"
                else "authorization"
            )
            return ClaudeModelAccess(
                base_url, header, lambda: token, private_paths, source_env_names=source_env_names
            )
        if databricks_profile:
            authority = f"https://{urlsplit(base_url).hostname}"
            executable = _pin_auth_python_runtime(_resolve_auth_executable())
            config_source = (
                Path(env.get("DATABRICKS_CONFIG_FILE") or (Path.home() / ".databrickscfg"))
                .expanduser()
                .absolute()
            )
            config_file = config_source.resolve()
            return ClaudeModelAccess(
                base_url,
                "authorization",
                lambda: asyncio.run(
                    mint_profile_token(
                        host=authority,
                        profile=databricks_profile,
                        _executable=executable,
                        _config_file=config_file,
                    )
                ),
                (
                    *private_paths,
                    config_source,
                    config_file,
                    Path.home() / ".databrickscfg",
                    Path.home() / ".databricks",
                ),
                auth_executable=executable,
                source_env_names=source_env_names,
            )
        raise ValueError(
            "sandboxed Claude cannot execute a renewable shell auth helper in the host; "
            "use a provider API key or a parent-owned credential source"
        )
    for key, header, oauth in (
        ("ANTHROPIC_AUTH_TOKEN", "authorization", False),
        ("CLAUDE_CODE_OAUTH_TOKEN", "authorization", True),
        ("ANTHROPIC_API_KEY", "x-api-key", False),
    ):
        token = env.get(key)
        if token:
            return ClaudeModelAccess(
                base_url,
                header,
                lambda token=token: token,
                private_paths,
                oauth,
                source_env_names=source_env_names,
            )

    credential_path = config_dir / ".credentials.json"
    canonical_credential_path = credential_path.resolve()

    def _oauth_token() -> str:
        if credential_path.resolve() != canonical_credential_path:
            raise ValueError("Claude credential source symlink target changed")
        if credential_path.exists():
            fd = os.open(canonical_credential_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, encoding="utf-8") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise ValueError(
                        "Claude credential source must be a regular file without hard links"
                    )
                raw = source.read()
                if (
                    credential_path.resolve() != canonical_credential_path
                    or os.fstat(source.fileno()).st_nlink != 1
                ):
                    raise ValueError("Claude credential source changed while reading")
        elif sys.platform == "darwin":
            result = subprocess.run(
                [
                    "/usr/bin/security",
                    "find-generic-password",
                    "-s",
                    "Claude Code-credentials",
                    "-w",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                cwd="/",
                env={"PATH": "/usr/bin:/bin"},
                check=True,
            )
            raw = result.stdout
        else:
            raise ClaudeAuthRequired()
        oauth = json.loads(raw)["claudeAiOauth"]
        if oauth.get("expiresAt", 0) <= time.time() * 1000:
            raise ClaudeAuthRequired()
        return str(oauth["accessToken"])

    return ClaudeModelAccess(
        base_url,
        "authorization",
        _oauth_token,
        private_paths,
        True,
        source_env_names=source_env_names,
    )
