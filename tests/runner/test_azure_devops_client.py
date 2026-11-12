"""Tests for :mod:`omnigent.runner.azure_devops_client`.

Nothing here reaches the network or runs a real ``az``. Every test replaces
``subprocess.run`` and the client talks to a recording :class:`httpx.MockTransport`.
"""

from __future__ import annotations

import ast
import base64
import json
import subprocess
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.runner import azure_devops_client
from omnigent.runner.azure_devops_client import (
    AzureDevOpsClient,
    AzureDevOpsError,
    AzureToken,
    reset_token_cache,
    resolve_token,
)
from omnigent.util.tls import client_ssl_context
from tests.runner.azure_devops_fixtures import (
    RecordingTransport,
    assert_only_dev_azure_com,
    request_path,
    request_query,
)

pytest_plugins = ["tests.runner.azure_devops_fixtures"]

NOW = 1_800_000_000.0
AZ_PATH = "/usr/bin/az"
RESOURCE_ID = "499b84ac-1321-427f-aa17-267ca6975798"


# ---------------------------------------------------------------------------
# Token chain
# ---------------------------------------------------------------------------


class Clock:
    """A settable replacement for ``time.time``."""

    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeAz:
    """Replacement for ``subprocess.run`` that records each call and replays one outcome."""

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.outcome: subprocess.CompletedProcess[str] | Exception = az_ok()

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(argv), kwargs))
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def az_ok(**payload: Any) -> subprocess.CompletedProcess[str]:
    """Return a successful ``az account get-access-token`` result."""
    return subprocess.CompletedProcess([], 0, stdout=json.dumps(payload), stderr="")


def az_failed(returncode: int = 1) -> subprocess.CompletedProcess[str]:
    """Return a failed ``az`` result, as when nobody is signed in."""
    return subprocess.CompletedProcess([], returncode, stdout="", stderr="Please run 'az login'")


def _refuse_subprocess(*args: Any, **kwargs: Any) -> None:
    raise AssertionError(f"unexpected subprocess.run call: {args!r}")


def write_token_file(home: Path, content: object) -> None:
    """Write ``content`` (a string, or a value to dump as JSON) as the token file."""
    path = home / ".config" / "omnigent" / "azure-devops" / "token.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")


