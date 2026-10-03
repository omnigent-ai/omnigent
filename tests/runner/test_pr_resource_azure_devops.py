"""The session PR panel serves Azure DevOps workspaces and tracked PRs through the real registry.

``pr_resource`` picks the provider from the workspace's git remote or the tracked PR, and
``load_facet`` imports the real Azure DevOps facet module. The fakes are the HTTP transport (the
facet module's ``PULL_REQUESTS`` gets an instance that uses it), the credential, and git remotes
in temp repos. Every test fails on a request to a host other than ``dev.azure.com``, and no
GitHub command runs.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from importlib import metadata
from pathlib import Path
from typing import Any

import pytest

from omnigent.git_providers import reset_for_tests
from omnigent.runner import azure_devops_client, github_resource, pr_resource
from omnigent.runner.azure_devops_client import AzureToken
from omnigent.runner.git_providers import ProviderCapabilities
from omnigent.runner.git_providers import azure_devops as azure_devops_facet
from omnigent.runner.git_providers.azure_devops import AzureDevOpsPullRequests
from omnigent.runner.pr_observer import observe_tool_completion
from omnigent.runner.session_prs import PullRequestRef, SessionPrRegistry
from tests.runner.azure_devops_fixtures import RecordingTransport, request_path, request_query

pytest_plugins = ["tests.runner.azure_devops_fixtures"]

SESSION = "conv_azure_devops"
TOKEN = AzureToken("secret-token", "bearer")
ORIGIN = "https://dev.azure.com/contoso/web/_git/app"
PR_URL = f"{ORIGIN}/pullrequest/7"
REPO_API = "/contoso/web/_apis/git/repositories/app"
PULLS = f"{REPO_API}/pullrequests"
PULL = f"{PULLS}/7"
EVALUATIONS = "/contoso/web/_apis/policy/evaluations"
INACCESSIBLE = "Cannot access this pull request with the Azure DevOps credentials on the host"
NO_CAPABILITIES = {
    "account_switching": False,
    "base_remote_selection": False,
    "line_counts": False,
    "linked_pr_diff": False,
}
LEGACY_FIELDS = {"gh_available", "authenticated", "accounts", "selected_account"}
CHANGES = [
    {"changeType": "edit", "item": {"path": "/src/app.py", "gitObjectType": "blob"}},
    {"changeType": "add", "item": {"path": "/docs/new.md", "gitObjectType": "blob"}},
    {"changeType": "add", "item": {"path": "/docs", "isFolder": True, "gitObjectType": "tree"}},
]
CHANGED_FILES = [
    {
        "object": "session.github.changed_file",
        "path": path,
        "name": name,
        "status": status,
        "lines_added": None,
        "lines_removed": None,
    }
    for path, name, status in (
        ("src/app.py", "app.py", "modified"),
        ("docs/new.md", "new.md", "created"),
    )
]


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def pull_request(number: int = 7, **fields: Any) -> dict[str, Any]:
    """Return a pull request as the REST API sends it."""
    return {
        "pullRequestId": number,
        "status": "active",
        "title": "Add the pipeline",
        "description": "Adds CI.",
        "isDraft": False,
        "createdBy": {"displayName": "Pat Example", "id": "user-1"},
        "sourceRefName": "refs/heads/feat",
        "targetRefName": "refs/heads/main",
        "lastMergeSourceCommit": {"commitId": "a" * 40},
        "lastMergeTargetCommit": {"commitId": "b" * 40},
        "repository": {"name": "app", "project": {"id": "project-1", "name": "web"}},
        **fields,
    }


def serve(transport: RecordingTransport, pr: dict[str, Any]) -> None:
    """Route one pull request and the lists its info payload reads."""
    path = f"{PULLS}/{pr['pullRequestId']}"
    transport.route("GET", path, json=pr)
    transport.route("GET", f"{path}/statuses", json={"value": []})
    transport.route("GET", EVALUATIONS, json={"value": []})
    transport.route("GET", f"{path}/threads", json={"value": []})


def serve_branch_pr(transport: RecordingTransport) -> None:
    """Route the pull request of branch ``feat`` and everything its info payload reads."""
    transport.route("GET", PULLS, json={"value": [pull_request()]})
    serve(transport, pull_request())


def serve_changes(transport: RecordingTransport) -> None:
    """Route the iterations of PR 7 and the changes of its latest one."""
    transport.route("GET", f"{PULL}/iterations", json={"value": [{"id": 1}, {"id": 2}]})
    transport.route("GET", f"{PULL}/iterations/2/changes", json={"changeEntries": CHANGES})


def paths(transport: RecordingTransport) -> list[str]:
    return [request_path(request) for request in transport.requests]


def track(url: str) -> None:
    SessionPrRegistry(SESSION).record(
        [PullRequestRef.from_url(url)], relationship="attached", source="test"
    )


def forbidden(*_args: object, **_kwargs: object) -> None:
    pytest.fail("the GitHub module ran a command for an Azure DevOps workspace")


@pytest.fixture(autouse=True)
def offline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ado_transport: RecordingTransport
) -> Iterator[None]:
    """Give every test the real providers, a token, git limited to local paths, and the host guard.

    The facet module's ``PULL_REQUESTS`` gets the recording transport. ``gh`` and the GitHub
    module's git calls fail the test, since these workspaces belong to Azure DevOps.
    """
    monkeypatch.setattr(metadata, "entry_points", lambda **_: ())
    for name in (
        "OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS",
        "OMNIGENT_GIT_PROVIDER_AZURE_DEVOPS_HOSTS",
        "GH_HOST",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "gh"))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "test@example.com")
    monkeypatch.setattr(azure_devops_client, "resolve_token", lambda: TOKEN)
    monkeypatch.setattr(azure_devops_client, "_find_az", lambda: None)
    monkeypatch.setattr(
        azure_devops_facet, "PULL_REQUESTS", AzureDevOpsPullRequests(transport=ado_transport)
    )
    monkeypatch.setattr(github_resource, "_run", forbidden)
    reset_for_tests()
    yield
    reset_for_tests()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A checkout of branch ``feat`` whose ``origin`` is the Azure DevOps repository."""
    root = tmp_path / "workspace"
    root.mkdir()
    git(root, "init", "-q")
    (root / "README.md").write_text("hello\n")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "base")
    git(root, "checkout", "-q", "-b", "feat")
    git(root, "remote", "add", "origin", ORIGIN)
    return root


