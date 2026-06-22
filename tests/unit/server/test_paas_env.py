"""Unit tests for PaaS bind-host + base-URL derivation (omnigent.server.paas_env)."""

import pytest

from omnigent.server.paas_env import detect_base_url, resolve_bind_host

# A minimal env that trips the Railway detection (any RAILWAY_-prefixed key).
_RAILWAY_ENV = {"RAILWAY_PUBLIC_DOMAIN": "svc.up.railway.app"}


@pytest.mark.parametrize(
    "configured_host, environ, expected",
    [
        # Explicit host wins, untouched, on a non-PaaS env.
        ("192.0.2.10", {}, "192.0.2.10"),
        # Unset → the supplied default.
        (None, {}, "0.0.0.0"),
        # Bracketed IPv6 wildcard is unwrapped (a socket bind rejects "[::]").
        ("[::]", {}, "::"),
        # Bare IPv6 wildcard is preserved off Railway (a deliberate v6 bind).
        ("::", {}, "::"),
        # On Railway the injected "[::]" is unwrapped AND coerced to IPv4,
        # because Railway's edge proxy only reaches the app over IPv4.
        ("[::]", _RAILWAY_ENV, "0.0.0.0"),
        ("::", _RAILWAY_ENV, "0.0.0.0"),
        # An explicit IPv4 host on Railway is left alone.
        ("0.0.0.0", _RAILWAY_ENV, "0.0.0.0"),
    ],
)
def test_resolve_bind_host(configured_host: str | None, environ: dict[str, str], expected: str):
    """resolve_bind_host unwraps bracketed IPv6 and coerces Railway's v6 wildcard.

    A failure means a platform host quirk regressed: either the bracket-strip
    dropped (Railway boot crashes on ``getaddrinfo("[::]")``) or the Railway
    v6->v4 coercion dropped (the app binds the v6 wildcard, Railway's v4 edge
    can't reach it, and health checks fail).
    """
    assert resolve_bind_host(configured_host, environ, default="0.0.0.0") == expected


@pytest.mark.parametrize(
    "environ, expected",
    [
        # Render gives a full https URL directly.
        ({"RENDER_EXTERNAL_URL": "https://svc.onrender.com"}, "https://svc.onrender.com"),
        # Railway gives a bare host → https:// prefix added.
        ({"RAILWAY_PUBLIC_DOMAIN": "svc.up.railway.app"}, "https://svc.up.railway.app"),
        # Fly gives the app name → <app>.fly.dev.
        ({"FLY_APP_NAME": "myapp"}, "https://myapp.fly.dev"),
        # HF Spaces gives a bare host.
        ({"SPACE_HOST": "user-space.hf.space"}, "https://user-space.hf.space"),
        # Precedence: Render beats every other provider var when several are set.
        (
            {
                "RENDER_EXTERNAL_URL": "https://svc.onrender.com",
                "RAILWAY_PUBLIC_DOMAIN": "svc.up.railway.app",
                "FLY_APP_NAME": "myapp",
                "SPACE_HOST": "user-space.hf.space",
            },
            "https://svc.onrender.com",
        ),
        # Precedence: Railway beats Fly + HF when Render is absent.
        (
            {
                "RAILWAY_PUBLIC_DOMAIN": "svc.up.railway.app",
                "FLY_APP_NAME": "myapp",
                "SPACE_HOST": "user-space.hf.space",
            },
            "https://svc.up.railway.app",
        ),
        # Precedence: Fly beats HF when only those two are set.
        ({"FLY_APP_NAME": "myapp", "SPACE_HOST": "user-space.hf.space"}, "https://myapp.fly.dev"),
        # No provider var → local fallback to the bind address.
        ({}, "http://0.0.0.0:8000"),
    ],
)
def test_detect_base_url(environ: dict[str, str], expected: str):
    """detect_base_url picks each provider's URL var, falling back to the bind addr.

    A failure means a provider's public-URL derivation regressed (wrong env
    var, missing ``https://`` prefix, or broken precedence), which would set a
    wrong ``OMNIGENT_ACCOUNTS_BASE_URL`` and break magic-link / cookie flows.
    """
    assert detect_base_url(environ, host="0.0.0.0", port=8000) == expected
