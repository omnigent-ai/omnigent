"""Tests for the sandbox-side GitHub credential helper + host git setup."""

from __future__ import annotations

import io
import sys

import pytest
import yaml

import omnigent.git_credential_github as h
from omnigent.host.identity import HOST_TOKEN_ENV_VAR


@pytest.fixture(autouse=True)
def _force_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    # The broker host integrations are sandbox-only; default every test to the
    # in-sandbox path. The not-in-sandbox no-op test clears IS_SANDBOX explicitly.
    monkeypatch.setenv("IS_SANDBOX", "1")


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


def test_configure_host_gh_writes_hosts_yml(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    # A connected owner: gh's hosts.yml is materialized with the brokered token
    # and the owner's login, 0600, so `gh api` authenticates as them.
    monkeypatch.setenv(HOST_TOKEN_ENV_VAR, "launch-tok")
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "gh"))
    monkeypatch.setattr(
        h,
        "_fetch",
        lambda *a, **k: {"connected": True, "token": "gho_user", "login": "octo"},
    )
    assert h.configure_host_gh("http://srv", "host1") is True
    hosts_path = tmp_path / "gh" / "hosts.yml"
    written = yaml.safe_load(hosts_path.read_text())
    assert written["github.com"] == {
        "oauth_token": "gho_user",
        "user": "octo",
        "git_protocol": "https",
    }
    # The credential file is owner-only (0600) — never a world-readable window.
    assert (hosts_path.stat().st_mode & 0o777) == 0o600


def test_configure_host_gh_preserves_other_hosts(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # Merge, don't truncate: an existing GitHub Enterprise entry (or a second
    # account) must survive — hosts.yml is a multi-host map, and the host's
    # GH_CONFIG_DIR can resolve to the developer's real ~/.config/gh.
    gh_dir = tmp_path / "gh"
    gh_dir.mkdir()
    (gh_dir / "hosts.yml").write_text(
        "github.mycompany.com:\n    oauth_token: enterprise-tok\n    user: alice\n"
    )
    monkeypatch.setenv(HOST_TOKEN_ENV_VAR, "launch-tok")
    monkeypatch.setenv("GH_CONFIG_DIR", str(gh_dir))
    monkeypatch.setattr(
        h, "_fetch", lambda *a, **k: {"connected": True, "token": "gho_user", "login": "octo"}
    )
    assert h.configure_host_gh("http://srv", "host1") is True
    written = yaml.safe_load((gh_dir / "hosts.yml").read_text())
    # The enterprise host survives; github.com is added.
    assert written["github.mycompany.com"]["oauth_token"] == "enterprise-tok"
    assert written["github.com"]["oauth_token"] == "gho_user"


def test_configure_host_gh_noop_when_not_connected(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # Not linked → leave any ambient gh auth untouched; write nothing.
    monkeypatch.setenv(HOST_TOKEN_ENV_VAR, "launch-tok")
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "gh"))
    monkeypatch.setattr(h, "_fetch", lambda *a, **k: {"connected": False})
    assert h.configure_host_gh("http://srv", "host1") is False
    assert not (tmp_path / "gh" / "hosts.yml").exists()


def test_configure_host_gh_noop_without_token(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.delenv(HOST_TOKEN_ENV_VAR, raising=False)
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "gh"))
    assert h.configure_host_gh("http://srv", "host1") is False
    assert not (tmp_path / "gh" / "hosts.yml").exists()


def test_host_integrations_are_noops_outside_sandbox(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # NOT in a managed sandbox → every auto-apply is a complete no-op even for a
    # connected owner, so a local `omnigent host` never touches the developer's
    # real ~/.gitconfig or ~/.config/gh.
    monkeypatch.delenv("IS_SANDBOX", raising=False)
    monkeypatch.setenv(HOST_TOKEN_ENV_VAR, "launch-tok")
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "gh"))
    calls: list[object] = []
    monkeypatch.setattr(h.subprocess, "run", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(
        h, "_fetch", lambda *a, **k: {"connected": True, "token": "t", "login": "o"}
    )
    h.configure_host_git("http://srv", "host1")
    assert h.configure_host_gh("http://srv", "host1") is False
    assert h.start_host_gh_refresh("http://srv", "host1") is None
    assert calls == []  # no git config writes
    assert not (tmp_path / "gh" / "hosts.yml").exists()  # no hosts.yml write


def test_gh_refresh_interval_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(h._GH_REFRESH_INTERVAL_ENV_VAR, raising=False)
    assert h._gh_refresh_interval_s() == h._GH_REFRESH_DEFAULT_S
    monkeypatch.setenv(h._GH_REFRESH_INTERVAL_ENV_VAR, "60")
    assert h._gh_refresh_interval_s() == 60
    monkeypatch.setenv(h._GH_REFRESH_INTERVAL_ENV_VAR, "garbage")
    assert h._gh_refresh_interval_s() == h._GH_REFRESH_DEFAULT_S


def test_start_host_gh_refresh_disabled_when_nonpositive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A non-positive interval disables the refresher (returns no thread).
    monkeypatch.setenv(h._GH_REFRESH_INTERVAL_ENV_VAR, "0")
    assert h.start_host_gh_refresh("http://srv", "host1") is None


def test_start_host_gh_refresh_rewrites_hosts_on_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The daemon loop re-materializes hosts.yml each tick. Drive exactly one
    # refresh, then park the (daemon) thread harmlessly on a never-set event so
    # the loop neither spins nor raises.
    import threading as _threading

    calls: list[tuple[str, str]] = []
    refreshed = _threading.Event()
    parked = _threading.Event()
    monkeypatch.setenv(h._GH_REFRESH_INTERVAL_ENV_VAR, "1")

    def _record(server: str, host_id: str) -> bool:
        calls.append((server, host_id))
        refreshed.set()
        return True

    monkeypatch.setattr(h, "configure_host_gh", _record)

    ticks = {"n": 0}

    def fake_sleep(_secs: float) -> None:
        ticks["n"] += 1
        if ticks["n"] >= 2:  # after one refresh, park forever (daemon → harmless)
            parked.wait()

    monkeypatch.setattr(h.time, "sleep", fake_sleep)
    t = h.start_host_gh_refresh("http://srv", "host1")
    assert t is not None
    assert refreshed.wait(timeout=5), "refresher never re-materialized hosts.yml"
    assert calls == [("http://srv", "host1")]
