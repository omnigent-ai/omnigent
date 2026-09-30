"""Git-remote attribution for policies, and the GitHub policy's hand-off to other providers.

``classify_remote`` names the provider behind a remote URL. The GitHub policy leaves a
flagless git command to another provider's policy only when that provider claims the remote
and declares a policy facet. Azure DevOps declares none, so every decision for its remotes
is the one the policy made before the provider layer existed; the ``ASK`` and ``DENY``
values pinned here come from that policy.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from omnigent.git_providers import (
    FacetModules,
    Instances,
    ParsedPullRequest,
    ParsedRemote,
    host_of,
    load_facet,
    register_provider,
    reset_for_tests,
)
from omnigent.policies.builtins._git_remote import classify_remote, other_policy_owns_remote
from omnigent.policies.builtins.github import POLICY, github_policy
from omnigent.policies.registry import get_registry, load_registry
from omnigent.policies.schema import PolicyEvent, PolicyResponse
from tests.policies.builtins.helpers import tool_call_event as tc

_REPO = "octo/hello"
_FORGE_URL = "https://git.example.test/o/r.git"
_FORGE_POLICY_MODULE = "gp_test_forge_policy"

# Exact decisions of the GitHub policy, as it made them before the provider layer existed.
_ASK_PUSH: PolicyResponse = {
    "result": "ASK",
    "reason": (
        "The target repo of `git push` could not be determined (e.g. a local remote alias). "
        "Approve this write?"
    ),
}
_DENY_FORCE: PolicyResponse = {
    "result": "DENY",
    "reason": (
        "Force push is blocked by policy. Remove the force flag (--force / -f / "
        "--force-with-lease / --force-if-includes / +refspec) or set deny_force_push=False."
    ),
}
_DENY_SECRET: PolicyResponse = {
    "result": "DENY",
    "reason": (
        "GitHub operation blocked by policy. Write is restricted to the configured repos; "
        "this call targets ['octo/secret']."
    ),
}
_DENY_SECRET_READ: PolicyResponse = {
    "result": "DENY",
    "reason": (
        "GitHub operation blocked by policy. Read is restricted to the configured repos; "
        "this call targets ['octo/secret']."
    ),
}


def _ask_read(sub: str) -> PolicyResponse:
    """The ``ASK`` for a read whose repo the policy cannot determine under restricted reads."""
    return {
        "result": "ASK",
        "reason": (
            "Reads are restricted to the configured repos, but the target repo of "
            f"`git {sub}` could not be determined. Approve?"
        ),
    }


def _sh(command: str) -> PolicyEvent:
    """Build a ``sys_os_shell`` ``tool_call`` event carrying *command*."""
    return tc("sys_os_shell", {"command": command})


def _strict_policy() -> Callable[[PolicyEvent], PolicyResponse | None]:
    """A policy that restricts reads, writes, and branches, so an unknown remote is an ASK."""
    return github_policy(
        read_all=False, read_repos=[_REPO], write_repos=[_REPO], write_branches=["main"]
    )


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Hide ambient provider configuration and start from the built-in providers."""
    for name in (
        "OMNIGENT_GIT_PROVIDER_MODULES",
        "OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS",
        "GH_HOST",
        "XDG_CONFIG_HOME",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "gh"))
    reset_for_tests()
    yield
    reset_for_tests()


@dataclass(frozen=True)
class Forge:
    """A provider that claims ``git.example.test`` and names its policy facet module."""

    facets: FacetModules
    id: str = "forge"
    display_name: str = "Forge"
    default_hosts: tuple[str, ...] = ("git.example.test",)

    def matches_host(self, host: str, instances: Instances) -> bool:
        return host.lower() in self.default_hosts

    def parse_remote_url(self, url: str, instances: Instances) -> ParsedRemote | None:
        host = host_of(url)
        if host is None or not self.matches_host(host, instances):
            return None
        return ParsedRemote(provider=self.id, host=host, repository="o/r")

    def parse_pr_url(self, url: str, instances: Instances) -> ParsedPullRequest | None:
        return None


