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


def test_configure_host_git_resets_then_adds_broker_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(HOST_TOKEN_ENV_VAR, "launch-tok")
    calls: list[list[str]] = []
    monkeypatch.setattr(h.subprocess, "run", lambda args, **k: calls.append(args) or None)
    cred = {"connected": True, "owner": "alice@example.com", "login": "octo"}
    monkeypatch.setattr(h, "_fetch", lambda *a, **k: cred)
    h.configure_host_git("http://srv", "host1")

    # The github.com helper chain is reset (empty value) BEFORE the broker helper
    # is --add'ed, so a wider-scope $GIT_TOKEN helper cannot shadow the broker.
    key = "credential.https://github.com.helper"
    reset_idx = next(
        i
        for i, c in enumerate(calls)
        if c[:5] == ["git", "config", "--global", "--replace-all", key] and c[-1] == ""
    )
    add_idx = next(
        i
        for i, c in enumerate(calls)
        if c[:5] == ["git", "config", "--global", "--add", key] and "host1" in c[-1]
    )
    assert reset_idx < add_idx
    # Commit identity attributed to the connected owner.
    flat = [" ".join(c) for c in calls]
    assert any("user.email alice@example.com" in c for c in flat)
    assert any("user.name octo" in c for c in flat)


def test_configure_host_git_noop_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(HOST_TOKEN_ENV_VAR, raising=False)
    calls: list[object] = []
    monkeypatch.setattr(h.subprocess, "run", lambda *a, **k: calls.append(a))
    h.configure_host_git("http://srv", "host1")
    assert calls == []  # no token → nothing configured


def test_configure_host_git_clears_stale_broker_when_not_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Scoping: a confirmed not-connected owner (shared-$GIT_TOKEN / local model)
    # must NOT install the broker. It must also CLEAR any stale broker a prior
    # (inconclusive) clone probe installed — otherwise the reset it left strands
    # in-session git behind a declining broker. So the one and only write is the
    # unset that restores the ambient/shared helper; no --add, no identity.
    monkeypatch.setenv(HOST_TOKEN_ENV_VAR, "launch-tok")
    calls: list[list[str]] = []
    monkeypatch.setattr(h.subprocess, "run", lambda args, **k: calls.append(args) or None)
    monkeypatch.setattr(h, "_fetch", lambda *a, **k: {"connected": False})
    h.configure_host_git("http://srv", "host1")
    key = "credential.https://github.com.helper"
    assert calls == [["git", "config", "--global", "--unset-all", key]]
    flat = [" ".join(c) for c in calls]
    assert not any("--add" in c for c in flat)
    assert not any("user.email" in c for c in flat)


def test_resolve_github_token_returns_token_when_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        h,
        "_fetch",
        lambda *a, **k: {"connected": True, "token": "ghu_x", "username": "x-access-token"},
    )
    assert h.resolve_github_token("http://srv", "host1", "tok") == "ghu_x"


def test_resolve_github_token_none_when_not_connected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(h, "_fetch", lambda *a, **k: {"connected": False})
    assert h.resolve_github_token("http://srv", "host1", "tok") is None


def test_credential_url_targets_the_generic_provider_path() -> None:
    # The helper hits the provider-generic broker with provider=github.
    assert h._credential_url("http://s", "hid") == "http://s/v1/hosts/hid/credentials/github"
    assert h._credential_url("http://s/", "hid") == "http://s/v1/hosts/hid/credentials/github"


def test_fetch_sends_launch_token_as_header_not_url(monkeypatch: pytest.MonkeyPatch) -> None:
    # Exercise the real request construction (the other tests stub _fetch): the
    # launch token must ride the header so it can't land in server access logs.
    seen: dict = {}

    class _Resp:
        status_code = 200

        def json(self) -> dict:
            return {"connected": True, "token": "T", "username": "x-access-token"}

    def fake_get(url: str, headers: dict, timeout: float) -> _Resp:
        seen["url"], seen["headers"] = url, headers
        return _Resp()

    monkeypatch.setattr(h.httpx, "get", fake_get)
    data = h._fetch("http://s", "hid", "launch-tok")
    assert data is not None and data["token"] == "T"
    assert seen["url"] == "http://s/v1/hosts/hid/credentials/github"
    assert seen["headers"][h.MANAGED_HOST_TOKEN_HEADER] == "launch-tok"
    assert "launch-tok" not in seen["url"]


def test_configure_clone_credentials_wires_broker_when_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(HOST_TOKEN_ENV_VAR, "launch-tok")
    calls: list[list[str]] = []
    monkeypatch.setattr(h.subprocess, "run", lambda args, **k: calls.append(args) or None)
    monkeypatch.setattr(h, "_fetch", lambda *a, **k: {"connected": True, "owner": "a@b.com"})
    assert h.configure_clone_credentials("http://srv", "host1") is True
    key = "credential.https://github.com.helper"
    reset_idx = next(
        i
        for i, c in enumerate(calls)
        if c[:5] == ["git", "config", "--global", "--replace-all", key] and c[-1] == ""
    )
    add_idx = next(
        i
        for i, c in enumerate(calls)
        if c[:5] == ["git", "config", "--global", "--add", key] and "host1" in c[-1]
    )
    assert reset_idx < add_idx


def test_configure_clone_credentials_noop_when_not_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Not connected → leave the ambient (image GIT_TOKEN) helper intact; no git config.
    monkeypatch.setenv(HOST_TOKEN_ENV_VAR, "launch-tok")
    calls: list[list[str]] = []
    monkeypatch.setattr(h.subprocess, "run", lambda args, **k: calls.append(args) or None)
    monkeypatch.setattr(h, "_fetch", lambda *a, **k: {"connected": False})
    assert h.configure_clone_credentials("http://srv", "host1") is False
    assert calls == []


def test_configure_clone_credentials_noop_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(HOST_TOKEN_ENV_VAR, raising=False)
    calls: list[object] = []
    monkeypatch.setattr(h.subprocess, "run", lambda *a, **k: calls.append(a))
    assert h.configure_clone_credentials("http://srv", "host1") is False
    assert calls == []


def test_configure_clone_credentials_fails_closed_when_probe_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A transient probe failure (``_fetch`` -> None) is indistinguishable from
    # "not linked", so fail closed: install the broker anyway rather than let the
    # clone silently fall back to the shared $GIT_TOKEN identity for what may be a
    # linked owner. Only a *successful* ``connected: false`` keeps the fallback.
    monkeypatch.setenv(HOST_TOKEN_ENV_VAR, "launch-tok")
    calls: list[list[str]] = []
    monkeypatch.setattr(h.subprocess, "run", lambda args, **k: calls.append(args) or None)
    monkeypatch.setattr(h, "_fetch", lambda *a, **k: None)
    assert h.configure_clone_credentials("http://srv", "host1") is True
    key = "credential.https://github.com.helper"
    assert any(
        c[:5] == ["git", "config", "--global", "--add", key] and "host1" in c[-1] for c in calls
    )
