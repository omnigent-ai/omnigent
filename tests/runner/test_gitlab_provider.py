"""GitLab data becomes shared-panel payloads without losing fork/revision identity."""

from __future__ import annotations

import base64
import subprocess
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from omnigent.git_providers import reset_for_tests
from omnigent.runner.git_providers import gitlab as module
from omnigent.runner.gitlab_client import GitLabClient, GitLabError, GitLabTimeoutError
from omnigent.runner.session_prs import PullRequestRef

URL = "https://gitlab.com/team/sub/project/-/merge_requests/7"
HEAD, BASE = "a" * 40, "b" * 40


@pytest.fixture(autouse=True)
def provider(monkeypatch: pytest.MonkeyPatch):
    reset_for_tests()
    monkeypatch.delenv("GITLAB_HOST", raising=False)
    monkeypatch.delenv("GLAB_HOST", raising=False)
    yield
    reset_for_tests()


@pytest.fixture
def root(tmp_path: Path) -> str:
    subprocess.run(
        ["git", "init", "-b", "feature", str(tmp_path)], check=True, capture_output=True
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "remote",
            "add",
            "origin",
            "git@gitlab.com:fork/sub/project.git",
        ],
        check=True,
    )
    return str(tmp_path)


@pytest.fixture
def mr() -> dict:
    return {
        "id": 99999,
        "iid": 7,
        "web_url": URL,
        "title": "Handle nested projects",
        "state": "opened",
        "draft": True,
        "description": "A useful **description**",
        "source_project_id": 20,
        "target_project_id": 10,
        "source_branch": "feature",
        "target_branch": "main",
        "author": {"id": 3, "username": "developer"},
        "diff_refs": {"head_sha": HEAD, "base_sha": BASE, "start_sha": "c" * 40},
        "changes_count": "1",
        "head_pipeline": {
            "id": 55,
            "project_id": 20,
            "status": "running",
            "web_url": "https://gitlab.com/fork/sub/project/-/pipelines/55",
        },
    }


@pytest.fixture
def change() -> dict:
    return {
        "old_path": "src/old.py",
        "new_path": "src/new.py",
        "renamed_file": True,
        "new_file": False,
        "deleted_file": False,
        "diff": "@@ -1 +1 @@\n-old\n+new\n",
    }


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, mr: dict, change: dict) -> Mock:
    api = Mock(spec=GitLabClient)

    def object_response(path: str, **query):
        if path == "projects/fork%2Fsub%2Fproject":
            return {"id": 20, "forked_from_project": {"web_url": mr["web_url"].split("/-/")[0]}}
        if "/repository/files/" in path:
            content = "old\n" if query["ref"] == BASE else "new\n"
            return {"encoding": "base64", "content": base64.b64encode(content.encode()).decode()}
        return mr

    def list_response(path: str, **query):
        if path.endswith("/merge_requests"):
            return ([mr] if "team%2Fsub%2Fproject" in path else []), False
        if path.endswith("/notes"):
            return (
                [
                    {
                        "id": 11,
                        "body": "Please test",
                        "author": {"id": 4, "username": "reviewer"},
                        "created_at": "2026-10-01T00:00:00Z",
                    },
                    {"id": 12, "body": "merged", "system": True},
                ],
                False,
            )
        if path.endswith("/jobs"):
            return (
                [
                    {
                        "id": 1,
                        "name": "unit",
                        "status": "success",
                        "web_url": "https://gitlab.com/job/1",
                    },
                    {"id": 2, "name": "optional", "status": "failed", "allow_failure": True},
                ],
                False,
            )
        if path.endswith("/bridges"):
            return (
                [
                    {
                        "id": 3,
                        "name": "downstream",
                        "status": "running",
                        "downstream_pipeline": {"web_url": "https://gitlab.com/pipeline/3"},
                    }
                ],
                False,
            )
        if path.endswith("/diffs"):
            return [change], False
        raise AssertionError(path)

    api.object.side_effect = object_response
    api.pages.side_effect = list_response
    monkeypatch.setattr(module, "_client", lambda *args, **kwargs: api)
    monkeypatch.setattr(
        module.shutil, "which", lambda name: "/bin/glab" if name == "glab" else None
    )
    return api


