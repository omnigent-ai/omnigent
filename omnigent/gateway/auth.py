"""Databricks bearer minting for the gateway servlet.

The servlet holds credentials in memory and *mints* them (via the Databricks
CLI's own OAuth cache) instead of re-reading a token file — the design that
keeps long sessions from serving an expired bearer forever. Minting happens
off the per-request hot path via a short-TTL cache; access tokens live ~1h,
so a 15-minute re-mint cadence always stays ahead of expiry.
"""

from __future__ import annotations

import asyncio
import configparser
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

# Re-mint cadence CEILING; matches the harness-side seams (claude apiKeyHelper
# TTL / codex refresh_interval_ms are both 900s). The effective cache window is
# clamped to the minted token's own remaining lifetime — the CLI returns its
# cached OAuth token, which can be minutes from expiry.
_TOKEN_TTL_S = 900.0
# Serve a token only while it has at least this much life left; below it the
# cache is skipped so every request re-mints (and the CLI refreshes).
_EXPIRY_SAFETY_S = 60.0
_MINT_TIMEOUT_S = 30.0


def databrickscfg_host_for_profile(profile: str) -> str | None:
    """
    Resolve a profile's workspace host from ``~/.databrickscfg``.

    Registration passes only a profile *name* (a pointer into shared host
    config); the servlet resolves the host itself from the same file the
    launcher read.

    :param profile: Profile section name, e.g. ``"oss"``.
    :returns: The workspace origin without a trailing slash, or ``None`` when
        the file/section/host is absent or unreadable.
    """
    parser = configparser.ConfigParser()
    try:
        parser.read(Path.home() / ".databrickscfg")
    except (OSError, configparser.Error):
        return None
    if not parser.has_section(profile):
        return None
    host = parser.get(profile, "host", fallback="").strip().rstrip("/")
    return host or None


class TokenMinter:
    """Per-profile Databricks bearer cache with async minting."""

    def __init__(self) -> None:
        self._cache: dict[str, tuple[str, float]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def bearer(self, profile: str) -> str:
        """
        Return a fresh-enough bearer for *profile*.

        :param profile: ``~/.databrickscfg`` profile name, e.g. ``"oss"``.
        :returns: An access token.
        :raises RuntimeError: When minting fails (dead auth); the caller
            surfaces this as a 502 with the real cause.
        """
        # Test/dev escape hatch. Profile-blind by design: it overrides every
        # profile, so it is only correct on single-workspace hosts.
        env_bearer = os.environ.get("DATABRICKS_BEARER", "").strip()
        if env_bearer:
            return env_bearer
        cached = self._cache.get(profile)
        if cached is not None and cached[1] > time.monotonic():
            return cached[0]
        lock = self._locks.setdefault(profile, asyncio.Lock())
        async with lock:
            cached = self._cache.get(profile)
            if cached is not None and cached[1] > time.monotonic():
                return cached[0]
            token, cache_ttl = await self._mint(profile)
            if cache_ttl > 0:
                self._cache[profile] = (token, time.monotonic() + cache_ttl)
            else:
                self._cache.pop(profile, None)
            return token

    def invalidate(self, profile: str) -> None:
        """
        Drop *profile*'s cached bearer.

        Called by the relay when the workspace rejects the minted bearer
        (401/403): the cached token is provably dead no matter what its
        clock says, so the next request must re-mint instead of failing
        for the rest of the cache window.

        :param profile: ``~/.databrickscfg`` profile name, e.g. ``"oss"``.
        :returns: None.
        """
        self._cache.pop(profile, None)

    async def _mint(self, profile: str) -> tuple[str, float]:
        """
        Mint one token via ``databricks auth token`` (never re-reads a file).

        :param profile: ``~/.databrickscfg`` profile name.
        :returns: ``(token, cache_ttl_seconds)`` — the TTL is the re-mint
            ceiling clamped to the token's own remaining lifetime, ``0``
            when the token is too close to expiry to cache.
        """
        env = {k: v for k, v in os.environ.items() if k != "DATABRICKS_CONFIG_PROFILE"}
        proc = await asyncio.create_subprocess_exec(
            "databricks",
            "auth",
            "token",
            "--profile",
            profile,
            "--output",
            "json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_MINT_TIMEOUT_S)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeError(
                f"databricks auth token timed out for profile {profile!r}"
            ) from None
        if proc.returncode != 0:
            detail = stderr.decode(errors="replace").strip()[:300]
            raise RuntimeError(
                f"databricks auth token failed for profile {profile!r}: {detail} "
                f"(run `databricks auth login --profile {profile}` to re-authenticate)"
            )
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            payload = {}
        token = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token:
            raise RuntimeError(f"databricks auth token returned no access_token for {profile!r}")
        return token, _cache_ttl_for(payload)


def _cache_ttl_for(payload: dict) -> float:
    """
    Cache window for a minted token: the re-mint ceiling clamped to its life.

    The CLI hands out its *cached* OAuth token, which can be near expiry;
    a flat cadence served dead bearers for the rest of the window (upstream
    401 "Invalid Token"). Prefers the CLI's ``expires_in`` seconds, falls
    back to parsing the RFC3339 ``expiry``, and keeps the plain ceiling when
    neither is usable.

    :param payload: Decoded ``databricks auth token --output json`` object.
    :returns: Seconds to cache for; ``0`` disables caching for this token.
    """
    remaining: float | None = None
    expires_in = payload.get("expires_in")
    if isinstance(expires_in, (int, float)) and not isinstance(expires_in, bool):
        remaining = float(expires_in)
    if remaining is None:
        expiry = payload.get("expiry")
        if isinstance(expiry, str):
            # Go emits RFC3339 with variable fractional precision and "Z";
            # normalize to what ``fromisoformat`` accepts on Python 3.9.
            normalized = re.sub(r"\.(\d{1,6})\d*", lambda m: "." + m.group(1), expiry)
            normalized = normalized.replace("Z", "+00:00")
            try:
                expiry_dt = datetime.fromisoformat(normalized)
            except ValueError:
                expiry_dt = None
            if expiry_dt is not None and expiry_dt.tzinfo is not None:
                remaining = (expiry_dt - datetime.now(timezone.utc)).total_seconds()
    if remaining is None:
        return _TOKEN_TTL_S
    return max(0.0, min(_TOKEN_TTL_S, remaining - _EXPIRY_SAFETY_S))
