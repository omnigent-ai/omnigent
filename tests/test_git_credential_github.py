"""Tests for the sandbox-side GitHub credential helper + host git setup."""

from __future__ import annotations

import io
import sys

import pytest

import omnigent.git_credential_github as h
from omnigent.host.identity import HOST_TOKEN_ENV_VAR


def test_get_prints_credentials_for_github(monkeypatch: pytest.MonkeyPatch) -> None:
    cred = {"connected": True, "username": "x-access-token", "token": "T"}
    monkeypatch.setattr(h, "_fetch", lambda *a, **k: cred)
    monkeypatch.setattr(sys, "stdin", io.StringIO("protocol=https\nhost=github.com\n\n"))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    rc = h.main(["--server", "http://s", "--host-id", "hid", "--host-token", "tok", "get"])
    assert rc == 0
    assert "username=x-access-token" in out.getvalue()
    assert "password=T" in out.getvalue()


def test_get_declines_for_non_github_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("protocol=https\nhost=gitlab.com\n\n"))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    rc = h.main(["--server", "http://s", "--host-id", "hid", "--host-token", "tok", "get"])
    assert rc == 0 and out.getvalue() == ""  # declined → git falls through


def test_get_fails_closed_on_missing_host_or_non_https(monkeypatch: pytest.MonkeyPatch) -> None:
    # Fail closed: an empty/missing host or a non-https protocol must decline
    # (no output) so the brokered token never leaks to an unintended host.
    for stdin in ("protocol=https\n\n", "protocol=http\nhost=github.com\n\n", "\n"):
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        out = io.StringIO()
        monkeypatch.setattr(sys, "stdout", out)
        rc = h.main(["--server", "http://s", "--host-id", "h", "--host-token", "t", "get"])
        assert rc == 0 and out.getvalue() == ""


def test_store_and_erase_are_noops(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    for op in ("store", "erase"):
        assert h.main(["--server", "http://s", "--host-id", "h", "--host-token", "t", op]) == 0


def test_configure_host_git_sets_helper_and_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(HOST_TOKEN_ENV_VAR, "launch-tok")
    calls: list[list[str]] = []
    monkeypatch.setattr(h.subprocess, "run", lambda args, **k: calls.append(args) or None)
    cred = {"connected": True, "owner": "alice@example.com", "login": "octo"}
    monkeypatch.setattr(h, "_fetch", lambda *a, **k: cred)
    h.configure_host_git("http://srv", "host1")
    flat = [" ".join(c) for c in calls]
    assert any("credential.https://github.com.helper" in c and "host1" in c for c in flat)
    assert any("user.email alice@example.com" in c for c in flat)
    assert any("user.name octo" in c for c in flat)


def test_configure_host_git_noop_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(HOST_TOKEN_ENV_VAR, raising=False)
    calls: list[object] = []
    monkeypatch.setattr(h.subprocess, "run", lambda *a, **k: calls.append(a))
    h.configure_host_git("http://srv", "host1")
    assert calls == []  # no token → nothing configured
