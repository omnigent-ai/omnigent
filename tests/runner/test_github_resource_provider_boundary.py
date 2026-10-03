"""The foundation keeps GitHub resources working while saving other provider identities."""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from omnigent import git_providers
from omnigent.runner import github_resource as github
from omnigent.runner.session_prs import PullRequestRef, SessionPrRegistry
from tests.git_providers.test_registry import FakeGitLab

GITHUB = "https://github.com/example/project/pull/42"
GITLAB = "https://git.example.test/g/s/p/-/merge_requests/7"


@pytest.fixture
def mixed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "gh"))
    monkeypatch.setattr(github.shutil, "which", lambda _: "/bin/gh")
    monkeypatch.setattr(github._config, "github_account_preference", lambda _: None)
    monkeypatch.setattr(github, "_list_accounts", lambda _: (True, []))
    git_providers.reset_for_tests()
    git_providers._providers = (*git_providers.providers(), FakeGitLab())
    registry = SessionPrRegistry("mixed-session")
    registry.record(
        [PullRequestRef.from_url(GITHUB)],
        relationship="created",
        source="test",
        timestamp=time.time() - 2,
    )
    registry.record([PullRequestRef.from_url(GITLAB)], relationship="created", source="test")
    assert registry.list()[0].url == GITLAB
    yield str(tmp_path)
    git_providers.reset_for_tests()


def test_latest_foreign_entry_does_not_hide_github_metadata_or_diff(
    mixed: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def gh(args: list[str], **_kwargs) -> tuple[int, str, str]:
        calls.append(args)
        assert "git.example.test" not in " ".join(args)
        if args[:2] == ["pr", "view"]:
            assert args[args.index("-R") + 1] == "github.com/example/project"
            return (
                0,
                json.dumps({"number": 42, "title": "GitHub remains visible", "state": "OPEN"}),
                "",
            )
        if args[:2] == ["pr", "diff"]:
            assert args[-1] == "github.com/example/project"
            return 0, "github patch", ""
        assert args[-1].startswith("repos/example/project/pulls/42/files")
        return 0, json.dumps([[{"filename": "github.py", "status": "modified"}]]), ""

    monkeypatch.setattr(github, "_gh", gh)
    info = github.github_info(mixed, session_id="mixed-session")
    assert info["selected_pr_url"] == GITHUB and info["pr"]["title"] == "GitHub remains visible"
    assert [entry["url"] for entry in info["prs"]] == [GITHUB]
    assert github.github_pr_diff(mixed, session_id="mixed-session")["patch"] == "github patch"
    assert (
        github.github_changed_files(mixed, session_id="mixed-session")["data"][0]["path"]
        == "github.py"
    )
    assert {entry.url for entry in SessionPrRegistry("mixed-session").list()} == {GITHUB, GITLAB}
    assert len(calls) == 3


def test_only_foreign_saved_entries_still_allow_github_branch_inference(
    mixed: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = SessionPrRegistry("foreign-only")
    registry.record([PullRequestRef.from_url(GITLAB)], relationship="created", source="test")
    monkeypatch.setattr(github, "_workspace_key", lambda _: None)
    monkeypatch.setattr(
        github,
        "_workspace_github_info",
        lambda _: {
            "available": True,
            "pr": {"url": GITHUB, "number": 42, "title": "Branch PR"},
        },
    )
    monkeypatch.setattr(github, "_gh", lambda *_a, **_kw: pytest.fail("foreign title lookup"))

    info = github.github_info(mixed, session_id="foreign-only")

    assert info["selected_pr_url"] == GITHUB
    assert [entry["url"] for entry in info["prs"]] == [GITHUB]
    assert {entry.url for entry in registry.list()} == {GITHUB, GITLAB}


@pytest.mark.parametrize("action", ["attach", "remove"])
def test_foreign_mutations_are_rejected_without_changing_saved_associations(
    mixed: str, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    registry = SessionPrRegistry("mixed-session")
    before = registry.path.read_bytes()
    monkeypatch.setattr(github, "_gh", lambda *_a, **_kw: pytest.fail("foreign GitHub request"))

    with pytest.raises(ValueError, match="only GitHub"):
        github.update_session_pr(mixed, "mixed-session", GITLAB, action)

    assert registry.path.read_bytes() == before


@pytest.mark.parametrize("operation", ["info", "changes", "diff", "file", "preference"])
def test_explicit_foreign_reads_do_not_reach_github(
    mixed: str, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    monkeypatch.setattr(github, "_gh", lambda *_a, **_kw: pytest.fail("foreign GitHub request"))
    monkeypatch.setattr(github, "_git", lambda *_a, **_kw: pytest.fail("foreign checkout request"))
    kwargs = {"session_id": "mixed-session", "pr_url": GITLAB}
    with pytest.raises(ValueError, match="only GitHub"):
        if operation == "info":
            github.github_info(mixed, **kwargs)
        elif operation == "changes":
            github.github_changed_files(mixed, **kwargs)
        elif operation == "diff":
            github.github_pr_diff(mixed, **kwargs)
        elif operation == "file":
            github.github_file_diff(mixed, "main", "file.py", **kwargs)
        else:
            github.set_github_preference(mixed, account="example", **kwargs)