def _register_forge(monkeypatch: pytest.MonkeyPatch, *, policy_facet: bool = True) -> None:
    """Register ``Forge``; its facet module exports ``POLICY`` when *policy_facet* is set."""
    if policy_facet:
        module = types.ModuleType(_FORGE_POLICY_MODULE)
        module.POLICY = "gp_test_forge_policy.forge_policy"
        monkeypatch.setitem(sys.modules, _FORGE_POLICY_MODULE, module)
    register_provider(Forge(FacetModules(policy=_FORGE_POLICY_MODULE if policy_facet else None)))


# ── classify_remote ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/octo/hello", ("github", "octo/hello")),
        ("https://github.com/octo/hello.git", ("github", "octo/hello")),
        ("git@github.com:octo/hello.git", ("github", "octo/hello")),
        ("ssh://git@github.com/octo/hello.git", ("github", "octo/hello")),
        ("https://dev.azure.com/o/p/_git/r", ("azure_devops", "o/p/r")),
        ("https://o@dev.azure.com/o/p/_git/r", ("azure_devops", "o/p/r")),
        ("https://o.visualstudio.com/p/_git/r", ("azure_devops", "o/p/r")),
        ("git@ssh.dev.azure.com:v3/o/p/r", ("azure_devops", "o/p/r")),
        ("ssh://git@ssh.dev.azure.com/v3/o/p/r", ("azure_devops", "o/p/r")),
    ],
)
def test_classify_remote_names_the_provider_and_repository(
    url: str, expected: tuple[str, str]
) -> None:
    assert classify_remote(url) == expected


@pytest.mark.parametrize("name", ["GH_HOST", "OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS"])
def test_classify_remote_reads_configured_github_enterprise_hosts(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    assert classify_remote("https://ghe.example.test/octo/hello.git") is None

    monkeypatch.setenv(name, "ghe.example.test")

    assert classify_remote("https://ghe.example.test/octo/hello.git") == ("github", "octo/hello")
    assert classify_remote("git@ghe.example.test:octo/hello.git") == ("github", "octo/hello")


@pytest.mark.parametrize(
    "url",
    [
        "git@gitlab.com:g/p.git",
        "https://gitlab.com/g/p.git",
        "https://ghe.example.test/octo/hello.git",
        "https://notgithub.com/octo/hello",
        "https://dev.azure.com.evil.example/o/p/_git/r",
        "https://evil-dev.azure.com/o/p/_git/r",
    ],
)
def test_classify_remote_is_none_for_hosts_no_provider_claims(url: str) -> None:
    assert classify_remote(url) is None


@pytest.mark.parametrize(
    "url",
    [
        "",
        "origin",
        "not a url",
        "::::",
        "https://",
        "git@",
        "https://github.com/octo",
        "https://dev.azure.com/o",
        "/srv/git/r.git",
        "../r",
        "file:///srv/git/r.git",
        "ext::sh -c id",
    ],
)
def test_classify_remote_is_none_for_text_that_is_not_a_remote(url: str) -> None:
    assert classify_remote(url) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example\\@dev.azure.com/o/p/_git/r",
        "https://evil.example\\@github.com/octo/hello",
    ],
)
def test_classify_remote_is_none_for_a_url_with_a_backslash(url: str) -> None:
    assert classify_remote(url) is None


# ── other_policy_owns_remote and the policy facet ───────────────────────────


def test_github_declares_its_policy_facet() -> None:
    assert POLICY == "omnigent.policies.builtins.github.github_policy"
    assert load_facet("github", "policy") == POLICY
    load_registry()
    assert POLICY in {entry.handler for entry in get_registry()}


def test_azure_devops_declares_no_policy_facet() -> None:
    assert load_facet("azure_devops", "policy") is None


