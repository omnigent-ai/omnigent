"""Forgejo URLs resolve on Codeberg, configured hosts, and CLI-login instances only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.git_providers import EnvInstances, reset_for_tests, resolve_pr_url, resolve_remote
from omnigent.git_providers import forgejo as descriptor
from omnigent.git_providers.forgejo import ForgejoProvider


@pytest.fixture(autouse=True)
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Hide the developer's fj and tea logins behind an empty home."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(descriptor.sys, "platform", "linux")
    for name in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "OMNIGENT_GIT_PROVIDER_FORGEJO_HOSTS"):
        monkeypatch.delenv(name, raising=False)
    descriptor._login_authorities.cache_clear()
    return home


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _fj_keys(home: Path, hosts: dict[str, object]) -> Path:
    keys = {"hosts": hosts, "aliases": {}, "default_ssh": []}
    return _write(home / ".local/share/forgejo-cli/keys.json", json.dumps(keys))


def _tea_config(home: Path, *urls: str) -> Path:
    logins = "".join(
        f"    - name: login{index}\n      url: {url}\n      token: secret{index}\n"
        for index, url in enumerate(urls)
    )
    return _write(
        home / ".config/tea/config.yml", f"logins:\n{logins}preferences:\n    editor: false\n"
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://codeberg.org/Owner/Repo/pulls/42",
        "https://codeberg.org/owner/repo/pulls/42/files",
        "https://CODEBERG.org:443/owner/repo/pulls/42/commits/",
        "https://codeberg.org/owner/repo/pulls/42#issuecomment-9",
    ],
)
def test_codeberg_pull_request_is_canonical(url: str) -> None:
    parsed = ForgejoProvider().parse_pr_url(url, EnvInstances())
    assert parsed is not None
    assert (parsed.provider, parsed.host, parsed.repository, parsed.number) == (
        "forgejo",
        "codeberg.org",
        "owner/repo",
        42,
    )
    assert parsed.url == "https://codeberg.org/owner/repo/pulls/42"


@pytest.mark.parametrize(
    "url",
    [
        "http://codeberg.org/owner/repo/pulls/1",
        "https://user:token@codeberg.org/owner/repo/pulls/1",
        "https://alice@codeberg.org/owner/repo/pulls/1",
        "https://codeberg.org.evil.test/owner/repo/pulls/1",
        "https://unknown.test/owner/repo/pulls/1",
        "https://codeberg.org/owner/repo/pull/1",
        "https://codeberg.org/owner/repo/pulls/0",
        "https://codeberg.org/group/owner/repo/pulls/1",
        "https://codeberg.org/owner/../pulls/1",
        "https://codeberg.org/owner%2Frepo/x/pulls/1",
        "https://codeberg.org/owner/repo/issues/1",
        "https://codeberg.org/owner/repo/pulls/1/edit",
        "https://codeber\ng.org/owner/repo/pulls/1",
        "https://codeberg.org/owner/repo/pulls/" + "1" * 5000,
    ],
)
def test_rejects_untrusted_or_invalid_pull_request(url: str) -> None:
    assert ForgejoProvider().parse_pr_url(url, EnvInstances()) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://codeberg.org/Owner/Repo.git",
        "https://alice@codeberg.org/owner/repo",
        "git@codeberg.org:owner/repo.git",
        "ssh://git@codeberg.org:2222/owner/repo.git",
    ],
)
def test_codeberg_remote(url: str) -> None:
    parsed = ForgejoProvider().parse_remote_url(url, EnvInstances())
    assert parsed is not None
    assert (parsed.provider, parsed.host, parsed.repository) == (
        "forgejo",
        "codeberg.org",
        "owner/repo",
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://alice:secret@codeberg.org/owner/repo.git",
        "http://codeberg.org/owner/repo.git",
        "https://codeberg.org/group/owner/repo.git",
        "https://codeberg.org/owner/repo.git?x=1",
        "https://unknown.test/owner/repo.git",
        "git@unknown.test:owner/repo.git",
        "file:///srv/owner/repo.git",
    ],
)
def test_remote_rejects_credentials_and_untrusted_authorities(url: str) -> None:
    assert ForgejoProvider().parse_remote_url(url, EnvInstances()) is None


def test_configured_host_keeps_its_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_GIT_PROVIDER_FORGEJO_HOSTS", "git.example.test:8443")
    provider = ForgejoProvider()
    assert provider.matches_host("git.example.test:8443", EnvInstances())
    assert not provider.matches_host("git.example.test", EnvInstances())
    remote = provider.parse_remote_url("git@git.example.test:team/app.git", EnvInstances())
    assert remote is not None and remote.host == "git.example.test:8443"
    parsed = provider.parse_pr_url(
        "https://git.example.test:8443/team/app/pulls/3", EnvInstances()
    )
    assert parsed is not None and parsed.url == "https://git.example.test:8443/team/app/pulls/3"


def test_ssh_cannot_guess_between_instances_on_one_hostname(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "OMNIGENT_GIT_PROVIDER_FORGEJO_HOSTS", "git.example.test,git.example.test:8443"
    )
    assert (
        ForgejoProvider().parse_remote_url("git@git.example.test:o/r.git", EnvInstances()) is None
    )
    https = ForgejoProvider().parse_remote_url("https://git.example.test/o/r.git", EnvInstances())
    assert https is not None and https.host == "git.example.test"


def test_fj_and_tea_logins_claim_their_hosts(home: Path) -> None:
    _fj_keys(
        home,
        {
            "forgejo.example.test": {"type": "Application", "token": "fj-token"},
            "localhost:3443": {"type": "OAuth", "token": "t", "refresh_token": "r"},
            "example.test/forgejo": {"type": "Application", "token": "subpath"},
        },
    )
    _tea_config(home, "https://gitea.example.test", "http://plain.example.test")
    provider = ForgejoProvider()
    assert provider.authorities(EnvInstances()) == {
        "codeberg.org",
        "forgejo.example.test",
        "localhost:3443",
        "gitea.example.test",
    }
    assert provider.parse_pr_url("https://localhost:3443/o/r/pulls/1", EnvInstances())
    assert not provider.matches_host("plain.example.test", EnvInstances())


def test_login_and_logout_refresh_discovery_without_restart(home: Path) -> None:
    provider = ForgejoProvider()
    assert not provider.matches_host("forgejo.example.test", EnvInstances())
    keys = _fj_keys(home, {"forgejo.example.test": {"type": "Application", "token": "t"}})
    assert provider.matches_host("forgejo.example.test", EnvInstances())
    keys.write_text(json.dumps({"hosts": {}, "aliases": {}}), encoding="utf-8")
    assert not provider.matches_host("forgejo.example.test", EnvInstances())


@pytest.mark.parametrize("content", ["{", "[]", '{"hosts": ["forgejo.example.test"]}', "\xff"])
def test_malformed_fj_keys_claim_nothing(home: Path, content: str) -> None:
    _write(home / ".local/share/forgejo-cli/keys.json", content)
    assert ForgejoProvider().authorities(EnvInstances()) == {"codeberg.org"}


@pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
def test_cli_login_locations_match_each_cli(
    home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    monkeypatch.setattr(descriptor.sys, "platform", platform)
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    fj_dir, fj_legacy, tea_dir = {
        "linux": (home / ".local/share/forgejo-cli", None, home / ".config"),
        "darwin": (
            home / "Library/Application Support/forgejo-cli.forgejo-cli",
            home / "Library/Application Support/Cyborus.forgejo-cli",
            home / "Library/Application Support",
        ),
        "win32": (
            tmp_path / "roaming/forgejo-cli/forgejo-cli/data",
            tmp_path / "roaming/Cyborus/forgejo-cli/data",
            tmp_path / "local",
        ),
    }[platform]
    if fj_legacy is not None:
        _write(fj_legacy / "keys.json", json.dumps({"hosts": {"legacy.test": {}}}))
        assert ForgejoProvider().matches_host("legacy.test", EnvInstances())
    _write(fj_dir / "keys.json", json.dumps({"hosts": {"fj.test": {}}}))
    _write(home / ".tea/tea.yml", "logins:\n- url: https://tea-legacy.test\n")
    assert ForgejoProvider().authorities(EnvInstances()) == {
        "codeberg.org",
        "fj.test",
        "tea-legacy.test",
    }
    _write(tea_dir / "tea/config.yml", "logins:\n- url: 'https://tea.test'\n")
    assert ForgejoProvider().matches_host("tea.test", EnvInstances())
    assert not ForgejoProvider().matches_host("tea-legacy.test", EnvInstances())

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    _write(tmp_path / "data/forgejo-cli/keys.json", json.dumps({"hosts": {"xdg-fj.test": {}}}))
    _write(tmp_path / "config/tea/config.yml", "logins:\n- url: https://xdg-tea.test\n")
    assert ForgejoProvider().matches_host("xdg-tea.test", EnvInstances())
    assert ForgejoProvider().matches_host("xdg-fj.test", EnvInstances()) is (platform == "linux")


def test_registry_resolves_codeberg_to_forgejo() -> None:
    reset_for_tests()
    try:
        remote = resolve_remote("git@codeberg.org:owner/repo.git")
        assert remote is not None and remote.provider == "forgejo"
        pull = resolve_pr_url("https://codeberg.org/owner/repo/pulls/5")
        assert pull is not None and pull.provider == "forgejo"
    finally:
        reset_for_tests()
