"""Git provider registry: installed plugins, facets, and URL resolution."""

from __future__ import annotations

import logging
import re
import sys
import types
from collections.abc import Iterator
from dataclasses import dataclass, field
from importlib.metadata import EntryPoint, entry_points
from pathlib import Path
from urllib.parse import urlsplit

import pytest

import omnigent.git_providers as registry
from omnigent.git_providers import (
    EnvInstances,
    FacetModules,
    Instances,
    ParsedPullRequest,
    ParsedRemote,
    host_of,
    load_facet,
    provider,
    provider_display,
    providers,
    reset_for_tests,
    resolve_pr_url,
    resolve_remote,
)
from omnigent.git_providers.github import PROVIDER as GITHUB
from omnigent.runner.session_prs import PullRequestRef

GITHUB_SHAPED_PR = "https://git.example.test/owner/repo/pull/7"
GITLAB_MR = "https://git.example.test/g/s/p/-/merge_requests/7"


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Start from the built-in providers, with no ambient GitHub host configuration."""
    monkeypatch.setattr(registry.importlib.metadata, "entry_points", lambda **_: ())
    for name in ("OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS", "GH_HOST"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "gh"))
    reset_for_tests()
    yield
    reset_for_tests()


@dataclass(frozen=True)
class FakeProvider:
    """Claims its default and configured hosts; ``any_host`` also parses unclaimed ones."""

    id: str
    display_name: str = "Fake"
    default_hosts: tuple[str, ...] = ()
    facets: FacetModules = field(default_factory=FacetModules)
    any_host: bool = False

    def matches_host(self, host: str, instances: Instances) -> bool:
        return host in self.default_hosts or host in instances.hosts_for(self.id)

    def _parsed_host(self, url: str, instances: Instances) -> str | None:
        host = host_of(url)
        if host is None or not (self.any_host or self.matches_host(host, instances)):
            return None
        return host

    def parse_remote_url(self, url: str, instances: Instances) -> ParsedRemote | None:
        host = self._parsed_host(url, instances)
        if host is None:
            return None
        return ParsedRemote(provider=self.id, host=host, repository="fake/repo")

    def parse_pr_url(self, url: str, instances: Instances) -> ParsedPullRequest | None:
        host = self._parsed_host(url, instances)
        match = re.search(r"/([1-9][0-9]*)/?$", url)
        if host is None or match is None:
            return None
        return ParsedPullRequest(
            provider=self.id, host=host, repository="fake/repo", number=int(match[1]), url=url
        )


class FakeGitLab:
    """A GitLab-shaped descriptor: merge requests of projects in nested groups."""

    id = "gitlab"
    display_name = "GitLab"
    default_hosts = ("git.example.test",)
    facets = FacetModules()

    def matches_host(self, host: str, instances: Instances) -> bool:
        return host in self.default_hosts

    def parse_remote_url(self, url: str, instances: Instances) -> ParsedRemote | None:
        return None

    def parse_pr_url(self, url: str, instances: Instances) -> ParsedPullRequest | None:
        parts = urlsplit(url)
        host = parts.hostname or ""
        match = re.fullmatch(r"/([\w.-]+(?:/[\w.-]+)+)/-/merge_requests/([1-9][0-9]*)", parts.path)
        if parts.scheme != "https" or match is None or not self.matches_host(host, instances):
            return None
        repository, number = match[1].lower(), int(match[2])
        return ParsedPullRequest(
            provider=self.id,
            host=host,
            repository=repository,
            number=number,
            url=f"https://{host}/{repository}/-/merge_requests/{number}",
        )


def _provider_module(monkeypatch: pytest.MonkeyPatch, name: str, descriptor: object) -> None:
    module = types.ModuleType(name)
    module.PROVIDER = descriptor
    monkeypatch.setitem(sys.modules, name, module)


def _ids() -> list[str]:
    return [descriptor.id for descriptor in providers()]


def _add_provider(descriptor: registry.GitProvider) -> None:
    registry._providers = (*providers(), descriptor)


def _plugins(monkeypatch: pytest.MonkeyPatch, *modules: str) -> None:
    entries = tuple(
        EntryPoint(name=name, value=f"{name}:PROVIDER", group=registry.ENTRY_POINT_GROUP)
        for name in modules
    )
    monkeypatch.setattr(registry.importlib.metadata, "entry_points", lambda **_: entries)


def test_plugins_load_on_first_use_and_stay_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def factory() -> FakeProvider:
        calls.append("loaded")
        return FakeProvider("lazy")

    _provider_module(monkeypatch, "gp_test_lazy", factory)
    _plugins(monkeypatch, "gp_test_lazy")
    assert calls == []
    loaded = providers()
    assert [descriptor.id for descriptor in loaded] == ["github", "lazy"]
    assert loaded[0] is GITHUB
    assert providers() is loaded
    assert calls == ["loaded"]


def test_installed_plugins_follow_builtins_and_broken_plugins_are_skipped(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _provider_module(monkeypatch, "gp_test_forge_a", FakeProvider("forge_a"))
    _provider_module(monkeypatch, "gp_test_forge_b", FakeProvider("forge_b"))
    _provider_module(monkeypatch, "gp_test_invalid", object())
    _plugins(
        monkeypatch, "gp_test_forge_a", "gp_test_missing", "gp_test_invalid", "gp_test_forge_b"
    )
    with caplog.at_level(logging.WARNING, logger="omnigent.git_providers"):
        assert _ids() == ["github", "forge_a", "forge_b"]
    assert "gp_test_missing" in caplog.text
    assert "gp_test_invalid" in caplog.text


def test_plugins_cannot_replace_builtins_or_other_plugins(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    original = FakeProvider("forge")
    _provider_module(monkeypatch, "gp_test_a", original)
    _provider_module(monkeypatch, "gp_test_b", FakeProvider("forge", display_name="Replacement"))
    _provider_module(monkeypatch, "gp_test_c", FakeProvider("github", display_name="Replacement"))
    _plugins(monkeypatch, "gp_test_c", "gp_test_b", "gp_test_a")
    assert _ids() == ["github", "forge"]
    assert provider("github") is GITHUB
    assert provider("forge") is original
    assert "already defined" in caplog.text


def test_broken_discovery_keeps_github_available(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(**_: object) -> object:
        raise RuntimeError("broken package metadata")

    monkeypatch.setattr(registry.importlib.metadata, "entry_points", broken)
    parsed = resolve_pr_url("https://github.com/o/r/pull/7")
    assert parsed is not None and parsed.provider == "github"


def test_installed_distribution_contributes_a_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    distribution = tmp_path / "example_forge-1.0.dist-info"
    distribution.mkdir()
    (distribution / "METADATA").write_text("Name: example-forge\nVersion: 1.0\n")
    (distribution / "entry_points.txt").write_text(
        "[omnigent.git_providers]\nexample = gp_test_packaged:PROVIDER\n"
    )
    descriptor = FakeProvider("example", default_hosts=("forge.example.test",))
    _provider_module(monkeypatch, "gp_test_packaged", descriptor)
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(registry.importlib.metadata, "entry_points", entry_points)

    parsed = resolve_pr_url("https://forge.example.test/o/r/pull/7")

    assert provider("example") is descriptor
    assert parsed is not None and parsed.provider == "example"


def test_plugin_labels_are_optional() -> None:
    _add_provider(FakeProvider("forge"))
    assert provider_display("forge") == {
        "id": "forge",
        "display_name": "Fake",
        "request_name": "pull request",
        "number_prefix": "#",
    }
    assert provider_display("missing") is None


# ── Facets ──────────────────────────────────────────────────────────────────


def _register_forge_facets(**facets: str) -> None:
    _add_provider(FakeProvider("forge", facets=FacetModules(**facets)))


def test_load_facet_returns_none_when_the_provider_or_facet_is_unset() -> None:
    _register_forge_facets()

    assert load_facet("no-such-provider", "pull_requests") is None
    assert load_facet("forge", "pull_requests") is None


@pytest.mark.parametrize(
    "module_path",
    ["tests.git_providers.gp_no_such_facet", "gp_no_such_package.facets.pull_requests"],
)
def test_load_facet_returns_none_when_the_facet_module_does_not_exist(module_path: str) -> None:
    _register_forge_facets(pull_requests=module_path)

    assert load_facet("forge", "pull_requests") is None


def test_load_facet_returns_the_kind_attribute(monkeypatch: pytest.MonkeyPatch) -> None:
    facet = object()
    module = types.ModuleType("gp_test_facets")
    module.PULL_REQUESTS = facet
    monkeypatch.setitem(sys.modules, "gp_test_facets", module)
    _register_forge_facets(pull_requests="gp_test_facets", credential="gp_test_facets")

    assert load_facet("forge", "pull_requests") is facet
    # The module exists but defines no CREDENTIAL attribute.
    assert load_facet("forge", "credential") is None


def test_load_facet_propagates_other_import_errors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "gp_test_facet_missing_dependency.py").write_text(
        "import gp_test_absent_dependency\n"
    )
    (tmp_path / "gp_test_facet_raises.py").write_text(
        "raise RuntimeError('facet import failed')\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    _register_forge_facets(
        pull_requests="gp_test_facet_missing_dependency", policy="gp_test_facet_raises"
    )

    with pytest.raises(ModuleNotFoundError) as missing:
        load_facet("forge", "pull_requests")
    assert missing.value.name == "gp_test_absent_dependency"
    with pytest.raises(RuntimeError, match="facet import failed"):
        load_facet("forge", "policy")


def test_load_facet_rejects_an_unknown_kind() -> None:
    with pytest.raises(ValueError, match="webhooks"):
        load_facet("github", "webhooks")


# ── Resolution ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("https://GitHub.com/o/r", "github.com"),
        ("http://git.example.test:8080/o/r.git", "git.example.test"),
        ("https://user:secret@git.example.test/o/r", "git.example.test"),
        ("ssh://git@Git.Example.Test:22/o/r.git", "git.example.test"),
        ("git@Git.Example.Test:o/r.git", "git.example.test"),
        ("  https://github.com/o/r\n", "github.com"),
        ("git://github.com/o/r", None),
        ("file:///srv/git/r.git", None),
        ("/srv/git/r.git", None),
        ("github.com/o/r", None),
        ("https:///o/r", None),
        ("https://[github.com/o/r", None),
        ("not a url", None),
        ("", None),
    ],
)
def test_host_of(url: str, host: str | None) -> None:
    assert host_of(url) == host


def test_env_instances_read_the_provider_host_list(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(
        "OMNIGENT_GIT_PROVIDER_FORGE_HOSTS", " Git.Example.Test , ,other.example.test"
    )
    monkeypatch.delenv("OMNIGENT_GIT_PROVIDER_UNSET_HOSTS", raising=False)

    assert EnvInstances().hosts_for("forge") == {"git.example.test", "other.example.test"}
    assert EnvInstances().hosts_for("unset") == frozenset()


def test_a_provider_that_claims_the_host_wins_over_earlier_host_agnostic_ones() -> None:
    # GitHub (registered first) parses PR URLs on any host, as does "agnostic".
    _add_provider(FakeProvider("agnostic", any_host=True))
    _add_provider(FakeProvider("claimer", default_hosts=("git.example.test",)))

    pull_request = resolve_pr_url(GITHUB_SHAPED_PR)
    remote = resolve_remote("https://git.example.test/owner/repo.git")

    assert pull_request is not None and pull_request.provider == "claimer"
    assert remote is not None and remote.provider == "claimer"


def test_unclaimed_hosts_fall_back_to_registration_order() -> None:
    _add_provider(FakeProvider("agnostic", any_host=True))
    _add_provider(FakeProvider("claimer", default_hosts=("git.example.test",)))

    pull_request = resolve_pr_url("https://other.example.test/owner/repo/pull/7")
    remote = resolve_remote("https://other.example.test/owner/repo.git")

    assert pull_request is not None and pull_request.provider == "github"
    assert remote is not None and remote.provider == "agnostic"
    assert resolve_remote("not a remote") is None


def test_resolution_reads_configured_instances_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _add_provider(FakeProvider("claimer"))
    monkeypatch.setenv("OMNIGENT_GIT_PROVIDER_CLAIMER_HOSTS", "git.example.test")

    parsed = resolve_pr_url(GITHUB_SHAPED_PR)

    assert parsed is not None and parsed.provider == "claimer"


def test_explicit_instances_replace_the_environment() -> None:
    class Configured:
        def hosts_for(self, provider_id: str) -> frozenset[str]:
            return frozenset({"git.example.test"} if provider_id == "claimer" else ())

    _add_provider(FakeProvider("claimer"))

    from_env = resolve_pr_url(GITHUB_SHAPED_PR)
    configured = resolve_pr_url(GITHUB_SHAPED_PR, Configured())

    assert from_env is not None and from_env.provider == "github"
    assert configured is not None and configured.provider == "claimer"


def test_pull_request_ref_uses_a_registered_gitlab_descriptor() -> None:
    with pytest.raises(ValueError):
        PullRequestRef.from_url(GITLAB_MR)
    _add_provider(FakeGitLab())

    reference = PullRequestRef.from_url(GITLAB_MR)

    assert reference.provider == "gitlab"
    assert reference.host == "git.example.test"
    assert reference.repository == "g/s/p"
    assert reference.number == 7
    assert reference.url == GITLAB_MR


# ── Broken providers ────────────────────────────────────────────────────────

REGISTRY_LOGGER = "omnigent.git_providers"


@dataclass(frozen=True, eq=False)
class RaisingProvider:
    """Claims ``git.example.test``; each descriptor method named in ``failing`` raises.

    ``calls`` lists the methods that ran, so a test can tell that the registry consulted it.
    ``parses`` makes ``parse_pr_url`` accept any URL, so a test can tell whether it was asked.
    """

    id: str
    failing: frozenset[str]
    parses: bool = False
    calls: list[str] = field(default_factory=list)
    display_name: str = "Raising"
    default_hosts: tuple[str, ...] = ("git.example.test",)
    facets: FacetModules = field(default_factory=FacetModules)

    def _enter(self, method: str) -> None:
        self.calls.append(method)
        if method in self.failing:
            raise KeyError(method)

    def matches_host(self, host: str, instances: Instances) -> bool:
        self._enter("matches_host")
        return host in self.default_hosts

    def parse_remote_url(self, url: str, instances: Instances) -> ParsedRemote | None:
        self._enter("parse_remote_url")
        return None

    def parse_pr_url(self, url: str, instances: Instances) -> ParsedPullRequest | None:
        self._enter("parse_pr_url")
        if not self.parses:
            return None
        return ParsedPullRequest(
            provider=self.id, host="git.example.test", repository="fake/repo", number=7, url=url
        )


def _registry_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [record for record in caplog.records if record.name == REGISTRY_LOGGER]


def test_a_provider_module_that_raises_at_import_is_skipped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / "gp_test_provider_raises.py").write_text(
        "raise RuntimeError('provider import failed')\n", encoding="utf-8"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    _provider_module(monkeypatch, "gp_test_forge_after", FakeProvider("forge_after"))
    _plugins(monkeypatch, "gp_test_provider_raises", "gp_test_forge_after")

    with caplog.at_level(logging.WARNING, logger=REGISTRY_LOGGER):
        ids = _ids()

    assert ids == ["github", "forge_after"]
    assert provider("github") is GITHUB
    github_pr = resolve_pr_url("https://github.com/o/r/pull/7")
    assert github_pr is not None and github_pr.provider == "github"
    [record] = _registry_records(caplog)
    assert record.levelno == logging.WARNING
    assert "gp_test_provider_raises" in record.getMessage()
    assert record.exc_info is not None and record.exc_info[0] is RuntimeError


def test_a_provider_module_whose_provider_attribute_raises_is_skipped(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    module = types.ModuleType("gp_test_provider_attribute_raises")

    def module_getattr(name: str) -> object:
        if name == "PROVIDER":
            raise RuntimeError("PROVIDER is unavailable")
        raise AttributeError(name)

    module.__getattr__ = module_getattr
    monkeypatch.setitem(sys.modules, "gp_test_provider_attribute_raises", module)
    _plugins(monkeypatch, "gp_test_provider_attribute_raises")

    with caplog.at_level(logging.WARNING, logger=REGISTRY_LOGGER):
        assert _ids() == ["github"]

    [record] = _registry_records(caplog)
    assert "gp_test_provider_attribute_raises" in record.getMessage()


@pytest.mark.parametrize("method", ["matches_host", "parse_pr_url"])
def test_a_descriptor_that_raises_does_not_stop_pull_request_resolution(method: str) -> None:
    # It would parse the URL if asked, so GitHub's answer shows that it was skipped.
    raising = RaisingProvider("raising", frozenset({method}), parses=True)
    _add_provider(raising)

    parsed = resolve_pr_url(GITHUB_SHAPED_PR)

    assert method in raising.calls
    assert parsed is not None and parsed.provider == "github"


@pytest.mark.parametrize("method", ["matches_host", "parse_remote_url"])
def test_a_descriptor_that_raises_does_not_stop_remote_resolution(method: str) -> None:
    raising = RaisingProvider("raising", frozenset({method}))
    _add_provider(raising)
    _add_provider(FakeProvider("agnostic", any_host=True))

    remote = resolve_remote("https://git.example.test/owner/repo.git")

    assert method in raising.calls
    assert remote is not None and remote.provider == "agnostic"


def test_a_failing_descriptor_warns_once_then_logs_at_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    _add_provider(RaisingProvider("raising", frozenset({"matches_host"})))

    with caplog.at_level(logging.DEBUG, logger=REGISTRY_LOGGER):
        for _ in range(3):
            resolve_pr_url(GITHUB_SHAPED_PR)

    records = _registry_records(caplog)
    assert [record.levelno for record in records] == [
        logging.WARNING,
        logging.DEBUG,
        logging.DEBUG,
    ]
    assert all("Git provider raising failed in matches_host" in r.getMessage() for r in records)
    assert records[0].exc_info is not None and records[0].exc_info[0] is KeyError


def test_each_failing_descriptor_warns_once_across_its_methods(
    caplog: pytest.LogCaptureFixture,
) -> None:
    _add_provider(RaisingProvider("first", frozenset({"matches_host", "parse_pr_url"})))
    _add_provider(RaisingProvider("second", frozenset({"matches_host"})))

    with caplog.at_level(logging.DEBUG, logger=REGISTRY_LOGGER):
        # No provider parses this URL, so each descriptor is asked for its claim and its parse.
        assert resolve_pr_url("https://git.example.test/not-a-pull-request") is None
        assert resolve_pr_url("https://git.example.test/not-a-pull-request") is None

    levels = {
        (name, method): [
            record.levelno
            for record in _registry_records(caplog)
            if f"Git provider {name} failed in {method};" in record.getMessage()
        ]
        for name, method in [
            ("first", "matches_host"),
            ("first", "parse_pr_url"),
            ("second", "matches_host"),
        ]
    }
    assert levels == {
        ("first", "matches_host"): [logging.WARNING, logging.DEBUG],
        ("first", "parse_pr_url"): [logging.DEBUG, logging.DEBUG],
        ("second", "matches_host"): [logging.WARNING, logging.DEBUG],
    }


def test_reset_for_tests_lets_the_next_failure_warn_again(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG, logger=REGISTRY_LOGGER):
        for _ in range(2):
            _add_provider(RaisingProvider("raising", frozenset({"matches_host"})))
            resolve_pr_url(GITHUB_SHAPED_PR)
            reset_for_tests()

    assert [record.levelno for record in _registry_records(caplog)] == [
        logging.WARNING,
        logging.WARNING,
    ]