def ref() -> PullRequestRef:
    return PullRequestRef.from_url(URL)


def test_workspace_fork_mr_keeps_target_identity_and_details(root: str, api: Mock) -> None:
    info = module.PULL_REQUESTS.workspace_info(root)
    assert info["provider"] == "gitlab" and info["auth"]["authenticated"]
    assert info["branch"] == "feature"
    assert info["repo"]["name_with_owner"] == "team/sub/project"
    assert info["selected_pr_url"] == URL
    pr = info["pr"]
    assert pr["number"] == 7 and pr["state"] == "OPEN" and pr["is_draft"]
    assert (pr["head_sha"], pr["base_sha"]) == (HEAD, BASE)
    assert pr["comments"] == [
        {
            "author": "reviewer",
            "author_id": "4",
            "body": "Please test",
            "created_at": "2026-10-01T00:00:00Z",
            "url": URL + "#note_11",
        }
    ]
    assert (pr["checks"]["passing"], pr["checks"]["pending"], pr["checks"]["total"]) == (2, 1, 3)
    assert info["warnings"] == []
    api.object.assert_any_call("projects/fork%2Fsub%2Fproject")
    api.object.assert_any_call("projects/team%2Fsub%2Fproject/merge_requests/7")


def test_explicit_reference_keeps_live_branch_and_uses_iid(root: str, api: Mock) -> None:
    info = module.PULL_REQUESTS.reference_info(root, ref())
    assert info["branch"] == "feature" and info["pr"]["number"] == 7
    api.object.assert_called_once_with("projects/team%2Fsub%2Fproject/merge_requests/7")


def test_no_oauth_or_cli_failure_hides_checkout(
    root: str, api: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module.shutil, "which", lambda name: None)
    info = module.PULL_REQUESTS.workspace_info(root)
    assert info["available"] and info["branch"] == "feature"
    assert info["auth"]["cli"] == {"name": "glab", "available": False}
    assert not info["auth"]["authenticated"]
    api.object.assert_not_called()


def test_auth_failure_keeps_repo_context(root: str, api: Mock) -> None:
    api.object.side_effect = GitLabError("Access denied")
    info = module.PULL_REQUESTS.reference_info(root, ref())
    assert info["available"] and info["repo"]["name_with_owner"] == ref().repository
    assert info["pr"] is None and not info["auth"]["authenticated"]
    assert info["warnings"] == ["Access denied"]


def test_no_current_mr_is_an_authenticated_empty_state(root: str, api: Mock) -> None:
    api.pages.return_value = ([], False)
    api.pages.side_effect = None
    info = module.PULL_REQUESTS.workspace_info(root)
    assert info["auth"]["authenticated"] and info["pr"] is None and not info["warnings"]
    api.object.assert_called_once_with("projects/fork%2Fsub%2Fproject")
    assert [call.args[0] for call in api.pages.call_args_list] == [
        "projects/fork%2Fsub%2Fproject/merge_requests",
        "projects/team%2Fsub%2Fproject/merge_requests",
    ]


@pytest.fixture(params=["detached", "no_remotes", "foreign_remote"])
def inference_error(root: str, request: pytest.FixtureRequest) -> str:
    if request.param == "detached":
        subprocess.run(
            [
                "git",
                "-C",
                root,
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "core.hooksPath=/dev/null",
                "commit",
                "--allow-empty",
                "-m",
                "Initial commit",
            ],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", root, "checkout", "--detach"], check=True, capture_output=True
        )
        return "Check out a branch"
    args = (
        ["remote", "remove", "origin"]
        if request.param == "no_remotes"
        else ["remote", "set-url", "origin", "https://github.com/example/project.git"]
    )
    subprocess.run(["git", "-C", root, *args], check=True)
    return "Configure a GitLab remote"


