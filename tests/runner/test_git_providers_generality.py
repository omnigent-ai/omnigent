"""A git provider defined outside omnigent drives the pull request panel and observer end to end.

The fake ``gitlab`` descriptor is registered at run time and its facet module is
injected into ``sys.modules``, so nothing in omnigent names it.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import types
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

import omnigent.git_providers as provider_registry
from omnigent.git_providers import (
    FacetModules,
    Instances,
    ParsedPullRequest,
    ParsedRemote,
    host_of,
    reset_for_tests,
)
from omnigent.runner import pr_resource
from omnigent.runner.git_providers import (
    FILE_DIFF_OBJECT,
    INFO_OBJECT,
    PR_DIFF_OBJECT,
    ProviderCapabilities,
    PullRequestAuth,
    ShellPrOp,
    ShellSegment,
)
from omnigent.runner.git_providers.tool_output import pr_reference, result_objects
from omnigent.runner.pr_observer import extract_prs
from omnigent.runner.session_prs import PullRequestRef, SessionPrRegistry
from tests.runner.git_provider_fixtures import register_provider

HOST = "git.example.test"
ORIGIN = f"https://{HOST}/g/s/p.git"
MR = f"https://{HOST}/g/s/p/-/merge_requests/7"
OTHER_MR = f"https://{HOST}/g/s/p/-/merge_requests/8"
INACCESSIBLE_MR = f"https://{HOST}/g/s/p/-/merge_requests/9"
_FACET_MODULE = "tests_git_providers_fake_gitlab_facet"
# The shell commands the fake recognizes: one write and one read.
_GLAB_CREATE = ("glab", "mr", "create")
_GLAB_VIEW = ("glab", "mr", "view")
_MCP_CREATE = "mcp__gitlab__create_merge_request"


class FakeGitLab:
    """Claims ``git.example.test`` and parses its merge request URLs."""

    id = "gitlab"
    display_name = "GitLab"
    request_name = "merge request"
    number_prefix = "!"
    default_hosts = (HOST,)
    facets = FacetModules(pull_requests=_FACET_MODULE)

    def matches_host(self, host: str, instances: Instances) -> bool:
        return host == HOST

    def parse_remote_url(self, url: str, instances: Instances) -> ParsedRemote | None:
        if host_of(url) != HOST:
            return None
        return ParsedRemote(self.id, HOST, urlsplit(url).path.strip("/").removesuffix(".git"))

    def parse_pr_url(self, url: str, instances: Instances) -> ParsedPullRequest | None:
        pattern = rf"https://{re.escape(HOST)}/(.+)/-/merge_requests/([1-9][0-9]*)"
        match = re.fullmatch(pattern, url.strip())
        if match is None:
            return None
        return ParsedPullRequest(self.id, HOST, match[1], int(match[2]), match[0])


class FakeGitLabFacet:
    """Records its calls; offers neither account nor base remote choices."""

    capabilities = ProviderCapabilities(
        account_switching=False,
        base_remote_selection=False,
        line_counts=False,
        linked_pr_diff=False,
    )

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.branch_pr: str | None = None

    def _info(self, **fields: Any) -> dict[str, Any]:
        auth: PullRequestAuth = {
            "authenticated": True,
            "hint": "Run glab auth login on the host.",
            "cli": None,
            "accounts": None,
            "selected_account": None,
        }
        return {
            "object": INFO_OBJECT,
            "available": True,
            "provider": "gitlab",
            "auth": auth,
            "capabilities": self.capabilities.to_json(),
            "branch": "feature",
            "base_ref": "main",
            "repo": {"name_with_owner": "g/s/p"},
            **fields,
        }

    def workspace_info(self, root: str) -> dict[str, Any]:
        self.calls.append("workspace_info")
        pr = {"url": self.branch_pr, "title": "Branch MR"} if self.branch_pr else None
        return self._info(pr=pr)

    def reference_info(self, root: str, reference: PullRequestRef) -> dict[str, Any]:
        self.calls.append("reference_info")
        pr = {
            "number": reference.number,
            "url": reference.url,
            "title": f" MR {reference.number} ",
        }
        return self._info(selected_pr_url=reference.url, pr=pr)

    def titles_available(self, root: str) -> bool:
        return True

    def pr_title(
        self, root: str, reference: PullRequestRef, deadline: float
    ) -> tuple[str | None, bool]:
        self.calls.append(f"pr_title:{reference.number}")
        return f"MR {reference.number}", False

    def verify_accessible(self, root: str, reference: PullRequestRef) -> None:
        self.calls.append("verify_accessible")
        if reference.number == 9:
            raise ValueError("Cannot read this merge request with glab on the host")

    def on_inferred_pr(self, root: str, reference: PullRequestRef) -> None:
        self.calls.append("on_inferred_pr")

    def changed_files(self, root: str, reference: PullRequestRef | None) -> dict[str, Any]:
        self.calls.append("changed_files")
        return {"object": "list", "data": [], "has_more": False}

    def pr_diff(self, root: str, reference: PullRequestRef | None) -> dict[str, Any]:
        self.calls.append("pr_diff")
        return {
            "object": PR_DIFF_OBJECT,
            "patch": "",
            "unavailable_reason": "pr_outside_workspace",
        }

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
        self.calls.append("file_diff")
        return {"object": FILE_DIFF_OBJECT, "path": path, "before": None, "after": "new"}

    def set_preference(
        self,
        root: str,
        reference: PullRequestRef | None,
        *,
        account: str | None,
        remote: str | None,
    ) -> None:
        self.calls.append("set_preference")

    def shell_pr_operations(self, segments: Sequence[ShellSegment]) -> list[ShellPrOp]:
        ops = []
        for segment in segments:
            command = segment.invocation_tokens[:3]
            if command in {_GLAB_CREATE, _GLAB_VIEW}:
                creates = command == _GLAB_CREATE
                ops.append(
                    ShellPrOp(tracks=creates, creates=creates, target=None, content_only=False)
                )
        return ops

    def pr_from_object(self, obj: Mapping[str, object]) -> PullRequestRef | None:
        # glab prints a merge request's URL as ``web_url``, not a generic URL field.
        return pr_reference(obj.get("web_url"))

    def mcp_prs(
        self, tool_name: str, arguments: dict[str, object], result: object
    ) -> tuple[list[PullRequestRef], bool] | None:
        if tool_name != _MCP_CREATE:
            return None
        return [ref for obj in result_objects(result) if (ref := self.pr_from_object(obj))], True


@pytest.fixture
def facet(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[FakeGitLabFacet]:
    """Register the fake provider and its facet module; forget both afterwards."""
    monkeypatch.setattr(provider_registry.importlib.metadata, "entry_points", lambda **_: ())
    monkeypatch.delenv("GH_HOST", raising=False)
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "gh"))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    fake = FakeGitLabFacet()
    module = types.ModuleType(_FACET_MODULE)
    module.PULL_REQUESTS = fake  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, _FACET_MODULE, module)
    reset_for_tests()
    register_provider(FakeGitLab())
    yield fake
    reset_for_tests()


@pytest.fixture
def repo(tmp_path: Path) -> str:
    """A git checkout whose ``origin`` is on the fake provider's host."""
    path = tmp_path / "repo"
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "remote", "add", "origin", ORIGIN], cwd=path, check=True)
    return str(path)


