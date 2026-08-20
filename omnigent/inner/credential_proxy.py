"""Runtime for the secretless sandbox credential proxy.

Real, long-lived secrets stay in the parent process. For each
:class:`~omnigent.inner.datamodel.CredentialProxyEntry` the parent
resolves the real secret and hands the egress MITM proxy a
:class:`CredentialRewriteRule` that binds it to one host. The proxy
attaches the real credential to outbound requests for that host (see
:mod:`omnigent.inner.egress.proxy`).

The default model is **swap-on-access**: nothing credential-shaped
enters the sandbox. A tool makes its request with no ``Authorization``
header and the proxy injects the real credential on the way out. An
entry may additionally opt into injecting a synthetic ``oa_cred_*``
placeholder into the sandbox env (``inject_env``) for clients that
refuse to issue a request without a local credential (e.g. ``gh``); the
proxy then recognises the placeholder and swaps it, rejecting a
placeholder replayed to a different host with HTTP 403.

This module owns two pieces:

- :func:`prepare_credential_proxy_runtime` — parent-side: resolve
  secrets, mint placeholders for ``inject_env`` entries, and build the
  helper env updates plus the proxy rewrite rules.
- :class:`CredentialRewriteRule` — the host-scoped real-credential
  mapping the proxy enforces.
"""

from __future__ import annotations

import os
import secrets
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.inner.datamodel import (
    CredentialProxySpec,
    CredentialSourceSpec,
    DatabricksProxySpec,
)

# Prefix on every minted placeholder. The proxy uses it to recognise a
# value as one of *its* synthetic credentials (so it can reject a
# placeholder sent to the wrong host instead of forwarding it). Chosen
# to be unmistakable and to use only base64url-safe characters so it
# survives Basic-auth ``base64(user:pass)`` round-trips intact.
SYNTHETIC_CREDENTIAL_PREFIX = "oa_cred_"
# Timeout for a ``command:`` source subprocess, in seconds.
_COMMAND_SOURCE_TIMEOUT_SECONDS = 30


@dataclass
class CredentialRewriteRule:
    """Host-scoped mapping to a real secret enforced by the egress proxy.

    Consumed by the egress proxy in two ways:

    - **Swap-on-access (default).** For an outbound request to
      :attr:`host` that carries no ``Authorization`` header, the proxy
      injects ``Authorization: <scheme> <real>`` on the way out.
    - **Placeholder swap (opt-in).** When :attr:`synthetic` is set, a
      request whose ``Authorization`` header carries that placeholder is
      rewritten to the real credential. A request carrying *any*
      ``oa_cred_*`` placeholder bound to a different host is rejected
      with HTTP 403 (the cross-host leak guard).

    :param host: Exact hostname this rewrite applies to (lower-cased),
        e.g. ``"github.com"``.
    :param scheme: ``Authorization`` scheme emitted upstream, one of
        ``"basic"``, ``"bearer"``, or ``"token"``.
    :param real_secret: The real upstream credential the proxy attaches.
    :param synthetic: The placeholder the sandbox sees when the entry
        opted into ``inject_env``, e.g. ``"oa_cred_xT9..."``. ``None``
        for pure swap-on-access entries (nothing is injected, so no
        placeholder will appear in requests).
    :param username: Basic-auth username emitted when ``scheme="basic"``,
        e.g. ``"x-access-token"``. ``None`` for ``bearer`` / ``token``.
    :param real_secret: A static real credential. Used when
        :attr:`secret_provider` is ``None`` (the ``env`` / ``file`` /
        ``command`` sources resolve once at helper start).
    :param secret_provider: A zero-arg callable returning the *current*
        real credential, re-resolved on each swap. Used for sources whose
        secret expires and must be refreshed for long sessions (e.g. a
        Databricks OAuth profile). Takes precedence over
        :attr:`real_secret` when set.
    """

    host: str
    scheme: str
    real_secret: str | None = None
    synthetic: str | None = None
    username: str | None = None
    secret_provider: Callable[[], str] | None = None

    def resolve_secret(self) -> str:
        """
        Return the current real credential for this rule.

        Prefers :attr:`secret_provider` (refreshing sources) and falls
        back to the static :attr:`real_secret`.

        :returns: The real upstream credential the proxy should attach.
        :raises ValueError: If neither a provider nor a static secret is
            configured (a malformed rule).
        """
        if self.secret_provider is not None:
            return self.secret_provider()
        if self.real_secret is not None:
            return self.real_secret
        raise ValueError(
            f"credential rewrite for host {self.host!r} has neither a static secret nor a provider"
        )