def test_failed_inference_preconditions_do_not_claim_authentication_or_call_api(
    root: str, inference_error: str, api: Mock, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = Mock(side_effect=AssertionError("Inference must stop before opening a GitLab client"))
    monkeypatch.setattr(module, "_client", client)
    info = module.PULL_REQUESTS.workspace_info(root)
    assert info["available"] and not info["auth"]["authenticated"] and info["pr"] is None
    assert info["auth"]["hint"].startswith(inference_error)
    assert info["warnings"] == [info["auth"]["hint"]]
    for operation in (module.PULL_REQUESTS.changed_files, module.PULL_REQUESTS.pr_diff):
        with pytest.raises(ValueError, match=inference_error):
            operation(root, None)
    with pytest.raises(ValueError, match=inference_error):
        module.PULL_REQUESTS.file_diff(
            root,
            None,
            "src/new.py",
            base="main",
            previous_path=None,
            head_sha=None,
            base_sha=None,
        )
    client.assert_not_called()
    api.object.assert_not_called()
    api.pages.assert_not_called()


def test_explicit_reference_bypasses_unavailable_workspace_inference(
    root: str, inference_error: str, api: Mock
) -> None:
    info = module.PULL_REQUESTS.reference_info(root, ref())
    assert info["auth"]["authenticated"] and info["pr"]["url"] == URL and not info["warnings"]
    api.object.assert_called_once_with("projects/team%2Fsub%2Fproject/merge_requests/7")
    assert module.PULL_REQUESTS.changed_files(root, ref())["data"][0]["path"] == "src/new.py"
    assert "@@" in module.PULL_REQUESTS.pr_diff(root, ref())["patch"]
    assert file_diff(root)["after"] == "new\n"
    assert not any(call.args[0].endswith("/merge_requests") for call in api.pages.call_args_list)


def test_optional_failures_keep_mr_and_report_partial_data(root: str, api: Mock) -> None:
    api.pages.side_effect = GitLabTimeoutError("expired")
    info = module.PULL_REQUESTS.reference_info(root, ref())
    assert info["auth"]["authenticated"] and info["pr"]["number"] == 7
    assert info["pr"]["comments_partial"] and info["pr"]["checks"]["partial"]
    assert len(info["warnings"]) == 3
    assert info["pr"]["checks"]["runs"][0]["name"] == "Pipeline #55"


def test_changed_files_and_whole_patch(root: str, api: Mock) -> None:
    files = module.PULL_REQUESTS.changed_files(root, ref())
    assert not files["has_more"] and files["warning"] is None
    assert files["data"][0]["previous_path"] == "src/old.py"
    assert (files["data"][0]["lines_added"], files["data"][0]["lines_removed"]) == (1, 1)
    patch = module.PULL_REQUESTS.pr_diff(root, ref())["patch"]
    assert "--- a/src/old.py\n+++ b/src/new.py\n@@ -1 +1 @@\n-old\n+new\n" in patch


def test_partial_changes_never_masquerade_as_complete_diff(root: str, api: Mock, mr: dict) -> None:
    mr["changes_count"] = "1000+"
    files = module.PULL_REQUESTS.changed_files(root, ref())
    assert files["has_more"] and files["warning"]
    result = module.PULL_REQUESTS.pr_diff(root, ref())
    assert not result["patch"] and result["unavailable_reason"] == "incomplete_diff"
    assert "incomplete diff" in result["message"]


def test_gitlab_omitted_patch_is_visible(root: str, api: Mock, change: dict) -> None:
    change["too_large"] = True
    files = module.PULL_REQUESTS.changed_files(root, ref())
    assert files["warning"] and files["data"][0]["lines_added"] is None
    result = module.PULL_REQUESTS.pr_diff(root, ref())
    assert not result["patch"] and result["unavailable_reason"] == "patch_unavailable"
    assert "omitted" in result["message"]


def file_diff(root: str, **overrides) -> dict:
    return module.PULL_REQUESTS.file_diff(
        root,
        ref(),
        "src/new.py",
        **{
            "base": "main",
            "previous_path": "src/old.py",
            "head_sha": HEAD,
            "base_sha": BASE,
            **overrides,
        },
    )


def test_fork_file_diff_reads_both_projects_at_exact_diff_refs(root: str, api: Mock) -> None:
    assert file_diff(root) == {
        "object": "session.github.file_diff",
        "path": "src/new.py",
        "before": "old\n",
        "after": "new\n",
    }
    calls = [(call.args[0], call.kwargs) for call in api.object.call_args_list]
    assert ("projects/10/repository/files/src%2Fold.py", {"ref": BASE}) in calls
    assert ("projects/20/repository/files/src%2Fnew.py", {"ref": HEAD}) in calls


@pytest.mark.parametrize(
    "override", [{"head_sha": "d" * 40}, {"base_sha": "e" * 40}, {"previous_path": "unrelated.py"}]
)
def test_file_context_never_mixes_refreshed_revisions(
    root: str, api: Mock, override: dict
) -> None:
    with pytest.raises(ValueError, match="Refresh"):
        file_diff(root, **override)
    assert not any("/repository/files/" in call.args[0] for call in api.object.call_args_list)


@pytest.mark.parametrize("created,deleted", [(True, False), (False, True)])
def test_only_known_additions_deletions_have_absent_content(
    root: str, api: Mock, change: dict, created: bool, deleted: bool
) -> None:
    change.update(new_file=created, deleted_file=deleted)
    result = file_diff(root)
    assert (result["before"] is None) == created
    assert (result["after"] is None) == deleted


def test_file_access_failure_is_not_reported_as_deleted(root: str, api: Mock, mr: dict) -> None:
    def response(path, **query):
        if "/repository/files/" in path:
            raise GitLabError("Cannot read exact revision")
        return mr

    api.object.side_effect = response
    with pytest.raises(GitLabError, match="exact revision"):
        file_diff(root)


def test_title_timeout_contract(root: str, api: Mock) -> None:
    api.object.side_effect = GitLabTimeoutError()
    assert module.PULL_REQUESTS.pr_title(root, ref(), time.monotonic()) == (None, True)
    api.object.side_effect = GitLabError("denied")
    assert module.PULL_REQUESTS.pr_title(root, ref(), time.monotonic()) == (None, False)


@pytest.mark.parametrize("expired", [True, False], ids=["deadline", "subprocess"])
@pytest.mark.parametrize(
    "operation", ["verify_accessible", "changed_files", "pr_diff", "file_diff"]
)
def test_public_timeouts_are_actionable_and_titles_keep_timeout_signal(
    root: str, monkeypatch: pytest.MonkeyPatch, operation: str, expired: bool
) -> None:
    from omnigent.runner import gitlab_client

    monkeypatch.setattr(gitlab_client, "REQUEST_BUDGET_SECONDS", -1 if expired else 8)
    run = Mock(side_effect=subprocess.TimeoutExpired(["glab"], 8))
    monkeypatch.setattr(gitlab_client.subprocess, "run", run)
    with pytest.raises(ValueError, match=r"GitLab request timed out\. Refresh to retry\."):
        if operation == "file_diff":
            file_diff(root)
        else:
            getattr(module.PULL_REQUESTS, operation)(root, ref())
    deadline = time.monotonic() + (-1 if expired else 8)
    assert module.PULL_REQUESTS.pr_title(root, ref(), deadline) == (None, True)
    assert run.call_count == (0 if expired else 2)


def test_outside_checkout_still_serves_manually_linked_mr(tmp_path: Path, api: Mock) -> None:
    assert not module.PULL_REQUESTS.workspace_info(str(tmp_path))["available"]
    assert module.PULL_REQUESTS.reference_info(str(tmp_path), ref())["pr"]["number"] == 7


def test_upstream_remote_precedence_preserves_other_gitlab_remotes(root: str) -> None:
    for args in (
        ["remote", "add", "upstream", "https://gitlab.com/team/sub/project.git"],
        ["config", "branch.feature.remote", "upstream"],
    ):
        subprocess.run(["git", "-C", root, *args], check=True)
    assert [repo.repository for repo in module._remotes(root)] == [
        "team/sub/project",
        "fork/sub/project",
    ]


@pytest.mark.parametrize(
    "patch",
    [
        "",
        None,
        "Binary files /dev/null and b/assets/sample.bin differ\n",
        "old mode 100644\nnew mode 100755\n",
    ],
)
def test_binary_or_metadata_only_patch_is_not_shown_as_complete(
    root: str, api: Mock, change: dict, patch: object
) -> None:
    change["diff"] = patch
    files = module.PULL_REQUESTS.changed_files(root, ref())
    assert files["warning"]
    assert files["data"][0]["lines_added"] is None
    assert files["data"][0]["lines_removed"] is None
    result = module.PULL_REQUESTS.pr_diff(root, ref())
    assert result["patch"] == "" and result["unavailable_reason"] == "patch_unavailable"


def test_generic_panel_inference_attach_selection_files_and_removal(
    root: str, api: Mock, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnigent.runner import pr_resource
    from omnigent.runner.session_prs import SessionPrRegistry

    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    session = "gitlab-panel"
    info = pr_resource.pr_info(root, session_id=session)
    assert info["pr"]["url"] == URL and info["provider"] == "gitlab"
    assert [entry.url for entry in SessionPrRegistry(session).list()] == [URL]
    selected = pr_resource.pr_info(root, session_id=session, pr_url=URL)
    assert selected["pr"]["number"] == 7
    assert (
        pr_resource.pr_changed_files(root, session_id=session, pr_url=URL)["data"][0]["path"]
        == "src/new.py"
    )
    assert "@@" in pr_resource.pr_diff(root, session_id=session, pr_url=URL)["patch"]
    expanded = pr_resource.pr_file_diff(
        root,
        "main",
        "src/new.py",
        session_id=session,
        pr_url=URL,
        previous_path="src/old.py",
        head_sha=HEAD,
        base_sha=BASE,
    )
    assert expanded["before"] == "old\n" and expanded["after"] == "new\n"
    removed = pr_resource.update_session_pr(root, session, URL, "remove")
    assert removed["pr"] is None and not removed["prs"]
    attached = pr_resource.update_session_pr(root, session, URL, "attach")
    assert attached["pr"]["number"] == 7 and attached["prs"][0]["relationship"] == "attached"


def test_generic_attach_failure_does_not_persist(
    root: str, api: Mock, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from omnigent.runner import pr_resource
    from omnigent.runner.session_prs import SessionPrRegistry

    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    api.object.side_effect = GitLabError("Access denied")
    with pytest.raises(ValueError, match="Access denied"):
        pr_resource.update_session_pr(root, "gitlab-panel", URL, "attach")
    assert SessionPrRegistry("gitlab-panel").list() == []


def test_private_port_remote_resolves_through_generic_panel(
    root: str, api: Mock, mr: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.runner import pr_resource

    host = "git.example.test:8443"
    monkeypatch.setenv("OMNIGENT_GIT_PROVIDER_GITLAB_HOSTS", host)
    subprocess.run(
        ["git", "-C", root, "remote", "set-url", "origin", f"https://{host}/fork/sub/project.git"],
        check=True,
    )
    mr["web_url"] = URL.replace("gitlab.com", host)
    info = pr_resource.pr_info(root)
    assert info["provider"] == "gitlab" and info["pr"]["url"] == mr["web_url"]


@pytest.mark.parametrize(
    "fields",
    [
        {"state": "invalid"},
        {"iid": 8},
        {"web_url": URL.replace("team/sub/project", "other/project")},
    ],
)
def test_invalid_identity_or_state_is_not_served(
    root: str, api: Mock, mr: dict, fields: dict
) -> None:
    mr.update(fields)
    info = module.PULL_REQUESTS.reference_info(root, ref())
    assert info["pr"] is None and info["warnings"]


@pytest.mark.parametrize("include_ours", [False, True])
def test_fork_discovery_rejects_same_branch_from_another_source_project(
    root: str, api: Mock, mr: dict, include_ours: bool
) -> None:
    original = api.pages.side_effect
    other = {**mr, "iid": 8, "source_project_id": 30, "web_url": URL.rsplit("/", 1)[0] + "/8"}

    def response(path: str, **query):
        if path.endswith("team%2Fsub%2Fproject/merge_requests"):
            assert query["source_branch"] == "feature" and query["state"] == "opened"
            return ([other, mr] if include_ours else [other]), False
        return original(path, **query)

    api.pages.side_effect = response
    info = module.PULL_REQUESTS.workspace_info(root)
    assert info["auth"]["authenticated"] and not info["warnings"]
    assert (info["pr"]["number"] if info["pr"] else None) == (7 if include_ours else None)
    assert not any(
        call.args[0].endswith("/merge_requests/8") for call in api.object.call_args_list
    )


def test_diff_preserves_final_line_whitespace(root: str, api: Mock, change: dict) -> None:
    change["diff"] = "@@ -1 +1 @@\n-before\n+after \t\n"
    assert module.PULL_REQUESTS.pr_diff(root, ref())["patch"].endswith("+after \t\n")
