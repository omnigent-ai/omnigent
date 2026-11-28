"""GitLab transports keep credentials host-bound and share one request deadline."""

from __future__ import annotations

import asyncio
import json
import socket
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import httpx
import pytest

from omnigent.runner import gitlab_client as module
from omnigent.runner.gitlab_client import GitLabClient, GitLabError, GitLabTimeoutError
from omnigent.util.tls import client_ssl_context


@pytest.fixture(autouse=True)
def isolated_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GITLAB_TOKEN", "GLAB_TOKEN", "GITLAB_HOST", "GLAB_HOST"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def token_api(monkeypatch: pytest.MonkeyPatch) -> tuple[Mock, Mock]:
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    monkeypatch.setattr("shutil.which", lambda _name: None)
    monkeypatch.setattr(
        module.subprocess,
        "run",
        Mock(return_value=subprocess.CompletedProcess([], 0, b"{}", b"")),
    )
    respond = Mock(return_value=httpx.Response(200, json={"iid": 7}))
    real_client = httpx.AsyncClient
    factory = Mock(
        side_effect=lambda **kwargs: real_client(transport=httpx.MockTransport(respond), **kwargs)
    )
    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return respond, factory


@pytest.mark.parametrize(
    "gitlab_host,glab_host,requested,expected",
    [
        (None, None, "gitlab.com", True),
        (None, None, "private.test", False),
        ("https://PRIVATE.test:8443/", None, "private.test:8443", True),
        ("private.test:8443", None, "private.test", False),
        (None, "private.test", "private.test", True),
        ("private.test", "other.test", "other.test", False),
        ("private.test", "other.test", "private.test", True),
        ("http://private.test", None, "private.test", False),
    ],
)
def test_environment_token_is_bound_to_one_instance(
    monkeypatch: pytest.MonkeyPatch,
    gitlab_host: str | None,
    glab_host: str | None,
    requested: str,
    expected: bool,
) -> None:
    monkeypatch.setenv("GITLAB_TOKEN", "primary-token")
    monkeypatch.setenv("GLAB_TOKEN", "fallback-token")
    for name, value in (("GITLAB_HOST", gitlab_host), ("GLAB_HOST", glab_host)):
        if value is not None:
            monkeypatch.setenv(name, value)
    assert module.env_token_for_host(requested) == ("primary-token" if expected else None)
    monkeypatch.delenv("GITLAB_TOKEN")
    assert module.env_token_for_host(requested) == ("fallback-token" if expected else None)


def test_token_api_preserves_host_port_project_and_query(
    monkeypatch: pytest.MonkeyPatch, token_api: tuple[Mock, Mock]
) -> None:
    monkeypatch.setenv("GITLAB_HOST", "git.example.test:8443")
    respond, factory = token_api
    client = GitLabClient("/checkout", "git.example.test:8443", deadline=time.monotonic() + 3)
    assert client.object(
        "projects/team%2Fsub%2Frepo/merge_requests/7", source_branch="feat/new"
    ) == {"iid": 7}
    [request] = respond.call_args.args
    assert request.method == "GET"
    assert str(request.url) == (
        "https://git.example.test:8443/api/v4/projects/team%2Fsub%2Frepo/merge_requests/7"
        "?source_branch=feat%2Fnew"
    )
    assert request.headers["PRIVATE-TOKEN"] == "test-token"
    assert factory.call_args.kwargs["verify"] is client_ssl_context()
    assert factory.call_args.kwargs["follow_redirects"] is False
    assert 0 < request.extensions["timeout"]["read"] <= 3


@pytest.mark.parametrize("same_host", [True, False])
@pytest.mark.parametrize("token_name", ["GITLAB_TOKEN", "GLAB_TOKEN"])
def test_installed_glab_keeps_only_the_matching_environment_token(
    monkeypatch: pytest.MonkeyPatch, token_api: tuple[Mock, Mock], same_host: bool, token_name: str
) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/glab")
    monkeypatch.setenv("GLAB_TOKEN", "fallback-token")
    if token_name == "GLAB_TOKEN":
        monkeypatch.delenv("GITLAB_TOKEN")
    if not same_host:
        monkeypatch.setenv("GITLAB_HOST", "private.test")
    run = Mock(return_value=subprocess.CompletedProcess([], 0, b'{"iid": 7}', b""))
    monkeypatch.setattr(module.subprocess, "run", run)
    assert GitLabClient("/checkout", "gitlab.com").object("user") == {"iid": 7}
    env = run.call_args.kwargs["env"]
    expected_token = "test-token" if token_name == "GITLAB_TOKEN" else "fallback-token"
    assert env.get("GITLAB_TOKEN") == (expected_token if same_host else None)
    assert env.get("GLAB_TOKEN") == ("fallback-token" if same_host else None)
    token_api[0].assert_not_called()


@pytest.mark.parametrize("status", [302, 401, 403, 404, 500])
def test_token_errors_and_redirects_do_not_expose_or_forward_credentials(
    token_api: tuple[Mock, Mock], status: int
) -> None:
    respond, _ = token_api
    respond.return_value = httpx.Response(
        status, text="secret-response", headers={"Location": "https://other.test/api/v4/user"}
    )
    with pytest.raises(GitLabError) as error:
        GitLabClient("/checkout", "gitlab.com").object("user")
    assert "test-token" not in str(error.value)
    assert "secret-response" not in str(error.value)
    assert respond.call_count == 1
    assert respond.call_args.args[0].url.host == "gitlab.com"