@dataclass
class MaterializedFile:
    """A file the parent writes into the sandbox scratch dir at start.

    Used by the ``databricks_cli`` policy to place a ``.databrickscfg``
    that lists the proxied profiles with **placeholder** tokens (never a
    real secret). The parent writes :attr:`content` to
    ``<scratch>/<name>`` and, when :attr:`env_var` is set, points that
    environment variable at the absolute path so the tool discovers it.

    :param name: File name relative to the sandbox scratch directory,
        e.g. ``"databrickscfg"``.
    :param content: Full file contents (placeholders only — no real
        secret ever lands in the sandbox).
    :param mode: POSIX file mode, e.g. ``0o600``.
    :param env_var: Environment variable pointed at the written file's
        absolute path, e.g. ``"DATABRICKS_CONFIG_FILE"``. ``None`` to
        write the file without exporting a pointer.
    """

    name: str
    content: str
    mode: int = 0o600
    env_var: str | None = None


@dataclass
class CredentialProxyRuntime:
    """Prepared parent-side assets for one helper process.

    :param helper_env_updates: Environment-variable names mapped to a
        synthetic placeholder, merged into the helper's spawn env so a
        credential-gating tool that reads them emits the placeholder,
        e.g. ``{"GH_TOKEN": "oa_cred_...", "GITHUB_TOKEN": "oa_cred_..."}``.
        Empty when every entry uses pure swap-on-access.
    :param rewrites: Host-scoped real-credential rewrite rules the egress
        proxy enforces.
    :param sandbox_files: Placeholder-only files the parent materializes
        into the sandbox scratch dir (e.g. a ``.databrickscfg``).
    """

    helper_env_updates: dict[str, str] = field(default_factory=dict)
    rewrites: list[CredentialRewriteRule] = field(default_factory=list)
    sandbox_files: list[MaterializedFile] = field(default_factory=list)


def prepare_credential_proxy_runtime(
    spec: CredentialProxySpec | None,
    *,
    parent_env: dict[str, str],
) -> CredentialProxyRuntime:
    """
    Resolve secrets, mint placeholders, and build the proxy rewrite rules.

    Each entry resolves its real secret in the (unsandboxed) parent. An
    entry that opts into ``inject_env`` also gets a freshly minted
    synthetic placeholder, which is injected into the helper env so a
    credential-gating client emits a request the proxy can recognise.
    Pure swap-on-access entries mint nothing — the proxy attaches the
    real credential to bound-host requests directly.

    :param spec: Parsed credential-proxy policy, or ``None`` (no-op).
    :param parent_env: Parent process environment used to resolve
        ``env`` / ``command`` sources. ``file`` sources read the
        filesystem directly.
    :returns: A :class:`CredentialProxyRuntime` with helper env updates
        (only for ``inject_env`` entries) and the proxy rewrite rules.
    :raises ValueError: If any configured source cannot be resolved to a
        non-empty secret.
    """
    runtime = CredentialProxyRuntime()
    if spec is None:
        return runtime

    if spec.databricks is not None:
        _prepare_databricks_runtime(spec.databricks, runtime)

    for entry in spec.entries:
        real_secret = _resolve_secret(entry.source, parent_env=parent_env)
        # Mint a placeholder only when the entry injects an env var. The
        # placeholder is what the cross-host leak guard keys on; pure
        # swap-on-access entries put nothing in the sandbox, so there is
        # no placeholder to mint or guard.
        synthetic: str | None = None
        if entry.inject_env:
            # 24 bytes -> 192 bits of entropy, well past any brute-force or
            # collision concern for a short-lived per-session placeholder.
            synthetic = f"{SYNTHETIC_CREDENTIAL_PREFIX}{secrets.token_urlsafe(24)}"
            for env_name in entry.inject_env:
                runtime.helper_env_updates[env_name] = synthetic
        runtime.rewrites.append(
            CredentialRewriteRule(
                host=entry.host,
                scheme=entry.scheme,
                real_secret=real_secret,
                synthetic=synthetic,
                username=entry.username,
            )
        )
    return runtime


