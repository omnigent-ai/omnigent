"""Tests for the generic git credential helper and the GitHub credential facet.

Git runs for real against a private global config file; the broker is a local fake. Token
values are fake sentinels, and assertions that involve them print redacted values only.
"""

from __future__ import annotations

import io
import json
import logging
import os
import stat
import subprocess
import sys
import threading
import time
import types
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

import omnigent.git_credential as gc
from omnigent.git_credential import github as gh_facet
from omnigent.git_providers import (
    EnvInstances,
    FacetModules,
    Instances,
    ParsedPullRequest,
    ParsedRemote,
    load_facet,
    register_provider,
    reset_for_tests,
)
from omnigent.host.identity import HOST_TOKEN_ENV_VAR, MANAGED_HOST_TOKEN_HEADER
from tests.budgets import budget

SERVER = "http://broker.example.test"
HOST_ID = "host1"
LAUNCH_TOKEN = "fake-launch-token-sentinel"
OWNER_TOKEN = "fake-owner-token-sentinel"
GITLAB_TOKEN = "fake-gitlab-token-sentinel"
TOKENS = (LAUNCH_TOKEN, OWNER_TOKEN, GITLAB_TOKEN)
GHE = "ghe.example.test"
GITLAB_HOST = "gitlab.example.test"
GITLAB_MODULE = "tests_fake_gitlab_credential"
HELPER = (
    f"!python3 -m omnigent.git_credential --server {SERVER} --host-id {HOST_ID} "
    f"--host-token {LAUNCH_TOKEN}"
)
GITHUB_URL = f"{SERVER}/v1/hosts/{HOST_ID}/credentials/github"
GITLAB_URL = f"{SERVER}/v1/hosts/{HOST_ID}/credentials/gitlab"


@pytest.fixture(autouse=True)
def _sandbox(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """A managed sandbox with private git and gh config, and only the built-in providers."""
    for name in (
        "OMNIGENT_GIT_PROVIDER_MODULES",
        "OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS",
        "OMNIGENT_GIT_PROVIDER_GITLAB_HOSTS",
        "GH_HOST",
        "XDG_CONFIG_HOME",
        gh_facet.REFRESH_INTERVAL_ENV_VAR,
    ):
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "gh"))
    monkeypatch.setenv("IS_SANDBOX", "1")
    monkeypatch.setenv(HOST_TOKEN_ENV_VAR, LAUNCH_TOKEN)
    reset_for_tests()
    yield
    reset_for_tests()


@dataclass
class FakeBroker:
    """The server's credential route: a JSON payload or a response per provider id.

    A provider without an entry gets the server's 404 for a provider it does not broker.
    """

    payloads: dict[str, Any] = field(default_factory=dict)
    urls: list[str] = field(default_factory=list)
    launch_token_sent: list[bool] = field(default_factory=list)

    def get(self, url: str, headers: dict[str, str], timeout: float) -> httpx.Response:
        self.urls.append(url)
        self.launch_token_sent.append(headers.get(MANAGED_HOST_TOKEN_HEADER) == LAUNCH_TOKEN)
        answer = self.payloads.get(url.rsplit("/", 1)[-1])
        if isinstance(answer, httpx.Response):
            return answer
        if answer is None:
            return httpx.Response(404, json={"detail": "unknown credential provider"})
        return httpx.Response(200, json=answer)


@pytest.fixture
def broker(monkeypatch: pytest.MonkeyPatch) -> FakeBroker:
    fake = FakeBroker()
    monkeypatch.setattr(gc.httpx, "get", fake.get)
    return fake


class FakeGitLab:
    """A GitLab-shaped descriptor whose credential facet module exists only in sys.modules."""

    id = "gitlab"
    display_name = "GitLab"
    default_hosts = (GITLAB_HOST,)
    facets = FacetModules(credential=GITLAB_MODULE)

    def matches_host(self, host: str, instances: Instances) -> bool:
        return host in self.default_hosts

    def parse_remote_url(self, url: str, instances: Instances) -> ParsedRemote | None:
        return None

    def parse_pr_url(self, url: str, instances: Instances) -> ParsedPullRequest | None:
        return None