# ---------------------------------------------------------------------------
# Info and branch inference
# ---------------------------------------------------------------------------


def test_pr_info_returns_the_azure_devops_payload_for_the_branch_pr(
    workspace: Path, ado_transport: RecordingTransport
) -> None:
    serve_branch_pr(ado_transport)

    info = pr_resource.pr_info(str(workspace), session_id=SESSION)

    assert info["provider"] == "azure_devops"
    assert info["capabilities"] == NO_CAPABILITIES
    assert info["auth"]["cli"] == {"name": "az", "available": False}
    assert not LEGACY_FIELDS & info.keys()
    assert info["repo"] == {"name_with_owner": "contoso/web/app"}
    assert (info["branch"], info["base_ref"]) == ("feat", "main")
    pr = info["pr"]
    assert (pr["number"], pr["url"], pr["title"], pr["state"]) == (
        7,
        PR_URL,
        "Add the pipeline",
        "OPEN",
    )
    assert (pr["head_ref"], pr["base_ref"]) == ("feat", "main")
    assert info["selected_pr_url"] == PR_URL
    assert [(entry["provider"], entry["url"], entry["title"]) for entry in info["prs"]] == [
        ("azure_devops", PR_URL, "Add the pipeline")
    ]
    find = next(request for request in ado_transport.requests if request_path(request) == PULLS)
    assert ("searchCriteria.sourceRefName", "refs/heads/feat") in request_query(find)


def test_branch_inference_records_the_azure_devops_ref(
    workspace: Path, ado_transport: RecordingTransport
) -> None:
    serve_branch_pr(ado_transport)

    pr_resource.pr_info(str(workspace), session_id=SESSION)

    [entry] = SessionPrRegistry(SESSION).list()
    assert (entry.provider, entry.host, entry.repository, entry.number, entry.url) == (
        "azure_devops",
        "dev.azure.com",
        "contoso/web/app",
        7,
        PR_URL,
    )
    assert (entry.relationship, entry.source) == ("inferred", "branch")


def test_branch_inference_survives_a_failed_pr_read(
    workspace: Path, ado_transport: RecordingTransport
) -> None:
    serve_branch_pr(ado_transport)
    ado_transport.route("GET", PULL, status=503, json={"message": "unavailable"})

    info = pr_resource.pr_info(str(workspace), session_id=SESSION)

    assert (info["pr"]["number"], info["pr"]["title"]) == (7, "Add the pipeline")
    assert info["selected_pr_url"] == PR_URL
    [entry] = SessionPrRegistry(SESSION).list()
    assert (entry.url, entry.relationship, entry.source) == (PR_URL, "inferred", "branch")


def test_a_branch_without_a_pr_infers_nothing(
    workspace: Path, ado_transport: RecordingTransport
) -> None:
    ado_transport.route("GET", PULLS, json={"value": []})

    info = pr_resource.pr_info(str(workspace), session_id=SESSION)

    assert (info["provider"], info["pr"], info["prs"]) == ("azure_devops", None, [])
    assert SessionPrRegistry(SESSION).list() == []


