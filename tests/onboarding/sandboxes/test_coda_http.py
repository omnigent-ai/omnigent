"""Tests for CoDA control transport failure handling."""

from __future__ import annotations

import sys
from types import ModuleType
from urllib import error, request

import click
import pytest

from omnigent.onboarding.sandboxes.coda import CodaProvider, _NoRedirectHandler


def _install_fake_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Config:
        def authenticate(self) -> dict[str, str]:
            return {}

    core = ModuleType("databricks.sdk.core")
    core.Config = _Config  # type: ignore[attr-defined]
    sdk = ModuleType("databricks.sdk")
    sdk.core = core  # type: ignore[attr-defined]
    databricks = ModuleType("databricks")
    databricks.sdk = sdk  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "databricks", databricks)
    monkeypatch.setitem(sys.modules, "databricks.sdk", sdk)
    monkeypatch.setitem(sys.modules, "databricks.sdk.core", core)


def test_http_control_errors_use_fixed_classifications(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Response:
        def read(self, _size: int = -1) -> bytes:
            return b'{"error":"' + b"x" * 2000 + b'","field":"value"}'

        def close(self) -> None:
            pass

    def _open(*args: object, **kwargs: object) -> _Response:
        raise error.HTTPError(
            "https://synthetic-coda.aws.databricksapps.com/api/omnigent-host/lease",
            500,
            "failure",
            {},
            _Response(),
        )

    _install_fake_sdk(monkeypatch)
    monkeypatch.setattr("omnigent.onboarding.sandboxes.coda._NO_REDIRECT_OPENER.open", _open)
    provider = CodaProvider(
        app_name="synthetic-coda",
        app_url="https://synthetic-coda.aws.databricksapps.com",
    )

    with pytest.raises(click.ClickException, match="upstream control request failed") as exc_info:
        provider._request("POST", "/api/omnigent-host/lease", {})
    assert "value" not in str(exc_info.value)


def test_http_409_keeps_capacity_classification(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Response:
        def read(self, _size: int = -1) -> bytes:
            return b'{"error":"' + b"x" * 2000 + b'","field":"value"}'

        def close(self) -> None:
            pass

    def _open(*args: object, **kwargs: object) -> _Response:
        raise error.HTTPError(
            "https://synthetic-coda.aws.databricksapps.com/api/omnigent-host/lease",
            409,
            "conflict",
            {},
            _Response(),
        )

    _install_fake_sdk(monkeypatch)
    monkeypatch.setattr("omnigent.onboarding.sandboxes.coda._NO_REDIRECT_OPENER.open", _open)
    provider = CodaProvider(
        app_name="synthetic-coda",
        app_url="https://synthetic-coda.aws.databricksapps.com",
    )

    with pytest.raises(click.ClickException, match="no available lease capacity") as exc_info:
        provider._request("POST", "/api/omnigent-host/lease", {})
    assert "value" not in str(exc_info.value)


def test_control_request_does_not_follow_redirect(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Response:
        def read(self, _size: int = -1) -> bytes:
            return b"{}"

        def close(self) -> None:
            pass

    _install_fake_sdk(monkeypatch)
    calls: list[str] = []

    def _open(req: object, *, timeout: int) -> _Response:
        calls.append(req.full_url)
        raise error.HTTPError(
            calls[-1],
            302,
            "redirect",
            {"Location": "https://attacker.example/collect"},
            _Response(),
        )

    monkeypatch.setattr("omnigent.onboarding.sandboxes.coda._NO_REDIRECT_OPENER.open", _open)
    provider = CodaProvider(
        app_name="synthetic-coda",
        app_url="https://synthetic-coda.aws.databricksapps.com",
    )

    with pytest.raises(click.ClickException, match="302"):
        provider._request("GET", "/api/omnigent-host/status", None)
    assert calls == ["https://synthetic-coda.aws.databricksapps.com/api/omnigent-host/status"]

    with pytest.raises(error.HTTPError):
        _NoRedirectHandler().redirect_request(
            request.Request("https://synthetic-coda.aws.databricksapps.com"),
            _Response(),
            302,
            "redirect",
            {"Location": "https://attacker.example"},
            "https://attacker.example",
        )


def test_invalid_utf8_success_response_becomes_click_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Response:
        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def read(self, _size: int = -1) -> bytes:
            return b"\xff"

    _install_fake_sdk(monkeypatch)
    monkeypatch.setattr(
        "omnigent.onboarding.sandboxes.coda._NO_REDIRECT_OPENER.open",
        lambda _req, timeout: _Response(),
    )
    provider = CodaProvider(
        app_name="synthetic-coda",
        app_url="https://synthetic-coda.aws.databricksapps.com",
    )

    with pytest.raises(click.ClickException, match="valid JSON"):
        provider._request("GET", "/api/omnigent-host/status", None)
