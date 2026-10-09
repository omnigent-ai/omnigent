"""Real tracking orchestration recognizes GitLab writes without doing I/O."""

from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path

import pytest

from omnigent.git_providers import reset_for_tests
from omnigent.runner.pr_observer import extract_prs, observe_tool_completion
from omnigent.runner.session_prs import PullRequestRef, SessionPrRegistry

URL = "https://gitlab.com/team/sub/project/-/merge_requests/7"
OTHER = "https://gitlab.com/team/sub/project/-/merge_requests/99"
GH_URL = "https://github.com/team/project/pull/8"
PRIVATE_URL = URL.replace("gitlab.com", "git.example.test:8443")


def payload(url: str = URL, iid: int = 7) -> dict:
    return {"web_url": url, "iid": iid, "id": 99999, "project_id": 10, "description": OTHER}


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "gh"))
    monkeypatch.setenv("OMNIGENT_GIT_PROVIDER_GITLAB_HOSTS", "git.example.test:8443")
    monkeypatch.delenv("GITLAB_HOST", raising=False)
    monkeypatch.delenv("GLAB_HOST", raising=False)

    def forbidden(*args, **kwargs):
        pytest.fail("the PR observer must not invoke a command or connect to a service")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    reset_for_tests()
    yield
    reset_for_tests()


def shell(command: str, stdout: object = URL, *, stream: str = "output", **status: object):
    return extract_prs(
        "exec_command",
        {"cmd": command},
        {
            stream: stdout if isinstance(stdout, str) else json.dumps(stdout),
            "exit_code": 0,
            **status,
        },
    )


def push_output(url: str = URL) -> str:
    return f"remote: \nremote: View merge request for feature:\nremote:   {url}\nremote: \n"


@pytest.mark.parametrize(
    "command",
    [
        "git push origin HEAD -o merge_request.create",
        "git -C /another/worktree push -u origin HEAD --push-option=merge_request.create",
        "git -c http.sslVerify=false push -u origin HEAD -omerge_request.create",
        "git --git-dir=/another/.git push --push-option merge_request.create origin HEAD",
        "zsh -lc 'cd /another/project && git push -o merge_request.create origin HEAD'",
        "git add . && git commit -m change && git push origin HEAD -o merge_request.create",
    ],
)
@pytest.mark.parametrize("url", [URL, PRIVATE_URL])
@pytest.mark.parametrize("stream", ["output", "stdout", "stderr"])
@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_push_options_track_server_reported_mr_across_worktrees(command, url, stream, newline):
    refs, created = shell(command, push_output(url).replace("\n", newline), stream=stream)
    assert [ref.url for ref in refs] == [url] and created


def test_push_banner_at_end_of_output_keeps_crlf_url():
    refs, created = shell(
        "git push origin HEAD -o merge_request.create",
        f"remote: View merge request for feature:\r\nremote:   {URL}\r\n",
        stream="stderr",
    )
    assert [ref.url for ref in refs] == [URL] and created


@pytest.mark.parametrize("stream", ["output", "stdout", "stderr"])
def test_push_options_track_update_and_preserve_removal(stream):
    command = "git push -o merge_request.title=Updated origin HEAD"
    refs, created = shell(command, push_output(), stream=stream)
    assert [ref.url for ref in refs] == [URL] and not created
    result = {stream: push_output(), "exit_code": 0}
    observe_tool_completion(
        "push-session",
        tool_name="shell",
        arguments={"command": command},
        result=result,
        call_id="push",
    )
    registry = SessionPrRegistry("push-session")
    assert [entry.url for entry in registry.list()] == [URL]
    registry.remove(URL)
    observe_tool_completion(
        "push-session",
        tool_name="shell",
        arguments={"command": command},
        result=result,
        call_id="push",
    )
    assert registry.list() == []


