"""Pull request facet protocol: structural typing, payload helpers, and import isolation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.runner.git_providers import (
    FILE_DIFF_OBJECT,
    INFO_OBJECT,
    PR_DIFF_OBJECT,
    PR_OUTSIDE_WORKSPACE,
    CliStatus,
    ProviderCapabilities,
    PullRequestAuth,
    PullRequestFacet,
    ShellPrOp,
    ShellSegment,
    unsupported_remote_info,
)
from omnigent.runner.session_prs import PullRequestRef
from tests.budgets import budget

# Every member a provider facet must define, panel methods first.
_MEMBERS = (
    "capabilities",
    "workspace_info",
    "reference_info",
    "titles_available",
    "pr_title",
    "verify_accessible",
    "on_inferred_pr",
    "changed_files",
    "pr_diff",
    "file_diff",
    "set_preference",
    "shell_pr_operations",
    "pr_from_object",
    "mcp_prs",
)


class FakeFacet:
    """A forge with one CLI, ``fake``, whose PRs are always readable."""

    capabilities = ProviderCapabilities(
        account_switching=False,
        base_remote_selection=False,
        line_counts=False,
        linked_pr_diff=False,
    )

    def _info(self, **fields: Any) -> dict[str, Any]:
        auth: PullRequestAuth = {
            "authenticated": True,
            "hint": None,
            "cli": {"name": "fake", "available": True},
            "accounts": None,
            "selected_account": None,
        }
        return {
            "object": INFO_OBJECT,
            "available": True,
            "provider": "fake",
            "auth": auth,
            "capabilities": self.capabilities.to_json(),
            **fields,
        }

    def workspace_info(self, root: str) -> dict[str, Any]:
        return self._info(branch="main", base_ref=None, repo=None, pr=None)

    def reference_info(self, root: str, reference: PullRequestRef) -> dict[str, Any]:
        return self._info(selected_pr_url=reference.url, pr={"url": reference.url})

    def titles_available(self, root: str) -> bool:
        return True

    def pr_title(
        self, root: str, reference: PullRequestRef, deadline: float
    ) -> tuple[str | None, bool]:
        return f"PR {reference.number}", False

    def verify_accessible(self, root: str, reference: PullRequestRef) -> None:
        pass

    def on_inferred_pr(self, root: str, reference: PullRequestRef) -> None:
        pass

    def changed_files(self, root: str, reference: PullRequestRef | None) -> dict[str, Any]:
        return {"object": "list", "data": [], "has_more": False}

    def pr_diff(self, root: str, reference: PullRequestRef | None) -> dict[str, Any]:
        return {"object": PR_DIFF_OBJECT, "patch": "", "unavailable_reason": PR_OUTSIDE_WORKSPACE}

    def file_diff(
        self,
        root: str,
        reference: PullRequestRef | None,
        path: str,
        *,
        base: str,
        previous_path: str | None,
        head_sha: str | None,
        base_sha: str | None,
    ) -> dict[str, Any]:
        return {"object": FILE_DIFF_OBJECT, "path": path, "before": None, "after": ""}

    def set_preference(
        self,
        root: str,
        reference: PullRequestRef | None,
        *,
        account: str | None,
        remote: str | None,
    ) -> None:
        raise ValueError("The fake forge has no account or remote choice")

    def shell_pr_operations(self, segments: Sequence[ShellSegment]) -> list[ShellPrOp]:
        return [
            ShellPrOp(
                tracks=segment.invocation_tokens[1:2] == ("create",),
                creates=segment.invocation_tokens[1:2] == ("create",),
                target=None,
                content_only=False,
            )
            for segment in segments
            if segment.invocation_tokens[0] == "fake"
        ]

    def pr_from_object(self, obj: Mapping[str, object]) -> PullRequestRef | None:
        return None

    def mcp_prs(
        self, tool_name: str, arguments: dict[str, object], result: object
    ) -> tuple[list[PullRequestRef], bool] | None:
        return None


def test_a_facet_class_satisfies_the_protocol_structurally() -> None:
    facet = FakeFacet()

    assert isinstance(facet, PullRequestFacet)
    segment = ShellSegment(
        raw_tokens=("X=1", "fake", "create"), invocation_tokens=("fake", "create")
    )
    assert facet.shell_pr_operations([segment]) == [
        ShellPrOp(tracks=True, creates=True, target=None, content_only=False)
    ]


@pytest.mark.parametrize("missing", _MEMBERS)
def test_each_member_is_part_of_the_protocol(missing: str) -> None:
    facet = FakeFacet()
    members = {name: getattr(facet, name) for name in _MEMBERS}

    assert isinstance(SimpleNamespace(**members), PullRequestFacet)
    del members[missing]
    assert not isinstance(SimpleNamespace(**members), PullRequestFacet)


def test_capabilities_serialize_as_the_four_flags() -> None:
    capabilities = ProviderCapabilities(
        account_switching=True,
        base_remote_selection=False,
        line_counts=True,
        linked_pr_diff=False,
    )

    assert capabilities.to_json() == {
        "account_switching": True,
        "base_remote_selection": False,
        "line_counts": True,
        "linked_pr_diff": False,
    }


def test_auth_block_has_the_documented_keys() -> None:
    assert PullRequestAuth.__required_keys__ == {
        "authenticated",
        "hint",
        "cli",
        "accounts",
        "selected_account",
    }
    assert CliStatus.__required_keys__ == {"name", "available"}


def test_unsupported_remote_info_is_a_fresh_unavailable_payload() -> None:
    info = unsupported_remote_info("gitlab.com")

    assert info == {
        "object": "session.github.info",
        "available": False,
        "reason": "unsupported_remote",
        "remote_host": "gitlab.com",
        "provider": None,
        "auth": None,
        "capabilities": None,
        "repo": None,
        "pr": None,
    }
    assert json.loads(json.dumps(info)) == info
    # The orchestrator adds the session's ``prs`` to the payload it returns.
    info["prs"] = []
    assert "prs" not in unsupported_remote_info("gitlab.com")


def test_importing_the_protocol_loads_neither_the_panel_nor_the_observer() -> None:
    """Facet modules import the protocol, so it must not pull in their consumers.

    Runs in a fresh interpreter so modules other tests imported cannot hide an import.
    """
    probe = (
        "import sys\n"
        "import omnigent.runner.git_providers\n"
        "loaded = {'omnigent.runner.github_resource', 'omnigent.runner.pr_observer'}\n"
        "loaded &= set(sys.modules)\n"
        "assert not loaded, sorted(loaded)\n"
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
