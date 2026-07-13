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
from dataclasses import dataclass, field
from pathlib import Path

from omnigent.inner.datamodel import (
    CredentialProxySpec,
    CredentialSourceSpec,
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
    """

    host: str
    scheme: str
    real_secret: str
    synthetic: str | None = None
    username: str | None = None


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
    """

    helper_env_updates: dict[str, str] = field(default_factory=dict)
    rewrites: list[CredentialRewriteRule] = field(default_factory=list)


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


__all__ = [
    "SYNTHETIC_CREDENTIAL_PREFIX",
    "CredentialProxyRuntime",
    "CredentialRewriteRule",
    "prepare_credential_proxy_runtime",
]