@pytest.mark.parametrize(
    "failure", ["json", "network", "timeout", "deadline", "connecting", "progressing"]
)
def test_token_failures_are_public_errors(token_api: tuple[Mock, Mock], failure: str) -> None:
    respond, _ = token_api
    client = GitLabClient("/checkout", "gitlab.com")
    expected = GitLabError
    if failure == "json":
        respond.return_value = httpx.Response(200, text="secret-response")
    elif failure == "network":
        respond.side_effect = httpx.ConnectError("test-token")
    elif failure == "timeout":
        respond.side_effect = httpx.ReadTimeout("test-token")
        expected = GitLabTimeoutError
    elif failure in {"connecting", "progressing"}:

        class ProgressingResponse(httpx.AsyncByteStream):
            delivered = 0
            closed = False

            async def __aiter__(self):
                for _ in range(100):
                    await asyncio.sleep(0.01)
                    self.delivered += 1
                    yield b" "
                yield b"{}"

            async def aclose(self):
                self.closed = True

        stream = ProgressingResponse()

        async def connect(_request):
            try:
                await asyncio.sleep(1)
                return httpx.Response(200, json={})
            finally:
                await stream.aclose()

        respond.return_value = httpx.Response(200, stream=stream)
        if failure == "connecting":
            respond.side_effect = connect
        client.deadline = time.monotonic() + 0.05
        expected = GitLabTimeoutError
    else:
        client.deadline = time.monotonic() - 1
        expected = GitLabTimeoutError
    with pytest.raises(expected) as error:
        client.object("user")
    assert "test-token" not in str(error.value)
    assert "secret-response" not in str(error.value)
    assert respond.call_count == (0 if failure == "deadline" else 1)
    if failure in {"connecting", "progressing"}:
        assert stream.closed
        assert stream.delivered < 100


def test_token_deadline_does_not_wait_for_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITLAB_TOKEN", "test-token")
    monkeypatch.setattr("shutil.which", lambda _name: None)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: real_client(trust_env=False, **kwargs)
    )
    started, release = threading.Event(), threading.Event()

    def resolve(*_args, **_kwargs):
        started.set()
        release.wait(2)
        return []

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    client = GitLabClient("/checkout", "gitlab.com", deadline=time.monotonic() + 0.2)
    with ThreadPoolExecutor(max_workers=1) as executor:
        request = executor.submit(client.object, "user")
        try:
            assert started.wait(1)
            with pytest.raises(GitLabTimeoutError):
                request.result(timeout=1)
        finally:
            release.set()


def test_token_pagination_keeps_prior_page_on_timeout(token_api: tuple[Mock, Mock]) -> None:
    respond, _ = token_api
    first = [{"id": i} for i in range(100)]
    respond.side_effect = [httpx.Response(200, json=first), httpx.ReadTimeout("timeout")]
    assert GitLabClient("/checkout", "gitlab.com").pages("projects/10/notes") == (first, True)
    assert [call.args[0].url.params["page"] for call in respond.call_args_list] == ["1", "2"]


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
def test_refuses_arbitrary_request_destinations(path: str, token_api: tuple[Mock, Mock]) -> None:
    with pytest.raises(GitLabError, match="Invalid GitLab API path"):
        GitLabClient("/checkout", "gitlab.com").get(path)
    token_api[0].assert_not_called()


def test_untrusted_instance_is_rejected_before_cli() -> None:
    with pytest.raises(GitLabError, match="Configure"):
        GitLabClient("/checkout", "other.test")


@pytest.mark.parametrize("expired", [True, False], ids=["deadline", "subprocess"])
def test_timeouts_are_public_errors_without_extra_requests(
    monkeypatch: pytest.MonkeyPatch, expired: bool
) -> None:
    run = Mock(side_effect=subprocess.TimeoutExpired(["glab"], 8))
    monkeypatch.setattr(module.subprocess, "run", run)
    deadline = time.monotonic() + (-1 if expired else 8)
    with pytest.raises(ValueError, match=r"GitLab request timed out\. Refresh to retry\."):
        GitLabClient("/checkout", "gitlab.com", deadline=deadline).get("user")
    assert run.call_count == (0 if expired else 1)


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
    get.side_effect = [[{"id": i} for i in range(100)], GitLabTimeoutError()]
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


@pytest.mark.parametrize("expired", [True, False], ids=["deadline", "subprocess"])
def test_timeout_after_a_page_preserves_prior_items(
    monkeypatch: pytest.MonkeyPatch, expired: bool
) -> None:
    client = GitLabClient("/checkout", "gitlab.com")
    first = [{"id": i} for i in range(100)]

    def first_page(*_args, **_kwargs):
        if expired:
            client.deadline = time.monotonic() - 1
        run.side_effect = subprocess.TimeoutExpired(["glab"], 8)
        return subprocess.CompletedProcess([], 0, json.dumps(first).encode(), b"")

    run = Mock(side_effect=first_page)
    monkeypatch.setattr(module.subprocess, "run", run)

    assert client.pages("projects/10/merge_requests/7/notes") == (first, True)
    assert run.call_count == (1 if expired else 2)
