"""GitHub descriptor: PR URL identity, remote host matching, and registry compatibility."""

from __future__ import annotations

import builtins
import json
import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

import pytest

import omnigent.git_providers.github as github_module
from omnigent.git_providers import (
    EnvInstances,
    ParsedPullRequest,
    ParsedRemote,
    provider,
    reset_for_tests,
    resolve_pr_url,
    resolve_remote,
)
from omnigent.git_providers.github import PROVIDER
from omnigent.runner.session_prs import PullRequestRef, SessionPrRegistry

A = "https://github.com/example/one/pull/42"
ENTERPRISE_REMOTE = "https://ghe.example.test/o/r.git"
GITHUB_REMOTE = ParsedRemote(provider="github", host="github.com", repository="o/r")
ENTERPRISE = ParsedRemote(provider="github", host="ghe.example.test", repository="o/r")
_MAX_DNS_HOST = ".".join(["a" * 63] * 3 + ["a" * 61])


@pytest.fixture(autouse=True)
def gh_config_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """Hide ambient GitHub host configuration; yield an empty gh config dir."""
    for name in (
        "OMNIGENT_GIT_PROVIDER_MODULES",
        "OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS",
        "GH_HOST",
        "XDG_CONFIG_HOME",
    ):
        monkeypatch.delenv(name, raising=False)
    config_dir = tmp_path / "gh"
    config_dir.mkdir()
    monkeypatch.setenv("GH_CONFIG_DIR", str(config_dir))
    reset_for_tests()
    yield config_dir
    reset_for_tests()


def test_descriptor_is_the_registered_github_provider() -> None:
    assert provider("github") is PROVIDER
    assert PROVIDER.id == "github"
    assert PROVIDER.display_name == "GitHub"
    assert PROVIDER.default_hosts == ("github.com",)
    assert PROVIDER.facets.pull_requests == "omnigent.runner.git_providers.github"


# ── Pull request URLs ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "host", "repository", "number"),
    [
        (A, "github.com", "example/one", 42),
        ("https://GITHUB.COM/EXAMPLE/ONE/pull/42/files#diff", "github.com", "example/one", 42),
        ("https://github.com/example/one/pull/42/", "github.com", "example/one", 42),
        ("https://github.com/example/one/pull/42/commits", "github.com", "example/one", 42),
        ("https://github.com/example/one/pull/42/checks/", "github.com", "example/one", 42),
        ("https://github.com/example/one/pull/42?notification=1", "github.com", "example/one", 42),
        ("  https://github.com/example/one/pull/42\n", "github.com", "example/one", 42),
        ("https://github.com/Example/My.Repo-1/pull/7", "github.com", "example/my.repo-1", 7),
        (
            "https://git-2.example.internal/example/one/pull/42",
            "git-2.example.internal",
            "example/one",
            42,
        ),
        pytest.param(
            f"https://{_MAX_DNS_HOST}/example/one/pull/42",
            _MAX_DNS_HOST,
            "example/one",
            42,
            id="maximum-dns-length",
        ),
        ("https://ghe.example.test/o/r/pull/3", "ghe.example.test", "o/r", 3),
    ],
)
def test_accepted_pr_urls_keep_their_identity(
    url: str, host: str, repository: str, number: int
) -> None:
    expected = ParsedPullRequest(
        provider="github",
        host=host,
        repository=repository,
        number=number,
        url=f"https://{host}/{repository}/pull/{number}",
    )

    assert PROVIDER.parse_pr_url(url, EnvInstances()) == expected
    assert resolve_pr_url(url) == expected
    reference = PullRequestRef.from_url(url)
    assert (
        reference.provider,
        reference.host,
        reference.repository,
        reference.number,
        reference.url,
    ) == ("github", host, repository, number, expected.url)


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/example/one/issues/42",
        "file:///example/one/pull/42",
        "https://token@github.com/example/one/pull/42",
        "https://localhost/example/one/pull/42",
        "https://github.com/../one/pull/42",
        "https://github.com/./one/pull/42",
        "https://github.com/example/one/pull/0",
        "http://github.com/example/one/pull/42",
        "https://github.com:443/example/one/pull/42",
        "https://github.com/example/one/pull/42/extra",
        "https://github.com/example/pull/42",
        "https://[github.com/example/one/pull/42",
        "https://127.0.0.1/example/one/pull/42",
        "https://git..example.com/example/one/pull/42",
        "https://-git.example.com/example/one/pull/42",
        "https://git-.example.com/example/one/pull/42",
        "https://git_host.example.com/example/one/pull/42",
        "https://github.com./example/one/pull/42",
        "https://gíthub.com/example/one/pull/42",
        pytest.param(f"https://{'a' * 64}.example.com/example/one/pull/42", id="overlong-label"),
        pytest.param(
            f"https://{'.'.join(['a' * 63] * 4)}/example/one/pull/42", id="overlong-host"
        ),
        pytest.param(f"https://{'0' * 100_000}/example/one/pull/42", id="large-numeric-host"),
        pytest.param(f"https://github.com/example/one/pull/{'9' * 5000}", id="huge-number"),
        "not a url",
        "",
    ],
)
def test_rejected_pr_urls_stay_rejected(url: str) -> None:
    assert PROVIDER.parse_pr_url(url, EnvInstances()) is None
    assert resolve_pr_url(url) is None
    with pytest.raises(ValueError):
        PullRequestRef.from_url(url)