def _resolve_secret(source: CredentialSourceSpec, *, parent_env: dict[str, str]) -> str:
    """
    Resolve a real secret from an ``env`` / ``file`` / ``command`` source.

    :param source: The parsed source descriptor.
    :param parent_env: Parent process environment for ``env`` lookups and
        as the environment for ``command`` execution.
    :returns: The resolved secret with surrounding whitespace stripped.
    :raises ValueError: If the source is misconfigured, missing, empty,
        or (for ``command``) exits non-zero.
    """
    if source.kind == "env":
        if not source.env:
            raise ValueError("credential_proxy env source requires an 'env' name")
        value = parent_env.get(source.env)
        if value is None or not value.strip():
            raise ValueError(f"credential_proxy env source {source.env!r} is missing or empty")
        return value.strip()
    if source.kind == "file":
        if not source.path:
            raise ValueError("credential_proxy file source requires a 'path'")
        path = Path(os.path.expanduser(source.path))
        if not path.is_file():
            raise ValueError(f"credential_proxy file source does not exist: {path}")
        value = path.read_text(encoding="utf-8").strip()
        if not value:
            raise ValueError(f"credential_proxy file source is empty: {path}")
        return value
    if source.kind == "command":
        if not source.command:
            raise ValueError("credential_proxy command source requires a 'command'")
        # ``shell=True`` is intentional: the command is spec-author
        # supplied (e.g. ``gh auth token``) and runs in the trusted
        # parent process, never inside the sandbox.
        completed = subprocess.run(
            source.command,
            shell=True,
            capture_output=True,
            text=True,
            env=parent_env,
            timeout=_COMMAND_SOURCE_TIMEOUT_SECONDS,
            check=False,
        )
        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()
            raise ValueError(
                f"credential_proxy command source exited {completed.returncode}"
                + (f": {stderr}" if stderr else "")
            )
        value = completed.stdout.strip()
        if not value:
            raise ValueError("credential_proxy command source produced empty stdout")
        return value
    raise ValueError(f"unsupported credential_proxy source kind: {source.kind!r}")


# Default seconds between re-authentications for a Databricks profile. The
# OAuth token lives ~1h and the SDK refreshes it in-memory near expiry, so a
# short throttle keeps per-request overhead negligible while still picking up
# a refreshed token well before it expires.
_DATABRICKS_REFRESH_INTERVAL_SECONDS = 60.0


