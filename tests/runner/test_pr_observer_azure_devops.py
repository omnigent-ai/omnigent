"""The PR observer records Azure DevOps pull requests through the real provider registry.

The observer, both built-in providers, and the session registry are real. The fakes are the
recording HTTP transport, the Azure DevOps credential, and the session data directory. The
observer must send no request, so every test also fails when the transport saw one.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from importlib import metadata
from pathlib import Path
from typing import Any

import pytest

from omnigent.git_providers import reset_for_tests
from omnigent.runner import azure_devops_client
from omnigent.runner.azure_devops_client import AzureToken
from omnigent.runner.git_providers import azure_devops as azure_devops_facet
from omnigent.runner.git_providers.azure_devops import AzureDevOpsPullRequests
from omnigent.runner.pr_observer import extract_prs, observe_tool_completion
from omnigent.runner.session_prs import PullRequestRef, SessionPrRegistry
from tests.runner.azure_devops_fixtures import RecordingTransport

pytest_plugins = ["tests.runner.azure_devops_fixtures"]

SESSION = "conv_azure_devops"
TOKEN = AzureToken("secret-token", "bearer")
ORG = "https://dev.azure.com/contoso"
WEB_URL = f"{ORG}/web/_git/app"
PR_URL = f"{WEB_URL}/pullrequest/7"
PROJECT_ID = "9d1c7a55-3f1e-4c1e-9d0e-0a5b1f7c2e11"
REPO_ID = "5e3a1c2b-7d4f-4e8a-8b6c-1f2e3d4c5b6a"
REST_REPO_URL = f"{ORG}/{PROJECT_ID}/_apis/git/repositories/{REPO_ID}"
GITHUB_PR_URL = "https://github.com/acme/tools/pull/12"
CREATE = f"az repos pr create --title T --source-branch feat --org {ORG} -p web -r app"
GH_CREATE = "gh pr create --title T --body B"
ADO_REF = PullRequestRef(
    provider="azure_devops",
    host="dev.azure.com",
    repository="contoso/web/app",
    number=7,
    url=PR_URL,
)

# The repository field in the shapes that identify a PR, each with only the fields it needs.
WEB_URL_REPOSITORY = {"id": REPO_ID, "name": "app", "webUrl": WEB_URL}
REMOTE_URL_REPOSITORY = {
    "id": REPO_ID,
    "remoteUrl": "https://contoso@dev.azure.com/contoso/web/_git/app",
}
REST_REPOSITORY = {
    "name": "app",
    "project": {"id": PROJECT_ID, "name": "web"},
    "url": REST_REPO_URL,
}
# Everything ``az`` prints for a repository, so no single field has to carry the identity.
AZ_REPOSITORY = {
    "defaultBranch": "refs/heads/main",
    "id": REPO_ID,
    "isDisabled": False,
    "isFork": False,
    "name": "app",
    "project": {"id": PROJECT_ID, "name": "web", "state": "wellFormed", "visibility": "private"},
    "remoteUrl": "https://contoso@dev.azure.com/contoso/web/_git/app",
    "sshUrl": "git@ssh.dev.azure.com:v3/contoso/web/app",
    "url": REST_REPO_URL,
    "webUrl": WEB_URL,
}

REVIEWER_ID = "0f0e0d0c-0b0a-4909-8807-060504030201"
# Output objects of commands that change a PR but print no PR.
REVIEWER = {
    "displayName": "Pat Example",
    "hasDeclined": False,
    "id": REVIEWER_ID,
    "isFlagged": False,
    "reviewerUrl": f"{REST_REPO_URL}/pullRequests/7/reviewers/{REVIEWER_ID}",
    "uniqueName": "pat@contoso.com",
    "url": f"https://spsprodcus1.vssps.visualstudio.com/Aabc/_apis/Identities/{REVIEWER_ID}",
    "vote": 10,
}
WORK_ITEM = {"id": "42", "url": f"{ORG}/_apis/wit/workItems/42"}
POLICY_EVALUATION = {
    "artifactId": f"vstfs:///CodeReview/CodeReviewId/{PROJECT_ID}/7",
    "evaluationId": "6b6a6968-6766-4564-8362-616059585756",
    "status": "queued",
    "configuration": {"id": 12, "type": {"displayName": "Build"}},
}

MULTILINE_CREATE = r"""az repos pr create \
  --title 'Add the pipeline' \
  --source-branch feat \
  --org https://dev.azure.com/contoso -p web -r app \
  --description "$(cat <<'EOF'