# ── Remotes ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url", ["https://github.com/o/r.git", "git@github.com:o/r.git", "ssh://git@github.com/o/r"]
)
def test_github_remotes_parse(url: str) -> None:
    assert PROVIDER.parse_remote_url(url, EnvInstances()) == GITHUB_REMOTE
    assert resolve_remote(url) == GITHUB_REMOTE


@pytest.mark.parametrize(
    "url",
    [
        "ssh://git@github.com:22/o/r.git",
        "https://github.com:443/o/r.git",
        "http://github.com:80/o/r",
        "https://token@github.com:443/o/r.git",
    ],
)
def test_github_remotes_with_a_port_parse(url: str) -> None:
    assert PROVIDER.parse_remote_url(url, EnvInstances()) == GITHUB_REMOTE
    assert resolve_remote(url) == GITHUB_REMOTE


@pytest.mark.parametrize("name", ["GH_HOST", "OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS"])
@pytest.mark.parametrize(
    "url", ["https://ghe.example.test:8443/o/r.git", "ssh://git@ghe.example.test:7999/o/r.git"]
)
def test_enterprise_remotes_with_a_port_parse_with_a_configured_host(
    monkeypatch: pytest.MonkeyPatch, name: str, url: str
) -> None:
    assert resolve_remote(url) is None

    monkeypatch.setenv(name, "ghe.example.test")

    assert PROVIDER.parse_remote_url(url, EnvInstances()) == ENTERPRISE
    assert resolve_remote(url) == ENTERPRISE


def test_host_matching_ignores_case() -> None:
    assert PROVIDER.matches_host("GitHub.COM", EnvInstances())
    assert resolve_remote("git@GitHub.com:o/r.git") == GITHUB_REMOTE


@pytest.mark.parametrize("url", [ENTERPRISE_REMOTE, "https://gitlab.com/g/p.git"])
def test_other_hosts_are_not_github_remotes(url: str) -> None:
    assert PROVIDER.parse_remote_url(url, EnvInstances()) is None
    assert resolve_remote(url) is None


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("GH_HOST", "ghe.example.test"),
        ("GH_HOST", " GHE.Example.Test "),
        ("OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS", "ghe.example.test"),
        ("OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS", "other.example.test, GHE.example.test"),
    ],
)
def test_enterprise_remote_parses_with_a_configured_host(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    assert PROVIDER.parse_remote_url(ENTERPRISE_REMOTE, EnvInstances()) == ENTERPRISE
    assert resolve_remote(ENTERPRISE_REMOTE) == ENTERPRISE
    assert resolve_remote("https://gitlab.com/g/p.git") is None


def test_enterprise_remote_parses_with_a_gh_hosts_file(gh_config_dir: Path) -> None:
    (gh_config_dir / "hosts.yml").write_text(
        "github.com:\n"
        "    users:\n"
        "        alice:\n"
        "    user: alice\n"
        "ghe.example.test:\n"
        "    git_protocol: https\n"
        "    nested.example.test:\n"
        "# commented.example.test:\n"
        "inline.example.test: {}\n",
        encoding="utf-8",
    )

    assert PROVIDER.parse_remote_url(ENTERPRISE_REMOTE, EnvInstances()) == ENTERPRISE
    assert resolve_remote(ENTERPRISE_REMOTE) == ENTERPRISE
    # Only unindented keys name hosts.
    for host in ("nested.example.test", "commented.example.test", "inline.example.test", "user"):
        assert not PROVIDER.matches_host(host, EnvInstances())


@pytest.mark.parametrize("location", ["xdg", "home"])
def test_hosts_file_falls_back_to_the_xdg_and_home_config_dirs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, location: str
) -> None:
    monkeypatch.delenv("GH_CONFIG_DIR")
    if location == "xdg":
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        config_dir = tmp_path / "xdg" / "gh"
    else:
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
        config_dir = tmp_path / "home" / ".config" / "gh"
    config_dir.mkdir(parents=True)
    assert not PROVIDER.matches_host("ghe.example.test", EnvInstances())

    (config_dir / "hosts.yml").write_text("ghe.example.test:\n    user: bob\n", encoding="utf-8")

    assert PROVIDER.matches_host("ghe.example.test", EnvInstances())