def make_executable(path: Path) -> Path:
    """Create an executable file at ``path`` and return it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture(autouse=True)
def home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Path]:
    """Start each test with no token file, no PAT, no ``az``, and no real subprocess."""
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.setenv("USERPROFILE", str(home_dir))
    monkeypatch.delenv("AZURE_DEVOPS_EXT_PAT", raising=False)
    monkeypatch.delenv("OMNIGENT_AZURE_DEVOPS_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setattr(azure_devops_client.shutil, "which", lambda _cmd, **_kw: None)
    monkeypatch.setattr(azure_devops_client, "_AZ_FALLBACK_PATHS", ())
    monkeypatch.setattr(azure_devops_client.subprocess, "run", _refuse_subprocess)
    reset_token_cache()
    yield home_dir
    reset_token_cache()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """Freeze ``time.time`` at :data:`NOW`."""
    fake = Clock()
    monkeypatch.setattr(azure_devops_client.time, "time", fake)
    return fake


@pytest.fixture
def fake_az(monkeypatch: pytest.MonkeyPatch) -> FakeAz:
    """Put ``az`` on PATH and route its invocations to a :class:`FakeAz`."""
    fake = FakeAz()
    monkeypatch.setattr(azure_devops_client.shutil, "which", lambda _cmd, **_kw: AZ_PATH)
    monkeypatch.setattr(azure_devops_client.subprocess, "run", fake)
    return fake


def test_token_file_beats_pat_and_az(
    home: Path, clock: Clock, fake_az: FakeAz, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_token_file(home, {"access_token": "file-token", "expires_at": NOW + 3600})
    monkeypatch.setenv("AZURE_DEVOPS_EXT_PAT", "pat-token")
    fake_az.outcome = az_ok(accessToken="az-token", expires_on=NOW + 3600)

    assert resolve_token() == AzureToken("file-token", "bearer", NOW + 3600)
    assert fake_az.calls == []


@pytest.mark.parametrize("expires_at", [NOW - 60, NOW])
def test_expired_token_file_is_ignored(
    home: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch, expires_at: float
) -> None:
    write_token_file(home, {"access_token": "file-token", "expires_at": expires_at})
    monkeypatch.setenv("AZURE_DEVOPS_EXT_PAT", "pat-token")

    assert resolve_token() == AzureToken("pat-token", "pat", None)


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        {"expires_at": NOW + 3600},
        {"access_token": "", "expires_at": NOW + 3600},
        {"access_token": 123, "expires_at": NOW + 3600},
        {"access_token": "file-token"},
        {"access_token": "file-token", "expires_at": "soon"},
        {"access_token": "file-token", "expires_at": True},
        '{"access_token": "file-token", "expires_at": NaN}',
    ],
    ids=[
        "invalid-json",
        "not-an-object",
        "no-token",
        "empty-token",
        "token-not-a-string",
        "no-expiry",
        "expiry-not-a-number",
        "expiry-is-a-bool",
        "expiry-is-nan",
    ],
)
def test_malformed_token_file_is_ignored(
    home: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch, content: object
) -> None:
    write_token_file(home, content)
    monkeypatch.setenv("AZURE_DEVOPS_EXT_PAT", "pat-token")

    assert resolve_token() == AzureToken("pat-token", "pat", None)


def test_pat_beats_az(fake_az: FakeAz, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_DEVOPS_EXT_PAT", "pat-token")
    fake_az.outcome = az_ok(accessToken="az-token", expires_on=NOW + 3600)

    token = resolve_token()

    assert token == AzureToken("pat-token", "pat", None)
    assert token is not None and token.expires_at is None
    assert fake_az.calls == []


def test_pat_is_stripped_and_follows_env_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AZURE_DEVOPS_EXT_PAT", "  first\n")
    assert resolve_token() == AzureToken("first", "pat", None)

    monkeypatch.setenv("AZURE_DEVOPS_EXT_PAT", "second")
    assert resolve_token() == AzureToken("second", "pat", None)

    monkeypatch.setenv("AZURE_DEVOPS_EXT_PAT", "   ")
    assert resolve_token() is None


def test_az_command_line_and_epoch_expiry(clock: Clock, fake_az: FakeAz) -> None:
    fake_az.outcome = az_ok(
        accessToken="az-token", expires_on=NOW + 3600, expiresOn="2026-09-29 18:12:43.000000"
    )

    assert resolve_token() == AzureToken("az-token", "bearer", NOW + 3600)

    [(argv, kwargs)] = fake_az.calls
    assert argv == [
        AZ_PATH,
        "account",
        "get-access-token",
        "--resource",
        RESOURCE_ID,
        "-o",
        "json",
    ]
    assert kwargs == {"capture_output": True, "text": True, "timeout": 15.0, "check": False}


def test_az_timeout_follows_env(
    clock: Clock, fake_az: FakeAz, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIGENT_AZURE_DEVOPS_TIMEOUT_SECONDS", "4")
    fake_az.outcome = az_ok(accessToken="az-token", expires_on=NOW + 3600)

    resolve_token()

    assert fake_az.calls[0][1]["timeout"] == 4.0


@pytest.mark.parametrize("text", ["2026-09-29 18:12:43.000000", "2026-09-29 18:12:43"])
def test_az_local_time_expiry_string(clock: Clock, fake_az: FakeAz, text: str) -> None:
    expected = time.mktime((2026, 9, 29, 18, 12, 43, 0, 0, -1))
    clock.now = expected - 3600
    fake_az.outcome = az_ok(accessToken="az-token", expiresOn=text)

    assert resolve_token() == AzureToken("az-token", "bearer", expected)


def test_az_token_is_reused_until_five_minutes_before_expiry(
    clock: Clock, fake_az: FakeAz
) -> None:
    expiry = NOW + 3600
    fake_az.outcome = az_ok(accessToken="az-token", expires_on=expiry)
    first = resolve_token()

    clock.now = expiry - 301
    assert resolve_token() == first
    assert len(fake_az.calls) == 1

    clock.now = expiry - 299
    assert resolve_token() == first
    assert len(fake_az.calls) == 2


def test_az_token_without_expiry_is_reused_for_five_minutes(clock: Clock, fake_az: FakeAz) -> None:
    fake_az.outcome = az_ok(accessToken="az-token")
    assert resolve_token() == AzureToken("az-token", "bearer", None)

    clock.now = NOW + 299
    resolve_token()
    assert len(fake_az.calls) == 1

    clock.now = NOW + 301
    resolve_token()
    assert len(fake_az.calls) == 2


@pytest.mark.parametrize("remaining", [30, 100, 300])
def test_near_expiry_az_token_is_reused_without_passing_its_expiry(
    clock: Clock, fake_az: FakeAz, remaining: int
) -> None:
    expiry = NOW + remaining
    fake_az.outcome = az_ok(accessToken="az-token", expires_on=expiry)
    token = AzureToken("az-token", "bearer", expiry)
    assert resolve_token() == token
    retry = min(expiry, NOW + 60)
    clock.now = retry - 1
    assert resolve_token() == token
    assert resolve_token() == token
    assert len(fake_az.calls) == 1

    clock.now = retry
    assert resolve_token() == (token if retry < expiry else None)
    assert len(fake_az.calls) == 2
    clock.now = expiry
    assert resolve_token() is None

    failed_probes = len(fake_az.calls)
    clock.now = expiry + 59
    assert resolve_token() is None
    assert len(fake_az.calls) == failed_probes
    fake_az.outcome = az_ok(accessToken="renewed-token", expires_on=expiry + 3600)
    clock.now = expiry + 60
    assert resolve_token() == AzureToken("renewed-token", "bearer", expiry + 3600)
    assert len(fake_az.calls) == failed_probes + 1


def test_az_failure_is_cached_for_a_minute_then_retried(clock: Clock, fake_az: FakeAz) -> None:
    fake_az.outcome = az_failed()
    assert resolve_token() is None

    clock.now = NOW + 59
    assert resolve_token() is None
    assert len(fake_az.calls) == 1

    fake_az.outcome = az_ok(accessToken="az-token", expires_on=NOW + 7200)
    clock.now = NOW + 61
    assert resolve_token() == AzureToken("az-token", "bearer", NOW + 7200)
    assert len(fake_az.calls) == 2


@pytest.mark.parametrize(
    "outcome",
    [
        az_failed(2),
        subprocess.TimeoutExpired(["az"], 15),
        FileNotFoundError("az"),
        subprocess.CompletedProcess([], 0, stdout="not json", stderr=""),
        subprocess.CompletedProcess([], 0, stdout="[]", stderr=""),
        az_ok(expires_on=NOW + 3600),
        az_ok(accessToken="", expires_on=NOW + 3600),
        az_ok(accessToken=5, expires_on=NOW + 3600),
        az_ok(accessToken="expired-token", expires_on=NOW - 1),
        az_ok(accessToken="expired-token", expires_on=NOW),
    ],
    ids=[
        "non-zero-exit",
        "timeout",
        "cannot-spawn",
        "bad-json",
        "json-not-an-object",
        "no-access-token",
        "empty-access-token",
        "access-token-not-a-string",
        "expired-access-token",
        "access-token-expiring-now",
    ],
)
def test_az_failures_yield_no_token(
    clock: Clock, fake_az: FakeAz, outcome: subprocess.CompletedProcess[str] | Exception
) -> None:
    fake_az.outcome = outcome

    assert resolve_token() is None


def test_reset_token_cache_forces_a_new_probe(clock: Clock, fake_az: FakeAz) -> None:
    fake_az.outcome = az_ok(accessToken="az-token", expires_on=NOW + 3600)
    resolve_token()
    resolve_token()
    assert len(fake_az.calls) == 1

    reset_token_cache()
    resolve_token()
    assert len(fake_az.calls) == 2


def test_token_file_written_after_a_failed_probe_is_used_at_once(
    home: Path, clock: Clock, fake_az: FakeAz
) -> None:
    fake_az.outcome = az_failed()
    assert resolve_token() is None

    write_token_file(home, {"access_token": "file-token", "expires_at": NOW + 3600})

    assert resolve_token() == AzureToken("file-token", "bearer", NOW + 3600)
    assert len(fake_az.calls) == 1


def test_homebrew_path_is_used_when_which_fails(
    clock: Clock, fake_az: FakeAz, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    homebrew = make_executable(tmp_path / "homebrew" / "az")
    usr_local = make_executable(tmp_path / "usr-local" / "az")
    monkeypatch.setattr(azure_devops_client.shutil, "which", lambda _cmd, **_kw: None)
    monkeypatch.setattr(azure_devops_client, "_AZ_FALLBACK_PATHS", (str(homebrew), str(usr_local)))
    fake_az.outcome = az_ok(accessToken="az-token", expires_on=NOW + 3600)

    assert resolve_token() == AzureToken("az-token", "bearer", NOW + 3600)
    assert fake_az.calls[0][0][0] == str(homebrew)


@pytest.mark.posix_only
def test_fallback_skips_missing_and_non_executable_paths(
    clock: Clock, fake_az: FakeAz, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    not_executable = tmp_path / "plain" / "az"
    not_executable.parent.mkdir()
    not_executable.write_text("", encoding="utf-8")
    not_executable.chmod(0o644)
    usr_local = make_executable(tmp_path / "usr-local" / "az")
    monkeypatch.setattr(azure_devops_client.shutil, "which", lambda _cmd, **_kw: None)
    monkeypatch.setattr(
        azure_devops_client,
        "_AZ_FALLBACK_PATHS",
        (str(tmp_path / "missing" / "az"), str(not_executable), str(usr_local)),
    )
    fake_az.outcome = az_ok(accessToken="az-token", expires_on=NOW + 3600)

    resolve_token()

    assert fake_az.calls[0][0][0] == str(usr_local)


def test_no_token_when_nothing_is_available(clock: Clock) -> None:
    # The autouse fixture leaves no file, PAT, or az, and fails on any subprocess call.
    assert resolve_token() is None


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

TOKEN = AzureToken("secret-token", "bearer")
REPO = "/contoso/Proj/_apis/git/repositories/repo"
PULL = f"{REPO}/pullrequests/42"
CHANGES = f"{PULL}/iterations/3/changes"
API = ("api-version", "7.1")
PREVIEW_API = ("api-version", "7.1-preview.1")


@pytest.fixture
def client(ado_transport: RecordingTransport) -> Iterator[AzureDevOpsClient]:
    with AzureDevOpsClient("contoso", TOKEN, transport=ado_transport) as ado:
        yield ado


@dataclass(frozen=True)
class Case:
    """One client method call with the request it must send and the value it returns."""

    call: Callable[[AzureDevOpsClient], Any]
    path: str
    query: list[tuple[str, str]]
    body: Any
    expected: Any


CONNECTION = {"authenticatedUser": {"id": "user-1"}, "providerDisplayName": "Pat Example"}
CASES = {
    "connection_data": Case(
        lambda c: c.connection_data(),
        "/contoso/_apis/connectionData",
        [PREVIEW_API],
        CONNECTION,
        CONNECTION,
    ),
    "find_pull_requests": Case(
        lambda c: c.find_pull_requests("Proj", "repo", "feat/x"),
        f"{REPO}/pullrequests",
        [
            ("searchCriteria.sourceRefName", "refs/heads/feat/x"),
            ("searchCriteria.status", "all"),
            API,
        ],
        {"count": 2, "value": [{"pullRequestId": 7}, {"pullRequestId": 9}]},
        [{"pullRequestId": 7}, {"pullRequestId": 9}],
    ),
    "find_pull_requests_active": Case(
        lambda c: c.find_pull_requests("Proj", "repo", "main", status="active"),
        f"{REPO}/pullrequests",
        [
            ("searchCriteria.sourceRefName", "refs/heads/main"),
            ("searchCriteria.status", "active"),
            API,
        ],
        {"value": []},
        [],
    ),
    "get_pull_request": Case(
        lambda c: c.get_pull_request("Proj", "repo", 42),
        PULL,
        [API],
        {"pullRequestId": 42, "title": "Fix it"},
        {"pullRequestId": 42, "title": "Fix it"},
    ),
    "iterations": Case(
        lambda c: c.iterations("Proj", "repo", 42),
        f"{PULL}/iterations",
        [API],
        {"count": 1, "value": [{"id": 3}]},
        [{"id": 3}],
    ),
    "iteration_changes": Case(
        lambda c: c.iteration_changes("Proj", "repo", 42, 3),
        CHANGES,
        [("$compareTo", "0"), ("$top", "100"), ("$skip", "0"), API],
        {"changeEntries": [{"changeId": 1}]},
        [{"changeId": 1}],
    ),
    "iteration_changes_compare_to": Case(
        lambda c: c.iteration_changes("Proj", "repo", 42, 3, compare_to=2),
        CHANGES,
        [("$compareTo", "2"), ("$top", "100"), ("$skip", "0"), API],
        {"changeEntries": []},
        [],
    ),
    "statuses": Case(
        lambda c: c.statuses("Proj", "repo", 42),
        f"{PULL}/statuses",
        [API],
        {"value": [{"state": "succeeded"}]},
        [{"state": "succeeded"}],
    ),
    "policy_evaluations": Case(
        lambda c: c.policy_evaluations("Proj", "proj-guid", 42),
        "/contoso/Proj/_apis/policy/evaluations",
        [("artifactId", "vstfs:///CodeReview/CodeReviewId/proj-guid/42"), PREVIEW_API],
        {"value": [{"status": "approved"}]},
        [{"status": "approved"}],
    ),
    "threads": Case(
        lambda c: c.threads("Proj", "repo", 42),
        f"{PULL}/threads",
        [API],
        {"value": [{"id": 1}]},
        [{"id": 1}],
    ),
    "item_content": Case(
        lambda c: c.item_content("Proj", "repo", "/src/a.py", "abc123"),
        f"{REPO}/items",
        [
            ("path", "/src/a.py"),
            ("versionDescriptor.versionType", "commit"),
            ("versionDescriptor.version", "abc123"),
            ("includeContent", "true"),
            ("$format", "json"),
            API,
        ],
        {"path": "/src/a.py", "content": "print(1)\n"},
        "print(1)\n",
    ),
}
ALL_CASES = pytest.mark.parametrize("case", CASES.values(), ids=CASES.keys())


@ALL_CASES
def test_method_sends_the_exact_request_and_returns_the_parsed_body(
    client: AzureDevOpsClient, ado_transport: RecordingTransport, case: Case
) -> None:
    ado_transport.route("GET", case.path, json=case.body)

    assert case.call(client) == case.expected

    [request] = ado_transport.requests
    assert request.method == "GET"
    assert request.url.host == "dev.azure.com"
    assert request_path(request) == case.path
    assert request_query(request) == case.query


@pytest.mark.parametrize("payload", [{}, None, {"value": "bad"}, {"value": [None]}])
def test_list_methods_reject_malformed_responses(
    client: AzureDevOpsClient, ado_transport: RecordingTransport, payload: object
) -> None:
    ado_transport.route("GET", f"{PULL}/iterations", json=payload)

    with pytest.raises(AzureDevOpsError, match=r"invalid list response|non-JSON response"):
        client.iterations("Proj", "repo", 42)


def test_bearer_token_header(client: AzureDevOpsClient, ado_transport: RecordingTransport) -> None:
    ado_transport.route("GET", "/contoso/_apis/connectionData", json={})

    client.connection_data()

    headers = ado_transport.requests[0].headers
    assert headers["Authorization"] == "Bearer secret-token"
    assert headers["Accept"] == "application/json"


def test_pat_uses_basic_auth_with_an_empty_user(ado_transport: RecordingTransport) -> None:
    ado_transport.route("GET", "/contoso/_apis/connectionData", json={})

    with AzureDevOpsClient("contoso", AzureToken("my-pat", "pat"), transport=ado_transport) as ado:
        ado.connection_data()

    expected = "Basic " + base64.b64encode(b":my-pat").decode("ascii")
    assert expected == "Basic Om15LXBhdA=="
    assert ado_transport.requests[0].headers["Authorization"] == expected


def test_unknown_token_kind_is_rejected(ado_transport: RecordingTransport) -> None:
    with pytest.raises(ValueError, match="oauth"):
        AzureDevOpsClient("contoso", AzureToken("t", "oauth"), transport=ado_transport)  # type: ignore[arg-type]


def test_path_segments_and_organization_are_percent_encoded(
    ado_transport: RecordingTransport,
) -> None:
    path = "/my%20org%2Fx/My%20Proj%3F/_apis/git/repositories/a%2Fb%23c/pullrequests/42"
    ado_transport.route("GET", path, json={"pullRequestId": 42})

    with AzureDevOpsClient("my org/x", TOKEN, transport=ado_transport) as ado:
        assert ado.get_pull_request("My Proj?", "a/b#c", 42) == {"pullRequestId": 42}

    [request] = ado_transport.requests
    assert request_path(request) == path
    assert request.url.host == "dev.azure.com"


def test_query_values_are_percent_encoded(
    client: AzureDevOpsClient, ado_transport: RecordingTransport
) -> None:
    ado_transport.route("GET", f"{REPO}/pullrequests", json={"value": []})

    client.find_pull_requests("Proj", "repo", "feat/a b#c")

    assert ado_transport.requests[0].url.query == (
        b"searchCriteria.sourceRefName=refs%2Fheads%2Ffeat%2Fa+b%23c"
        b"&searchCriteria.status=all&api-version=7.1"
    )


def test_item_content_returns_none_on_404(
    client: AzureDevOpsClient, ado_transport: RecordingTransport
) -> None:
    ado_transport.route(
        "GET", f"{REPO}/items", status=404, json={"message": "TF401174: item not found"}
    )

    assert client.item_content("Proj", "repo", "/gone.py", "abc123") is None


@pytest.mark.parametrize("body", [{}, {"content": None}, {"content": 5}, []])
def test_item_content_returns_none_without_text_content(
    client: AzureDevOpsClient, ado_transport: RecordingTransport, body: Any
) -> None:
    ado_transport.route("GET", f"{REPO}/items", json=body)

    assert client.item_content("Proj", "repo", "/dir", "abc123") is None


@pytest.mark.parametrize("status", [401, 403, 500])
@ALL_CASES
def test_error_status_raises(
    client: AzureDevOpsClient, ado_transport: RecordingTransport, case: Case, status: int
) -> None:
    ado_transport.route("GET", case.path, status=status)

    with pytest.raises(AzureDevOpsError) as excinfo:
        case.call(client)

    assert excinfo.value.status == status


@pytest.mark.parametrize(
    "case", [c for name, c in CASES.items() if name != "item_content"], ids=lambda c: c.path
)
def test_404_raises_except_for_item_content(
    client: AzureDevOpsClient, ado_transport: RecordingTransport, case: Case
) -> None:
    ado_transport.route("GET", case.path, status=404)

    with pytest.raises(AzureDevOpsError) as excinfo:
        case.call(client)

    assert excinfo.value.status == 404


def test_error_message_has_azure_devops_detail_and_no_token(
    client: AzureDevOpsClient, ado_transport: RecordingTransport
) -> None:
    ado_transport.route(
        "GET", PULL, status=404, json={"message": "TF401019: The Git repository does not exist"}
    )

    with pytest.raises(AzureDevOpsError) as excinfo:
        client.get_pull_request("Proj", "repo", 42)

    assert "TF401019" in str(excinfo.value)
    assert "secret-token" not in str(excinfo.value)


def test_non_json_success_body_raises(
    client: AzureDevOpsClient, ado_transport: RecordingTransport
) -> None:
    ado_transport.route("GET", PULL, status=203, text="<html>Sign In</html>")

    with pytest.raises(AzureDevOpsError, match="non-JSON") as excinfo:
        client.get_pull_request("Proj", "repo", 42)

    assert excinfo.value.status == 203


def test_redirects_are_not_followed(
    client: AzureDevOpsClient, ado_transport: RecordingTransport
) -> None:
    location = "https://vssps.dev.azure.com/_signin"
    ado_transport.route("GET", PULL, status=302, headers={"Location": location})

    with pytest.raises(AzureDevOpsError) as excinfo:
        client.get_pull_request("Proj", "repo", 42)

    assert excinfo.value.status == 302
    assert len(ado_transport.requests) == 1


def changes_pages(total: int) -> Callable[[httpx.Request], httpx.Response]:
    """Answer ``changes`` requests from a list of ``total`` entries, honoring $skip and $top."""

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request_query(request))
        skip, top = int(params["$skip"]), int(params["$top"])
        entries = [{"changeId": n} for n in range(skip, min(skip + top, total))]
        return httpx.Response(200, json={"changeEntries": entries})

    return handler


@pytest.mark.parametrize(("total", "pages"), [(0, 1), (99, 1), (100, 2), (205, 3)])
def test_iteration_changes_pages_until_a_short_page(
    client: AzureDevOpsClient, ado_transport: RecordingTransport, total: int, pages: int
) -> None:
    ado_transport.route("GET", CHANGES, handler=changes_pages(total))

    changes = client.iteration_changes("Proj", "repo", 42, 3)

    assert [change["changeId"] for change in changes] == list(range(total))
    queries = [dict(request_query(request)) for request in ado_transport.requests]
    assert [q["$skip"] for q in queries] == [str(100 * n) for n in range(pages)]
    assert {q["$top"] for q in queries} == {"100"}


def test_iteration_changes_stops_when_every_page_is_full(
    client: AzureDevOpsClient, ado_transport: RecordingTransport
) -> None:
    full_page = {"changeEntries": [{"changeId": n} for n in range(100)]}
    ado_transport.route("GET", CHANGES, json=full_page)

    with pytest.raises(AzureDevOpsError, match="page limit reached"):
        client.iteration_changes("Proj", "repo", 42, 3)

    assert len(ado_transport.requests) == azure_devops_client._MAX_CHANGE_PAGES


def test_change_pagination_follows_the_server_offset_after_a_short_page(
    client: AzureDevOpsClient, ado_transport: RecordingTransport
) -> None:
    def page(request: httpx.Request) -> httpx.Response:
        offset = int(dict(request_query(request))["$skip"])
        return httpx.Response(
            200,
            json={"changeEntries": [{"changeId": offset}], "nextSkip": 5 if offset == 0 else 0},
        )

    ado_transport.route("GET", CHANGES, handler=page)

    assert client.iteration_changes("Proj", "repo", 42, 3) == [{"changeId": 0}, {"changeId": 5}]
    assert [dict(request_query(request))["$skip"] for request in ado_transport.requests] == [
        "0",
        "5",
    ]


def test_repeated_change_pagination_stops_with_an_incomplete_result(
    client: AzureDevOpsClient, ado_transport: RecordingTransport
) -> None:
    ado_transport.route("GET", CHANGES, json={"changeEntries": [{"changeId": 1}], "nextSkip": 1})

    with pytest.raises(AzureDevOpsError, match="repeated a change page"):
        client.iteration_changes("Proj", "repo", 42, 3)
    assert len(ado_transport.requests) == 2


def test_iteration_change_pages_are_requested_one_at_a_time(
    client: AzureDevOpsClient, ado_transport: RecordingTransport
) -> None:
    ado_transport.route("GET", CHANGES, handler=changes_pages(205))

    pages = client.iteration_change_pages("Proj", "repo", 42, 3)

    assert ado_transport.requests == []
    assert len(next(pages)) == 100
    assert len(ado_transport.requests) == 1
    assert [len(page) for page in pages] == [100, 5]
    assert len(ado_transport.requests) == 3


@pytest.mark.parametrize(
    "organization_url",
    [
        "https://vssps.dev.azure.com/contoso",
        "https://example.com/contoso",
        "http://dev.azure.com/contoso",
    ],
)
def test_request_outside_dev_azure_com_raises_before_sending(
    client: AzureDevOpsClient, ado_transport: RecordingTransport, organization_url: str
) -> None:
    client._client.base_url = httpx.URL(organization_url)

    with pytest.raises(AzureDevOpsError) as excinfo:
        client.connection_data()

    assert excinfo.value.status == 0
    assert ado_transport.requests == []


def test_client_is_built_with_the_shared_tls_context_and_the_organization_url(
    monkeypatch: pytest.MonkeyPatch, ado_transport: RecordingTransport
) -> None:
    captured: dict[str, Any] = {}
    real_client = httpx.Client

    def spy(**kwargs: Any) -> httpx.Client:
        captured.update(kwargs)
        return real_client(**kwargs)

    monkeypatch.setattr(httpx, "Client", spy)

    AzureDevOpsClient("my org", TOKEN, transport=ado_transport).close()

    assert captured["base_url"] == "https://dev.azure.com/my%20org"
    assert captured["verify"] is client_ssl_context()
    assert captured["timeout"] == 15.0
    assert captured["transport"] is ado_transport
    assert captured["follow_redirects"] is False


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (None, 15.0),
        ("3", 3.0),
        ("0.5", 0.5),
        ("0", 15.0),
        ("-2", 15.0),
        ("abc", 15.0),
        ("nan", 15.0),
        ("inf", 15.0),
    ],
)
def test_request_timeout_follows_env(
    monkeypatch: pytest.MonkeyPatch,
    ado_transport: RecordingTransport,
    raw: str | None,
    expected: float,
) -> None:
    if raw is not None:
        monkeypatch.setenv("OMNIGENT_AZURE_DEVOPS_TIMEOUT_SECONDS", raw)

    with AzureDevOpsClient("contoso", TOKEN, transport=ado_transport) as ado:
        assert ado._client.timeout == httpx.Timeout(expected)


def test_each_request_times_out_at_the_deadline(ado_transport: RecordingTransport) -> None:
    ado_transport.route("GET", PULL, json={"pullRequestId": 42})
    deadline = time.monotonic() + 2.0

    with AzureDevOpsClient("contoso", TOKEN, deadline=deadline, transport=ado_transport) as ado:
        ado.get_pull_request("Proj", "repo", 42)
        ado.get_pull_request("Proj", "repo", 42)

    first, second = (request.extensions["timeout"] for request in ado_transport.requests)
    assert 0 < second["read"] <= first["read"] <= 2.0
    assert set(first.values()) == {first["read"]}


def test_a_distant_deadline_keeps_the_configured_timeout(
    ado_transport: RecordingTransport,
) -> None:
    ado_transport.route("GET", PULL, json={"pullRequestId": 42})
    deadline = time.monotonic() + 3600.0

    with AzureDevOpsClient("contoso", TOKEN, deadline=deadline, transport=ado_transport) as ado:
        ado.get_pull_request("Proj", "repo", 42)

    assert ado_transport.requests[0].extensions["timeout"]["read"] == 15.0


def test_a_request_after_the_deadline_is_not_sent(ado_transport: RecordingTransport) -> None:
    deadline = time.monotonic()

    with AzureDevOpsClient("contoso", TOKEN, deadline=deadline, transport=ado_transport) as ado:
        with pytest.raises(httpx.TimeoutException):
            ado.get_pull_request("Proj", "repo", 42)

    assert ado_transport.requests == []


def test_close_and_context_manager(ado_transport: RecordingTransport) -> None:
    ado = AzureDevOpsClient("contoso", TOKEN, transport=ado_transport)

    with ado as entered:
        assert entered is ado
        assert not ado._client.is_closed

    assert ado._client.is_closed
    ado.close()


def test_token_value_is_left_out_of_repr() -> None:
    assert "secret-token" not in repr(TOKEN)


def test_module_imports_nothing_from_the_provider_layer() -> None:
    tree = ast.parse(Path(azure_devops_client.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
            imported.update(f"{node.module}.{alias.name}" for alias in node.names)

    forbidden = ("omnigent.git_providers", "omnigent.runner.github_resource", "web")
    offenders = {
        name
        for name in imported
        for prefix in forbidden
        if name == prefix or name.startswith(f"{prefix}.")
    }
    assert not offenders


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def test_recording_transport_routes_by_method_and_path() -> None:
    transport = RecordingTransport()
    transport.route("GET", "/a%20b", json={"ok": True})

    with httpx.Client(transport=transport, base_url="https://dev.azure.com") as http:
        assert http.get("/a b").json() == {"ok": True}
        assert http.post("/a b").status_code == 404
        unrouted = http.get("/other")

    assert unrouted.status_code == 404
    assert "no test route" in unrouted.json()["message"]
    assert [(r.method, request_path(r)) for r in transport.requests] == [
        ("GET", "/a%20b"),
        ("POST", "/a%20b"),
        ("GET", "/other"),
    ]


def test_stray_host_check_flags_the_vssps_host() -> None:
    transport = RecordingTransport()
    with httpx.Client(transport=transport) as http:
        http.get("https://dev.azure.com/contoso/_apis/connectionData")
        assert_only_dev_azure_com(transport)
        http.get("https://vssps.dev.azure.com/contoso/_apis/identities")

    with pytest.raises(AssertionError, match=r"vssps\.dev\.azure\.com"):
        assert_only_dev_azure_com(transport)