@pytest.mark.parametrize(
    "command",
    [
        "git push origin HEAD",
        "git push --dry-run -o merge_request.create",
        "git push -n -o merge_request.create",
        "git push -nu -o merge_request.create",
        "git push --delete -o merge_request.create origin feature",
        "git push --help -o merge_request.create",
        "git push -- -o merge_request.create",
        "git log -o merge_request.create",
        "echo git push -o merge_request.create",
        "git -c alias.publish=push publish -o merge_request.create",
        "git push -o merge_request.create || true",
        "git push -o merge_request.create; glab mr view 7",
        "git push -o merge_request.create; glab mr note 7 -m comment",
        "git push -o merge_request.create origin HEAD; git push other HEAD",
    ],
)
@pytest.mark.parametrize("stream", ["output", "stdout", "stderr"])
def test_non_mutating_or_ambiguous_push_does_not_track(command, stream):
    assert shell(command, push_output(), stream=stream)[0] == []


@pytest.mark.parametrize(
    "output",
    [
        URL,
        f"remote: {URL}",
        push_output(GH_URL),
        push_output().replace("View merge request for", "To create a merge request for"),
        push_output() + "error: failed to push some refs to 'origin'\n",
        push_output().replace(URL, URL + "/diffs"),
    ],
)
@pytest.mark.parametrize("stream", ["output", "stdout", "stderr"])
def test_push_tracks_only_successful_gitlab_mr_banner(output, stream):
    assert shell("git push -o merge_request.create", output, stream=stream)[0] == []


@pytest.mark.parametrize("stream", ["output", "stdout", "stderr"])
def test_failed_or_background_push_does_not_track(stream):
    command = "git push -o merge_request.create"
    assert shell(command, push_output(), stream=stream, exit_code=1) == ([], False)
    assert shell(command, push_output(), stream=stream, exit_code=None, session_id=12) == (
        [],
        False,
    )


def test_normal_push_before_github_creation_still_tracks():
    refs, created = shell("git push -u origin HEAD && gh pr create --title T --body B", GH_URL)
    assert [ref.url for ref in refs] == [GH_URL] and created


@pytest.mark.parametrize(
    "command,status,expected",
    [
        ("gh pr create --title T --body B", {"exit_code": 0}, [GH_URL]),
        ("gh pr create --title T --body B", {"exit_code": 1}, []),
        ("gh pr create --title T --body B", {"interrupted": True}, []),
        ("gh pr view 8", {"exit_code": 0}, []),
        ("gh pr comment 8 --body T", {"exit_code": 0}, []),
        ("gh pr diff 8; gh pr create", {"exit_code": 0}, []),
    ],
)
def test_gitlab_stderr_does_not_change_github_attribution(command, status, expected):
    refs, _ = extract_prs(
        "exec_command",
        {"cmd": command},
        {"stdout": GH_URL, "stderr": push_output(), **status},
    )
    assert [ref.url for ref in refs] == expected


@pytest.mark.parametrize(
    "command",
    [
        "glab mr create --title T --description B",
        "glab mr create -f --draft --yes",
        "glab mr new --description-file /tmp/body --yes",
        "glab -R team/sub/project mr create --title=T --allow-collaboration=true",
        "env GLAB_NO_PROMPT=1 /usr/bin/glab mr create --title T --description B",
        "bash -lc 'cd /checkout && glab mr create --title T --description B'",
        "glab api projects/team%2Fsub%2Fproject/merge_requests -f title=T -F source_branch=feat",
    ],
)
def test_create_tracks_successful_result_not_body_urls(command: str) -> None:
    refs, created = shell(command, payload())
    assert [ref.url for ref in refs] == [URL] and created


@pytest.mark.parametrize(
    "action",
    ["update", "merge", "accept", "close", "reopen", "approve", "revoke", "rebase", "delete"],
)
def test_explicit_mutation_tracks_iid_without_requiring_output(action: str) -> None:
    refs, created = shell(f"GITLAB_HOST=gitlab.com glab mr {action} 7 -R team/sub/project", "")
    assert [ref.url for ref in refs] == [URL] and not created


@pytest.mark.parametrize(
    "command",
    [
        "glab mr merge 7 -R team/sub/project -s -d -r --auto-merge=false -y",
        "glab mr update 7 --repo git@gitlab.com:team/sub/project.git --description-file body.md",
        "glab api --method PUT projects/team%2Fsub%2Fproject/merge_requests/7 -f title=T",
        "glab api -XPOST projects/team%2Fsub%2Fproject/merge_requests/7/approve",
    ],
)
def test_cli_short_flags_repo_urls_and_api_writes(command: str) -> None:
    assert shell(f"GITLAB_HOST=gitlab.com {command}", "") == (
        [PullRequestRef.from_url(URL)],
        False,
    )