def test_other_policy_owns_remote_needs_a_claiming_provider_with_a_policy_facet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert not other_policy_owns_remote(_FORGE_URL, "github")

    _register_forge(monkeypatch)

    assert other_policy_owns_remote(_FORGE_URL, "github")
    # The asking provider's own remotes, and another provider's, are told apart.
    assert not other_policy_owns_remote(_FORGE_URL, "forge")
    assert other_policy_owns_remote("https://github.com/octo/hello", "forge")
    # No claiming provider, or a claiming provider without a policy facet.
    assert not other_policy_owns_remote("git@gitlab.com:g/p.git", "github")
    assert not other_policy_owns_remote("origin", "github")
    assert not other_policy_owns_remote("https://dev.azure.com/o/p/_git/r", "github")


def test_other_policy_owns_remote_ignores_a_facet_module_that_exports_no_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, _FORGE_POLICY_MODULE, types.ModuleType(_FORGE_POLICY_MODULE))
    register_provider(Forge(FacetModules(policy=_FORGE_POLICY_MODULE)))

    assert not other_policy_owns_remote(_FORGE_URL, "github")


def test_other_policy_owns_remote_ignores_a_missing_facet_module() -> None:
    register_provider(Forge(FacetModules(policy="gp_test_no_such_policy_module")))

    assert not other_policy_owns_remote(_FORGE_URL, "github")


# ── GitHub policy: real providers keep today's decisions ────────────────────


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        pytest.param("git push https://dev.azure.com/o/p/_git/r main", _ASK_PUSH, id="ado-https"),
        pytest.param(
            "git push https://dev.azure.com/o/p/_git/r +main", _DENY_FORCE, id="ado-force-refspec"
        ),
        pytest.param("git push git@ssh.dev.azure.com:v3/o/p/r main", _ASK_PUSH, id="ado-ssh"),
        pytest.param("git push https://github.com/octo/hello main", None, id="github-allowed"),
        pytest.param(
            "git push https://github.com/octo/secret main", _DENY_SECRET, id="github-denied"
        ),
        pytest.param("git push git@gitlab.com:g/p.git main", _ASK_PUSH, id="gitlab"),
        pytest.param("git push origin main", _ASK_PUSH, id="remote-name"),
        pytest.param("git push https://ghe.example.test/octo/hello main", _ASK_PUSH, id="ghe"),
    ],
)
def test_real_providers_keep_todays_decisions(
    command: str, expected: PolicyResponse | None
) -> None:
    policy = github_policy(write_repos=[_REPO])

    assert policy(_sh(command)) == expected