def test_pr_urls_resolve_to_the_fake_provider(facet: FakeGitLabFacet) -> None:
    reference = PullRequestRef.from_url(MR)

    assert (reference.provider, reference.host, reference.repository, reference.number) == (
        "gitlab",
        HOST,
        "g/s/p",
        7,
    )


def test_pr_info_runs_the_fake_workspace_info(facet: FakeGitLabFacet, repo: str) -> None:
    info = pr_resource.pr_info(repo)

    assert facet.calls == ["workspace_info"]
    assert info["provider"] == "gitlab"
    assert info["provider_display"]["number_prefix"] == "!"
    assert info["provider_display"]["request_name"] == "merge request"
    assert info["auth"]["hint"] == "Run glab auth login on the host."
    assert info["capabilities"] == facet.capabilities.to_json()
    assert "gh_available" not in info


def test_branch_inference_records_the_pr_and_calls_the_hook(
    facet: FakeGitLabFacet, repo: str
) -> None:
    facet.branch_pr = MR

    info = pr_resource.pr_info(repo, session_id="session")

    assert facet.calls == ["workspace_info", "on_inferred_pr"]
    assert info["selected_pr_url"] == MR
    assert [(pr["url"], pr["title"]) for pr in info["prs"]] == [(MR, "Branch MR")]
    assert SessionPrRegistry("session").list()[0].relationship == "inferred"