@dataclass
class FakeGitLabCredential:
    """A credential facet for the GitLab host that records its CLI config writes."""

    interval: int = 1800
    fail_hosts: bool = False
    cli_writes: list[Path] = field(default_factory=list)

    def refresh_interval_s(self) -> int:
        return self.interval

    def hosts(self, instances: Instances) -> frozenset[str]:
        if self.fail_hosts:
            raise RuntimeError("gitlab facet failed")
        return frozenset({GITLAB_HOST})

    def api_hosts(self) -> frozenset[str]:
        return frozenset({GITLAB_HOST})

    def git_username(self, cred: dict[str, Any]) -> str:
        return str(cred.get("username") or "oauth2")

    def write_cli_config(self, cred: dict[str, Any], home: Path) -> bool:
        self.cli_writes.append(home)
        return True

    def clear_cli_config(self, home: Path) -> None:
        pass


@pytest.fixture
def gitlab(monkeypatch: pytest.MonkeyPatch) -> FakeGitLabCredential:
    credential = FakeGitLabCredential()
    module = types.ModuleType(GITLAB_MODULE)
    module.CREDENTIAL = credential
    monkeypatch.setitem(sys.modules, GITLAB_MODULE, module)
    register_provider(FakeGitLab())
    return credential


def _github(**extra: object) -> dict[str, object]:
    """Return the broker's GitHub payload for a connected owner."""
    return {
        "connected": True,
        "owner": "alice@example.com",
        "login": "octo",
        "username": "x-access-token",
        "token": OWNER_TOKEN,
        **extra,
    }


def _redact(value: object) -> str:
    text = repr(value)
    for token in TOKENS:
        text = text.replace(token, "<token>")
    return text


def _same(actual: object, expected: object) -> None:
    """Assert equality and show only redacted values when it fails."""
    if actual != expected:
        raise AssertionError(f"{_redact(actual)} != {_redact(expected)}")


def _leaks(*texts: str) -> bool:
    return any(token in text for token in TOKENS for text in texts)


def _answer(username: str, token: str) -> str:
    return f"username={username}\npassword={token}\n"


def _run_helper(monkeypatch: pytest.MonkeyPatch, host: str | None, protocol: str = "https") -> str:
    """Run the helper's ``get`` for one git request and return what it printed."""
    lines = [f"protocol={protocol}", *([] if host is None else [f"host={host}"])]
    out = io.StringIO()
    # Undo the stream patches at once, so they cannot outlive a capture fixture.
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdin", io.StringIO("\n".join(lines) + "\n\n"))
        patch.setattr(sys, "stdout", out)
        argv = ["--server", SERVER, "--host-id", HOST_ID, "--host-token", LAUNCH_TOKEN, "get"]
        rc = gc.main(argv)
    assert rc == 0
    return out.getvalue()