@pytest.mark.parametrize("name", ["GH_HOST", "OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS"])
def test_a_configured_github_enterprise_remote_keeps_todays_ask(
    monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    monkeypatch.setenv(name, "ghe.example.test")
    policy = github_policy(write_repos=[_REPO])

    assert policy(_sh("git push https://ghe.example.test/octo/hello main")) == _ASK_PUSH


def test_azure_devops_reads_keep_todays_asks_under_restricted_reads() -> None:
    policy = github_policy(read_all=False, read_repos=[_REPO], write_repos=[_REPO])

    assert policy(_sh("git fetch git@ssh.dev.azure.com:v3/o/p/r")) == _ask_read("fetch")
    assert policy(_sh("git clone https://dev.azure.com/o/p/_git/r")) == _ask_read("clone")


# ── GitHub policy: a provider with a policy facet owns its remotes ──────────

_FLAGLESS_FORGE_COMMANDS = [
    f"git push {_FORGE_URL} main",
    f"git push {_FORGE_URL}",
    f"git push {_FORGE_URL} +main",
    f"git push {_FORGE_URL} :old",
    f"git clone {_FORGE_URL}",
    f"git clone {_FORGE_URL} work",
    f"git fetch {_FORGE_URL}",
    f"git pull {_FORGE_URL} main",
    f"git ls-remote {_FORGE_URL}",
    "git fetch git@git.example.test:o/r.git",
    f"bash -c 'git push {_FORGE_URL} main'",
    f"env CI=1 git clone {_FORGE_URL}",
]


@pytest.mark.parametrize("command", _FLAGLESS_FORGE_COMMANDS)
def test_a_flagless_remote_of_a_provider_with_a_policy_gets_no_github_decision(
    monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    policy = _strict_policy()
    assert policy(_sh(command)) is not None  # the decision before any provider claims the host

    _register_forge(monkeypatch)

    assert policy(_sh(command)) is None


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        pytest.param(f"git push -u {_FORGE_URL} main", _ASK_PUSH, id="push-flag"),
        pytest.param(f"git push --force {_FORGE_URL} main", _DENY_FORCE, id="force-flag"),
        pytest.param(f"git push {_FORGE_URL} main --force", _DENY_FORCE, id="trailing-force"),
        pytest.param(f"git push -o {_FORGE_URL} evil main", _ASK_PUSH, id="flag-value-is-a-url"),
        pytest.param(f"git clone --depth 1 {_FORGE_URL}", _ask_read("clone"), id="clone-flag"),
        pytest.param(
            f"git fetch --multiple {_FORGE_URL} evil", _ask_read("fetch"), id="fetch-multiple"
        ),
        pytest.param(
            f"git fetch {_FORGE_URL} --multiple evil", _ask_read("fetch"), id="trailing-multiple"
        ),
    ],
)
def test_a_command_with_a_flag_keeps_todays_decision(
    monkeypatch: pytest.MonkeyPatch, command: str, expected: PolicyResponse
) -> None:
    _register_forge(monkeypatch)

    assert _strict_policy()(_sh(command)) == expected


def test_a_provider_without_a_policy_facet_keeps_todays_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _register_forge(monkeypatch, policy_facet=False)

    assert github_policy(write_repos=[_REPO])(_sh(f"git push {_FORGE_URL} main")) == _ASK_PUSH


def test_a_github_repo_in_the_command_keeps_the_github_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _register_forge(monkeypatch)
    policy = _strict_policy()

    assert policy(_sh(f"git push {_FORGE_URL} https://github.com/octo/secret")) == _DENY_SECRET
    assert policy(_sh(f"git clone {_FORGE_URL} https://github.com/octo/secret")) == (
        _DENY_SECRET_READ
    )


def test_each_command_in_a_chain_is_gated_on_its_own_remote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _register_forge(monkeypatch)
    policy = github_policy(write_repos=[_REPO])

    denied = f"git push {_FORGE_URL} main && git push https://github.com/octo/secret main"
    asked = f"git push {_FORGE_URL} main; git push origin main"

    assert policy(_sh(denied)) == _DENY_SECRET
    assert policy(_sh(asked)) == _ASK_PUSH


def test_github_and_unclaimed_remotes_keep_their_decisions_beside_a_provider_with_a_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _register_forge(monkeypatch)
    monkeypatch.setenv("GH_HOST", "ghe.example.test")
    policy = github_policy(write_repos=[_REPO])

    assert policy(_sh("git push https://github.com/octo/hello main")) is None
    assert policy(_sh("git push https://github.com/octo/secret main")) == _DENY_SECRET
    assert policy(_sh("git push https://ghe.example.test/octo/hello main")) == _ASK_PUSH
    assert policy(_sh("git push git@gitlab.com:g/p.git main")) == _ASK_PUSH
    assert policy(_sh("git push origin main")) == _ASK_PUSH


def test_a_remote_url_with_a_backslash_keeps_todays_decision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _register_forge(monkeypatch)
    policy = github_policy(write_repos=[_REPO])
    # urlsplit reads git.example.test as the host; git or curl may connect to evil.example.
    backslash = "git push 'https://evil.example\\@git.example.test/o/r.git' main"
    plain = "git push https://evil.example@git.example.test/o/r.git main"

    assert policy(_sh(backslash)) == _ASK_PUSH
    assert policy(_sh(plain)) is None
