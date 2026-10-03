"""The glab subprocess is host-bound, read-only and limited by a shared deadline."""

from __future__ import annotations

import subprocess
import time
from unittest.mock import Mock

import pytest

from omnigent.runner import gitlab_client as module
from omnigent.runner.gitlab_client import GitLabClient, GitLabError


def test_api_command_preserves_host_port_and_escapes_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OMNIGENT_GIT_PROVIDER_GITLAB_HOSTS", "git.example.test:8443")
    run = Mock(return_value=subprocess.CompletedProcess([], 0, b'{"iid": 7}', b""))
    monkeypatch.setattr(module.subprocess, "run", run)
    client = GitLabClient("/checkout", "git.example.test:8443", deadline=time.monotonic() + 3)
    assert client.object(
        "projects/team%2Fsub%2Frepo/merge_requests/7", source_branch="feat/new"
    ) == {"iid": 7}
    argv = run.call_args.args[0]
    assert argv == [
        "glab",
        "api",
        "--method",
        "GET",
        "projects/team%2Fsub%2Frepo/merge_requests/7?source_branch=feat%2Fnew",
    ]
    assert run.call_args.kwargs["cwd"] == "/checkout"
    assert 0 < run.call_args.kwargs["timeout"] <= 3
    assert run.call_args.kwargs["stdin"] == subprocess.DEVNULL
    assert run.call_args.kwargs["env"]["GITLAB_HOST"] == "git.example.test:8443"
    assert run.call_args.kwargs["env"]["GLAB_NO_PROMPT"] == "1"


@pytest.mark.parametrize(
    "path", ["https://evil.test/api", "//evil.test", "-XDELETE", "projects/../user"]
)
def test_refuses_arbitrary_request_destinations(path: str) -> None:
    with pytest.raises(GitLabError, match="Invalid GitLab API path"):
        GitLabClient("/checkout", "gitlab.com").get(path)


def test_untrusted_instance_is_rejected_before_cli() -> None:
    with pytest.raises(GitLabError, match="Configure"):
        GitLabClient("/checkout", "other.test")


def test_no_request_after_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    run = Mock()
    monkeypatch.setattr(module.subprocess, "run", run)
    with pytest.raises(TimeoutError):
        GitLabClient("/checkout", "gitlab.com", deadline=time.monotonic() - 1).get("user")
    run.assert_not_called()


@pytest.mark.parametrize("code,body", [(1, b""), (0, b"<html>sign in</html>")])
def test_errors_do_not_expose_cli_output(
    monkeypatch: pytest.MonkeyPatch, code: int, body: bytes
) -> None:
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess([], code, body, b"secret-token"),
    )
    with pytest.raises(GitLabError) as error:
        GitLabClient("/checkout", "gitlab.com").get("user")
    assert "secret-token" not in str(error.value)


def test_paginated_lists_are_complete_until_cap_or_later_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GitLabClient("/checkout", "gitlab.com")
    get = Mock(side_effect=[[{"id": i} for i in range(100)], [{"id": 100}]])
    monkeypatch.setattr(client, "get", get)
    values, partial = client.pages("projects/1/merge_requests/1/notes")
    assert len(values) == 101 and not partial
    assert [call.kwargs["page"] for call in get.call_args_list] == [1, 2]
    get.side_effect = [[{"id": i} for i in range(100)], TimeoutError()]
    values, partial = client.pages("projects/1/merge_requests/1/notes")
    assert len(values) == 100 and partial
    monkeypatch.setattr(module, "_MAX_PAGES", 1)
    get.side_effect = [[{"id": i} for i in range(100)]]
    assert client.pages("projects/1/merge_requests/1/notes")[1]


def test_malformed_later_page_preserves_prior_items(monkeypatch: pytest.MonkeyPatch) -> None:
    client = GitLabClient("/checkout", "gitlab.com")
    first = [{"id": i} for i in range(100)]
    monkeypatch.setattr(client, "get", Mock(side_effect=[first, {"message": "invalid"}]))
    assert client.pages("projects/10/merge_requests/7/notes") == (first, True)