def test_attach_verifies_through_the_fake_facet(facet: FakeGitLabFacet, repo: str) -> None:
    info = pr_resource.update_session_pr(repo, "session", MR, "attach")

    assert facet.calls[:2] == ["verify_accessible", "reference_info"]
    assert (info["provider"], info["selected_pr_url"]) == ("gitlab", MR)
    assert [(pr["provider"], pr["relationship"]) for pr in info["prs"]] == [("gitlab", "attached")]
    assert info["prs"][0]["provider_display"] == info["provider_display"]
    with pytest.raises(ValueError, match="glab on the host"):
        pr_resource.update_session_pr(repo, "session", INACCESSIBLE_MR, "attach")
    assert [entry.url for entry in SessionPrRegistry("session").list()] == [MR]


def test_unselected_titles_come_from_the_fake_facet(facet: FakeGitLabFacet, repo: str) -> None:
    SessionPrRegistry("session").record(
        [PullRequestRef.from_url(MR), PullRequestRef.from_url(OTHER_MR)],
        relationship="created",
        source="test",
    )

    info = pr_resource.pr_info(repo, session_id="session", pr_url=MR)

    assert facet.calls == ["reference_info", "pr_title:8"]
    assert {pr["url"]: pr["title"] for pr in info["prs"]} == {MR: "MR 7", OTHER_MR: "MR 8"}


def test_reads_go_to_the_fake_facet(facet: FakeGitLabFacet, repo: str) -> None:
    pr_resource.update_session_pr(repo, "session", MR, "attach")
    facet.calls.clear()

    assert pr_resource.pr_changed_files(repo, session_id="session")["data"] == []
    diff = pr_resource.pr_diff(repo, session_id="session")
    contents = pr_resource.pr_file_diff(repo, "", "a.py", session_id="session", pr_url=MR)

    assert facet.calls == ["changed_files", "pr_diff", "file_diff"]
    assert diff["unavailable_reason"] == "pr_outside_workspace"
    assert contents["after"] == "new"


def test_preferences_skip_a_facet_without_the_capabilities(
    facet: FakeGitLabFacet, repo: str
) -> None:
    pr_resource.update_session_pr(repo, "session", MR, "attach")

    by_pr = pr_resource.set_pr_preference(
        repo, account="bob", remote="upstream", session_id="session", pr_url=MR
    )
    by_workspace = pr_resource.set_pr_preference(
        repo, account="", remote="upstream", session_id="session"
    )

    assert "set_preference" not in facet.calls
    assert (by_pr["provider"], by_workspace["provider"]) == ("gitlab", "gitlab")


def test_a_shell_create_records_the_mr_that_its_json_output_names(
    facet: FakeGitLabFacet,
) -> None:
    refs, created = extract_prs(
        "Bash",
        {"command": "cd /repo && glab mr create --fill --yes"},
        {"stdout": json.dumps({"iid": 7, "web_url": MR}), "exit_code": 0},
    )

    assert [(ref.provider, ref.url) for ref in refs] == [("gitlab", MR)]
    assert created


@pytest.mark.parametrize(
    "command,created",
    [("glab mr view 8", False), ("glab mr view 8; glab mr create --fill", True)],
)
def test_a_read_keeps_shared_output_from_naming_the_mr(
    facet: FakeGitLabFacet, command: str, created: bool
) -> None:
    result = {"stdout": json.dumps({"iid": 8, "web_url": OTHER_MR})}

    assert extract_prs("Bash", {"command": command}, result) == ([], created)


def test_the_fake_answers_only_for_its_mcp_tool(facet: FakeGitLabFacet) -> None:
    result = {"structuredContent": {"iid": 7, "web_url": MR}}

    refs, created = extract_prs(_MCP_CREATE, {"project": "g/s/p"}, result)

    assert ([ref.url for ref in refs], created) == ([MR], True)
    assert extract_prs("mcp__gitlab__list_merge_requests", {}, result) == ([], False)