@pytest.mark.parametrize(("platform", "found"), [("nt", True), ("posix", False)])
def test_hosts_file_falls_back_to_appdata_only_on_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, platform: str, found: bool
) -> None:
    monkeypatch.delenv("GH_CONFIG_DIR")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    config_dir = tmp_path / "appdata" / "GitHub CLI"
    config_dir.mkdir(parents=True)
    (config_dir / "hosts.yml").write_text("ghe.example.test:\n    user: bob\n", encoding="utf-8")

    with monkeypatch.context() as patch:
        patch.setattr(os, "name", platform)
        matched = PROVIDER.matches_host("ghe.example.test", EnvInstances())

    assert matched is found


def test_unreadable_hosts_file_is_ignored(gh_config_dir: Path) -> None:
    (gh_config_dir / "hosts.yml").mkdir()

    assert not PROVIDER.matches_host("ghe.example.test", EnvInstances())
    assert resolve_remote(ENTERPRISE_REMOTE) is None


# ── Hosts file cache ────────────────────────────────────────────────────────


@dataclass
class HostsFileIo:
    """The stats and opens of a ``hosts.yml`` that the GitHub descriptor made."""

    stats: int = 0
    opens: int = 0


@pytest.fixture
def hosts_io(monkeypatch: pytest.MonkeyPatch) -> HostsFileIo:
    """Count the ``os.stat`` and ``open`` calls on ``hosts.yml``; other paths pass through."""
    io = HostsFileIo()
    real_stat = os.stat

    def is_hosts_file(path: object) -> bool:
        return isinstance(path, str | os.PathLike) and os.path.basename(path) == "hosts.yml"

    def counting_stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if is_hosts_file(path):
            io.stats += 1
        return real_stat(path, *args, **kwargs)

    def counting_open(file: Any, *args: Any, **kwargs: Any) -> IO[Any]:
        if is_hosts_file(file):
            io.opens += 1
        return builtins.open(file, *args, **kwargs)

    monkeypatch.setattr(os, "stat", counting_stat)
    # Shadows ``open`` in the descriptor module only.
    monkeypatch.setattr(github_module, "open", counting_open, raising=False)
    return io


def test_an_unchanged_hosts_file_is_read_once_and_stat_once_per_call(
    gh_config_dir: Path, hosts_io: HostsFileIo
) -> None:
    (gh_config_dir / "hosts.yml").write_text(
        "ghe.example.test:\n    user: bob\n", encoding="utf-8"
    )
    hosts = ["ghe.example.test", "gitlab.com", "dev.azure.com", "GHE.Example.Test"] * 25

    matched = [PROVIDER.matches_host(host, EnvInstances()) for host in hosts]

    assert matched == [host.lower() == "ghe.example.test" for host in hosts]
    assert hosts_io.opens == 1
    assert hosts_io.stats == len(hosts)


def test_resolving_urls_on_other_hosts_reads_the_hosts_file_once(
    gh_config_dir: Path, hosts_io: HostsFileIo
) -> None:
    (gh_config_dir / "hosts.yml").write_text(
        "ghe.example.test:\n    user: bob\n", encoding="utf-8"
    )

    for _ in range(10):
        resolve_pr_url("https://dev.azure.com/contoso/web/_git/app/pullrequest/7")
        resolve_remote("https://dev.azure.com/contoso/web/_git/app")

    assert hosts_io.opens == 1