## Summary
Adds CI.
EOF
)"
"""


def pull_request(repository: dict[str, Any], **fields: Any) -> dict[str, Any]:
    """Return a pull request as ``az repos pr`` prints it, with the repository under test."""
    return {
        "pullRequestId": 7,
        "status": "active",
        "title": "T",
        # A link to another PR in the body must not become the PR's identity.
        "description": f"Follow-up to {WEB_URL}/pullrequest/99",
        "sourceRefName": "refs/heads/feat",
        "targetRefName": "refs/heads/main",
        "url": f"{REST_REPO_URL}/pullRequests/7",
        "repository": repository,
        **fields,
    }


def az_output(value: object) -> str:
    """Render ``value`` as ``az`` prints JSON: indented, with a final newline."""
    return json.dumps(value, indent=2) + "\n"


def bash_result(stdout: str, **fields: object) -> dict[str, object]:
    """Return a Bash tool result for a command that finished."""
    return {"stdout": stdout, "stderr": "", "interrupted": False, **fields}


CREATE_OUTPUT = az_output(pull_request(WEB_URL_REPOSITORY))


@pytest.fixture(autouse=True)
def offline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ado_transport: RecordingTransport
) -> Iterator[None]:
    """Use the real providers, a fake credential, and an isolated session store.

    The facet module's ``PULL_REQUESTS`` gets the recording transport, so a request the
    observer sent would show up in ``ado_transport`` instead of leaving the machine.
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
    monkeypatch.setattr(azure_devops_client, "resolve_token", lambda: TOKEN)
    monkeypatch.setattr(azure_devops_client, "_find_az", lambda: None)
    monkeypatch.setattr(
        azure_devops_facet, "PULL_REQUESTS", AzureDevOpsPullRequests(transport=ado_transport)
    )
    reset_for_tests()
    yield
    reset_for_tests()
    assert ado_transport.requests == [], "the observer must not send requests"


# ---------------------------------------------------------------------------
# Creating and updating
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "repository",
    [WEB_URL_REPOSITORY, REMOTE_URL_REPOSITORY, REST_REPOSITORY, AZ_REPOSITORY],
    ids=["web-url", "remote-url", "rest-names-and-url", "az-full"],
)
def test_a_create_records_the_pr_that_its_json_output_names(repository: dict[str, Any]) -> None:
    result = bash_result(az_output(pull_request(repository)))

    assert extract_prs("Bash", {"command": CREATE}, result) == ([ADO_REF], True)


@pytest.mark.parametrize("indent", [None, 2], ids=["one-line", "indented"])
@pytest.mark.parametrize("envelope", [False, True], ids=["text", "envelope"])
def test_the_json_output_is_read_in_any_layout(indent: int | None, envelope: bool) -> None:
    text = json.dumps(pull_request(WEB_URL_REPOSITORY), indent=indent) + "\n"
    result = bash_result(text) if envelope else text

    assert extract_prs("Bash", {"command": CREATE}, result) == ([ADO_REF], True)


def test_an_update_records_the_pr_without_claiming_creation() -> None:
    command = "az repos pr update --id 7 --status completed"
    result = bash_result(az_output(pull_request(WEB_URL_REPOSITORY, status="completed")))

    assert extract_prs("Bash", {"command": command}, result) == ([ADO_REF], False)


@pytest.mark.parametrize(
    "command",
    [
        f"cd /workspace && {CREATE}",
        f"/opt/homebrew/bin/az repos pr create --title T --org {ORG} -p web -r app",
        f"AZURE_DEVOPS_EXT_PAT=secret {CREATE}",
        f"bash -lc 'cd /workspace && {CREATE}'",
        MULTILINE_CREATE,
    ],
    ids=["cd-prefix", "absolute-path", "env-prefix", "bash-wrapper", "multiline-heredoc"],
)
def test_az_is_found_however_the_shell_reaches_it(command: str) -> None:
    result = bash_result(CREATE_OUTPUT)

    assert extract_prs("Bash", {"command": command}, result) == ([ADO_REF], True)


# ---------------------------------------------------------------------------
# Commands and results that record nothing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "output"),
    [
        ("az repos pr set-vote --id 7 --vote approve", REVIEWER),
        ("az repos pr reviewer add --id 7 --reviewers pat@contoso.com", [REVIEWER]),
        ("az repos pr work-item add --id 7 --work-items 42", [WORK_ITEM]),
        ("az repos pr policy queue --id 7 --evaluation-id 6b6a", POLICY_EVALUATION),
    ],
    ids=["set-vote", "reviewer-add", "work-item-add", "policy-queue"],
)
def test_a_write_that_prints_no_pr_records_nothing(command: str, output: object) -> None:
    result = bash_result(az_output(output), exit_code=0)

    assert extract_prs("Bash", {"command": command}, result) == ([], False)


