"""GitLab URLs preserve instance identity, nested projects and project-local IIDs."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.git_providers import EnvInstances
from omnigent.git_providers import gitlab as descriptor
from omnigent.git_providers.gitlab import GitLabProvider, instance_authority


@pytest.fixture(autouse=True)
def configured_instances(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("OMNIGENT_GIT_PROVIDER_GITLAB_HOSTS", "git.example.test:8443")
    monkeypatch.delenv("GITLAB_HOST", raising=False)
    monkeypatch.delenv("GLAB_HOST", raising=False)
    monkeypatch.setenv("GLAB_CONFIG_DIR", str(tmp_path))
    descriptor._read_glab_hosts.cache_clear()


@pytest.mark.parametrize(
    "url,host",
    [
        ("https://gitlab.com/team/sub/project/-/merge_requests/42", "gitlab.com"),
        (
            "https://git.example.test:8443/team/sub/project/-/merge_requests/42/diffs?view=parallel#note_9",
            "git.example.test:8443",
        ),
        ("https://GITLAB.COM:443/team/sub/project/-/merge_requests/42/", "gitlab.com"),
        ("https://gitlab.com/Team/Sub/Project/-/merge_requests/42", "gitlab.com"),
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
        "https://alice@gitlab.com/team/project/-/merge_requests/1",
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
        ("git@gitlab.com:Team/Sub/Project.git", "gitlab.com"),
        ("https://alice@gitlab.com/team/sub/project.git", "gitlab.com"),
        ("https://alice@GITLAB.COM:443/Team/Sub/Project.git", "gitlab.com"),
        ("https://git.example.test:8443/team/sub/project.git", "git.example.test:8443"),
        ("https://alice@git.example.test:8443/team/sub/project.git", "git.example.test:8443"),
        ("ssh://git@git.example.test:2222/team/sub/project.git", "git.example.test:8443"),
        ("git@git.example.test:team/sub/project.git", "git.example.test:8443"),
    ],
)
def test_remote_preserves_nested_project_and_api_port(url: str, host: str) -> None:
    parsed = GitLabProvider().parse_remote_url(url, EnvInstances())
    assert parsed is not None
    assert (parsed.host, parsed.repository) == (host, "team/sub/project")


@pytest.mark.parametrize(
    "url",
    [
        "https://alice:token@gitlab.com/team/project.git",
        "https://alice:@gitlab.com/team/project.git",
        "http://alice@gitlab.com/team/project.git",
        "https://gitlab.com@unknown.test/team/project.git",
        "https://alice@gitlab.com.evil.test/team/project.git",
        "https://alice@git.example.test/team/project.git",
        "https://alice@git.example.test:9443/team/project.git",
        "https://alice@gitlab.com:bad/team/project.git",
        "https://alice@gitlab.com/team/project.git?token=example",
        "https://alice@gitlab.com/team/project.git#fragment",
    ],
)
def test_remote_rejects_passwords_and_untrusted_authorities(url: str) -> None:
    assert GitLabProvider().parse_remote_url(url, EnvInstances()) is None


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


@pytest.mark.parametrize(
    "credential", ["job_token: example", "token: example", 'use_keyring: "true"']
)
def test_signed_in_host_needs_no_omnigent_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, credential: str
) -> None:
    monkeypatch.delenv("OMNIGENT_GIT_PROVIDER_GITLAB_HOSTS")
    (tmp_path / "config.yml").write_text(
        f"hosts:\n    localhost:18443:\n        {credential}\n"
        "        api_host: localhost:18443\n        api_protocol: https\n",
        encoding="utf-8",
    )
    provider = GitLabProvider()
    parsed = provider.parse_remote_url("https://localhost:18443/team/repo.git", EnvInstances())
    assert parsed and parsed.host == "localhost:18443"
    assert provider.parse_pr_url(
        "https://localhost:18443/team/repo/-/merge_requests/7", EnvInstances()
    )
    assert not provider.matches_host("localhost:18444", EnvInstances())


def test_config_api_host_does_not_claim_a_separate_web_origin(tmp_path: Path) -> None:
    (tmp_path / "config.yml").write_text(
        "hosts:\n  private.test:\n    token: example\n    api_host: private.test:8443\n",
        encoding="utf-8",
    )
    provider = GitLabProvider()
    assert not provider.matches_host("private.test:8443", EnvInstances())
    assert provider.matches_host("private.test", EnvInstances())
    parsed = provider.parse_remote_url("git@private.test:team/repo.git", EnvInstances())
    assert parsed and parsed.host == "private.test"


def test_valid_quoted_inline_yaml_hosts_are_discovered(tmp_path: Path) -> None:
    (tmp_path / "config.yml").write_text(
        '"hosts": {"private.test:8443": {"use_keyring": true}}\n', encoding="utf-8"
    )
    assert GitLabProvider().matches_host("private.test:8443", EnvInstances())


def test_login_logout_and_new_config_dir_refresh_discovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = GitLabProvider()
    path = tmp_path / "config.yml"
    assert not provider.matches_host("private.test", EnvInstances())
    path.write_text("hosts:\n  private.test:\n    token: example\n", encoding="utf-8")
    assert provider.matches_host("private.test", EnvInstances())
    path.write_text("hosts:\n  private.test:\n    user: alice\n    token: \n", encoding="utf-8")
    assert provider.matches_host("private.test", EnvInstances())
    assert provider.parse_pr_url(
        "https://private.test/team/repo/-/merge_requests/7", EnvInstances()
    )
    path.write_text("hosts:\n  private.test:\n    token: example\n", encoding="utf-8")
    assert provider.matches_host("private.test", EnvInstances())
    monkeypatch.setenv("GLAB_CONFIG_DIR", str(tmp_path / "other"))
    assert not provider.matches_host("private.test", EnvInstances())
    monkeypatch.setenv("GLAB_CONFIG_DIR", str(tmp_path))
    path.unlink()
    assert not provider.matches_host("private.test", EnvInstances())


@pytest.mark.parametrize(
    "content",
    [
        "hosts: [private.test]",
        "hosts: [",
        "hosts: {private.test: invalid}",
        "hosts:\n\tprivate.test:\n\t\ttoken: example\n",
        "other:\n  private.test:\n    token: example\n",
        "hosts:\n  private.test:\n    api_protocol: http\n    token: example\n",
    ],
)
def test_malformed_host_config_does_not_claim_a_host(tmp_path: Path, content: str) -> None:
    (tmp_path / "config.yml").write_text(content, encoding="utf-8")
    assert not GitLabProvider().matches_host("private.test", EnvInstances())


def test_unreadable_config_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "config.yml").mkdir()
    assert descriptor._glab_configured_hosts() == frozenset()


@pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
def test_glab_config_precedence_matches_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    monkeypatch.delenv("GLAB_CONFIG_DIR")
    monkeypatch.setattr(descriptor.sys, "platform", platform)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("XDG_CONFIG_DIRS", str(tmp_path / "system"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "windows"))
    user_dir = {
        "linux": tmp_path / "home/.config",
        "darwin": tmp_path / "home/Library/Application Support",
        "win32": tmp_path / "windows",
    }[platform]
    legacy = tmp_path / "home/.config/glab-cli/config.yml"
    user = user_dir / "glab-cli/config.yml"
    system = tmp_path / "system/glab-cli/config.yml"
    for path, host in [(system, "system.test"), (user, "user.test"), (legacy, "legacy.test")]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"hosts:\n  {host}:\n    token: example\n", encoding="utf-8")
    assert descriptor._glab_configured_hosts() == {"legacy.test"}
    legacy.unlink()
    expected = "system.test" if platform == "linux" else "user.test"
    assert descriptor._glab_configured_hosts() == {expected}
    xdg = tmp_path / "xdg/glab-cli/config.yml"
    xdg.parent.mkdir(parents=True)
    xdg.write_text("hosts:\n  xdg.test:\n    token: example\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg.parent.parent))
    assert descriptor._glab_configured_hosts() == {"xdg.test"}
    monkeypatch.setenv("GLAB_CONFIG_DIR", str(tmp_path / "missing"))
    assert descriptor._glab_configured_hosts() == frozenset()
