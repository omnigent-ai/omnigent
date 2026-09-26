"""GitLab merge-request resource behavior."""

from __future__ import annotations

import json

import pytest

from omnigent.runner import gitlab_resource

_URL = "https://gitlab.example/group/subgroup/project/-/merge_requests/42"


def test_info_returns_structured_glab_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        gitlab_resource.shutil, "which", lambda name: "/usr/bin/glab" if name == "glab" else None
    )
    monkeypatch.setattr(
        gitlab_resource,
        "_git",
        lambda args, *, root: (
            (0, "feature/selected-mr\n", "")
            if args == ["branch", "--show-current"]
            else (1, "", "")
        ),
    )
    calls: list[list[str]] = []

    def fake_run(argv: list[str], *, root: str):
        calls.append(argv)
        return 0, json.dumps({"iid": 42, "web_url": _URL, "title": "MR"}), ""

    monkeypatch.setattr(gitlab_resource, "_run", fake_run)
    result = gitlab_resource.gitlab_info("/workspace", pr_url=_URL)

    assert result["object"] == "session.gitlab.info"
    assert result["available"] is True
    assert result["branch"] == "feature/selected-mr"
    assert result["selected_mr_url"] == _URL
    assert result["merge_request"]["iid"] == 42
    assert calls == [
        [
            "glab",
            "mr",
            "view",
            "42",
            "--output",
            "json",
            "-R",
            "gitlab.example/group/subgroup/project",
        ]
    ]


def test_diff_returns_empty_when_glab_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gitlab_resource.shutil, "which", lambda name: None)
    assert gitlab_resource.gitlab_mr_diff("/workspace", pr_url=_URL) == {
        "object": "session.gitlab.mr_diff",
        "patch": "",
    }


def test_resource_rejects_github_url() -> None:
    with pytest.raises(ValueError, match="GitLab merge request"):
        gitlab_resource.gitlab_info("/workspace", pr_url="https://github.com/example/repo/pull/42")


def test_info_reports_non_git_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gitlab_resource, "_git", lambda _args, *, root: (128, "", "not git"))

    result = gitlab_resource.gitlab_info("/workspace")

    assert result == {
        "object": "session.gitlab.info",
        "available": False,
        "reason": "not_a_git_repo",
    }


def test_info_discovers_nested_gitlab_remote_without_glab(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_git(args: list[str], *, root: str):
        del root
        responses = {
            ("rev-parse", "--show-toplevel"): (0, "/workspace\n", ""),
            ("branch", "--show-current"): (0, "feature\n", ""),
            ("config", "branch.feature.remote"): (1, "", ""),
            ("remote",): (0, "origin\n", ""),
            ("remote", "get-url", "origin"): (
                0,
                "git@gitlab.example:group/subgroup/project.git\n",
                "",
            ),
        }
        return responses[tuple(args)]

    monkeypatch.setattr(gitlab_resource, "_git", fake_git)
    monkeypatch.setattr(gitlab_resource.shutil, "which", lambda _name: None)

    result = gitlab_resource.gitlab_info("/workspace")

    assert result["available"] is True
    assert result["branch"] == "feature"
    assert result["glab_available"] is False
    assert result["repo"] == {
        "host": "gitlab.example",
        "path_with_namespace": "group/subgroup/project",
        "remote_url": "git@gitlab.example:group/subgroup/project.git",
    }


def test_info_resolves_current_branch_merge_request(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_git(args: list[str], *, root: str):
        del root
        responses = {
            ("rev-parse", "--show-toplevel"): (0, "/workspace\n", ""),
            ("branch", "--show-current"): (0, "feature\n", ""),
            ("config", "branch.feature.remote"): (1, "", ""),
            ("remote",): (0, "origin\n", ""),
            ("remote", "get-url", "origin"): (
                0,
                "https://gitlab.example/group/subgroup/project.git\n",
                "",
            ),
        }
        return responses[tuple(args)]

    calls: list[list[str]] = []

    def fake_run(argv: list[str], *, root: str):
        del root
        calls.append(argv)
        if argv[:3] == ["glab", "auth", "status"]:
            return 0, "", ""
        return 0, json.dumps({"iid": 42, "web_url": _URL, "title": "MR"}), ""

    monkeypatch.setattr(gitlab_resource, "_git", fake_git)
    monkeypatch.setattr(gitlab_resource, "_run", fake_run)
    monkeypatch.setattr(gitlab_resource.shutil, "which", lambda _name: "/usr/bin/glab")

    result = gitlab_resource.gitlab_info("/workspace")

    assert result["authenticated"] is True
    assert result["selected_mr_url"] == _URL
    assert result["merge_request"]["iid"] == 42
    assert calls[0] == ["glab", "auth", "status", "--hostname", "gitlab.example"]
    assert calls[1] == [
        "glab",
        "mr",
        "view",
        "--output",
        "json",
        "-R",
        "gitlab.example/group/subgroup/project",
    ]


def test_info_discovers_merge_requests_across_multiple_upstreams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    urls = {
        "origin": "https://github.com/example/project.git\n",
        "company": "git@gitlab.example:group/project.git\n",
        "mirror": "https://gitlab.other.example/group/project.git\n",
    }

    def fake_git(args: list[str], *, root: str):
        del root
        if args == ["rev-parse", "--show-toplevel"]:
            return 0, "/workspace\n", ""
        if args == ["branch", "--show-current"]:
            return 0, "feature\n", ""
        if args == ["config", "branch.feature.remote"]:
            return 0, "company\n", ""
        if args == ["remote"]:
            return 0, "origin\ncompany\nmirror\n", ""
        if args[:2] == ["remote", "get-url"]:
            return 0, urls[args[2]], ""
        raise AssertionError(args)

    def fake_run(argv: list[str], *, root: str):
        del root
        if argv[:3] == ["glab", "auth", "status"]:
            return (1, "", "not authenticated") if argv[-1] == "github.com" else (0, "", "")
        repo = argv[-1]
        host = repo.split("/", 1)[0]
        number = 7 if host == "gitlab.example" else 8
        return (
            0,
            json.dumps(
                {
                    "iid": number,
                    "title": f"MR {number}",
                    "web_url": f"https://{host}/group/project/-/merge_requests/{number}",
                }
            ),
            "",
        )

    monkeypatch.setattr(gitlab_resource, "_git", fake_git)
    monkeypatch.setattr(gitlab_resource, "_run", fake_run)
    monkeypatch.setattr(gitlab_resource.shutil, "which", lambda _name: "/usr/bin/glab")

    result = gitlab_resource.gitlab_info("/workspace")

    assert result["authenticated"] is True
    assert result["repo"]["host"] == "gitlab.example"
    assert [mr["iid"] for mr in result["merge_requests"]] == [7, 8]
    assert result["merge_request"]["iid"] == 7
    assert result["selected_mr_url"].endswith("/7")
