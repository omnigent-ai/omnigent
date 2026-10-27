"""Real tracking orchestration recognizes fj, tea, Forgejo MCP, and AGit writes without I/O."""

from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path

import pytest

from omnigent.git_providers import forgejo as descriptor
from omnigent.git_providers import reset_for_tests
from omnigent.runner.pr_observer import extract_prs, observe_tool_completion
from omnigent.runner.session_prs import SessionPrRegistry

HOST = "git.example.test:3443"
URL = f"https://{HOST}/team/app/pulls/7"
OTHER = f"https://{HOST}/team/app/pulls/99"
CODEBERG = "https://codeberg.org/team/app/pulls/7"


@pytest.fixture(autouse=True)
def offline(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "gh"))
    monkeypatch.setenv("GLAB_CONFIG_DIR", str(tmp_path / "glab"))
    monkeypatch.setenv("OMNIGENT_GIT_PROVIDER_FORGEJO_HOSTS", HOST)
    monkeypatch.delenv("GH_HOST", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(descriptor.sys, "platform", "linux")
    descriptor._login_authorities.cache_clear()

    def forbidden(*args, **kwargs):
        pytest.fail("the PR observer must not invoke a command or connect to a service")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    reset_for_tests()
    yield
    reset_for_tests()


def shell(command: str, stdout: object = "", *, stream: str = "output", **status: object):
    return extract_prs(
        "exec_command",
        {"cmd": command},
        {
            stream: stdout if isinstance(stdout, str) else json.dumps(stdout),
            "exit_code": 0,
            **status,
        },
    )


def urls(found: tuple[list, bool]) -> tuple[list[str], bool]:
    references, created = found
    assert all(ref.provider == "forgejo" for ref in references)
    return [ref.url for ref in references], created


def agit_banner(url: str = URL, status: str = "[new reference]") -> str:
    return (
        "remote: \nremote: Visit the existing pull request:        \n"
        f"remote:   {url}        \nremote: \nTo https://{HOST}/team/app.git\n"
        f" * {status}   HEAD -> refs/pull/7/head\n"
    )


@pytest.mark.parametrize(
    "command",
    [
        "git push origin HEAD:refs/for/main -o topic=feature -o title=Feature",
        "git -C /other/worktree push origin HEAD:refs/for/main/feature",
        "git push --push-option=topic=x origin feature:refs/for/main",
        "sh -c 'cd /other && git push origin HEAD:refs/for/main -o topic=x'",
    ],
)
@pytest.mark.parametrize("stream", ["output", "stderr"])
def test_agit_push_tracks_the_reported_pull_request(command: str, stream: str) -> None:
    assert urls(shell(command, agit_banner(), stream=stream)) == ([URL], False)


@pytest.mark.parametrize(
    "command,output",
    [
        ("git push origin feature", agit_banner()),
        ("git push --dry-run origin HEAD:refs/for/main", agit_banner()),
        (
            "git push origin HEAD:refs/for/main",
            agit_banner() + "error: failed to push some refs\n",
        ),
        (
            "git push origin HEAD:refs/for/main",
            "remote: Create a new pull request for 'x':\n"
            f"remote:   https://{HOST}/team/app/compare/main...x\n",
        ),
        ("git push origin HEAD:refs/for/main", agit_banner("https://unknown.test/o/r/pulls/1")),
        ("git push origin HEAD:refs/for/main && git push mirror main", agit_banner()),
    ],
)
def test_ordinary_failed_or_unowned_pushes_do_not_track(command: str, output: str) -> None:
    assert shell(command, output) == ([], False)


@pytest.mark.parametrize(
    "command,expected",
    [
        (f"fj -H {HOST} pr create --repo team/app 'Feature' --body x", URL),
        (f"fj pr create -r {HOST}/team/app -A", URL),
        ("fj pr create -r codeberg.org/team/app --body-file b.md T", CODEBERG),
    ],
)
def test_fj_create_combines_the_named_repository_with_the_printed_number(
    command: str, expected: str
) -> None:
    printed = f"created pull request #7: Feature\nbody mentions {OTHER}\n"
    assert urls(shell(command, printed)) == ([expected], True)


@pytest.mark.parametrize(
    "command",
    [
        "fj pr create 'Feature' --body x",
        f"fj -H {HOST} pr create 'Feature' --body x",
        f"fj pr create --agit -r {HOST}/team/app 'Feature'",
        f"fj pr create --web -r {HOST}/team/app",
        f"fj pr create --help -r {HOST}/team/app",
    ],
)
def test_fj_create_without_a_full_identity_records_nothing(command: str) -> None:
    printed = "created pull request: Feature\n" if "--agit" in command else f"{OTHER}\n"
    assert shell(command, printed)[0] == []


@pytest.mark.parametrize(
    "command",
    [
        f"fj pr merge '{HOST}/team/app#7' -M squash -d",
        f"fj -H {HOST} pr merge 'team/app#7' -m",
        f"fj pr -H {HOST} close 'team/app#7' --with-msg 'superseded'",
        f"fj -H {HOST} pr edit 'team/app#7' title 'New title'",
        f"fj -H {HOST} pr edit 'team/app#7' labels --add bug",
        f"fj -H {HOST} pr assign -p 'team/app#7' alice bob",
    ],
)
def test_fj_mutations_track_their_explicit_target(command: str) -> None:
    assert urls(shell(command, f"{OTHER}\n")) == ([URL], False)


@pytest.mark.parametrize(
    "command",
    [
        f"fj -H {HOST} pr view 'team/app#7'",
        f"fj -H {HOST} pr comment 'team/app#7' 'hello'",
        f"fj -H {HOST} pr edit 'team/app#7' comment 3 'fixed'",
        f"fj -H {HOST} pr status 'team/app#7'",
        "fj pr merge 7",
        f"fj -H {HOST} pr merge 'team/app#7' --help",
        f"fj -H {HOST} repo view team/app",
    ],
)
def test_fj_reads_comments_and_unnamed_targets_do_not_track(command: str) -> None:
    assert shell(command, f"{URL}\n") == ([], False)


@pytest.mark.parametrize(
    "command,created",
    [
        ("tea pulls create --title T --description D --head feature", True),
        ("tea pr c -t T", True),
        ("tea pr edit 7 --add-labels bug", False),
        ("tea pr approve 7", False),
        ("tea pulls --login work create --title T", True),
    ],
)
def test_tea_writes_track_the_url_tea_prints_last(command: str, created: bool) -> None:
    printed = f"  # #7 Feature (open)\n\n  See {OTHER}\n\n  {URL}\n"
    assert urls(shell(command, printed)) == ([URL], created)


def test_tea_json_output_uses_the_returned_url() -> None:
    printed = {"index": 7, "title": "T", "url": URL, "state": "open"}
    assert urls(shell("tea pr create --title T --output json", printed)) == ([URL], True)


def test_tea_merge_with_a_pull_request_url_tracks_it() -> None:
    assert urls(shell(f"tea pr merge {URL} --style squash")) == ([URL], False)


@pytest.mark.parametrize(
    "command",
    [
        "tea pulls",
        "tea pr 7",
        "tea pr list --state all",
        "tea pr checkout 7",
        "tea pr review-comments 7",
        "tea pr create --title T --help",
        "tea pr merge 7",
        "tea issues create --title T",
    ],
)
def test_tea_reads_and_silent_writes_do_not_track(command: str) -> None:
    assert shell(command, f"{URL}\n")[0] == []


def test_a_read_beside_a_write_keeps_shared_output_ambiguous() -> None:
    command = f"tea pr create --title T && fj -H {HOST} pr view 'team/app#9'"
    assert shell(command, f"{URL}\n")[0] == []


def _mcp_result(payload: object) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


def test_forgejo_mcp_review_approval_tracks_the_reviewed_pull_request() -> None:
    review = {"id": 3, "html_url": f"{URL}#issuecomment-6", "pull_request_url": URL}
    arguments = {"owner": "Team", "repo": "App", "index": 7, "state": "APPROVED"}
    found = extract_prs("mcp__forgejo__create_pull_review", arguments, _mcp_result(review))
    assert urls(found) == ([URL], False)
    comment = {**arguments, "state": "COMMENT"}
    assert extract_prs("mcp__forgejo__submit_pull_review", comment, _mcp_result(review)) == (
        [],
        False,
    )


def test_gitea_mcp_write_creates_by_method() -> None:
    pull = {"number": 7, "html_url": URL, "body": OTHER}
    create = {"method": "create", "owner": "team", "repo": "app", "title": "T"}
    assert urls(extract_prs("mcp__gitea__pull_request_write", create, _mcp_result(pull))) == (
        [URL],
        True,
    )
    merge = {"method": "merge", "owner": "team", "repo": "app", "pull_number": 7}
    assert urls(extract_prs("pull_request_write", merge, _mcp_result(pull))) == ([URL], False)


@pytest.mark.parametrize(
    "arguments,payload",
    [
        ({"owner": "team", "repo": "other", "index": 7}, {"html_url": URL}),
        ({"owner": "team", "repo": "app", "index": 8}, {"html_url": URL}),
        (
            {"owner": "team", "repo": "app", "index": 1},
            {"html_url": "https://unknown.test/o/r/pulls/1"},
        ),
        ({"owner": "team", "repo": "app"}, {"body": URL}),
    ],
)
def test_mcp_results_must_name_the_requested_forgejo_pull_request(
    arguments: dict, payload: dict
) -> None:
    result = _mcp_result(payload)
    assert extract_prs("mcp__forgejo__create_review_requests", arguments, result) == ([], False)


def test_forgejo_mcp_create_sharing_githubs_tool_name_records_the_forgejo_url() -> None:
    pull = {"number": 7, "html_url": URL, "body": OTHER}
    arguments = {"owner": "team", "repo": "app", "head": "f", "base": "main", "title": "T"}
    assert urls(
        extract_prs("mcp__forgejo__create_pull_request", arguments, _mcp_result(pull))
    ) == (
        [URL],
        True,
    )


def test_observed_pull_request_persists_as_created() -> None:
    observe_tool_completion(
        "session",
        tool_name="exec_command",
        arguments={"cmd": "tea pr create --title T"},
        result={"output": f"{URL}\n", "exit_code": 0},
        call_id="call-1",
    )
    [entry] = SessionPrRegistry("session").list()
    assert (entry.provider, entry.url, entry.relationship) == ("forgejo", URL, "created")
