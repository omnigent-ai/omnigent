"""
Unit tests for the ``require_trusted_origin`` CSRF guard dependency.

The dependency adapts the shared, protocol-neutral
:func:`omnigent.server.ws_origin.origin_allowed` policy into a FastAPI
dependency for HTTP routes that accept ``multipart/form-data`` (which the
JSON Content-Type guard cannot protect, because multipart is
CORS-safelisted).

The full origin decision table is already covered by
``tests/server/test_ws_origin.py`` against ``origin_allowed`` directly, so
these tests deliberately do NOT re-enumerate it. They verify only how the
dependency wires that policy into an HTTP route:

- a **missing** ``Origin`` currently fails open (request proceeds) — a
  temporary backward-compat posture, to be closed soon, so that clients
  not yet sending an ``Origin`` keep working. When it is closed, flip
  ``test_absent_origin_is_allowed`` to expect a 403; the rest of the suite
  already sends the sentinel ``Origin`` and so is unaffected;
- it sources ``local_mode`` from ``local_single_user_enabled()`` and
  ``extra_allowed`` from ``parse_allowed_origins()`` (the env wiring) —
  proven by flipping ``OMNIGENT_LOCAL_SINGLE_USER`` /
  ``OMNIGENT_WS_ALLOWED_ORIGINS`` and watching the same Origin change verdict;
- an allowed verdict returns ``None`` (request proceeds);
- a denied verdict raises ``HTTPException`` with status ``403``.

The dependency only reads ``request.headers``, so each case is driven by a
real :class:`starlette.requests.Request` built from a minimal ASGI scope
carrying just the ``Origin`` header under test — no body or transport.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from omnigent.server.routes._origin import require_trusted_origin

_LOCAL_ENV = "OMNIGENT_LOCAL_SINGLE_USER"
_ALLOWLIST_ENV = "OMNIGENT_WS_ALLOWED_ORIGINS"

# A concrete cross-site origin used as the attacker's page throughout.
_EVIL_ORIGIN = "https://evil.example.com"


@pytest.fixture(autouse=True)
def _clean_origin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Start every test from a known origin-env baseline.

    ``require_trusted_origin`` reads ``OMNIGENT_LOCAL_SINGLE_USER`` and
    ``OMNIGENT_WS_ALLOWED_ORIGINS`` per call (the test suite sets the
    former to ``"1"`` globally, and a developer shell may set either), so
    a value inherited from the environment would flip the policy and make
    these tests pass or fail for the wrong reason. Each test sets only
    what it needs on top of this cleared baseline.

    :param monkeypatch: pytest env patcher.
    :returns: None.
    """
    monkeypatch.delenv(_LOCAL_ENV, raising=False)
    monkeypatch.delenv(_ALLOWLIST_ENV, raising=False)


def _request_with_origin(origin: str | None) -> Request:
    """
    Build a real Starlette ``Request`` carrying (or omitting) an Origin.

    :param origin: The ``Origin`` header value to set, e.g.
        ``"https://evil.example.com"``. ``None`` omits the header
        entirely, modelling a non-browser client that sends no Origin.
    :returns: A real :class:`starlette.requests.Request` whose only
        meaningful state is its header set.
    """
    raw_headers: list[tuple[bytes, bytes]] = []
    if origin is not None:
        raw_headers.append((b"origin", origin.encode("latin-1")))
    return Request({"type": "http", "method": "POST", "headers": raw_headers})


@pytest.mark.parametrize("local_mode", [True, False])
def test_absent_origin_is_allowed(monkeypatch: pytest.MonkeyPatch, local_mode: bool) -> None:
    """
    A request with no ``Origin`` is allowed in every mode.

    Modern browsers attach ``Origin`` to every cross-site (and same-site
    state-changing) request — and it is on the forbidden-header list, so
    page JS cannot strip or forge it — so an absent ``Origin`` is not a
    browser CSRF vector. Allowing it preserves backward compatibility for
    non-browser and older first-party clients that send none. The mode is
    irrelevant — absent passes whether or not local mode is set — so a
    raise in either case would wrongly block those clients.
    """
    if local_mode:
        monkeypatch.setenv(_LOCAL_ENV, "1")
    assert require_trusted_origin(_request_with_origin(None)) is None


def test_loopback_origin_is_allowed_in_local_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A loopback ``Origin`` is allowed in local mode (the user's own UI).

    The local web UI is served from a loopback host, so its same-origin
    POSTs must pass. A raise here would block the legitimate local UI.
    """
    monkeypatch.setenv(_LOCAL_ENV, "1")
    assert require_trusted_origin(_request_with_origin("http://localhost:8000")) is None


def test_cross_origin_is_rejected_in_local_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A cross-site ``Origin`` is rejected with 403 in local mode.

    This is the CSRF guard itself: the local server has no cookie / proxy
    auth, so a non-loopback browser Origin is the attack. A non-403 (or no
    raise) would mean a cross-site multipart upload reaches the handler.
    """
    monkeypatch.setenv(_LOCAL_ENV, "1")
    with pytest.raises(HTTPException) as exc_info:
        require_trusted_origin(_request_with_origin(_EVIL_ORIGIN))
    # 403 Forbidden is the documented rejection status for an untrusted
    # Origin; any other code (or no raise) means the guard did not fire.
    assert exc_info.value.status_code == 403


def test_cross_origin_is_allowed_when_not_local_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    The SAME cross-site ``Origin`` is allowed when local mode is off.

    Proves the dependency actually sources ``local_mode`` from
    ``local_single_user_enabled()``: with the env unset, authenticated
    (cookie / proxy) modes guard CSRF by other means, so the policy passes
    the origin through. If this raised, the dependency would be ignoring
    the env and hard-coding local-mode strictness — breaking deployed
    multi-user servers whose browser Origin is not loopback.
    """
    # _clean_origin_env left OMNIGENT_LOCAL_SINGLE_USER unset → non-local.
    assert require_trusted_origin(_request_with_origin(_EVIL_ORIGIN)) is None


def test_sentinel_origin_is_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    The first-party sentinel ``Origin`` is allowed, even in local mode.

    The project's own non-browser clients may announce
    ``OMNIGENT_INTERNAL_WS_ORIGIN``; the shared policy always admits it. A
    raise here would block first-party traffic that opts to send the
    sentinel.
    """
    monkeypatch.setenv(_LOCAL_ENV, "1")
    assert require_trusted_origin(_request_with_origin(OMNIGENT_INTERNAL_WS_ORIGIN)) is None


def test_allowlisted_origin_is_allowed_and_unlisted_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``OMNIGENT_WS_ALLOWED_ORIGINS`` wiring: listed passes, unlisted 403s.

    Proves the dependency sources ``extra_allowed`` from
    ``parse_allowed_origins()``. In non-local mode a configured allowlist
    flips the default to deny, so an origin ON the list passes while one
    NOT on it is rejected with 403. If the allowlist were not read, the
    listed origin would still pass (non-local passthrough) but the
    unlisted one would wrongly pass too — the second assertion catches that.
    """
    monkeypatch.setenv(_ALLOWLIST_ENV, "https://ui.example.com")
    # On the allowlist → allowed.
    assert require_trusted_origin(_request_with_origin("https://ui.example.com")) is None
    # Not on the allowlist, and a non-empty allowlist flips non-local mode
    # to deny-by-default → 403.
    with pytest.raises(HTTPException) as exc_info:
        require_trusted_origin(_request_with_origin(_EVIL_ORIGIN))
    assert exc_info.value.status_code == 403
