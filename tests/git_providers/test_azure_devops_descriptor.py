"""Azure DevOps descriptor: remote and PR URL identity, host matching, and registry use."""

from __future__ import annotations

from collections.abc import Iterator
from importlib import metadata
from pathlib import Path

import pytest

from omnigent.git_providers import (
    EnvInstances,
    ParsedPullRequest,
    ParsedRemote,
    provider,
    reset_for_tests,
    resolve_pr_url,
    resolve_remote,
)
from omnigent.git_providers.azure_devops import (
    PROVIDER,
    AzureRepo,
    canonical_pr_url,
    parse_azure_devops_pr_url,
    parse_azure_devops_remote,
)
from omnigent.runner.session_prs import PullRequestRef, SessionPrRegistry

CANONICAL = "https://dev.azure.com/org/project/_git/repo/pullrequest/42"
REPO = AzureRepo("org", "project", "repo")
SPACED_REPO = AzureRepo("org", "My Project", "My Repo")
GITHUB_REMOTE = ParsedRemote(provider="github", host="github.com", repository="o/r")


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Hide ambient provider host configuration and start from the built-in providers."""
    monkeypatch.setattr(metadata, "entry_points", lambda **_: ())
    for name in (
        "OMNIGENT_GIT_PROVIDER_AZURE_DEVOPS_HOSTS",
        "OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS",
        "GH_HOST",
        "XDG_CONFIG_HOME",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "gh"))
    reset_for_tests()
    yield
    reset_for_tests()


def test_descriptor_is_the_registered_azure_devops_provider() -> None:
    assert provider("azure_devops") is PROVIDER
    assert PROVIDER.id == "azure_devops"
    assert PROVIDER.display_name == "Azure DevOps"
    assert PROVIDER.default_hosts == ("dev.azure.com", "ssh.dev.azure.com")
    assert PROVIDER.facets.pull_requests == "omnigent.runner.git_providers.azure_devops"


# ── Remotes ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://dev.azure.com/org/project/_git/repo", REPO),
        ("https://dev.azure.com/org/project/_git/repo.git", REPO),
        ("https://dev.azure.com/org/project/_git/repo/", REPO),
        ("https://org@dev.azure.com/org/project/_git/repo", REPO),
        ("https://user:token@dev.azure.com/org/project/_git/repo", REPO),
        ("https://dev.azure.com/org/_git/repo", AzureRepo("org", "repo", "repo")),
        ("https://org.visualstudio.com/project/_git/repo", REPO),
        ("https://org.visualstudio.com/DefaultCollection/project/_git/repo", REPO),
        ("https://org.visualstudio.com/_git/repo", AzureRepo("org", "repo", "repo")),
        ("git@ssh.dev.azure.com:v3/org/project/repo", REPO),
        ("git@ssh.dev.azure.com:v3/org/project/repo.git", REPO),
        ("org@vs-ssh.visualstudio.com:v3/org/project/repo", REPO),
        ("ssh://git@ssh.dev.azure.com/v3/org/project/repo", REPO),
        ("ssh://git@ssh.dev.azure.com/v3/org/project/repo.git", REPO),
        ("ssh://org@vs-ssh.visualstudio.com/v3/org/project/repo", REPO),
        pytest.param(
            "https://DEV.Azure.com/Org/Project/_Git/Repo",
            AzureRepo("Org", "Project", "Repo"),
            id="mixed-case",
        ),
        pytest.param(
            "ssh://git@SSH.Dev.Azure.com/v3/Org/Project/Repo",
            AzureRepo("Org", "Project", "Repo"),
            id="ssh-url-mixed-case",
        ),
        pytest.param(
            "https://Org.VisualStudio.com/Project/_git/Repo",
            AzureRepo("org", "Project", "Repo"),
            id="legacy-organization-comes-from-the-lower-cased-host",
        ),
        ("  https://dev.azure.com/org/project/_git/repo\n", REPO),
        ("https://dev.azure.com/org/My%20Project/_git/My%20Repo", SPACED_REPO),
        ("git@ssh.dev.azure.com:v3/org/My%20Project/My%20Repo", SPACED_REPO),
        ("ssh://git@ssh.dev.azure.com/v3/org/My%20Project/My%20Repo", SPACED_REPO),
    ],
)
def test_azure_devops_remotes_parse(url: str, expected: AzureRepo) -> None:
    remote = ParsedRemote(
        provider="azure_devops",
        host="dev.azure.com",
        repository=f"{expected.org}/{expected.project}/{expected.repo}",
    )

    assert parse_azure_devops_remote(url) == expected
    assert PROVIDER.parse_remote_url(url, EnvInstances()) == remote
    assert resolve_remote(url) == remote


@pytest.mark.parametrize(
    "url",
    [
        "https://notdev.azure.com/o/p/_git/r",
        "https://dev.azure.com.evil.test/o/p/_git/r",
        "https://evil.test/dev.azure.com/o/p/_git/r",
        "https://github.com/o/r.git",
        "git@github.com:o/r.git",
        "git@ssh.dev.azure.com.evil.test:v3/o/p/r",
        "git@notssh.dev.azure.com:v3/o/p/r",
        "git@dev.azure.com:v3/o/p/r",
        "git@ssh.dev.azure.com:o/p/r",
        "git@ssh.dev.azure.com:v4/o/p/r",
        "git@ssh.dev.azure.com:v3/o/p",
        "git@ssh.dev.azure.com:v3/o/p/r/extra",
        "ssh://git@example.test/v3/o/p/r",
        "ssh://git@dev.azure.com/v3/o/p/r",
        "ssh://git@ssh.dev.azure.com.evil.test/v3/o/p/r",
        "ssh://git@ssh.dev.azure.com:22/v3/o/p/r",
        "ssh://org@vs-ssh.visualstudio.com:22/v3/o/p/r",
        "ssh://git@ssh.dev.azure.com/o/p/r",
        "ssh://git@ssh.dev.azure.com/v4/o/p/r",
        "ssh://git@ssh.dev.azure.com/o/p/_git/r",
        "ssh://git@ssh.dev.azure.com/v3/o/p",
        "ssh://git@ssh.dev.azure.com/v3/o/p/r/extra",
        "https://dev.azure.com:443/o/p/_git/r",
        "https://ssh.dev.azure.com/o/p/_git/r",
        "https://vs-ssh.visualstudio.com/p/_git/r",
        "https://a.b.visualstudio.com/p/_git/r",
        "https://visualstudio.com/p/_git/r",
        "https://dev.azure.com/o/p/r",
        "https://dev.azure.com/o/p/_git",
        "https://dev.azure.com/o/_git",
        "https://dev.azure.com/_git/r",
        "https://dev.azure.com/o/p/_git/r/extra",
        "https://dev.azure.com/o/p/_git/r/pullrequest/1",
        "https://dev.azure.com/o/../_git/r",
        "https://dev.azure.com//p/_git/r",
        "https://dev.azure.com/o/p%2Fq/_git/r",
        "https://dev.azure.com/o/p/_git/.git",
        "not a url",
        "",
    ],
)
def test_other_remotes_are_not_azure_devops(url: str) -> None:
    resolved = resolve_remote(url)

    assert parse_azure_devops_remote(url) is None
    assert PROVIDER.parse_remote_url(url, EnvInstances()) is None
    assert resolved is None or resolved.provider != "azure_devops"


def test_github_remotes_still_resolve_to_github() -> None:
    assert resolve_remote("https://github.com/o/r.git") == GITHUB_REMOTE
    assert resolve_remote("git@github.com:o/r.git") == GITHUB_REMOTE


# ── Pull request URLs ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "repository", "canonical"),
    [
        (CANONICAL, "org/project/repo", CANONICAL),
        (
            "https://dev.azure.com/Org/Project/_git/Repo/pullrequest/42",
            "org/project/repo",
            CANONICAL,
        ),
        (
            "https://DEV.AZURE.COM/org/project/_git/repo/pullrequest/42",
            "org/project/repo",
            CANONICAL,
        ),
        (f"{CANONICAL}?_a=files&path=%2Fa.py#discussion", "org/project/repo", CANONICAL),
        (f"{CANONICAL}/", "org/project/repo", CANONICAL),
        (f"  {CANONICAL}\n", "org/project/repo", CANONICAL),
        pytest.param(
            "https://dev.azure.com/org/_git/repo/pullrequest/42",
            "org/repo/repo",
            "https://dev.azure.com/org/repo/_git/repo/pullrequest/42",
            id="project-equals-repo",
        ),
        pytest.param(
            "https://org.visualstudio.com/project/_git/repo/pullrequest/42",
            "org/project/repo",
            CANONICAL,
            id="legacy-host",
        ),
        pytest.param(
            "https://org.visualstudio.com/DefaultCollection/project/_git/repo/pullrequest/42",
            "org/project/repo",
            CANONICAL,
            id="legacy-host-with-default-collection",
        ),
        pytest.param(
            "https://Org.VisualStudio.com/Project/_git/Repo/pullrequest/42",
            "org/project/repo",
            CANONICAL,
            id="legacy-host-mixed-case",
        ),
        pytest.param(
            "https://dev.azure.com/org/My%20Project/_git/My%20Repo/pullrequest/42",
            "org/my project/my repo",
            "https://dev.azure.com/org/my%20project/_git/my%20repo/pullrequest/42",
            id="names-with-spaces",
        ),
    ],
)
def test_accepted_pr_urls_keep_their_identity(url: str, repository: str, canonical: str) -> None:
    expected = ParsedPullRequest(
        provider="azure_devops",
        host="dev.azure.com",
        repository=repository,
        number=42,
        url=canonical,
    )

    assert parse_azure_devops_pr_url(url) == expected
    assert PROVIDER.parse_pr_url(url, EnvInstances()) == expected
    assert resolve_pr_url(url) == expected
    reference = PullRequestRef.from_url(url)
    assert (
        reference.provider,
        reference.host,
        reference.repository,
        reference.number,
        reference.url,
    ) == ("azure_devops", "dev.azure.com", repository, 42, canonical)


@pytest.mark.parametrize(
    "url",
    [
        "https://notdev.azure.com/o/p/_git/r/pullrequest/1",
        "https://dev.azure.com.evil.test/o/p/_git/r/pullrequest/1",
        "https://evil.test/o/p/_git/r/pullrequest/1",
        "https://ssh.dev.azure.com/o/p/_git/r/pullrequest/1",
        "https://vs-ssh.visualstudio.com/p/_git/r/pullrequest/1",
        "https://a.b.visualstudio.com/p/_git/r/pullrequest/1",
        "https://visualstudio.com/p/_git/r/pullrequest/1",
        "http://dev.azure.com/o/p/_git/r/pullrequest/1",
        "file:///o/p/_git/r/pullrequest/1",
        "https://org@dev.azure.com/o/p/_git/r/pullrequest/1",
        "https://user:secret@dev.azure.com/o/p/_git/r/pullrequest/1",
        "https://dev.azure.com:443/o/p/_git/r/pullrequest/1",
        "https://dev.azure.com:8443/o/p/_git/r/pullrequest/1",
        "https://dev.azure.com./o/p/_git/r/pullrequest/1",
        "https://dev.azure.com/o/p/_git/r/pullrequest/0",
        "https://dev.azure.com/o/p/_git/r/pullrequest/01",
        "https://dev.azure.com/o/p/_git/r/pullrequest/-1",
        "https://dev.azure.com/o/p/_git/r/pullrequest/abc",
        "https://dev.azure.com/o/p/_git/r/pullrequest/1.5",
        "https://dev.azure.com/o/p/_git/r/pullrequest/",
        "https://dev.azure.com/o/p/_git/r/pullrequest",
        "https://dev.azure.com/o/p/_git/r/pullrequest/1/files",
        "https://dev.azure.com/o/p/_git/r/pullrequests/1",
        "https://dev.azure.com/o/p/_git/r",
        "https://dev.azure.com/o/p/r/pullrequest/1",
        "https://dev.azure.com/o/p/_git/pullrequest/1",
        "https://dev.azure.com/_git/r/pullrequest/1",
        "https://dev.azure.com/pullrequest/1",
        "https://dev.azure.com/../p/_git/r/pullrequest/1",
        "https://dev.azure.com/o/%2E%2E/_git/r/pullrequest/1",
        "https://dev.azure.com/o/p%2Fq/_git/r/pullrequest/1",
        "https://dev.azure.com/o/p%0A/_git/r/pullrequest/1",
        "https://dev.azure.com/o/p%FF/_git/r/pullrequest/1",
        "https://dev.azure.com//o/p/_git/r/pullrequest/1",
        "https://dev.azure.com/o//_git/r/pullrequest/1",
        "https://[dev.azure.com/o/p/_git/r/pullrequest/1",
        pytest.param(
            f"https://dev.azure.com/o/p/_git/r/pullrequest/{'9' * 5000}", id="huge-number"
        ),
        "not a url",
        "",
    ],
)
def test_rejected_pr_urls_stay_rejected(url: str) -> None:
    assert parse_azure_devops_pr_url(url) is None
    assert PROVIDER.parse_pr_url(url, EnvInstances()) is None
    assert resolve_pr_url(url) is None
    with pytest.raises(ValueError):
        PullRequestRef.from_url(url)


def test_github_pr_urls_still_resolve_to_github() -> None:
    url = "https://github.com/example/one/pull/42"

    parsed = resolve_pr_url(url)

    assert parse_azure_devops_pr_url(url) is None
    assert PROVIDER.parse_pr_url(url, EnvInstances()) is None
    assert parsed is not None and parsed.provider == "github"
    assert PullRequestRef.from_url(url).provider == "github"


# ── Names ───────────────────────────────────────────────────────────────────


def test_canonical_pr_url_lower_cases_and_encodes_each_name() -> None:
    assert canonical_pr_url("Org", "My Project", "Repo", 7) == (
        "https://dev.azure.com/org/my%20project/_git/repo/pullrequest/7"
    )
    # A slash or ampersand inside a name cannot change the URL structure.
    assert canonical_pr_url("o", "a/b", "r&d", 1) == (
        "https://dev.azure.com/o/a%2Fb/_git/r%26d/pullrequest/1"
    )


def test_a_project_with_a_space_round_trips() -> None:
    remote = "https://dev.azure.com/Org/My%20Project/_git/Repo"

    parsed = parse_azure_devops_pr_url(f"{remote}/pullrequest/3")

    assert parse_azure_devops_remote(remote) == AzureRepo("Org", "My Project", "Repo")
    assert parsed is not None
    assert parsed.repository == "org/my project/repo"
    assert parsed.url == canonical_pr_url("Org", "My Project", "Repo", 3)
    assert parsed.url == "https://dev.azure.com/org/my%20project/_git/repo/pullrequest/3"
    # The canonical URL parses back to itself.
    assert parse_azure_devops_pr_url(parsed.url) == parsed


# ── Host matching ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "host",
    [
        "dev.azure.com",
        "DEV.Azure.COM",
        "ssh.dev.azure.com",
        "vs-ssh.visualstudio.com",
        "org.visualstudio.com",
        "Org.VisualStudio.com",
    ],
)
def test_azure_devops_hosts_match(host: str) -> None:
    assert PROVIDER.matches_host(host, EnvInstances())


@pytest.mark.parametrize(
    "host",
    [
        "github.com",
        "azure.com",
        "notdev.azure.com",
        "dev.azure.com.evil.test",
        "visualstudio.com",
        "evilvisualstudio.com",
        "visualstudio.com.evil.test",
        "",
    ],
)
def test_other_hosts_do_not_match(host: str) -> None:
    assert not PROVIDER.matches_host(host, EnvInstances())


def test_configured_hosts_are_not_claimed(monkeypatch: pytest.MonkeyPatch) -> None:
    # Azure DevOps Server is out of scope: the URL parsers read only the Services hosts.
    monkeypatch.setenv(
        "OMNIGENT_GIT_PROVIDER_AZURE_DEVOPS_HOSTS", "ado.example.test, Other.Example.Test"
    )

    assert not PROVIDER.matches_host("ADO.example.test", EnvInstances())
    assert not PROVIDER.matches_host("other.example.test", EnvInstances())
    assert not PROVIDER.matches_host("git.example.test", EnvInstances())
    assert PROVIDER.matches_host("dev.azure.com", EnvInstances())
    remote = "https://ado.example.test/org/project/_git/repo"
    assert PROVIDER.parse_remote_url(remote, EnvInstances()) is None
    assert PROVIDER.parse_pr_url(f"{remote}/pullrequest/42", EnvInstances()) is None


# ── Session PR registry ─────────────────────────────────────────────────────


def test_registry_records_one_entry_for_urls_that_differ_in_case_or_host(tmp_path: Path) -> None:
    store = SessionPrRegistry("conv_ado", root=tmp_path)
    references = [
        PullRequestRef.from_url("https://dev.azure.com/Org/Project/_git/Repo/pullrequest/42"),
        PullRequestRef.from_url("https://org.visualstudio.com/project/_git/repo/pullrequest/42"),
    ]

    store.record(references, relationship="created", source="test")

    [entry] = store.list()
    assert entry.provider == "azure_devops"
    assert entry.url == CANONICAL