def test_private_authority_and_nested_namespace_are_preserved() -> None:
    url = URL.replace("gitlab.com", "git.example.test:8443")
    assert shell("GITLAB_HOST=git.example.test:8443 glab mr update 7 -R team/sub/project", "") == (
        [PullRequestRef.from_url(url)],
        False,
    )
    url = URL.replace("team/sub/project", "team.name/sub/project")
    assert shell("GITLAB_HOST=gitlab.com glab mr update 7 -R team.name/sub/project", "") == (
        [PullRequestRef.from_url(url)],
        False,
    )


@pytest.mark.parametrize(
    "command",
    [
        "GITLAB_HOST=gitlab.com glab mr update 7 -R team/sub/project "
        "-d GITLAB_HOST=git.example.test:8443",
        "GITLAB_HOST=gitlab.com glab mr update 7 -R team/sub/project "
        "--description 'GLAB_HOST=git.example.test:8443'",
        "GITLAB_HOST=gitlab.com glab mr update 7 -R team/sub/project "
        "--title GITLAB_HOST=git.example.test:8443",
        "env GLAB_NO_PROMPT=1 GITLAB_HOST=gitlab.com glab mr update 7 -R team/sub/project "
        "-d GITLAB_HOST=git.example.test:8443",
        "bash -lc 'GITLAB_HOST=gitlab.com glab mr update 7 -R team/sub/project "
        "-d GITLAB_HOST=git.example.test:8443'",
    ],
)
def test_assignment_text_in_arguments_does_not_change_host(command: str) -> None:
    assert shell(command, "") == ([PullRequestRef.from_url(URL)], False)


@pytest.mark.parametrize(
    "prefix",
    [
        "GITLAB_HOST=git.example.test:8443",
        "env GITLAB_HOST=git.example.test:8443",
        "/usr/bin/env GLAB_NO_PROMPT=1 GITLAB_HOST=git.example.test:8443",
    ],
)
def test_environment_prefix_sets_host_without_reading_description(prefix: str) -> None:
    url = URL.replace("gitlab.com", "git.example.test:8443")
    command = f"{prefix} glab mr update 7 -R team/sub/project -d GITLAB_HOST=gitlab.com"
    assert shell(command, "") == ([PullRequestRef.from_url(url)], False)


def test_hostname_flag_precedes_environment_prefix() -> None:
    command = (
        "GITLAB_HOST=git.example.test:8443 glab mr update 7 "
        "-R team/sub/project --hostname gitlab.com"
    )
    assert shell(command, "") == ([PullRequestRef.from_url(URL)], False)


@pytest.mark.parametrize(
    ("prefix", "url"),
    [
        ("GITLAB_HOST=gitlab.com GITLAB_HOST=git.example.test:8443", PRIVATE_URL),
        ("GITLAB_HOST=git.example.test:8443 GITLAB_HOST=gitlab.com", URL),
        ("GLAB_HOST=git.example.test:8443 GITLAB_HOST=gitlab.com", URL),
        ("GITLAB_HOST=gitlab.com GLAB_HOST=git.example.test:8443", URL),
        ("env GITLAB_HOST=gitlab.com GITLAB_HOST=git.example.test:8443", PRIVATE_URL),
    ],
)
def test_last_gitlab_host_assignment_controls_the_cli_target(prefix: str, url: str) -> None:
    assert shell(f"{prefix} glab mr update 7 -R team/sub/project", "") == (
        [PullRequestRef.from_url(url)],
        False,
    )


