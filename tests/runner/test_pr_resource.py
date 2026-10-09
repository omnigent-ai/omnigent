"""Provider resolution and the provider fields of the pull request dispatcher."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path

import pytest

import omnigent.git_providers as provider_registry
from omnigent.errors import OmnigentError
from omnigent.git_providers import (
    FacetModules,
    Instances,
    ParsedPullRequest,
    ParsedRemote,
    host_of,
    reset_for_tests,
)
from omnigent.runner import github_resource, pr_resource
from omnigent.runner.pr_resource import ProviderResolution
from omnigent.runner.session_prs import PullRequestRef, SessionPrRegistry
from tests.budgets import budget
from tests.runner.git_provider_fixtures import register_provider

LEGACY_FIELDS = ("gh_available", "authenticated", "accounts", "selected_account")
GITHUB_CAPABILITIES = {
    "account_switching": True,
    "base_remote_selection": True,
    "line_counts": True,
    "linked_pr_diff": True,
}
# An ssh config host alias (``Host github.com-work`` / ``HostName github.com``).
ALIAS_ORIGIN = "git@github.com-work:o/r.git"
ALIAS_PR_URL = "https://github.com/o/r/pull/7"
AUTH_STATUS = ("auth", "status", "--json", "hosts")
SIGNED_IN = {"hosts": {"github.com": [{"login": "alice", "active": True, "state": "success"}]}}
FORGE_HOST = "forge.example.test"
_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """No ambient provider hosts, gh sign-ins, git config, or session registry."""
    monkeypatch.setattr(provider_registry.importlib.metadata, "entry_points", lambda **_: ())
    for name in ("OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS", "GH_HOST"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "gh"))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for name, value in _GIT_IDENTITY.items():
        monkeypatch.setenv(name, value)
    reset_for_tests()
    yield
    reset_for_tests()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _git_value(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
    return result.stdout.strip()


class _ForgeWithoutFacet:
    """Claims ``forge.example.test`` remotes but has no pull request facet."""

    id = "forge"
    display_name = "Forge"
    default_hosts = (FORGE_HOST,)
    facets = FacetModules()

    def matches_host(self, host: str, instances: Instances) -> bool:
        return host == FORGE_HOST

    def parse_remote_url(self, url: str, instances: Instances) -> ParsedRemote | None:
        return ParsedRemote(self.id, FORGE_HOST, "o/r") if host_of(url) == FORGE_HOST else None

    def parse_pr_url(self, url: str, instances: Instances) -> ParsedPullRequest | None:
        return None


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git checkout on ``feature`` with one commit and no remotes."""
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q")
    _git(path, "checkout", "-q", "-b", "feature")
    (path / "a.txt").write_text("a")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", "init")
    return path


