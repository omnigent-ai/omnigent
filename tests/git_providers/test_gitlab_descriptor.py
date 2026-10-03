"""GitLab URLs preserve instance identity, nested projects and project-local IIDs."""

from __future__ import annotations

import pytest

from omnigent.git_providers import EnvInstances
from omnigent.git_providers.gitlab import GitLabProvider, instance_authority


@pytest.fixture(autouse=True)
def configured_instances(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_GIT_PROVIDER_GITLAB_HOSTS", "git.example.test:8443")
    monkeypatch.delenv("GITLAB_HOST", raising=False)
    monkeypatch.delenv("GLAB_HOST", raising=False)


@pytest.mark.parametrize(
    "url,host",
    [
        ("https://gitlab.com/team/sub/project/-/merge_requests/42", "gitlab.com"),
        (
            "https://git.example.test:8443/team/sub/project/-/merge_requests/42/diffs?view=parallel#note_9",
            "git.example.test:8443",
        ),
        ("https://GITLAB.COM:443/team/sub/project/-/merge_requests/42/", "gitlab.com"),
    ],
)
def test_merge_request(url: str, host: str) -> None:
    parsed = GitLabProvider().parse_pr_url(url, EnvInstances())
    assert parsed is not None
    assert (parsed.provider, parsed.host, parsed.repository, parsed.number) == (
        "gitlab",
        host,
        "team/sub/project",
        42,
    )
    assert parsed.url == f"https://{host}/team/sub/project/-/merge_requests/42"


@pytest.mark.parametrize(
    "url",
    [
        "http://gitlab.com/team/project/-/merge_requests/1",
        "https://user:token@gitlab.com/team/project/-/merge_requests/1",
        "https://gitlab.com.evil.test/team/project/-/merge_requests/1",
        "https://git.example.test/team/project/-/merge_requests/1",
        "https://git.example.test:9443/team/project/-/merge_requests/1",
        "https://unknown.test/team/project/-/merge_requests/1",
        "https://gitlab.com/team/project/-/merge_requests/0",
        "https://gitlab.com/team/../project/-/merge_requests/1",
        "https://gitlab.com/team%2Fproject/-/merge_requests/1",
        "https://gitlab.com/team/project/-/issues/1",
        "https://gitlab.com/team/project/-/merge_requests/1/edit",
        "https://gitla\nb.com/team/project/-/merge_requests/1",
        "https://gitlab.com/team/project/-/merge_requests/" + "1" * 5000,
    ],
)
def test_rejects_untrusted_or_invalid_mr(url: str) -> None:
    assert GitLabProvider().parse_pr_url(url, EnvInstances()) is None


@pytest.mark.parametrize(
    "url,host",
    [
        ("git@gitlab.com:team/sub/project.git", "gitlab.com"),
        ("https://git.example.test:8443/team/sub/project.git", "git.example.test:8443"),
        ("ssh://git@git.example.test:2222/team/sub/project.git", "git.example.test:8443"),
        ("git@git.example.test:team/sub/project.git", "git.example.test:8443"),
    ],
)
def test_remote_preserves_nested_project_and_api_port(url: str, host: str) -> None:
    parsed = GitLabProvider().parse_remote_url(url, EnvInstances())
    assert parsed is not None
    assert (parsed.host, parsed.repository) == (host, "team/sub/project")


def test_ssh_cannot_guess_between_instances_on_one_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "OMNIGENT_GIT_PROVIDER_GITLAB_HOSTS", "git.example.test:8443,git.example.test:9443"
    )
    provider = GitLabProvider()
    assert (
        provider.parse_remote_url("git@git.example.test:team/project.git", EnvInstances()) is None
    )
    parsed = provider.parse_remote_url(
        "https://git.example.test:9443/team/project.git", EnvInstances()
    )
    assert parsed is not None and parsed.host == "git.example.test:9443"


@pytest.mark.parametrize(
    "value",
    [
        "https://host/path",
        "https://user@host",
        "https://host?token=x",
        "host:bad",
        "http://host",
        "https://host/#fragment",
        "-host",
        "host..test",
        "gitla\nb.com",
        " gitlab.com",
    ],
)
def test_instance_must_be_an_origin(value: str) -> None:
    assert instance_authority(value) is None


def test_glab_explicit_host_is_trusted_without_oauth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITLAB_HOST", "https://private.test:8443")
    assert GitLabProvider().matches_host("private.test:8443", EnvInstances())
    assert not GitLabProvider().matches_host("private.test", EnvInstances())


def test_exact_https_authority_precedes_a_bare_host_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnigent.git_providers import reset_for_tests, resolve_remote

    monkeypatch.setenv("GH_HOST", "git.example.test")
    reset_for_tests()
    try:
        parsed = resolve_remote("https://git.example.test:8443/team/project.git")
        assert parsed is not None and parsed.provider == "gitlab"
        assert parsed.host == "git.example.test:8443"
        fallback = resolve_remote("https://git.example.test/team/project.git")
        assert fallback is not None and fallback.provider == "github"
        ssh = resolve_remote("ssh://git@git.example.test:2222/team/project.git")
        assert ssh is not None and ssh.provider == "github" and ssh.host == "git.example.test"
    finally:
        reset_for_tests()