def _helpers(host: str) -> list[str]:
    """Return the global git credential helpers for https requests to *host*."""
    result = subprocess.run(
        ["git", "config", "--global", "--null", "--get-all", f"credential.https://{host}.helper"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.split("\0")[:-1] if result.returncode == 0 else []


def _add_helper(host: str, value: str) -> None:
    subprocess.run(
        ["git", "config", "--global", "--add", f"credential.https://{host}.helper", value],
        capture_output=True,
        check=True,
    )


def _git_global(key: str) -> str | None:
    result = subprocess.run(
        ["git", "config", "--global", "--get", key], capture_output=True, text=True, check=False
    )
    return result.stdout.rstrip("\n") if result.returncode == 0 else None


# ── The helper ──────────────────────────────────────────────────────────────


def test_main_vends_the_github_token_for_github_com_over_https(
    monkeypatch: pytest.MonkeyPatch, broker: FakeBroker
) -> None:
    broker.payloads["github"] = _github()

    _same(_run_helper(monkeypatch, "github.com"), _answer("x-access-token", OWNER_TOKEN))
    assert broker.urls == [GITHUB_URL]
    assert broker.launch_token_sent == [True]


@pytest.mark.parametrize(
    ("request_host", "extra", "username"),
    [
        ("github.com", {"hosts": ["github.com"]}, "x-access-token"),
        ("github.com", {"hosts": ["GitHub.com"], "expires_at": 1767225600}, "x-access-token"),
        ("GitHub.com", {"username": "octo-bot"}, "octo-bot"),
        ("github.com", {"username": None}, "x-access-token"),
    ],
    ids=["hosts-listed", "hosts-mixed-case-and-expiry", "request-mixed-case", "no-username"],
)
def test_main_accepts_the_optional_broker_fields(
    monkeypatch: pytest.MonkeyPatch,
    broker: FakeBroker,
    request_host: str,
    extra: dict[str, object],
    username: str,
) -> None:
    broker.payloads["github"] = _github(**extra)

    _same(_run_helper(monkeypatch, request_host), _answer(username, OWNER_TOKEN))


@pytest.mark.parametrize(
    ("host", "protocol"),
    [
        ("gitlab.com", "https"),
        ("github.com", "ssh"),
        ("github.com", "http"),
        (None, "https"),
        ("", "https"),
        ("github.com:443", "https"),
    ],
    ids=["other-host", "ssh", "http", "no-host", "empty-host", "host-with-port"],
)
def test_main_declines_without_asking_the_broker(
    monkeypatch: pytest.MonkeyPatch, broker: FakeBroker, host: str | None, protocol: str
) -> None:
    broker.payloads["github"] = _github()

    assert _run_helper(monkeypatch, host, protocol) == ""
    assert broker.urls == []


@pytest.mark.parametrize(
    "answer",
    [
        None,
        {"connected": False},
        {"connected": True},
        _github(hosts=[GHE]),
        _github(hosts=[]),
        httpx.Response(404, json={"detail": "Not Found"}),
        httpx.Response(503, json={"detail": "unavailable"}),
    ],
    ids=[
        "unknown-provider-404",
        "not-connected",
        "no-token",
        "hosts-without-github-com",
        "hosts-empty",
        "generic-404",
        "unavailable",
    ],
)
def test_main_declines_when_the_broker_vends_nothing_for_the_host(
    monkeypatch: pytest.MonkeyPatch, broker: FakeBroker, answer: object
) -> None:
    if answer is not None:
        broker.payloads["github"] = answer

    assert _run_helper(monkeypatch, "github.com") == ""
    assert broker.urls == [GITHUB_URL]


def test_store_and_erase_are_noops(monkeypatch: pytest.MonkeyPatch, broker: FakeBroker) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("protocol=https\nhost=github.com\n\n"))
    for operation in ("store", "erase"):
        argv = ["--server", SERVER, "--host-id", HOST_ID, "--host-token", LAUNCH_TOKEN, operation]
        assert gc.main(argv) == 0
    assert broker.urls == []


def test_main_serves_a_registered_provider_from_its_own_broker_route(
    monkeypatch: pytest.MonkeyPatch, broker: FakeBroker, gitlab: FakeGitLabCredential
) -> None:
    broker.payloads["gitlab"] = {"connected": True, "token": GITLAB_TOKEN}
    broker.payloads["github"] = _github()

    _same(_run_helper(monkeypatch, GITLAB_HOST), _answer("oauth2", GITLAB_TOKEN))
    assert broker.urls == [GITLAB_URL]
    _same(_run_helper(monkeypatch, "github.com"), _answer("x-access-token", OWNER_TOKEN))
    assert broker.urls == [GITLAB_URL, GITHUB_URL]


@pytest.mark.parametrize(
    "extra",
    [{}, {"hosts": ["github.com"]}, {"hosts": GHE}],
    ids=["hosts-omitted", "hosts-github-only", "hosts-not-a-list"],
)
def test_main_gives_a_configured_instance_host_no_token_unless_the_broker_lists_it(
    monkeypatch: pytest.MonkeyPatch, broker: FakeBroker, extra: dict[str, object]
) -> None:
    monkeypatch.setenv("OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS", GHE)
    broker.payloads["github"] = _github(**extra)

    assert _run_helper(monkeypatch, GHE) == ""
    _same(_run_helper(monkeypatch, "github.com"), _answer("x-access-token", OWNER_TOKEN))


def test_main_vends_for_an_instance_host_the_broker_lists(
    monkeypatch: pytest.MonkeyPatch, broker: FakeBroker
) -> None:
    monkeypatch.setenv("OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS", GHE)
    broker.payloads["github"] = _github(hosts=["github.com", GHE])

    _same(_run_helper(monkeypatch, GHE), _answer("x-access-token", OWNER_TOKEN))
    # A listed host that no facet serves still gets nothing.
    assert _run_helper(monkeypatch, "unclaimed.example.test") == ""


# ── Host setup ──────────────────────────────────────────────────────────────


def test_configure_host_credentials_wires_git_identity_and_gh(
    broker: FakeBroker, tmp_path: Path
) -> None:
    broker.payloads["github"] = _github()

    gc.configure_host_credentials(SERVER, HOST_ID)
    gc.configure_host_credentials(SERVER, HOST_ID)

    _same(_helpers("github.com"), ["", HELPER])
    assert _git_global("user.email") == "alice@example.com"
    assert _git_global("user.name") == "octo"
    hosts_path = tmp_path / "gh" / "hosts.yml"
    _same(
        yaml.safe_load(hosts_path.read_text()),
        {"github.com": {"oauth_token": OWNER_TOKEN, "user": "octo", "git_protocol": "https"}},
    )
    assert stat.S_IMODE(hosts_path.stat().st_mode) == 0o600
    # One probe per run serves git, the identity, and gh.
    assert broker.urls == [GITHUB_URL, GITHUB_URL]


@pytest.mark.parametrize(
    ("answer", "github_helpers"),
    [
        (_github(), ["", HELPER]),
        (_github(hosts=["github.com"]), ["", HELPER]),
        (httpx.Response(503, json={"detail": "unavailable"}), ["", HELPER]),
        ({"connected": False}, []),
    ],
    ids=["hosts-omitted", "hosts-github-only", "inconclusive", "not-connected"],
)
def test_configure_host_credentials_gives_an_instance_host_no_helper_unless_listed(
    monkeypatch: pytest.MonkeyPatch,
    broker: FakeBroker,
    answer: object,
    github_helpers: list[str],
) -> None:
    monkeypatch.setenv("OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS", GHE)
    broker.payloads["github"] = answer

    gc.configure_host_credentials(SERVER, HOST_ID)

    assert _helpers(GHE) == []
    _same(_helpers("github.com"), github_helpers)


def test_configure_host_credentials_serves_exactly_the_listed_hosts(
    monkeypatch: pytest.MonkeyPatch, broker: FakeBroker, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS", GHE)
    broker.payloads["github"] = _github(hosts=["github.com", GHE])
    gc.configure_host_credentials(SERVER, HOST_ID)
    _same(_helpers("github.com"), ["", HELPER])
    _same(_helpers(GHE), ["", HELPER])

    broker.payloads["github"] = _github(hosts=[GHE])
    gc.configure_host_credentials(SERVER, HOST_ID)

    assert _helpers("github.com") == []
    _same(_helpers(GHE), ["", HELPER])


def test_gh_gets_no_github_com_token_the_broker_does_not_list(
    broker: FakeBroker, tmp_path: Path
) -> None:
    broker.payloads["github"] = _github(hosts=[GHE])

    gc.configure_host_credentials(SERVER, HOST_ID)

    assert not (tmp_path / "gh" / "hosts.yml").exists()


def test_clearing_removes_only_this_helpers_entries(
    monkeypatch: pytest.MonkeyPatch, broker: FakeBroker
) -> None:
    monkeypatch.setenv("OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS", GHE)
    broker.payloads["github"] = _github(hosts=["github.com", GHE])
    gc.configure_host_credentials(SERVER, HOST_ID)
    _add_helper(GHE, "!user-ghe-helper")

    # The broker stops listing the instance host: only this helper's entries go.
    broker.payloads["github"] = _github(hosts=["github.com"])
    gc.configure_host_credentials(SERVER, HOST_ID)
    assert _helpers(GHE) == ["!user-ghe-helper"]
    _same(_helpers("github.com"), ["", HELPER])

    # The owner is not linked: github.com gets its ambient chain back, user helpers stay.
    _add_helper("github.com", "!user-github-helper")
    del broker.payloads["github"]
    gc.configure_host_credentials(SERVER, HOST_ID)
    assert _helpers("github.com") == ["!user-github-helper"]
    assert _helpers(GHE) == ["!user-ghe-helper"]


@pytest.mark.parametrize(
    "answer",
    [
        _github(),
        _github(hosts=["github.com"]),
        {"connected": False},
        httpx.Response(503, json={"detail": "unavailable"}),
    ],
    ids=["hosts-omitted", "hosts-github-only", "not-connected", "inconclusive"],
)
def test_a_user_helper_on_another_facet_host_survives(
    monkeypatch: pytest.MonkeyPatch, broker: FakeBroker, answer: object
) -> None:
    monkeypatch.setenv("OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS", GHE)
    _add_helper(GHE, "")
    _add_helper(GHE, "!user-ghe-helper")
    broker.payloads["github"] = answer

    gc.configure_host_credentials(SERVER, HOST_ID)

    assert _helpers(GHE) == ["", "!user-ghe-helper"]


def test_clearing_also_removes_the_deprecated_github_helper(broker: FakeBroker) -> None:
    _add_helper("github.com", "")
    _add_helper(
        "github.com",
        "!python3 -m omnigent.git_credential_github --server s --host-id h --host-token t",
    )

    gc.configure_host_credentials(SERVER, HOST_ID)

    assert _helpers("github.com") == []


def test_configure_host_credentials_wires_every_credential_facet(
    broker: FakeBroker, gitlab: FakeGitLabCredential
) -> None:
    broker.payloads["github"] = _github()
    broker.payloads["gitlab"] = {
        "connected": True,
        "owner": "alice@example.com",
        "login": "gl-user",
        "token": GITLAB_TOKEN,
    }

    gc.configure_host_credentials(SERVER, HOST_ID)

    _same(_helpers("github.com"), ["", HELPER])
    _same(_helpers(GITLAB_HOST), ["", HELPER])
    assert gitlab.cli_writes == [Path.home()]
    # The first connected provider in registration order sets the commit identity.
    assert _git_global("user.name") == "octo"
    assert broker.urls == [GITHUB_URL, GITLAB_URL]


def test_a_failing_facet_neither_stops_the_others_nor_logs_a_token(
    monkeypatch: pytest.MonkeyPatch,
    broker: FakeBroker,
    gitlab: FakeGitLabCredential,
    caplog: pytest.LogCaptureFixture,
    capfd: pytest.CaptureFixture[str],
) -> None:
    gitlab.fail_hosts = True
    broker.payloads["github"] = _github()
    broker.payloads["gitlab"] = {"connected": True, "token": GITLAB_TOKEN}

    with caplog.at_level(logging.DEBUG):
        gc.configure_host_credentials(SERVER, HOST_ID)
        declined = _run_helper(monkeypatch, GITLAB_HOST)
        vended = _run_helper(monkeypatch, "github.com")

    _same(_helpers("github.com"), ["", HELPER])
    assert declined == ""
    _same(vended, _answer("x-access-token", OWNER_TOKEN))
    assert "provider gitlab" in caplog.text
    captured = capfd.readouterr()
    assert not _leaks(caplog.text, captured.out, captured.err)


def test_outside_a_sandbox_host_setup_and_refresh_do_nothing(
    monkeypatch: pytest.MonkeyPatch, broker: FakeBroker, tmp_path: Path
) -> None:
    monkeypatch.delenv("IS_SANDBOX")
    broker.payloads["github"] = _github()

    gc.configure_host_credentials(SERVER, HOST_ID)

    assert gc.start_credential_refresh(SERVER, HOST_ID) == []
    assert not (tmp_path / "gitconfig").exists()
    assert not (tmp_path / "gh" / "hosts.yml").exists()
    assert broker.urls == []


def test_host_setup_needs_the_launch_token(
    monkeypatch: pytest.MonkeyPatch, broker: FakeBroker, tmp_path: Path
) -> None:
    monkeypatch.delenv(HOST_TOKEN_ENV_VAR)

    gc.configure_host_credentials(SERVER, HOST_ID)

    assert gc.configure_clone_credentials(SERVER, HOST_ID) is False
    assert not (tmp_path / "gitconfig").exists()
    assert broker.urls == []


# ── Clone setup ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("answer", "wired"),
    [
        (None, False),
        ({"connected": False}, False),
        (httpx.Response(404, json={"detail": "Not Found"}), True),
        (httpx.Response(503, json={"detail": "unavailable"}), True),
        (_github(), True),
    ],
    ids=["unknown-provider-404", "not-connected", "generic-404", "unavailable", "connected"],
)
def test_configure_clone_credentials_is_connected_gated(
    monkeypatch: pytest.MonkeyPatch,
    broker: FakeBroker,
    tmp_path: Path,
    answer: object,
    wired: bool,
) -> None:
    monkeypatch.setenv("OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS", GHE)
    if answer is not None:
        broker.payloads["github"] = answer

    assert gc.configure_clone_credentials(SERVER, HOST_ID) is wired

    _same(_helpers("github.com"), ["", HELPER] if wired else [])
    assert _helpers(GHE) == []
    assert not (tmp_path / "gh" / "hosts.yml").exists()


def test_clone_setup_never_removes_a_helper_and_is_not_sandbox_gated(
    monkeypatch: pytest.MonkeyPatch, broker: FakeBroker
) -> None:
    monkeypatch.delenv("IS_SANDBOX")
    _add_helper("github.com", HELPER)

    assert gc.configure_clone_credentials(SERVER, HOST_ID) is False
    _same(_helpers("github.com"), [HELPER])

    broker.payloads["github"] = _github()
    assert gc.configure_clone_credentials(SERVER, HOST_ID) is True
    _same(_helpers("github.com"), ["", HELPER])


# ── Refresh ─────────────────────────────────────────────────────────────────


def test_start_credential_refresh_runs_one_thread_per_facet(
    monkeypatch: pytest.MonkeyPatch,
    broker: FakeBroker,
    gitlab: FakeGitLabCredential,
    tmp_path: Path,
) -> None:
    broker.payloads["github"] = _github()
    broker.payloads["gitlab"] = {"connected": True, "token": GITLAB_TOKEN}
    real_sleep = time.sleep
    parked = threading.Event()
    refreshed = {
        "github-credential-refresh": threading.Event(),
        "gitlab-credential-refresh": threading.Event(),
    }
    sleeps: dict[str, int] = {}
    lock = threading.Lock()

    def fake_sleep(seconds: float) -> None:
        # A refresher's second sleep follows its first refresh; park it there for good.
        name = threading.current_thread().name
        if name not in refreshed:
            real_sleep(seconds)
            return
        with lock:
            sleeps[name] = sleeps.get(name, 0) + 1
            count = sleeps[name]
        if count >= 2:
            refreshed[name].set()
            parked.wait()

    monkeypatch.setattr(gc.time, "sleep", fake_sleep)

    threads = gc.start_credential_refresh(SERVER, HOST_ID)

    assert sorted(thread.name for thread in threads) == sorted(refreshed)
    assert all(thread.daemon for thread in threads)
    assert all(event.wait(budget(10)) for event in refreshed.values())
    written = yaml.safe_load((tmp_path / "gh" / "hosts.yml").read_text())
    _same(written["github.com"]["oauth_token"], OWNER_TOKEN)
    assert gitlab.cli_writes == [Path.home()]
    # Refresh ticks only re-write CLI config; git keeps fetching per operation.
    assert not (tmp_path / "gitconfig").exists()


def test_start_credential_refresh_skips_a_disabled_facet(
    monkeypatch: pytest.MonkeyPatch, gitlab: FakeGitLabCredential
) -> None:
    gitlab.interval = 0
    monkeypatch.setenv(gh_facet.REFRESH_INTERVAL_ENV_VAR, "0")

    assert gc.start_credential_refresh(SERVER, HOST_ID) == []


# ── The GitHub facet ────────────────────────────────────────────────────────


def test_the_github_descriptor_names_its_credential_facet() -> None:
    assert load_facet("github", "credential") is gh_facet.CREDENTIAL


def test_github_facet_hosts_username_and_refresh_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    facet = gh_facet.CREDENTIAL
    assert facet.hosts(EnvInstances()) == {"github.com"}
    monkeypatch.setenv("OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS", "GHE.Example.Test")
    assert facet.hosts(EnvInstances()) == {"github.com", GHE}
    assert facet.api_hosts() == {"api.github.com"}
    assert facet.git_username({}) == "x-access-token"
    assert facet.git_username({"username": "octo-bot"}) == "octo-bot"
    assert facet.refresh_interval_s() == 1800
    for raw, interval in (("60", 60), ("0", 0), ("garbage", 1800)):
        monkeypatch.setenv(gh_facet.REFRESH_INTERVAL_ENV_VAR, raw)
        assert facet.refresh_interval_s() == interval


def test_github_facet_writes_hosts_yml_privately_and_atomically(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("GH_CONFIG_DIR")
    home = tmp_path / "facet-home"
    hosts_path = home / ".config" / "gh" / "hosts.yml"
    hosts_path.parent.mkdir(parents=True)
    hosts_path.write_text(f"{GHE}:\n    oauth_token: enterprise-value\n    user: alice\n")
    replaced: list[tuple[bool, int, bool]] = []
    real_replace = os.replace

    def checking_replace(src: str, dst: str) -> None:
        # At swap time the new file is private and beside the target, which is still intact.
        source = Path(src)
        replaced.append(
            (
                source.parent == hosts_path.parent,
                stat.S_IMODE(source.stat().st_mode),
                "github.com" not in Path(dst).read_text(),
            )
        )
        real_replace(src, dst)

    monkeypatch.setattr(gh_facet.os, "replace", checking_replace)

    assert gh_facet.CREDENTIAL.write_cli_config(_github(), home) is True

    assert replaced == [(True, 0o600, True)]
    written = yaml.safe_load(hosts_path.read_text())
    assert written[GHE] == {"oauth_token": "enterprise-value", "user": "alice"}
    _same(
        written["github.com"],
        {"oauth_token": OWNER_TOKEN, "user": "octo", "git_protocol": "https"},
    )
    assert stat.S_IMODE(hosts_path.stat().st_mode) == 0o600
    assert [path.name for path in hosts_path.parent.iterdir()] == ["hosts.yml"]


def test_github_facet_keeps_hosts_yml_when_the_write_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    hosts_path = tmp_path / "gh" / "hosts.yml"
    hosts_path.parent.mkdir()
    original = "ghe.example.test:\n    user: alice\n"
    hosts_path.write_text(original)

    def failing_dump(*_args: object, **_kwargs: object) -> None:
        raise yaml.YAMLError("cannot write")

    monkeypatch.setattr(gh_facet.yaml, "safe_dump", failing_dump)

    assert gh_facet.CREDENTIAL.write_cli_config(_github(), tmp_path / "home") is False
    assert hosts_path.read_text() == original
    assert [path.name for path in hosts_path.parent.iterdir()] == ["hosts.yml"]


def test_github_facet_clear_removes_only_the_github_com_account(tmp_path: Path) -> None:
    hosts_path = tmp_path / "gh" / "hosts.yml"
    hosts_path.parent.mkdir()
    hosts_path.write_text(
        yaml.safe_dump(
            {
                "github.com": {
                    "oauth_token": OWNER_TOKEN,
                    "user": "octo",
                    "git_protocol": "https",
                    "users": {"octo": {"oauth_token": OWNER_TOKEN}, "other": {"x": "y"}},
                },
                GHE: {"oauth_token": "enterprise-value", "user": "alice"},
            }
        )
    )

    gh_facet.CREDENTIAL.clear_cli_config(tmp_path / "home")

    assert yaml.safe_load(hosts_path.read_text()) == {
        "github.com": {"git_protocol": "https", "users": {"other": {"x": "y"}}},
        GHE: {"oauth_token": "enterprise-value", "user": "alice"},
    }
    assert stat.S_IMODE(hosts_path.stat().st_mode) == 0o600


def test_github_facet_clear_leaves_a_file_without_a_github_com_token(tmp_path: Path) -> None:
    hosts_path = tmp_path / "gh" / "hosts.yml"
    gh_facet.CREDENTIAL.clear_cli_config(tmp_path / "home")
    assert not hosts_path.exists()

    hosts_path.parent.mkdir()
    original = "github.com:\n    git_protocol: ssh\n"
    hosts_path.write_text(original)
    gh_facet.CREDENTIAL.clear_cli_config(tmp_path / "home")
    assert hosts_path.read_text() == original


# ── Process boundaries ──────────────────────────────────────────────────────


def _child_env() -> dict[str, str]:
    """This process's env without the launch token, resolving ``omnigent`` like this process."""
    env = {key: value for key, value in os.environ.items() if key != HOST_TOKEN_ENV_VAR}
    env["PYTHONPATH"] = os.pathsep.join(path for path in sys.path if path)
    return env


def test_helper_modules_do_not_import_the_server_stack() -> None:
    roots = [path for path in sys.path if path]
    probe = (
        f"import sys\nsys.path[:0] = {roots!r}\n"
        "import omnigent.git_credential, omnigent.git_credential.github, "
        "omnigent.git_credential_github\n"
        "print('\\n'.join(sorted(sys.modules)))\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", probe],
        env=_child_env(),
        capture_output=True,
        text=True,
        timeout=budget(120),
    )

    assert result.returncode == 0, result.stderr
    loaded = result.stdout.split()
    assert "omnigent.git_credential_github" in loaded
    heavy = [
        name
        for name in loaded
        if name.partition(".")[0] in {"fastapi", "sqlalchemy"}
        or name == "omnigent.server"
        or name.startswith("omnigent.server.")
    ]
    assert heavy == []


@pytest.mark.posix_only
def test_git_fetches_the_token_through_the_installed_helper() -> None:
    requests: list[bool] = []

    class Broker(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            valid = (
                self.path == f"/v1/hosts/{HOST_ID}/credentials/github"
                and self.headers.get(MANAGED_HOST_TOKEN_HEADER) == LAUNCH_TOKEN
            )
            requests.append(valid)
            self.send_response(200 if valid else 401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(_github() if valid else {}).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Broker)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        gc.configure_host_credentials(f"http://127.0.0.1:{server.server_port}", HOST_ID)
        env = {
            key: value
            for key, value in _child_env().items()
            if key not in {"GIT_ASKPASS", "SSH_ASKPASS"}
        }
        # git runs the helper as ``python3``; resolve it to this interpreter.
        env["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{env['PATH']}"
        env["GIT_TERMINAL_PROMPT"] = "0"
        result = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            env=env,
            capture_output=True,
            text=True,
            timeout=budget(60),
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    lines = result.stdout.splitlines()
    vended = "username=x-access-token" in lines and f"password={OWNER_TOKEN}" in lines
    assert vended
    assert requests
    assert all(requests)


@pytest.mark.parametrize("module", ["omnigent.git_credential", "omnigent.git_credential_github"])
def test_helper_modules_run_as_scripts(module: str, tmp_path: Path) -> None:
    helper_args = ["--server", SERVER, "--host-id", HOST_ID, "--host-token", "unused", "get"]
    result = subprocess.run(
        [sys.executable, "-m", module, *helper_args],
        input="protocol=https\nhost=gitlab.com\n\n",
        env=_child_env(),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=budget(120),
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