def test_legacy_resources_route_a_mixed_provider_session(
    facet: FakeGitLabFacet, repo: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.runner import github_resource as github

    github_url = "https://github.com/example/project/pull/42"
    registry = SessionPrRegistry("mixed-session")
    for timestamp, url in enumerate([github_url, MR], start=1):
        registry.record(
            [PullRequestRef.from_url(url)],
            relationship="created",
            source="test",
            timestamp=timestamp,
        )
    monkeypatch.setattr(github.shutil, "which", lambda _: "/bin/gh")
    monkeypatch.setattr(github._config, "github_account_preference", lambda _: None)
    monkeypatch.setattr(github, "_list_accounts", lambda _: (True, []))
    calls = []

    def gh(args: list[str], **_kwargs) -> tuple[int, str, str]:
        calls.append(args)
        assert HOST not in " ".join(args)
        if args[:2] == ["pr", "view"]:
            assert args[args.index("-R") + 1] == "github.com/example/project"
            return 0, json.dumps({"number": 42, "title": "GitHub PR", "state": "OPEN"}), ""
        if args[:2] == ["pr", "diff"]:
            assert args[-1] == "github.com/example/project"
            return 0, "github patch", ""
        assert args[-1].startswith("repos/example/project/pulls/42/files")
        return 0, json.dumps([[{"filename": "github.py", "status": "modified"}]]), ""

    monkeypatch.setattr(github, "_gh", gh)
    info = github.github_info(repo, session_id="mixed-session")
    assert (info["provider"], info["selected_pr_url"]) == ("gitlab", MR)
    assert {entry["url"]: entry["title"] for entry in info["prs"]} == {
        github_url: "GitHub PR",
        MR: "MR 7",
    }
    info = github.github_info(repo, session_id="mixed-session", pr_url=github_url)
    assert (info["provider"], info["selected_pr_url"]) == ("github", github_url)
    assert {entry["url"] for entry in info["prs"]} == {github_url, MR}

    calls.clear()
    facet.calls.clear()
    assert github.github_changed_files(repo, session_id="mixed-session")["data"] == []
    assert (
        github.github_pr_diff(repo, session_id="mixed-session")["unavailable_reason"]
        == "pr_outside_workspace"
    )
    assert (
        github.github_file_diff(repo, "", "a.py", session_id="mixed-session", pr_url=MR)["after"]
        == "new"
    )
    assert facet.calls == ["changed_files", "pr_diff", "file_diff"]
    assert calls == []

    assert (
        github.github_changed_files(repo, session_id="mixed-session", pr_url=github_url)["data"][
            0
        ]["path"]
        == "github.py"
    )
    assert (
        github.github_pr_diff(repo, session_id="mixed-session", pr_url=github_url)["patch"]
        == "github patch"
    )
    assert len(calls) == 2
    info = github.update_session_pr(repo, "mixed-session", MR, "remove")
    assert (info["provider"], info["selected_pr_url"]) == ("github", github_url)
    assert [entry["url"] for entry in info["prs"]] == [github_url]
    assert [entry.url for entry in registry.list()] == [github_url]


@pytest.mark.parametrize("method", ["load_facet", "titles_available", "pr_title"])
def test_optional_title_failure_keeps_selected_pr_and_healthy_titles(
    facet: FakeGitLabFacet, repo: str, monkeypatch: pytest.MonkeyPatch, method: str
) -> None:
    selected = "https://github.com/example/project/pull/42"
    healthy = "https://github.com/example/project/pull/43"
    registry = SessionPrRegistry("mixed-session")
    registry.record(
        [PullRequestRef.from_url(url) for url in [selected, healthy, MR]],
        relationship="created",
        source="test",
    )
    registry.update_titles({MR: "Last known MR title"}, timestamp=1)
    before = next(entry for entry in registry.list() if entry.url == MR)
    github = provider_registry.load_facet("github", "pull_requests")
    monkeypatch.setattr(
        github,
        "reference_info",
        lambda _root, ref: {
            "provider": "github",
            "selected_pr_url": ref.url,
            "pr": {"title": "Selected GitHub PR"},
        },
    )
    monkeypatch.setattr(github, "titles_available", lambda _root: True)
    monkeypatch.setattr(github, "pr_title", lambda *_args: ("Healthy GitHub title", False))

    def broken(*_args):
        raise RuntimeError("External forge unavailable")

    if method == "load_facet":
        original = pr_resource._facet

        def load(provider_id):
            if provider_id == "gitlab":
                broken()
            return original(provider_id)

        monkeypatch.setattr(pr_resource, "_facet", load)
    else:
        monkeypatch.setattr(facet, method, broken)
    info = pr_resource.pr_info(repo, session_id="mixed-session", pr_url=selected)

    assert (info["provider"], info["selected_pr_url"]) == ("github", selected)
    assert {entry["url"]: entry["title"] for entry in info["prs"]} == {
        selected: "Selected GitHub PR",
        healthy: "Healthy GitHub title",
        MR: "Last known MR title",
    }
    assert next(entry for entry in registry.list() if entry.url == MR) == before