class DatabricksProfileTokenProvider:
    """Refreshing bearer-token source for one ``~/.databrickscfg`` profile.

    Resolves a Databricks workspace profile via the ``databricks`` SDK in
    the (trusted) parent. The workspace URL / host are read once from the
    profile at construction; the bearer token is minted lazily on first
    use and re-minted via ``Config.authenticate()`` at most once per
    :attr:`_refresh_interval` so a long agent session survives OAuth
    access-token expiry (the SDK caches in-memory and only re-shells the
    CLI near expiry). Thread-safe: the egress proxy resolves from its own
    event-loop thread.

    The real token never leaves the parent — only a synthetic placeholder
    is written into the sandbox ``.databrickscfg``.
    """

    def __init__(
        self,
        profile: str,
        *,
        refresh_interval: float = _DATABRICKS_REFRESH_INTERVAL_SECONDS,
        config_factory: Callable[[str], object] | None = None,
        authenticate: Callable[[object], str] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """
        Build a provider for *profile*, resolving its workspace host now.

        :param profile: The ``~/.databrickscfg`` profile (section) name.
        :param refresh_interval: Minimum seconds between re-authentications.
        :param config_factory: Test seam returning an object exposing a
            ``host`` attribute for *profile* (defaults to the SDK ``Config``).
        :param authenticate: Test seam minting a bearer from the config
            object (defaults to ``Config.authenticate()`` unpacking).
        :param clock: Monotonic clock, injectable for tests.
        :raises OmnigentError: If the SDK is unavailable or the profile has
            no ``host``.
        """
        self.profile = profile
        self._refresh_interval = refresh_interval
        self._authenticate = authenticate or _databricks_authenticate
        self._clock = clock
        self._lock = threading.Lock()
        self._token: str | None = None
        self._next_refresh = 0.0

        factory = config_factory or _databricks_config
        self._config = factory(profile)
        host = getattr(self._config, "host", None)
        if not host or not str(host).strip():
            raise OmnigentError(
                f"Databricks profile {profile!r} has no 'host'; add it to "
                "~/.databrickscfg (e.g. `databricks auth login --profile "
                f"{profile}`).",
                code=ErrorCode.INVALID_INPUT,
            )
        self.workspace_url = str(host).strip().rstrip("/")
        hostname = urlsplit(self.workspace_url).hostname
        if not hostname:
            raise OmnigentError(
                f"Databricks profile {profile!r} host {self.workspace_url!r} is not a valid URL.",
                code=ErrorCode.INVALID_INPUT,
            )
        self.hostname = hostname.lower()

    def resolve(self) -> str:
        """
        Return the current bearer token, re-minting past the throttle window.

        :returns: A live workspace bearer token.
        :raises OmnigentError: If authentication fails.
        """
        with self._lock:
            now = self._clock()
            if self._token is not None and now < self._next_refresh:
                return self._token
            token = self._authenticate(self._config)
            if not token:
                raise OmnigentError(
                    f"Databricks profile {self.profile!r} produced an empty token.",
                    code=ErrorCode.INVALID_INPUT,
                )
            self._token = token
            self._next_refresh = now + self._refresh_interval
            return token


def _databricks_config(profile: str) -> object:
    """
    Build a ``databricks.sdk.config.Config`` for *profile* (SDK required).

    :param profile: The ``~/.databrickscfg`` profile name.
    :returns: A configured SDK ``Config`` instance.
    :raises OmnigentError: If the ``databricks`` extra is not installed or
        the profile cannot be loaded.
    """
    try:
        from databricks.sdk.config import Config
    except ImportError as exc:
        raise OmnigentError(
            "os_env.sandbox.credential_proxy type 'databricks_cli' requires the "
            "Databricks SDK. Install the 'databricks' extra (e.g. "
            "`pip install omnigent[databricks]`).",
            code=ErrorCode.INVALID_INPUT,
        ) from exc
    try:
        return Config(profile=profile)
    except Exception as exc:
        raise OmnigentError(
            f"Failed to load Databricks profile {profile!r}: {exc}",
            code=ErrorCode.INVALID_INPUT,
        ) from exc


def _databricks_authenticate(config: object) -> str:
    """
    Mint a fresh bearer from an SDK ``Config`` via ``authenticate()``.

    :param config: An SDK ``Config`` instance.
    :returns: The bearer token (``Authorization`` value minus ``Bearer ``).
    :raises OmnigentError: If authentication fails or returns a non-Bearer
        scheme.
    """
    try:
        headers = config.authenticate()  # type: ignore[attr-defined]
    except Exception as exc:
        raise OmnigentError(
            f"Databricks authentication failed: {exc}",
            code=ErrorCode.INVALID_INPUT,
        ) from exc
    auth = (headers or {}).get("Authorization", "")
    if not isinstance(auth, str) or not auth.startswith("Bearer "):
        raise OmnigentError(
            "Databricks authentication returned a non-Bearer scheme; only "
            "Bearer tokens (PAT / OAuth) are supported by the credential proxy.",
            code=ErrorCode.INVALID_INPUT,
        )
    return auth.removeprefix("Bearer ").strip()


def _prepare_databricks_runtime(
    spec: DatabricksProxySpec,
    runtime: CredentialProxyRuntime,
    *,
    provider_factory: Callable[[str], DatabricksProfileTokenProvider] | None = None,
) -> None:
    """
    Resolve Databricks profiles into rewrite rules + a placeholder cfg.

    For each profile the parent resolves the workspace host, mints one
    ``oa_cred_*`` placeholder, and adds a refreshing bearer rewrite rule
    bound to that host. A ``.databrickscfg`` naming every profile with its
    placeholder token is materialized into the sandbox. Reaching the
    workspace still requires the operator to list its host in
    ``egress_rules`` — the same as every other credential-proxy type.

    :param spec: The parsed Databricks proxy policy.
    :param runtime: The runtime being assembled (mutated in place).
    :param provider_factory: Test seam building a provider per profile.
    :raises OmnigentError: If a profile can't be resolved or two profiles
        resolve to the same workspace host (they'd collide in the proxy's
        host-keyed rewrite table).
    """
    factory = provider_factory or DatabricksProfileTokenProvider
    sections: list[str] = []
    seen_hosts: dict[str, str] = {}
    for binding in spec.profiles:
        provider = factory(binding.profile)
        host = provider.hostname
        if host in seen_hosts:
            raise OmnigentError(
                f"Databricks profiles {seen_hosts[host]!r} and "
                f"{binding.profile!r} both resolve to workspace host {host!r}; "
                "the credential proxy binds one credential per host. Proxy only "
                "one profile per workspace.",
                code=ErrorCode.INVALID_INPUT,
            )
        seen_hosts[host] = binding.profile
        synthetic = f"{SYNTHETIC_CREDENTIAL_PREFIX}{secrets.token_urlsafe(24)}"
        runtime.rewrites.append(
            CredentialRewriteRule(
                host=host,
                scheme="bearer",
                secret_provider=provider.resolve,
                synthetic=synthetic,
            )
        )
        sections.append(
            f"[{binding.profile}]\nhost = {provider.workspace_url}\ntoken = {synthetic}\n"
        )
    runtime.sandbox_files.append(
        MaterializedFile(
            name="databrickscfg",
            content="\n".join(sections),
            mode=0o600,
            env_var=spec.config_env,
        )
    )
    if spec.default is not None:
        runtime.helper_env_updates["DATABRICKS_CONFIG_PROFILE"] = spec.default


__all__ = [
    "SYNTHETIC_CREDENTIAL_PREFIX",
    "CredentialProxyRuntime",
    "CredentialRewriteRule",
    "DatabricksProfileTokenProvider",
    "MaterializedFile",
    "prepare_credential_proxy_runtime",
]