def _forbid_gh(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail("gh ran for a workspace that GitHub does not serve")

    monkeypatch.setattr(github_resource, "_gh", forbidden)


def _stub_gh(
    monkeypatch: pytest.MonkeyPatch, responses: dict[tuple[str, ...], tuple[int, str, str]]
) -> list[tuple[str, ...]]:
    """Put ``gh`` on PATH and answer each call by its leading argv; return the calls.

    Unmatched calls fail, as for a repository that ``gh`` cannot reach.
    """
    calls: list[tuple[str, ...]] = []

    def fake_gh(
        argv: Sequence[str], *, cwd: str, token: str | None = None
    ) -> tuple[int, str, str]:
        calls.append(tuple(argv))
        for prefix, value in responses.items():
            if tuple(argv[: len(prefix)]) == prefix:
                return value
        return 1, "", "no stub"

    monkeypatch.setattr(github_resource, "_gh", fake_gh)
    monkeypatch.setattr(github_resource.shutil, "which", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(github_resource, "_workspace_key", lambda _root: None)
    return calls


def test_github_info_equals_pr_info_and_keeps_the_legacy_fields(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git(repo, "remote", "add", "origin", "https://github.com/acme/repo.git")
    hosts = {"hosts": {"github.com": [{"login": "alice", "active": True, "state": "success"}]}}

    def fake_gh(
        argv: Sequence[str], *, cwd: str, token: str | None = None
    ) -> tuple[int, str, str]:
        if list(argv[:4]) == ["auth", "status", "--json", "hosts"]:
            return 0, json.dumps(hosts), ""
        # No PR and the repo is unreachable, so all four legacy fields are filled.
        return 1, "", "no access"

    monkeypatch.setattr(github_resource, "_gh", fake_gh)
    monkeypatch.setattr(github_resource.shutil, "which", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(github_resource, "_workspace_key", lambda _root: None)

    info = github_resource.github_info(str(repo))

    assert info == pr_resource.pr_info(str(repo))
    assert info["provider"] == "github"
    assert info["capabilities"] == GITHUB_CAPABILITIES
    assert [field for field in LEGACY_FIELDS if field not in info] == []
    assert info["auth"] == {
        "authenticated": True,
        "hint": None,
        "cli": {"name": "gh", "available": True},
        "accounts": info["accounts"],
        "selected_account": "alice",
    }


def test_tracked_github_pr_info_carries_the_provider_fields(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = "https://github.com/acme/repo/pull/7"
    SessionPrRegistry("session").record(
        [PullRequestRef.from_url(url)], relationship="created", source="test"
    )
    monkeypatch.setattr(github_resource.shutil, "which", lambda _name: None)

    info = pr_resource.pr_info(str(repo), session_id="session")

    assert info["selected_pr_url"] == url
    assert (info["provider"], info["capabilities"]) == ("github", GITHUB_CAPABILITIES)
    assert info["auth"]["cli"] == {"name": "gh", "available": False}
    assert info["gh_available"] is False
    assert [pr["provider"] for pr in info["prs"]] == ["github"]


def test_a_directory_outside_git_reports_not_a_git_repo_from_github(tmp_path: Path) -> None:
    info = pr_resource.pr_info(str(tmp_path))

    assert (info["available"], info["reason"]) == (False, "not_a_git_repo")
    assert (info["provider"], info["auth"]) == ("github", None)
    assert info["capabilities"] == GITHUB_CAPABILITIES


def test_an_unknown_origin_is_an_unsupported_remote(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git(repo, "remote", "add", "origin", "https://unknown-forge.example.test/g/p.git")
    # gh is installed and signed in, yet resolves no repository for this remote.
    _stub_gh(monkeypatch, {AUTH_STATUS: (0, json.dumps(SIGNED_IN), "")})

    assert pr_resource.resolve_provider(str(repo)) == ProviderResolution(
        "github", "unknown-forge.example.test", unclaimed=True
    )
    info = pr_resource.pr_info(str(repo))
    assert (info["available"], info["reason"]) == (False, "unsupported_remote")
    assert info["remote_host"] == "unknown-forge.example.test"
    session = pr_resource.pr_info(str(repo), session_id="session")
    assert session["reason"] == "unsupported_remote"
    assert (session["prs"], session["tracking_available"]) == ([], True)
    assert pr_resource.pr_changed_files(str(repo))["data"] == []
    assert pr_resource.pr_diff(str(repo))["patch"] == ""
    with pytest.raises(OmnigentError, match="is not available locally"):
        pr_resource.pr_file_diff(str(repo), "main", "a.txt")


@pytest.mark.parametrize("gh_state", ["missing", "signed_out"])
def test_an_unclaimed_origin_keeps_the_cli_and_sign_in_guidance(
    repo: Path, monkeypatch: pytest.MonkeyPatch, gh_state: str
) -> None:
    _git(repo, "remote", "add", "origin", "https://unknown-forge.example.test/g/p.git")
    _stub_gh(monkeypatch, {("auth", "status"): (1, "", "not logged in")})
    if gh_state == "missing":
        monkeypatch.setattr(github_resource.shutil, "which", lambda _name: None)

    info = pr_resource.pr_info(str(repo))

    assert (info["available"], info["provider"], info.get("reason")) == (True, "github", None)
    assert info["auth"]["cli"] == {"name": "gh", "available": gh_state == "signed_out"}
    assert info["auth"]["authenticated"] is False


def test_an_unclaimed_origin_keeps_the_account_choice(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git(repo, "remote", "add", "origin", ALIAS_ORIGIN)
    accounts = [
        {"login": "alice", "active": True, "state": "success"},
        {"login": "bob", "active": False, "state": "success"},
    ]
    hosts = {"hosts": {"github.com": accounts}}
    _stub_gh(monkeypatch, {AUTH_STATUS: (0, json.dumps(hosts), "")})

    info = pr_resource.pr_info(str(repo))

    # The active account cannot read the repository, but the panel can switch to another.
    assert (info["available"], info["provider"], info["repo"]) == (True, "github", None)
    assert [account["login"] for account in info["auth"]["accounts"]] == ["alice", "bob"]


def test_an_ssh_alias_origin_falls_back_to_github_and_records_the_branch_pr(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git(repo, "remote", "add", "origin", ALIAS_ORIGIN)
    pr = {"number": 7, "title": "Alias PR", "state": "OPEN", "url": ALIAS_PR_URL}
    _stub_gh(monkeypatch, {("pr", "view"): (0, json.dumps(pr), "")})

    assert pr_resource.resolve_provider(str(repo)) == ProviderResolution(
        "github", "github.com-work", unclaimed=True
    )
    info = pr_resource.pr_info(str(repo), session_id="session")

    assert (info["provider"], info["selected_pr_url"]) == ("github", ALIAS_PR_URL)
    assert [(listed["url"], listed["title"]) for listed in info["prs"]] == [
        (ALIAS_PR_URL, "Alias PR")
    ]
    [entry] = SessionPrRegistry("session").list()
    assert (entry.url, entry.relationship) == (ALIAS_PR_URL, "inferred")


def test_reads_and_preferences_for_an_ssh_alias_origin_reach_github(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git(repo, "remote", "add", "origin", ALIAS_ORIGIN)
    files = [{"filename": "a.txt", "status": "modified", "additions": 1, "deletions": 1}]
    calls = _stub_gh(
        monkeypatch,
        {
            ("pr", "view"): (0, json.dumps({"number": 7}), ""),
            ("pr", "diff"): (0, "alias-patch", ""),
            ("api",): (0, json.dumps(files), ""),
        },
    )

    assert [f["path"] for f in pr_resource.pr_changed_files(str(repo))["data"]] == ["a.txt"]
    assert pr_resource.pr_diff(str(repo))["patch"] == "alias-patch"
    assert pr_resource.pr_file_diff(str(repo), "", "a.txt")["after"] == "a"
    pr_resource.set_pr_preference(str(repo), remote="upstream")
    assert ("repo", "set-default", "upstream") in calls


@pytest.mark.parametrize("url", ["ssh://git@github.com:22/o/r.git", "https://github.com:443/o/r"])
def test_a_github_origin_with_a_port_resolves_to_github(repo: Path, url: str) -> None:
    _git(repo, "remote", "add", "origin", url)

    assert pr_resource.resolve_provider(str(repo)) == ProviderResolution("github", "github.com")


def test_a_partial_clone_origin_is_still_matched(repo: Path) -> None:
    _git(repo, "remote", "add", "origin", "https://github.com/acme/repo.git")
    _git(repo, "remote", "add", "mirror", "https://unknown-forge.example.test/g/p.git")
    # With these, ``git remote -v`` ends origin's fetch line with `` [blob:none]``.
    _git(repo, "config", "remote.origin.promisor", "true")
    _git(repo, "config", "remote.origin.partialclonefilter", "blob:none")

    assert pr_resource.resolve_provider(str(repo)) == ProviderResolution("github", "github.com")


def test_only_the_repository_config_pins_the_provider(
    repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git(repo, "remote", "add", "origin", "https://github.com/acme/repo.git")
    global_config = tmp_path / "global.gitconfig"
    _git(repo, "config", "--file", str(global_config), "omnigent.gitprovider", "forgejo")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    assert _git_value(repo, "config", "--get", "omnigent.gitprovider") == "forgejo"

    assert pr_resource.resolve_provider(str(repo)) == ProviderResolution("github", "github.com")

    _git(repo, "config", "omnigent.gitprovider", "Example_Forge")
    worktree = tmp_path / "worktree"
    _git(repo, "worktree", "add", "-q", "-b", "other", str(worktree))

    for root in (repo, worktree):
        assert pr_resource.resolve_provider(str(root)) == ProviderResolution(
            "example_forge", "github.com"
        )


def test_a_remote_whose_provider_has_no_facet_is_skipped(repo: Path) -> None:
    register_provider(_ForgeWithoutFacet())
    _git(repo, "remote", "add", "origin", f"https://{FORGE_HOST}/o/r.git")
    assert pr_resource.resolve_provider(str(repo)) == ProviderResolution(
        "github", FORGE_HOST, unclaimed=True
    )

    _git(repo, "remote", "add", "upstream", "https://github.com/acme/repo.git")

    assert pr_resource.resolve_provider(str(repo)) == ProviderResolution("github", FORGE_HOST)


def test_attaching_without_a_facet_preserves_the_registry(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    register_provider(_ForgeWithoutFacet())
    url = f"https://{FORGE_HOST}/o/r/pull/7"
    monkeypatch.setattr(
        _ForgeWithoutFacet,
        "parse_pr_url",
        lambda *_: ParsedPullRequest("forge", FORGE_HOST, "o/r", 7, url),
    )
    registry = SessionPrRegistry("no-facet")
    with pytest.raises(ValueError, match="Forge pull requests are not supported"):
        pr_resource.update_session_pr(str(repo), "no-facet", url, "attach")
    assert registry.list() == []


def test_git_config_provider_wins_over_remote_matching(repo: Path) -> None:
    _git(repo, "remote", "add", "origin", "https://github.com/acme/repo.git")
    _git(repo, "config", "omnigent.gitprovider", "Example_Forge")

    assert pr_resource.resolve_provider(str(repo)) == ProviderResolution(
        "example_forge", "github.com"
    )


def test_a_configured_provider_without_a_facet_is_unsupported(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git(repo, "remote", "add", "origin", "https://github.com/acme/repo.git")
    _git(repo, "config", "omnigent.gitprovider", "forgejo")
    _forbid_gh(monkeypatch)

    info = pr_resource.pr_info(str(repo))

    assert (info["reason"], info["remote_host"]) == ("unsupported_remote", "github.com")


def test_a_ghes_origin_resolves_to_github_through_gh_host(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git(repo, "remote", "add", "origin", "git@ghe.example.com:org/repo.git")
    assert pr_resource.resolve_provider(str(repo)) == ProviderResolution(
        "github", "ghe.example.com", unclaimed=True
    )

    monkeypatch.setenv("GH_HOST", "ghe.example.com")

    assert pr_resource.resolve_provider(str(repo)) == ProviderResolution(
        "github", "ghe.example.com"
    )


def test_origin_is_matched_before_the_other_remotes(repo: Path) -> None:
    _git(repo, "remote", "add", "aaa", "https://unknown-forge.example.test/g/p.git")
    _git(repo, "remote", "add", "origin", "https://github.com/acme/repo.git")
    assert pr_resource.resolve_provider(str(repo)) == ProviderResolution("github", "github.com")

    # An unclaimed origin still names the remote host; a later remote may be claimed.
    _git(repo, "remote", "set-url", "origin", "https://unknown-forge.example.test/g/p.git")
    _git(repo, "remote", "set-url", "aaa", "https://github.com/acme/repo.git")
    assert pr_resource.resolve_provider(str(repo)) == ProviderResolution(
        "github", "unknown-forge.example.test"
    )


def test_workspaces_without_a_network_remote_use_the_first_provider(
    repo: Path, tmp_path: Path
) -> None:
    assert pr_resource.resolve_provider(str(repo)) == ProviderResolution("github")

    _git(repo, "remote", "add", "origin", (tmp_path / "upstream").as_uri())
    _git(repo, "remote", "add", "local", str(tmp_path / "other"))

    assert pr_resource.resolve_provider(str(repo)) == ProviderResolution("github")
    assert pr_resource.resolve_provider(str(tmp_path / "missing")) == ProviderResolution("github")


def test_a_tracked_pr_decides_the_provider(repo: Path) -> None:
    _git(repo, "remote", "add", "origin", "https://unknown-forge.example.test/g/p.git")
    _git(repo, "config", "omnigent.gitprovider", "forgejo")
    url = "https://github.com/acme/repo/pull/7"
    SessionPrRegistry("session").record(
        [PullRequestRef.from_url(url)], relationship="attached", source="test"
    )

    assert pr_resource.resolve_provider(
        str(repo), session_id="session", pr_url=url
    ) == ProviderResolution("github", "github.com")
    assert pr_resource.resolve_provider(str(repo), session_id="session").provider == "github"
    assert pr_resource.resolve_provider(str(repo)).provider == "forgejo"
    with pytest.raises(ValueError, match="not associated"):
        pr_resource.resolve_provider(str(repo), session_id="session", pr_url=url.replace("7", "8"))


def test_the_github_facet_loads_without_the_panel_module() -> None:
    """The observer loads every facet, so GitHub's must not import ``github_resource``.

    Runs in a fresh interpreter so modules other tests imported cannot hide an import.
    """
    probe = (
        "import sys\n"
        "from omnigent.git_providers import load_facet\n"
        "from omnigent.runner.git_providers import PullRequestFacet\n"
        "facet = load_facet('github', 'pull_requests')\n"
        "assert isinstance(facet, PullRequestFacet), facet\n"
        f"assert facet.capabilities.to_json() == {GITHUB_CAPABILITIES!r}\n"
        "assert 'omnigent.runner.github_resource' not in sys.modules\n"
    )
    child_env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}

    result = subprocess.run(
        [sys.executable, "-c", probe],
        env=child_env,
        capture_output=True,
        text=True,
        timeout=budget(120),
    )

    assert result.returncode == 0, result.stderr
