"""The GitHub policy gates every git remote, including ones another provider claims.

A provider can declare a policy facet, but the GitHub policy never hands a command over to
it. The URL text does not prove where git pushes (``url.<base>.insteadOf`` can redirect it),
and that provider's policy might not be enabled for the agent. The ``ASK`` and ``DENY``
values pinned here are the decisions the policy made before the provider layer existed.
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
    resolve_remote,
)
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


def _register_forge(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register ``Forge`` with a policy facet module that exports ``POLICY``."""
    module = types.ModuleType(_FORGE_POLICY_MODULE)
    module.POLICY = "gp_test_forge_policy.forge_policy"
    monkeypatch.setitem(sys.modules, _FORGE_POLICY_MODULE, module)
    register_provider(Forge(FacetModules(policy=_FORGE_POLICY_MODULE)))


def test_github_declares_its_policy_facet() -> None:
    assert POLICY == "omnigent.policies.builtins.github.github_policy"
    assert load_facet("github", "policy") == POLICY
    load_registry()
    assert POLICY in {entry.handler for entry in get_registry()}


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        pytest.param(f"git push {_FORGE_URL} +main", _DENY_FORCE, id="force-refspec"),
        pytest.param(f"git push {_FORGE_URL} main", _ASK_PUSH, id="push"),
        pytest.param(f"git clone {_FORGE_URL}", _ask_read("clone"), id="clone"),
        pytest.param(f"git fetch {_FORGE_URL}", _ask_read("fetch"), id="fetch"),
    ],
)
def test_a_provider_with_a_policy_facet_does_not_change_the_github_decision(
    monkeypatch: pytest.MonkeyPatch, command: str, expected: PolicyResponse
) -> None:
    policy = _strict_policy()
    before = policy(_sh(command))

    _register_forge(monkeypatch)
    claimed = resolve_remote(_FORGE_URL)

    assert claimed is not None and claimed.provider == "forge"
    assert load_facet("forge", "policy") is not None
    assert before == expected
    assert policy(_sh(command)) == before
