"""Tests that tracked-PR reads reuse gh's account list until gh's local auth state changes."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from omnigent.runner import github_resource
from omnigent.runner.session_prs import PullRequestRef

_AUTH_STATUS = ["gh", "auth", "status", "--json", "hosts"]
_REFERENCE = PullRequestRef(
    host="github.com",
    repository="octo/repo",
    number=7,
    url="https://github.com/octo/repo/pull/7",
)


class _FakeGh:
    """Stands in for the gh process, which would call GitHub, and records each run."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.envs: list[dict[str, str] | None] = []
        self.accounts: list[dict[str, Any]] = [
            {"login": "alice", "active": True, "state": "success", "host": "github.com"},
            {"login": "bob", "active": False, "state": "success", "host": "github.com"},
        ]
        self.auth_output: str | None = None
        # Accounts gh reports when run in a given checkout, for a config dir relative to it.
        self.accounts_by_cwd: dict[str, list[dict[str, Any]]] = {}

    def __call__(
        self, argv: list[str], *, cwd: str, timeout: float, env: dict[str, str] | None = None
    ) -> tuple[int | None, str, str]:
        self.calls.append(argv)
        self.envs.append(env)
        if argv == _AUTH_STATUS:
            if self.auth_output is not None:
                return 1, self.auth_output, ""
            accounts = self.accounts_by_cwd.get(cwd, self.accounts)
            return 0, json.dumps({"hosts": {"github.com": accounts}}), ""
        if argv[:3] == ["gh", "pr", "view"]:
            return 0, json.dumps({"title": "Fix", "state": "OPEN", "headRefName": "b"}), ""
        return 1, "", "unexpected"

    def count(self, argv: list[str]) -> int:
        return sum(1 for call in self.calls if call == argv)


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _FakeGh:
    fake = _FakeGh()
    monkeypatch.setattr(github_resource, "_run", fake)
    monkeypatch.setattr(github_resource.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(github_resource._config, "github_account_preference", lambda key: None)
    monkeypatch.setattr(github_resource, "_accounts_cache", None)
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path))
    for name in (*github_resource._GH_TOKEN_ENV_VARS, "GH_HOST", "IS_SANDBOX"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "hosts.yml").write_text("github.com:\n  user: alice\n")
    return fake


def _rewrite_hosts_file(text: str) -> None:
    # gh replaces hosts.yml on login, logout, and switch; a new file gets a new inode.
    path = Path(os.environ["GH_CONFIG_DIR"]) / "hosts.yml"
    replacement = path.with_suffix(".new")
    replacement.write_text(text)
    replacement.replace(path)


def test_repeated_tracked_pr_reads_run_auth_status_once(gh: _FakeGh, tmp_path: Path) -> None:
    infos = [github_resource._reference_info(str(tmp_path), _REFERENCE) for _ in range(5)]

    assert gh.count(_AUTH_STATUS) == 1
    assert sum(1 for call in gh.calls if call[:3] == ["gh", "pr", "view"]) == 5
    for info in infos:
        # The merge-as picker and the selected account keep the same data.
        assert info["accounts"] == gh.accounts
        assert info["selected_account"] == "alice"
        assert info["pr"]["title"] == "Fix"


def test_switching_accounts_is_seen_on_the_next_read(gh: _FakeGh, tmp_path: Path) -> None:
    github_resource._reference_info(str(tmp_path), _REFERENCE)
    gh.accounts = [
        {"login": "alice", "active": False, "state": "success", "host": "github.com"},
        {"login": "bob", "active": True, "state": "success", "host": "github.com"},
    ]
    _rewrite_hosts_file("github.com:\n  user: bob\n")

    info = github_resource._reference_info(str(tmp_path), _REFERENCE)

    assert gh.count(_AUTH_STATUS) == 2
    assert info["selected_account"] == "bob"
    assert github_resource._active_login(str(tmp_path), "github.com") == "bob"
    assert gh.count(_AUTH_STATUS) == 2


def test_logging_out_is_seen_on_the_next_read(gh: _FakeGh, tmp_path: Path) -> None:
    github_resource._reference_info(str(tmp_path), _REFERENCE)
    gh.accounts = []
    (Path(os.environ["GH_CONFIG_DIR"]) / "hosts.yml").unlink()

    info = github_resource._reference_info(str(tmp_path), _REFERENCE)

    assert gh.count(_AUTH_STATUS) == 2
    assert info["accounts"] == []
    assert info["selected_account"] is None