@pytest.mark.parametrize(
    ("new_content", "mtime_shift_ns", "new_host"),
    [
        # A longer file that keeps the old modification time.
        pytest.param(
            "ghe.example.test:\nother.example.test:\n", 0, "other.example.test", id="size"
        ),
        # A file of the same size with a later modification time.
        pytest.param("ghf.example.test:\n", 10_000_000_000, "ghf.example.test", id="mtime"),
    ],
)
def test_a_modified_hosts_file_is_read_again(
    gh_config_dir: Path,
    hosts_io: HostsFileIo,
    new_content: str,
    mtime_shift_ns: int,
    new_host: str,
) -> None:
    hosts_file = gh_config_dir / "hosts.yml"
    hosts_file.write_text("ghe.example.test:\n", encoding="utf-8")
    written_ns = os.stat(hosts_file).st_mtime_ns
    assert PROVIDER.matches_host("ghe.example.test", EnvInstances())
    assert not PROVIDER.matches_host(new_host, EnvInstances())
    assert hosts_io.opens == 1

    hosts_file.write_text(new_content, encoding="utf-8")
    os.utime(hosts_file, ns=(written_ns + mtime_shift_ns,) * 2)

    assert PROVIDER.matches_host(new_host, EnvInstances())
    assert hosts_io.opens == 2


def test_a_hosts_file_that_appears_later_is_picked_up(
    gh_config_dir: Path, hosts_io: HostsFileIo
) -> None:
    for _ in range(5):
        assert not PROVIDER.matches_host("ghe.example.test", EnvInstances())
    assert hosts_io.opens == 0

    (gh_config_dir / "hosts.yml").write_text(
        "ghe.example.test:\n    user: bob\n", encoding="utf-8"
    )

    assert PROVIDER.matches_host("ghe.example.test", EnvInstances())
    assert hosts_io.opens == 1


def test_an_unreadable_hosts_file_is_tried_once_until_it_changes(
    gh_config_dir: Path, hosts_io: HostsFileIo
) -> None:
    hosts_path = gh_config_dir / "hosts.yml"
    hosts_path.mkdir()
    for _ in range(5):
        assert not PROVIDER.matches_host("ghe.example.test", EnvInstances())
    assert hosts_io.opens == 1

    hosts_path.rmdir()
    hosts_path.write_text("ghe.example.test:\n    user: bob\n", encoding="utf-8")

    assert PROVIDER.matches_host("ghe.example.test", EnvInstances())
    assert hosts_io.opens == 2


def test_a_cached_hosts_file_does_not_answer_for_another_config_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("GH_CONFIG_DIR")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    windows_host, home_host = "win.example.test", "hom.example.test"
    for config_dir, host in (
        (tmp_path / "appdata" / "GitHub CLI", windows_host),
        (tmp_path / "home" / ".config" / "gh", home_host),
    ):
        config_dir.mkdir(parents=True)
        hosts_file = config_dir / "hosts.yml"
        hosts_file.write_text(f"{host}:\n    user: bob\n", encoding="utf-8")
        # Same size and modification time, so only the path tells the two files apart.
        os.utime(hosts_file, ns=(1_700_000_000_000_000_000,) * 2)

    def matches(platform: str) -> tuple[bool, bool]:
        with monkeypatch.context() as patch:
            patch.setattr(os, "name", platform)
            return (
                PROVIDER.matches_host(windows_host, EnvInstances()),
                PROVIDER.matches_host(home_host, EnvInstances()),
            )

    assert [matches(platform) for platform in ("nt", "posix", "posix", "nt")] == [
        (True, False),
        (False, True),
        (False, True),
        (True, False),
    ]


# ── Session PR registry ─────────────────────────────────────────────────────


def test_registry_entries_without_provider_load_as_github(tmp_path: Path) -> None:
    store = SessionPrRegistry("conv_legacy", root=tmp_path)
    legacy_entry = {
        "host": "github.com",
        "repository": "example/one",
        "number": 42,
        "url": A,
        "relationship": "created",
        "source": "test",
        "first_seen_at": 10,
        "last_seen_at": 10,
    }
    store.path.write_text(json.dumps({"schema_version": 1, "prs": [legacy_entry]}))

    [entry] = store.list()

    assert entry.provider == "github"
    assert entry.url == A


def test_registry_records_the_provider(tmp_path: Path) -> None:
    store = SessionPrRegistry("conv_new", root=tmp_path)

    store.record([PullRequestRef.from_url(A)], relationship="created", source="test")

    assert json.loads(store.path.read_text())["prs"][0]["provider"] == "github"