@pytest.mark.parametrize(
    "remote",
    [
        "https://contoso@dev.azure.com/contoso/web/_git/app",
        "git@ssh.dev.azure.com:v3/contoso/web/app",
        "https://contoso.visualstudio.com/web/_git/app",
    ],
    ids=["clone-dialog", "ssh", "visualstudio"],
)
def test_every_remote_form_reaches_the_same_repository(
    workspace: Path, ado_transport: RecordingTransport, remote: str
) -> None:
    git(workspace, "remote", "set-url", "origin", remote)
    serve_branch_pr(ado_transport)

    info = pr_resource.pr_info(str(workspace), session_id=SESSION)

    assert info["provider"] == "azure_devops"
    assert info["repo"] == {"name_with_owner": "contoso/web/app"}
    assert info["pr"]["url"] == PR_URL


def test_a_tracked_pr_is_read_by_id_instead_of_by_branch(
    workspace: Path, ado_transport: RecordingTransport
) -> None:
    serve(ado_transport, pull_request())
    track(PR_URL)

    info = pr_resource.pr_info(str(workspace), session_id=SESSION)

    assert (info["selected_pr_url"], info["pr"]["number"]) == (PR_URL, 7)
    assert PULLS not in paths(ado_transport)


def test_a_selected_azure_devops_pr_is_served_by_azure_devops_in_a_github_workspace(
    workspace: Path, ado_transport: RecordingTransport
) -> None:
    git(workspace, "remote", "set-url", "origin", "https://github.com/acme/tools.git")
    serve(ado_transport, pull_request())
    track(PR_URL)

    info = pr_resource.pr_info(str(workspace), session_id=SESSION, pr_url=PR_URL)

    assert (info["provider"], info["selected_pr_url"]) == ("azure_devops", PR_URL)
    assert info["repo"] == {"name_with_owner": "contoso/web/app"}
    assert info["pr"]["title"] == "Add the pipeline"


# ---------------------------------------------------------------------------
# Attach
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [PR_URL, "https://Contoso.VisualStudio.com/Web/_git/App/pullrequest/7?_a=overview"],
    ids=["canonical", "legacy-host"],
)
def test_attach_verifies_the_pr_through_rest_and_records_it(
    workspace: Path, ado_transport: RecordingTransport, url: str
) -> None:
    serve(ado_transport, pull_request())

    info = pr_resource.update_session_pr(str(workspace), SESSION, url, "attach")

    assert paths(ado_transport)[0] == PULL
    assert (info["provider"], info["selected_pr_url"], info["pr"]["number"]) == (
        "azure_devops",
        PR_URL,
        7,
    )
    [entry] = SessionPrRegistry(SESSION).list()
    assert (entry.provider, entry.url, entry.relationship, entry.source) == (
        "azure_devops",
        PR_URL,
        "attached",
        "user",
    )


def test_attaching_a_pr_that_cannot_be_read_raises_and_records_nothing(
    workspace: Path, ado_transport: RecordingTransport
) -> None:
    ado_transport.route("GET", PULL, status=404, json={"message": "TF401180: not found"})

    with pytest.raises(ValueError) as excinfo:
        pr_resource.update_session_pr(str(workspace), SESSION, PR_URL, "attach")

    assert str(excinfo.value) == INACCESSIBLE
    assert paths(ado_transport) == [PULL]
    assert SessionPrRegistry(SESSION).list() == []


# ---------------------------------------------------------------------------
# Changed files and diff
# ---------------------------------------------------------------------------


def test_changed_files_of_a_tracked_pr_come_from_azure_devops(
    workspace: Path, ado_transport: RecordingTransport
) -> None:
    serve_changes(ado_transport)
    track(PR_URL)

    result = pr_resource.pr_changed_files(str(workspace), session_id=SESSION)

    assert result == {"object": "list", "data": CHANGED_FILES, "has_more": False}
    assert paths(ado_transport) == [f"{PULL}/iterations", f"{PULL}/iterations/2/changes"]


def test_changed_files_of_the_branch_pr_come_from_azure_devops(
    workspace: Path, ado_transport: RecordingTransport
) -> None:
    ado_transport.route("GET", PULLS, json={"value": [pull_request()]})
    serve_changes(ado_transport)

    result = pr_resource.pr_changed_files(str(workspace), session_id=SESSION)

    assert result == {"object": "list", "data": CHANGED_FILES, "has_more": False}
    assert paths(ado_transport)[0] == PULLS


