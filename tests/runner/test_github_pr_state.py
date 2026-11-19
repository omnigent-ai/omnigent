"""The summary read of a tracked GitHub PR: one conditional ``gh api`` request.

The server's refresher keeps only a listed session's PR state, draft flag and title,
so it asks the runner or host for a summary instead of the panel's full read
(``gh auth status``, ``gh pr view`` with checks, reviews and comments, then GraphQL).
The responses below have the shape of ``gh api -i`` output, trimmed to the headers
and fields the summary reads.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

import pytest

from omnigent.host import frames
from omnigent.host.connect import HostProcess
from omnigent.runner import github_pr_state, github_resource, pr_resource
from omnigent.runner.session_prs import PullRequestRef, SessionPrRegistry
from omnigent.server import session_pull_requests as server_prs
from omnigent.workspace_fs import WorkspaceReader

URL = "https://github.com/example/repo/pull/42"
SESSION = "c0ffee00c0ffee00c0ffee00c0ffee00"
LAST_MODIFIED = "Tue, 17 Nov 2026 04:53:28 GMT"
LATER = "Tue, 17 Nov 2026 05:09:00 GMT"
PATH = "repos/example/repo/pulls/42?skip_mergeability_checks=true"

# ``gh api -i`` ends the status line with LF and each header with CRLF.
HEADERS_200 = (
    "HTTP/1.1 200 OK\n"
    "Content-Type: application/json; charset=utf-8\r\n"
    "Date: Tue, 17 Nov 2026 05:08:26 GMT\r\n"
    'Etag: "ec5e76d21d50ab5fee191df00082f8c9008ef4976a12cc63e275db33b4cf1355"\r\n'
    f"Last-Modified: {LAST_MODIFIED}\r\n"
    "X-Ratelimit-Remaining: 4903\r\n"
    "X-Ratelimit-Resource: core\r\n\r\n"
)
LATER_HEADERS_200 = HEADERS_200.replace(LAST_MODIFIED, LATER).replace("05:08:26", "05:10:00")
BODY_200 = {
    "number": 42,
    "state": "closed",
    "merged": True,
    "draft": False,
    "title": "Add a retry to the upload step",
    "updated_at": "2026-11-17T04:53:28Z",
}
# gh exits 1 on a 304 and writes ``gh: HTTP 304`` to stderr.
RESPONSE_304 = (
    1,
    "HTTP/1.1 304 Not Modified\n"
    "Date: Tue, 17 Nov 2026 05:08:33 GMT\r\n"
    'Etag: "329be3ee39c098b6ab6ad8f5ce127993ceac5fd85f1969888c84387a52b209e1"\r\n'
    f"Last-Modified: {LAST_MODIFIED}\r\n"
    "X-Ratelimit-Remaining: 4900\r\n"
    "X-Ratelimit-Resource: core\r\n\r\n",
    "gh: HTTP 304\n",
)
RESPONSE_404 = (
    1,
    "HTTP/1.1 404 Not Found\n"
    "Content-Type: application/json; charset=utf-8\r\n"
    "Date: Tue, 17 Nov 2026 05:08:33 GMT\r\n"
    "X-Ratelimit-Remaining: 4899\r\n"
    "X-Ratelimit-Resource: core\r\n\r\n"
    '{"message":"Not Found","status":"404"}',
    "gh: Not Found (HTTP 404)\n",
)


def _response_200(headers: str = HEADERS_200, **fields: Any) -> tuple[int, str, str]:
    return 0, headers + json.dumps({**BODY_200, **fields}), ""


class GhCalls:
    """Answers ``gh`` calls in order and records their arguments.

    ``during`` maps a call's index to work that runs while that call is in flight,
    standing in for a concurrent read of the same PR.
    """

    def __init__(self, *responses: tuple[int | None, str, str]) -> None:
        self.responses = list(responses)
        self.argvs: list[list[str]] = []
        self.during: dict[int, Any] = {}

    def __call__(self, argv: list[str], **_: Any) -> tuple[int | None, str, str]:
        index = len(self.argvs)
        self.argvs.append(argv)
        response = self.responses.pop(0)
        if index in self.during:
            self.during.pop(index)()
        return response


@pytest.fixture(autouse=True)
def _github(monkeypatch: pytest.MonkeyPatch) -> None:
    github_pr_state._CACHE.clear()
    monkeypatch.setattr(github_resource, "_in_sandbox", lambda *_: True)
    monkeypatch.setattr(github_resource, "_pr_token", lambda *_: None)
    yield
    github_pr_state._CACHE.clear()


def _read(monkeypatch: pytest.MonkeyPatch, gh: GhCalls, url: str = URL) -> dict[str, Any] | None:
    monkeypatch.setattr(github_resource, "_gh", gh)
    return github_pr_state.reference_state("/workspace", PullRequestRef.from_url(url))


def test_an_unchanged_pr_is_read_conditionally_and_reuses_its_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = GhCalls(_response_200(), RESPONSE_304)

    first, second = _read(monkeypatch, gh), _read(monkeypatch, gh)

    expected = {
        "number": 42,
        "url": URL,
        "title": "Add a retry to the upload step",
        "state": "MERGED",
        "is_draft": False,
    }
    assert first == expected
    assert second == expected
    assert gh.argvs[0] == ["api", "-i", "--hostname", "github.com", PATH]
    assert gh.argvs[1] == [
        "api",
        "-i",
        "--hostname",
        "github.com",
        "-H",
        f"If-Modified-Since: {LAST_MODIFIED}",
        PATH,
    ]


def test_a_changed_pr_replaces_the_cached_state(monkeypatch: pytest.MonkeyPatch) -> None:
    gh = GhCalls(
        _response_200(),
        _response_200(LATER_HEADERS_200, state="open", merged=False, draft=True, title="Reopened"),
        RESPONSE_304,
    )

    _read(monkeypatch, gh)
    changed, reused = _read(monkeypatch, gh), _read(monkeypatch, gh)

    assert changed == reused
    assert changed is not None
    assert (changed["state"], changed["is_draft"], changed["title"]) == ("OPEN", True, "Reopened")
    assert f"If-Modified-Since: {LATER}" in gh.argvs[2]


def test_a_pr_read_in_the_second_it_changed_is_not_reused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    same_second = HEADERS_200.replace("05:08:26", "04:53:28")
    gh = GhCalls(_response_200(same_second), _response_200())

    _read(monkeypatch, gh)
    _read(monkeypatch, gh)

    assert "-H" not in gh.argvs[1]


def test_open_and_closed_states_map_to_the_panel_names(monkeypatch: pytest.MonkeyPatch) -> None:
    gh = GhCalls(
        _response_200(state="open", merged=False),
        _response_200(state="closed", merged=False),
    )
    first = _read(monkeypatch, gh)
    github_pr_state._CACHE.clear()
    second = _read(monkeypatch, gh)

    assert first is not None and second is not None
    assert (first["state"], second["state"]) == ("OPEN", "CLOSED")


def test_github_errors_return_no_state_and_drop_the_validator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gh = GhCalls(_response_200(), RESPONSE_404, _response_200())

    _read(monkeypatch, gh)
    assert _read(monkeypatch, gh) is None
    _read(monkeypatch, gh)

    assert "-H" not in gh.argvs[2]


def test_a_304_without_a_cached_read_returns_no_state(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _read(monkeypatch, GhCalls(RESPONSE_304)) is None


def test_an_enterprise_host_without_an_account_is_not_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_account(*_: Any) -> list[str]:
        raise ValueError("sign in")

    monkeypatch.setattr(github_resource, "_host_args", no_account)
    gh = GhCalls()
    assert _read(monkeypatch, gh) is None
    assert gh.argvs == []


def test_the_cache_keeps_the_most_recently_read_prs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github_pr_state, "_CACHE_LIMIT", 2)
    urls = [f"https://github.com/example/repo/pull/{number}" for number in (1, 2, 3)]
    for url in urls:
        _read(monkeypatch, GhCalls(_response_200()), url)
    assert list(github_pr_state._CACHE) == urls[1:]


def test_a_304_returns_a_newer_state_a_concurrent_read_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _read(monkeypatch, GhCalls(_response_200()))
    gh = GhCalls(RESPONSE_304)
    newer = GhCalls(_response_200(LATER_HEADERS_200, title="Renamed"))
    gh.during[0] = lambda: _read(monkeypatch, newer)

    state = _read(monkeypatch, gh)

    assert state is not None and state["title"] == "Renamed"
    assert github_pr_state._CACHE[URL][0] == LATER


def test_an_older_response_finishing_last_keeps_the_newer_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    older = GhCalls(_response_200(title="Old title"))
    newer = GhCalls(_response_200(LATER_HEADERS_200, title="Renamed"))
    older.during[0] = lambda: _read(monkeypatch, newer)

    state = _read(monkeypatch, older)

    assert state is not None and state["title"] == "Renamed"
    assert github_pr_state._CACHE[URL] == (LATER, state)


def test_an_error_keeps_a_state_a_concurrent_read_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _read(monkeypatch, GhCalls(_response_200()))
    failing = GhCalls(RESPONSE_404)
    newer = GhCalls(_response_200(LATER_HEADERS_200, title="Renamed"))
    failing.during[0] = lambda: _read(monkeypatch, newer)

    assert _read(monkeypatch, failing) is None
    assert github_pr_state._CACHE[URL][0] == LATER


class _Registry:
    def __init__(self, session_id: str) -> None:
        del session_id

    def list(self) -> list[Any]:
        return []

    def update_titles(self, *_: Any, **__: Any) -> None:
        pass


def _pr_info(monkeypatch: pytest.MonkeyPatch, url: str, **kwargs: Any) -> dict[str, Any]:
    monkeypatch.setattr(pr_resource, "SessionPrRegistry", _Registry)
    with pr_resource.server_tracking(True, url):
        return pr_resource.pr_info("/workspace", session_id=SESSION, **kwargs)


def _fail(name: str) -> Any:
    def fail(*_: Any, **__: Any) -> Any:
        raise AssertionError(f"{name} ran")

    return fail


def test_a_summary_reads_only_the_selected_pr(monkeypatch: pytest.MonkeyPatch) -> None:
    state = {"number": 42, "url": URL, "title": "T", "state": "OPEN", "is_draft": False}
    read: list[PullRequestRef] = []

    def reference_state(root: str, reference: PullRequestRef) -> dict[str, Any]:
        read.append(reference)
        return state

    monkeypatch.setattr(github_pr_state, "reference_state", reference_state)
    monkeypatch.setattr(pr_resource, "_reference_info", _fail("the full read"))
    monkeypatch.setattr(pr_resource, "_workspace_providers", _fail("discovery"))

    info = _pr_info(monkeypatch, URL, summary=True)

    assert info == {
        "object": "session.github.info",
        "available": True,
        "provider": "github",
        "selected_pr_url": URL,
        "pr": state,
        "pr_states": [],
    }
    assert [reference.url for reference in read] == [URL]


def _tracked(url: str, provider: str = "github") -> pr_resource.SessionPullRequest:
    reference = PullRequestRef.from_url(url).model_copy(update={"provider": provider})
    return pr_resource.SessionPullRequest(
        **reference.model_dump(),
        relationship="created",
        source="runner",
        first_seen_at=0,
        last_seen_at=0,
    )


def _track(monkeypatch: pytest.MonkeyPatch, entries: list[pr_resource.SessionPullRequest]) -> None:
    class Registry(_Registry):
        def list(self) -> list[Any]:
            return entries

    monkeypatch.setattr(pr_resource, "SessionPrRegistry", Registry)


def test_a_summary_reads_every_tracked_pr_and_the_server_stores_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = "https://github.com/example/repo/pull/43"
    states = {
        URL: {"number": 42, "url": URL, "title": "T", "state": "OPEN", "is_draft": False},
        other: {"number": 43, "url": other, "title": "U", "state": "MERGED", "is_draft": False},
    }
    read: list[str] = []

    def reference_state(root: str, reference: PullRequestRef) -> dict[str, Any]:
        read.append(reference.url)
        return states[reference.url]

    class Store:
        def apply_report(self, *args: Any, **kwargs: Any) -> None:
            self.kwargs = kwargs

    monkeypatch.setattr(github_pr_state, "reference_state", reference_state)
    monkeypatch.setattr(pr_resource, "_reference_info", _fail("the full read"))
    entries = [_tracked(URL), _tracked(other)]
    monkeypatch.setattr(pr_resource, "_selected_pr", lambda *_: entries[0])
    _track(monkeypatch, entries)
    with pr_resource.server_tracking(True, URL):
        info = pr_resource.pr_info("/workspace", session_id=SESSION, summary=True)
    store = Store()
    server_prs.ingest_info(store, SESSION, info, requested_at=5.0)  # type: ignore[arg-type]

    assert read == [URL, other]
    assert info["pr_states"] == [states[other]]
    assert [(state.url, state.state) for state in store.kwargs["fetched"]] == [
        (other, "MERGED"),
        (URL, "OPEN"),
    ]


def test_a_summary_past_the_request_deadline_reads_no_pr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = "https://github.com/example/repo/pull/43"
    read: list[str] = []
    monkeypatch.setattr(
        github_pr_state, "reference_state", lambda _, reference: read.append(reference.url)
    )
    monkeypatch.setattr(pr_resource, "_PR_TITLE_REQUEST_SECONDS", -1.0)
    entries = [_tracked(URL), _tracked(other)]
    monkeypatch.setattr(pr_resource, "_selected_pr", lambda *_: entries[0])
    _track(monkeypatch, entries)
    with pr_resource.server_tracking(True, URL):
        info = pr_resource.pr_info("/workspace", session_id=SESSION, summary=True)

    assert read == []
    assert info["pr"] is None
    assert info["pr_states"] == []


class _Clock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now


@pytest.mark.parametrize("seconds_per_read", [1.0, 3.0, 9.0])
def test_summaries_cut_by_the_budget_read_every_tracked_pr_in_turn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, seconds_per_read: float
) -> None:
    # More sessions and PRs than any history shared between sessions would keep.
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    sessions = {
        f"{index:032x}": [
            f"https://github.com/example/repo/pull/{2 * index + offset}" for offset in (42, 43)
        ]
        for index in range(150)
    }
    for session_id, urls in sessions.items():
        references = [PullRequestRef.from_url(url) for url in urls]
        SessionPrRegistry(session_id).record(
            references, relationship="created", source="runner", timestamp=0
        )
    clock = _Clock(1_000.0)
    monkeypatch.setattr(pr_resource, "time", clock)
    read: dict[str, list[str]] = {session_id: [] for session_id in sessions}
    reading = ""

    def reference_state(_: str, reference: PullRequestRef) -> dict[str, Any]:
        read[reading].append(reference.url)
        clock.now += seconds_per_read
        return {"number": reference.number, "url": reference.url, "title": "T", "state": "OPEN"}

    monkeypatch.setattr(github_pr_state, "reference_state", reference_state)
    for _ in range(2):
        for session_id, urls in sessions.items():
            reading = session_id
            with pr_resource.server_tracking(True, urls[0]):
                pr_resource.pr_info("/workspace", session_id=session_id, summary=True)
            clock.now += 300.0

    for session_id, urls in sessions.items():
        assert set(read[session_id]) == set(urls)


def test_a_failed_summary_read_is_retried_as_a_timed_out_title(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    registry = SessionPrRegistry(SESSION)
    registry.record(
        [PullRequestRef.from_url(URL)], relationship="created", source="runner", timestamp=0
    )
    monkeypatch.setattr(pr_resource, "time", _Clock(1_000.0))
    monkeypatch.setattr(github_pr_state, "reference_state", lambda *_: None)
    with pr_resource.server_tracking(True, URL):
        pr_resource.pr_info("/workspace", session_id=SESSION, summary=True)

    [entry] = registry.list()
    assert (entry.title_checked_at, entry.title_lookup_timed_out) == (1_000.0, True)


def test_a_summary_bounds_each_github_call_by_its_request_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = [_tracked(URL), _tracked("https://github.com/example/repo/pull/43")]
    _track(monkeypatch, entries)
    monkeypatch.setattr(pr_resource, "_selected_pr", lambda *_: entries[0])
    clock = _Clock()
    monkeypatch.setattr(pr_resource, "time", clock)
    monkeypatch.setattr(github_resource, "time", clock)

    def slow_run(argv: list[str], *, cwd: str, timeout: float, env: Any) -> tuple[int, str, str]:
        # A response that takes six seconds unless the call's timeout is shorter.
        clock.now += min(6.0, timeout)
        return _response_200()

    monkeypatch.setattr(github_resource, "_run", slow_run)
    with pr_resource.server_tracking(True, URL):
        pr_resource.pr_info("/workspace", session_id=SESSION, summary=True)

    assert clock.now <= pr_resource._PR_TITLE_REQUEST_SECONDS


def test_a_session_also_tracking_another_provider_gets_the_full_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = [_tracked(URL), _tracked("https://github.com/example/repo/pull/43", "gitlab")]
    full: list[Any] = []
    monkeypatch.setattr(pr_resource, "_selected_pr", lambda *_: entries[0])
    monkeypatch.setattr(github_pr_state, "reference_state", _fail("the summary read"))
    monkeypatch.setattr(
        pr_resource, "_reference_info", lambda *args: full.append(args) or {"pr": None}
    )
    monkeypatch.setattr(pr_resource, "_session_prs_with_titles", lambda *_, **__: [])
    monkeypatch.setattr(pr_resource, "_workspace_providers", lambda *_: [])
    _track(monkeypatch, entries)
    with pr_resource.server_tracking(True, URL):
        info = pr_resource.pr_info("/workspace", session_id=SESSION, summary=True)

    assert len(full) == 1
    assert info["tracking_available"] is True


def test_a_pr_from_another_provider_gets_the_full_read(monkeypatch: pytest.MonkeyPatch) -> None:
    reference = PullRequestRef.from_url(URL).model_copy(update={"provider": "gitlab"})
    selected = pr_resource.SessionPullRequest(
        **reference.model_dump(),
        relationship="attached",
        source="server",
        first_seen_at=0,
        last_seen_at=0,
    )
    full: list[Any] = []
    monkeypatch.setattr(pr_resource, "_selected_pr", lambda *_: selected)
    monkeypatch.setattr(github_pr_state, "reference_state", _fail("the summary read"))
    monkeypatch.setattr(
        pr_resource, "_reference_info", lambda *args: full.append(args) or {"pr": None}
    )
    monkeypatch.setattr(pr_resource, "_session_prs_with_titles", lambda *_, **__: [])

    info = _pr_info(monkeypatch, URL, pr_url=URL, summary=True)

    assert len(full) == 1
    assert info["tracking_available"] is True


def test_a_summary_updates_only_the_stored_state(monkeypatch: pytest.MonkeyPatch) -> None:
    class Store:
        def apply_report(self, *args: Any, **kwargs: Any) -> None:
            self.call = (args, kwargs)

    store = Store()
    state = {"number": 42, "url": URL, "title": "T", "state": "MERGED", "is_draft": False}
    monkeypatch.setattr(github_pr_state, "reference_state", lambda *_: state)
    info = _pr_info(monkeypatch, URL, summary=True)
    server_prs.ingest_info(store, SESSION, info, requested_at=5.0)  # type: ignore[arg-type]

    args, kwargs = store.call
    assert args == (SESSION, [])
    assert kwargs["complete"] is False
    assert kwargs["fetched"] == [
        server_prs.FetchedState(url=URL, number=42, state="MERGED", is_draft=False, title="T")
    ]


def _host_read(monkeypatch: pytest.MonkeyPatch, params: dict[str, Any]) -> dict[str, Any]:
    """Send ``params`` through a host frame, as the server does when the runner is offline."""
    sent = frames.encode_host_frame(
        frames.HostFsRequestFrame(
            request_id="req_fs_1",
            op="github_info",
            workspace="/workspace",
            session_id=SESSION,
            params=params,
        )
    )
    frame = frames.decode_host_frame(sent)
    assert isinstance(frame, frames.HostFsRequestFrame)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(pr_resource, "pr_info", lambda *_, **kwargs: calls.append(kwargs) or {})
    with tempfile.TemporaryDirectory() as root:
        HostProcess._dispatch_fs_op(
            WorkspaceReader(Path(root)), frame.op, frame.session_id, frame.params
        )
    return calls[0]


def test_the_summary_flag_reaches_the_host_read(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _host_read(monkeypatch, {"pr_url": URL, "summary": True})["summary"] is True


def test_without_the_flag_the_host_does_a_full_read(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _host_read(monkeypatch, {"pr_url": URL})["summary"] is False