def test_windows_appdata_hosts_file_is_watched(
    gh: _FakeGh, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Without GH_CONFIG_DIR or XDG_CONFIG_HOME, gh on Windows keeps hosts.yml under %APPDATA%.
    monkeypatch.delenv("GH_CONFIG_DIR")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    config_dir = tmp_path / "appdata" / "GitHub CLI"
    config_dir.mkdir(parents=True)
    hosts = str(config_dir / "hosts.yml")
    with open(hosts, "w") as f:
        f.write("github.com:\n  user: alice\n")
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    root = str(tmp_path)
    with monkeypatch.context() as windows:
        # Only the config-dir choice reads os.name; no Path is built while it is "nt".
        windows.setattr(os, "name", "nt")
        github_resource._reference_info(root, _REFERENCE)
        gh.accounts = [
            {"login": "alice", "active": False, "state": "success", "host": "github.com"},
            {"login": "bob", "active": True, "state": "success", "host": "github.com"},
        ]
        with open(hosts + ".new", "w") as f:
            f.write("github.com:\n  user: bob\n")
        os.replace(hosts + ".new", hosts)
        info = github_resource._reference_info(root, _REFERENCE)

    assert gh.count(_AUTH_STATUS) == 2
    assert info["selected_account"] == "bob"


def test_relative_config_dir_is_read_per_checkout(
    gh: _FakeGh, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # gh resolves a relative GH_CONFIG_DIR against the checkout it runs in.
    monkeypatch.setenv("GH_CONFIG_DIR", "gh-config")
    monkeypatch.chdir(tmp_path)
    roots = {}
    for name, login in (("one", "alice"), ("two", "bob")):
        config_dir = tmp_path / name / "gh-config"
        config_dir.mkdir(parents=True)
        (config_dir / "hosts.yml").write_text(f"github.com:\n  user: {login}\n")
        roots[login] = str(tmp_path / name)
        gh.accounts_by_cwd[roots[login]] = [
            {"login": login, "active": True, "state": "success", "host": "github.com"}
        ]

    selected = {
        login: github_resource._reference_info(root, _REFERENCE)["selected_account"]
        for login, root in roots.items()
    }

    assert selected == {"alice": "alice", "bob": "bob"}
    assert gh.count(_AUTH_STATUS) == 2


def test_env_token_change_reruns_auth_status(
    gh: _FakeGh, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    github_resource._reference_info(str(tmp_path), _REFERENCE)
    monkeypatch.setenv("GH_TOKEN", "token-for-carol")
    github_resource._reference_info(str(tmp_path), _REFERENCE)
    github_resource._reference_info(str(tmp_path), _REFERENCE)

    assert gh.count(_AUTH_STATUS) == 2


def test_sandbox_ignores_env_tokens_gh_never_sees(
    gh: _FakeGh, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("IS_SANDBOX", "1")
    github_resource._reference_info(str(tmp_path), _REFERENCE)
    monkeypatch.setenv("GH_TOKEN", "ambient")
    github_resource._reference_info(str(tmp_path), _REFERENCE)

    assert gh.count(_AUTH_STATUS) == 1
    assert all(env is None or "GH_TOKEN" not in env for env in gh.envs)


@pytest.mark.parametrize("output", ["", "unknown flag: --json"])
def test_unparsed_output_is_not_reused(gh: _FakeGh, tmp_path: Path, output: str) -> None:
    gh.auth_output = output
    info = github_resource._reference_info(str(tmp_path), _REFERENCE)
    assert info["accounts"] == []

    gh.auth_output = None
    info = github_resource._reference_info(str(tmp_path), _REFERENCE)

    assert gh.count(_AUTH_STATUS) == 2
    assert info["selected_account"] == "alice"


def test_unresolved_active_account_is_not_reused(gh: _FakeGh, tmp_path: Path) -> None:
    # gh lists an env token it couldn't resolve without a login; only the inactive one remains.
    gh.accounts = [
        {"login": "", "active": True, "state": "error", "host": "github.com"},
        {"login": "bob", "active": False, "state": "success", "host": "github.com"},
    ]
    github_resource._reference_info(str(tmp_path), _REFERENCE)
    gh.accounts = [
        {"login": "carol", "active": True, "state": "success", "host": "github.com"},
        {"login": "bob", "active": False, "state": "success", "host": "github.com"},
    ]

    info = github_resource._reference_info(str(tmp_path), _REFERENCE)

    assert gh.count(_AUTH_STATUS) == 2
    assert info["selected_account"] == "carol"


def test_host_with_only_an_unresolved_account_is_not_reused(gh: _FakeGh, tmp_path: Path) -> None:
    # An enterprise host whose only entry gh couldn't resolve drops out entirely.
    resolved = {"login": "alice", "active": True, "state": "success", "host": "github.com"}
    gh.accounts = [
        resolved,
        {"login": "", "active": True, "state": "error", "host": "ghe.example.com"},
    ]
    github_resource._gh_accounts(str(tmp_path))
    gh.accounts = [
        resolved,
        {"login": "dave", "active": True, "state": "success", "host": "ghe.example.com"},
    ]

    accounts = github_resource._gh_accounts(str(tmp_path))

    assert gh.count(_AUTH_STATUS) == 2
    assert [a["login"] for a in accounts] == ["alice", "dave"]


def test_callers_cannot_mutate_the_reused_list(gh: _FakeGh, tmp_path: Path) -> None:
    github_resource._gh_accounts(str(tmp_path))[0]["login"] = "mallory"
    assert github_resource._gh_accounts(str(tmp_path))[0]["login"] == "alice"


def test_token_validity_checks_stay_live(gh: _FakeGh, tmp_path: Path) -> None:
    # The sign-in state and PR discovery read each token's validity, so they still ask gh.
    github_resource._reference_info(str(tmp_path), _REFERENCE)
    gh.accounts = [{"login": "alice", "active": True, "state": "error", "host": "github.com"}]

    authenticated, accounts = github_resource._list_accounts(str(tmp_path))

    assert gh.count(_AUTH_STATUS) == 2
    assert authenticated is False
    assert accounts[0]["state"] == "error"