def test_the_diff_of_a_pr_in_another_repository_is_outside_the_workspace(
    workspace: Path, ado_transport: RecordingTransport
) -> None:
    track("https://dev.azure.com/contoso/other/_git/lib/pullrequest/9")

    result = pr_resource.pr_diff(str(workspace), session_id=SESSION)

    assert result == {
        "object": "session.github.pr_diff",
        "patch": "",
        "unavailable_reason": "pr_outside_workspace",
    }
    assert ado_transport.requests == []


# ---------------------------------------------------------------------------
# Observer to panel
# ---------------------------------------------------------------------------


def test_a_pr_the_observer_recorded_is_read_from_its_own_repository(
    workspace: Path, ado_transport: RecordingTransport
) -> None:
    # Spaces and capitals in the names must reach the REST path in its canonical form.
    web_url = "https://dev.azure.com/Contoso/My%20Web/_git/My%20App"
    output = json.dumps({"pullRequestId": 7, "repository": {"webUrl": web_url}})
    observe_tool_completion(
        SESSION,
        tool_name="Bash",
        arguments={"command": "az repos pr create --title T"},
        result={"stdout": output, "stderr": "", "interrupted": False},
        call_id="call-1",
    )
    pull = "/contoso/my%20web/_apis/git/repositories/my%20app/pullrequests/7"
    ado_transport.route("GET", pull, json=pull_request(repository={"name": "My App"}))
    ado_transport.route("GET", f"{pull}/statuses", json={"value": []})
    ado_transport.route("GET", f"{pull}/threads", json={"value": []})

    info = pr_resource.pr_info(str(workspace), session_id=SESSION)

    assert (info["provider"], info["pr"]["title"]) == ("azure_devops", "Add the pipeline")
    assert info["selected_pr_url"] == (
        "https://dev.azure.com/contoso/my%20web/_git/my%20app/pullrequest/7"
    )
    assert info["repo"] == {"name_with_owner": "contoso/my web/my app"}
    assert paths(ado_transport)[0] == pull


@pytest.mark.parametrize("origin_provider", ["github", "gitlab", "azure_devops"])
@pytest.mark.parametrize("failed_provider", [None, "github", "gitlab", "azure_devops"])
def test_all_remote_providers_are_discovered_without_configuration(
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    origin_provider: str,
    failed_provider: str | None,
) -> None:
    remotes = {
        "github": "https://github.com/acme/app.git",
        "gitlab": "https://gitlab.com/acme/nested/app.git",
        "azure_devops": ORIGIN,
    }
    urls = {
        "github": "https://github.com/acme/app/pull/11",
        "gitlab": "https://gitlab.com/acme/nested/app/-/merge_requests/22",
        "azure_devops": PR_URL,
    }
    for name in ("OMNIGENT_GIT_PROVIDER_GITLAB_HOSTS", "GITLAB_HOST", "GLAB_HOST"):
        monkeypatch.delenv(name, raising=False)
    git(workspace, "remote", "set-url", "origin", remotes[origin_provider])
    for provider_id, remote in remotes.items():
        if provider_id != origin_provider:
            git(workspace, "remote", "add", provider_id, remote)
    visited: list[str] = []

    class Facet:
        capabilities = ProviderCapabilities(**NO_CAPABILITIES)

        def __init__(self, provider_id: str) -> None:
            self.provider_id = provider_id

        def workspace_info(self, root: str) -> dict[str, Any]:
            assert root == str(workspace)
            visited.append(self.provider_id)
            if self.provider_id == failed_provider:
                raise ValueError("The forge is unavailable")
            reference = PullRequestRef.from_url(urls[self.provider_id])
            return {
                "provider": self.provider_id,
                "available": True,
                "branch": "feat",
                "repo": {"name_with_owner": reference.repository},
                "pr": {
                    "number": reference.number,
                    "url": reference.url,
                    "title": f"Change on {self.provider_id}",
                },
            }

        def on_inferred_pr(self, root: str, reference: PullRequestRef) -> None:
            pass

        def titles_available(self, root: str) -> bool:
            return False

    facets = {provider_id: Facet(provider_id) for provider_id in remotes}
    monkeypatch.setattr(pr_resource, "_facet", facets.get)

    info = pr_resource.pr_info(str(workspace), session_id=SESSION)

    expected = {
        provider_id: url for provider_id, url in urls.items() if provider_id != failed_provider
    }
    assert set(visited) == set(remotes)
    assert {entry.provider: entry.url for entry in SessionPrRegistry(SESSION).list()} == expected
    assert {entry["provider"]: entry["url"] for entry in info["prs"]} == expected
    assert all(entry.relationship == "inferred" for entry in SessionPrRegistry(SESSION).list())
    if origin_provider != failed_provider:
        assert info["selected_pr_url"] == urls[origin_provider]
    else:
        assert info["selected_pr_url"] in expected.values()
    if failed_provider is not None:
        assert any(failed_provider in warning for warning in info["warnings"])
