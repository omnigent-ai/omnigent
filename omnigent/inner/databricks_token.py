"""Mint a Databricks bearer via the databricks-sdk unified auth resolver.

Run as ``python3 -m omnigent.inner.databricks_token --host <h> [--profile <p>]``.
This is the auth-command fallback the native-harness gateway path installs (see
:func:`omnigent.inner.databricks_executor.databricks_bearer_token_command`).

``databricks auth token`` supports **U2M only** ("M2M authentication using a
client ID and secret is not supported"), so an agent authenticating as an OAuth
**M2M service principal** (a workload identity, e.g. a self-hosted Fargate host)
never got a Gateway bearer. The databricks-sdk's ``Config.authenticate()`` does
the client-credentials exchange (and env / file OIDC and a static PAT), so this
mints through the SDK rather than reimplementing any OAuth.

Identity is **fail-closed and profile-pinned**. The SDK resolves config in the
order kwargs > environment > profile-section, so ``Config(profile=P)`` alone does
NOT pin identity: an ambient ``DATABRICKS_TOKEN`` / ``DATABRICKS_CLIENT_ID`` (etc.)
outranks the named profile's own section and would mint a *different* identity on
the very hosts this targets (Fargate routinely injects ``DATABRICKS_*``). So the
profiled branch first scrubs every ambient credential env var (mirroring the CLI
mint's ``env -u``), leaving the profile section authoritative. Ambient env / OIDC
is used only in the unprofiled ``--host`` mode. In both cases the token is
withheld unless the resolved workspace matches the requested ``--host`` (the
gateway's workspace, an https match), so a profile aimed elsewhere can't hand its
bearer here.

Prints the bearer on stdout, or nothing (exit 0) on any failure, so the caller's
shell falls through cleanly — mirroring
:func:`omnigent.host.databricks_credential.main`. A genuine M2M misconfig (bad
secret, unreachable token endpoint) surfaces as an empty bearer / 401 at the
gateway; the harness re-runs this command near expiry, so a transient blip
self-heals next refresh.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys

# Bound the SDK's network (OIDC metadata discovery + token exchange) to parity
# with the timeout-bounded CLI mint (``--timeout 15s``) and broker (15s). The SDK
# path is otherwise unbounded; ``Config.__init__``'s metadata probe was observed
# hanging minutes against an unreachable endpoint, and this runs as an auth
# command the harness re-runs near token expiry.
_SDK_NETWORK_TIMEOUT_S = 15.0


def _ambient_credential_env_vars() -> set[str]:
    """Env vars the databricks-sdk reads into a Config, minus the config-file
    locator (``DATABRICKS_CONFIG_FILE``, needed to find the profile).

    Enumerated from the SDK's own attribute table so the set stays complete as
    the SDK gains auth types. Scrubbing these before ``Config(profile=...)``
    makes a named profile's own section authoritative — the SDK otherwise lets
    ambient env outrank it (kwargs > env > profile-section).
    """
    from databricks.sdk.config import Config

    names: set[str] = set()
    for attr in Config.attributes():
        candidates = [attr.env] if getattr(attr, "env", None) else []
        candidates += list(getattr(attr, "env_aliases", []) or [])
        for name in candidates:
            if name and name != "DATABRICKS_CONFIG_FILE":
                names.add(name)
    return names


def _sdk_bearer(profile: str | None, host: str | None) -> tuple[str, str] | None:
    """Return ``(resolved_host, bearer)`` from the databricks-sdk, or ``None``.

    A named *profile* is pinned to its own section: ambient credential env vars
    are scrubbed first so they cannot outrank it. Only the unprofiled path
    consults env / OIDC. The network (metadata probe + token exchange) is bounded
    to ``_SDK_NETWORK_TIMEOUT_S``. Raises on a genuine SDK error (e.g. an
    unreachable token endpoint) so :func:`main` can fall through quietly.
    """
    from databricks.sdk.config import Config

    # Bound network so a slow/unreachable OIDC or token endpoint can't stall this
    # auth command; restore the prior default so the bound never leaks out.
    previous_timeout = socket.getdefaulttimeout()
    socket.setdefaulttimeout(_SDK_NETWORK_TIMEOUT_S)
    try:
        if profile:
            # Pin identity to the profile section: strip ambient DATABRICKS_*
            # creds (which the SDK would otherwise rank above the profile),
            # mirroring the CLI mint's ``env -u``.
            for name in _ambient_credential_env_vars():
                os.environ.pop(name, None)
            cfg = Config(profile=profile)
        else:
            # Unprofiled: mirror ``databricks auth token --host`` — drop any
            # ambient profile and pin the SDK to this workspace so env / OIDC
            # service-principal credentials mint against it.
            os.environ.pop("DATABRICKS_CONFIG_PROFILE", None)
            cfg = Config(host=host) if host else Config()
        auth = cfg.authenticate().get("Authorization", "")
        if not cfg.host or not auth.startswith("Bearer "):
            return None
        return cfg.host, auth[len("Bearer ") :]
    finally:
        socket.setdefaulttimeout(previous_timeout)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--profile")
    parser.add_argument("--host")
    args, _ = parser.parse_known_args(argv)
    try:
        import databricks.sdk.config  # noqa: F401 - the `databricks` extra is required to mint
    except ImportError:
        # Base install without the extra can't mint M2M. Hint on stderr (stdout
        # stays the token channel) so this is debuggable, not a silent no-op.
        # Scoped to the SDK import so an optional-dep ImportError from inside
        # authenticate() is not misattributed.
        sys.stderr.write(
            "omnigent.inner.databricks_token: databricks-sdk is not installed; "
            "install the 'databricks' extra to mint M2M / service-principal tokens.\n"
        )
        return 0
    try:
        resolved = _sdk_bearer(args.profile or None, args.host)
    except Exception:  # noqa: BLE001 - a fallback must never emit noise; fall through.
        return 0
    if resolved is None:
        return 0
    resolved_host, bearer = resolved
    # The gateway base URL is pinned to ``--host``; only release a token minted
    # for that same https workspace, so a profile (or ambient creds) aimed
    # elsewhere can't present its bearer here. Fail closed on an empty host, and
    # reuse the sibling helper's HTTPS-requiring comparator.
    from omnigent.host.databricks_credential import https_url_on_workspace_host

    if not args.host or not https_url_on_workspace_host(resolved_host, args.host):
        return 0
    if not bearer:
        return 0
    sys.stdout.write(bearer + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