@pytest.mark.parametrize(
    ("command", "output"),
    [
        ("az repos pr show --id 7", pull_request(WEB_URL_REPOSITORY)),
        ("az repos pr list --status active", [pull_request(WEB_URL_REPOSITORY)]),
    ],
    ids=["show", "list"],
)
def test_a_read_records_nothing_although_it_prints_a_pr(command: str, output: object) -> None:
    result = bash_result(az_output(output), exit_code=0)

    assert extract_prs("Bash", {"command": command}, result) == ([], False)


@pytest.mark.parametrize(
    "result",
    [
        {"stdout": CREATE_OUTPUT, "stderr": "", "exit_code": 1},
        CREATE_OUTPUT + "[exit code: 1]",
        {"stdout": CREATE_OUTPUT + "[exit code: 1]"},
    ],
    ids=["exit-code-field", "trailer-text", "trailer-in-stdout"],
)
def test_a_create_that_exits_non_zero_records_nothing(result: object) -> None:
    assert extract_prs("Bash", {"command": CREATE}, result) == ([], False)


# ---------------------------------------------------------------------------
# Plain-text output
# ---------------------------------------------------------------------------


def test_a_pr_url_on_its_own_line_is_recorded() -> None:
    command = f"{CREATE} --output none && echo {PR_URL}"

    assert extract_prs("Bash", {"command": command}, bash_result(f"{PR_URL}\n")) == (
        [ADO_REF],
        True,
    )


def test_a_pr_url_inside_a_sentence_is_not_recorded() -> None:
    sentence = f"Opened {PR_URL} for review"
    command = f"{CREATE} --output none && echo '{sentence}'"

    refs, _ = extract_prs("Bash", {"command": command}, bash_result(f"{sentence}\n"))

    assert refs == []


# ---------------------------------------------------------------------------
# Two providers in one command
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("azure_first", [False, True], ids=["gh-first", "az-first"])
@pytest.mark.parametrize(
    "github_output",
    [json.dumps({"html_url": GITHUB_PR_URL, "number": 12}), GITHUB_PR_URL],
    ids=["html-url-object", "url-line"],
)
def test_a_command_that_creates_a_pr_on_each_forge_records_both(
    azure_first: bool, github_output: str
) -> None:
    commands = [GH_CREATE, CREATE]
    outputs = [github_output, CREATE_OUTPUT]
    if azure_first:
        commands.reverse()
        outputs.reverse()

    refs, created = extract_prs(
        "Bash", {"command": " && ".join(commands)}, bash_result("\n".join(outputs))
    )

    assert len(refs) == 2
    assert {ref.provider: ref.url for ref in refs} == {
        "github": GITHUB_PR_URL,
        "azure_devops": PR_URL,
    }
    assert created is True


# ---------------------------------------------------------------------------
# The session registry
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("command", "status", "relationship"),
    [
        (CREATE, "active", "created"),
        ("az repos pr update --id 7 --status completed", "completed", "worked_on"),
    ],
    ids=["create", "update"],
)
def test_the_registry_stores_the_pr_with_its_provider(
    command: str, status: str, relationship: str
) -> None:
    output = az_output(pull_request(WEB_URL_REPOSITORY, status=status))

    observe_tool_completion(
        SESSION,
        tool_name="Bash",
        arguments={"command": command},
        result=bash_result(output),
        call_id="call-1",
    )

    [entry] = SessionPrRegistry(SESSION).list()
    assert (entry.provider, entry.host, entry.repository, entry.number, entry.url) == (
        "azure_devops",
        "dev.azure.com",
        "contoso/web/app",
        7,
        PR_URL,
    )
    assert (entry.relationship, entry.source) == (relationship, "tool")


def test_a_failed_create_leaves_the_registry_empty() -> None:
    observe_tool_completion(
        SESSION,
        tool_name="Bash",
        arguments={"command": CREATE},
        result={"stdout": CREATE_OUTPUT, "stderr": "", "exit_code": 1},
        call_id="call-1",
    )

    assert SessionPrRegistry(SESSION).list() == []


def test_one_command_stores_a_pr_for_each_provider() -> None:
    observe_tool_completion(
        SESSION,
        tool_name="Bash",
        arguments={"command": f"{GH_CREATE} && {CREATE}"},
        result=bash_result(f"{GITHUB_PR_URL}\n{CREATE_OUTPUT}"),
        call_id="call-1",
    )

    entries = SessionPrRegistry(SESSION).list()
    assert {entry.provider: entry.url for entry in entries} == {
        "github": GITHUB_PR_URL,
        "azure_devops": PR_URL,
    }
    assert {entry.relationship for entry in entries} == {"created"}