@pytest.mark.parametrize(
    ("prefix", "ambient"),
    [
        ("", None),
        ("", "gitlab.com"),
        ("GLAB_HOST=git.example.test:8443", None),
        ("GITLAB_HOST=", "gitlab.com"),
        ("env GITLAB_HOST=", "gitlab.com"),
        ("env -i", "gitlab.com"),
        ("env --ignore-environment", "gitlab.com"),
        ("env -u GITLAB_HOST", "gitlab.com"),
        ("env --unset=GITLAB_HOST", "gitlab.com"),
        ("/usr/bin/env -u UNUSED", "gitlab.com"),
    ],
)
@pytest.mark.parametrize(
    "invocation",
    [
        "glab mr update 7 -R team/sub/project",
        "glab api -XPUT projects/team%2Fsub%2Fproject/merge_requests/7 -f title=T",
        "bash -lc 'glab mr update 7 -R team/sub/project'",
    ],
)
def test_unknown_cli_host_requires_a_returned_identity(
    prefix: str, ambient: str | None, invocation: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    if ambient:
        monkeypatch.setenv("GITLAB_HOST", ambient)
    monkeypatch.setenv("GLAB_HOST", "gitlab.com")
    command = f"{prefix} {invocation}"
    assert shell(command, "") == ([], False)
    assert shell(command, payload(PRIVATE_URL)) == ([PullRequestRef.from_url(PRIVATE_URL)], False)


@pytest.mark.parametrize(
    "command",
    [
        f"glab mr update {PRIVATE_URL}",
        "glab mr update 7 -R https://git.example.test:8443/team/sub/project",
        "glab mr update 7 -R git@git.example.test:team/sub/project.git",
        "glab mr update 7 -R git.example.test:8443/team/sub/project",
        "env -i glab mr update 7 -R team/sub/project --hostname git.example.test:8443",
    ],
)
def test_explicit_authority_does_not_need_a_cli_host_default(command: str) -> None:
    assert shell(command, "") == ([PullRequestRef.from_url(PRIVATE_URL)], False)


@pytest.mark.parametrize(
    "command",
    [
        "glab mr view 7",
        "glab mr list",
        "glab mr diff 7",
        "glab mr note 7 -m hello",
        "glab mr create --web",
        "glab mr create --help",
        "glab mr create --unknown-flag",
        "glab api projects/team%2Fsub%2Fproject/merge_requests/7",
        "glab api -XPOST projects/team%2Fsub%2Fproject/merge_requests/7/notes -f body=hello",
        "echo glab mr create",
        "glab mr create || echo ignored",
    ],
)
def test_reads_comments_and_nonexecuted_writes_do_not_track(command: str) -> None:
    assert shell(command, payload())[0] == []


@pytest.mark.parametrize(
    "status",
    [
        {"exit_code": 1},
        {"isError": True},
        {"exit_code": None, "session_id": 123},
        {"interrupted": True},
    ],
)
def test_failed_or_unfinished_tools_do_not_track(status: dict) -> None:
    assert shell("glab mr create", payload(), **status) == ([], False)


@pytest.mark.parametrize(
    "output",
    [
        {"description": URL},
        {"body": URL},
        {"web_url": URL, "iid": 99999},
        "Created description with " + URL,
        "'" + URL + "'",
    ],
)
def test_body_text_and_global_ids_never_become_mr_identity(output: object) -> None:
    assert shell("glab mr create", output) == ([], True)


def test_mixed_read_write_output_only_records_explicit_write_target() -> None:
    assert shell(
        "GITLAB_HOST=gitlab.com glab mr update 7 -R team/sub/project && glab mr view 99",
        payload(OTHER, 99),
    ) == ([PullRequestRef.from_url(URL)], False)
    assert shell("glab mr create && gh pr view 8", {"url": GH_URL}) == ([], True)
    assert shell("gh pr create --title T --body B && glab mr view 7", payload()) == ([], True)


def test_mixed_provider_creates_remain_independent() -> None:
    refs, created = shell("glab mr create && gh pr create --title T --body B", URL + "\n" + GH_URL)
    assert {ref.url for ref in refs} == {URL, GH_URL} and created


def test_content_projection_does_not_attribute_body_link() -> None:
    assert shell("glab api -XPUT projects/10/merge_requests/7 --jq .description", OTHER)[0] == []


def test_mcp_create_uses_result_identity_and_checks_project() -> None:
    arguments = {"project_id": "team/sub/project", "description": OTHER}
    result = {"content": [{"type": "text", "text": json.dumps(payload())}]}
    assert extract_prs("mcp__gitlab__create_merge_request", arguments, result) == (
        [PullRequestRef.from_url(URL)],
        True,
    )
    arguments["project_id"] = "unrelated/project"
    assert extract_prs("mcp__gitlab__create_merge_request", arguments, result) == ([], True)
    arguments["project_id"] = "11"
    assert extract_prs("mcp__gitlab__create_merge_request", arguments, result) == ([], True)


def test_mcp_update_and_nontracking_tools() -> None:
    args = {"project_id": "team/sub/project", "merge_request_iid": 7, "hostname": "gitlab.com"}
    assert extract_prs("mcp__gitlab__update_merge_request", args, {}) == (
        [PullRequestRef.from_url(URL)],
        False,
    )
    for name in ("get_merge_request", "create_merge_request_note"):
        assert extract_prs("mcp__gitlab__" + name, args, payload()) == ([], False)


def test_observed_mr_persists_and_removal_survives_replay() -> None:
    kwargs = {
        "tool_name": "Bash",
        "arguments": {"command": "glab mr create"},
        "result": URL,
        "call_id": "call-1",
    }
    observe_tool_completion("gitlab-session", **kwargs)
    [entry] = SessionPrRegistry("gitlab-session").list()
    assert entry.url == URL and entry.relationship == "created" and entry.number == 7
    SessionPrRegistry("gitlab-session").remove(URL)
    observe_tool_completion("gitlab-session", **kwargs)
    observe_tool_completion("gitlab-session", **{**kwargs, "call_id": "call-2"})
    assert SessionPrRegistry("gitlab-session").list() == []


def test_known_target_cannot_gain_an_unrelated_output_mr() -> None:
    assert shell(
        "GITLAB_HOST=gitlab.com glab mr update 7 -R team/sub/project", payload(OTHER, 99)
    ) == (
        [PullRequestRef.from_url(URL)],
        False,
    )
    assert shell("gh pr edit 8 -R team/project", payload()) == (
        [PullRequestRef.from_url(GH_URL)],
        False,
    )


def test_mcp_fork_creation_uses_explicit_target_project() -> None:
    result = {**payload(), "project_id": 20, "source_project_id": 10, "target_project_id": 20}
    assert extract_prs(
        "mcp__gitlab__create_merge_request", {"project_id": 10, "target_project_id": 20}, result
    ) == ([PullRequestRef.from_url(URL)], True)


@pytest.mark.parametrize("ambient", [None, "GITLAB_HOST", "GLAB_HOST"])
@pytest.mark.parametrize("project", ["team/sub/project", 10])
def test_mcp_host_comes_from_result_not_cli_defaults(
    ambient: str | None, project: str | int, monkeypatch: pytest.MonkeyPatch
) -> None:
    if ambient:
        monkeypatch.setenv(ambient, "gitlab.com")
    args = {"project_id": project, "merge_request_iid": 7}
    assert extract_prs("mcp__gitlab__update_merge_request", args, {}) == ([], False)
    assert extract_prs("mcp__gitlab__update_merge_request", args, payload(PRIVATE_URL)) == (
        [PullRequestRef.from_url(PRIVATE_URL)],
        False,
    )


@pytest.mark.parametrize(
    "authority",
    [
        {"hostname": "git.example.test:8443"},
        {"host": "https://git.example.test:8443"},
        {"project_id": "https://git.example.test:8443/team/sub/project"},
    ],
)
def test_mcp_explicit_authority_supports_mutations_without_output(authority: dict) -> None:
    args = {"project_id": "team/sub/project", "merge_request_iid": 7, **authority}
    assert extract_prs("mcp__gitlab__update_merge_request", args, {}) == (
        [PullRequestRef.from_url(PRIVATE_URL)],
        False,
    )


@pytest.mark.parametrize(
    "result",
    [
        payload(PRIVATE_URL, 8),
        payload(PRIVATE_URL.replace("team/sub/project", "other/project")),
        payload(PRIVATE_URL.replace("git.example.test:8443", "untrusted.example")),
        {"description": PRIVATE_URL},
    ],
)
def test_mcp_unknown_host_still_checks_project_iid_and_instance_trust(result: dict) -> None:
    args = {"project_id": "team/sub/project", "merge_request_iid": 7}
    assert extract_prs("mcp__gitlab__update_merge_request", args, result) == ([], False)
