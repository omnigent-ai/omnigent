"""GitHub PR label editing: shaping, the repository list, edits, and request validation."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from omnigent.runner import create_runner_app, pr_resource
from omnigent.runner import github_resource as github
from omnigent.runner.session_prs import PullRequestRef, SessionPrRegistry
from tests.runner.helpers import NullServerClient

PR_URL = "https://github.com/acme/widgets/pull/17"
ENTERPRISE_PR_URL = "https://ghe.example.com/acme/widgets/pull/17"
GITLAB_MR_URL = "https://gitlab.com/acme/widgets/-/merge_requests/17"
LABELS_ENDPOINT = "repos/acme/widgets/issues/17/labels"

GhCall = tuple[list[str], str | None]


def _label(name: str, color: str = "ededed", description: str = "") -> dict[str, str]:
    return {"name": name, "color": color, "description": description}


def _fake_gh(
    calls: list[GhCall], responses: list[tuple[int | None, str, str]]
) -> Callable[..., tuple[int | None, str, str]]:
    """A ``_gh`` stand-in that records ``(argv, token)`` and answers in order."""

    def gh(argv: list[str], *, cwd: str, token: str | None = None) -> tuple[int | None, str, str]:
        assert cwd == "/workspace"
        calls.append((argv, token))
        return responses.pop(0)

    return gh


@pytest.fixture
def reference(monkeypatch: pytest.MonkeyPatch) -> PullRequestRef:
    monkeypatch.setattr(github, "_pr_token", lambda _root, _reference: "selected-token")
    return PullRequestRef.from_url(PR_URL)


def test_shape_labels_keeps_valid_colors_and_skips_malformed_entries() -> None:
    assert github._shape_labels(
        [
            None,
            "bug",
            {"name": ""},
            {"color": "ffffff"},
            {"name": "upper", "color": "D73A4A", "description": "Hex is lowered"},
            {"name": "short", "color": "fff", "description": ""},
            {"name": "styled", "color": "fff;background:url(x)", "description": None},
            {"name": "hashed", "color": "#ffffff"},
            {"name": "bare"},
        ]
    ) == [
        {"name": "upper", "color": "d73a4a", "description": "Hex is lowered"},
        {"name": "short", "color": None, "description": None},
        {"name": "styled", "color": None, "description": None},
        {"name": "hashed", "color": None, "description": None},
        {"name": "bare", "color": None, "description": None},
    ]
    assert github._shape_labels(None) == []
    assert github._shape_labels({"name": "bug"}) == []


def test_repo_labels_lists_as_the_pr_account_sorted_by_name(
    monkeypatch: pytest.MonkeyPatch, reference: PullRequestRef
) -> None:
    calls: list[GhCall] = []
    raw = [_label("Zeta"), _label("alpha", "0075CA", "First"), _label("Beta")]
    monkeypatch.setattr(github, "_gh", _fake_gh(calls, [(0, json.dumps(raw), "")]))

    result = github._repo_labels("/workspace", reference)

    assert result == {
        "object": "list",
        "data": [
            {"name": "alpha", "color": "0075ca", "description": "First"},
            {"name": "Beta", "color": "ededed", "description": None},
            {"name": "Zeta", "color": "ededed", "description": None},
        ],
        "has_more": False,
    }
    assert calls == [
        (
            [
                "label",
                "list",
                "-R",
                "github.com/acme/widgets",
                "--json",
                "name,color,description",
                "--limit",
                "1001",
            ],
            "selected-token",
        )
    ]


@pytest.mark.parametrize("extra, has_more", [(0, False), (1, True)])
def test_repo_labels_report_only_a_truncated_list(
    monkeypatch: pytest.MonkeyPatch, reference: PullRequestRef, extra: int, has_more: bool
) -> None:
    raw = [_label(f"label-{index:04}") for index in range(github._MAX_REPO_LABELS + extra)]
    monkeypatch.setattr(github, "_gh", _fake_gh([], [(0, json.dumps(raw), "")]))

    result = github._repo_labels("/workspace", reference)

    assert len(result["data"]) == github._MAX_REPO_LABELS
    assert result["has_more"] is has_more


@pytest.mark.parametrize(
    "response, message",
    [
        ((1, "", "HTTP 403"), "could not list this repository's labels"),
        ((None, "", "timed out"), "could not list this repository's labels"),
        ((0, "not json", ""), "unexpected label list"),
    ],
)
def test_repo_labels_failures_are_user_facing(
    monkeypatch: pytest.MonkeyPatch,
    reference: PullRequestRef,
    response: tuple[int | None, str, str],
    message: str,
) -> None:
    monkeypatch.setattr(github, "_gh", _fake_gh([], [response]))

    with pytest.raises(ValueError, match=message):
        github._repo_labels("/workspace", reference)


def test_enterprise_labels_require_a_signed_in_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github, "_list_accounts", lambda _root: (True, [{"host": "github.com"}]))
    monkeypatch.setattr(github, "_gh", _fake_gh([], []))
    reference = PullRequestRef.from_url(ENTERPRISE_PR_URL)

    with pytest.raises(ValueError, match="Sign in to this GitHub host"):
        github._repo_labels("/workspace", reference)
    with pytest.raises(ValueError, match="Sign in to this GitHub host"):
        github._edit_labels("/workspace", reference, add=["bug"], remove=[])


def test_edit_labels_removes_then_adds_and_returns_the_final_labels(
    monkeypatch: pytest.MonkeyPatch, reference: PullRequestRef
) -> None:
    calls: list[GhCall] = []
    after_remove = [_label("bug"), _label("needs review")]
    after_add = [*after_remove, _label("ready", "0E8A16"), _label("a = b")]
    monkeypatch.setattr(
        github,
        "_gh",
        _fake_gh(
            calls,
            [
                (0, json.dumps([_label("bug"), _label("needs review")]), ""),
                (0, json.dumps(after_remove), ""),
                (0, json.dumps(after_add), ""),
            ],
        ),
    )

    result = github._edit_labels(
        "/workspace", reference, add=["ready", "a = b"], remove=["size/XS", "won't fix?"]
    )

    assert result == {
        "object": "session.github.pr_labels",
        "labels": github._shape_labels(after_add),
    }
    host = ["api", "--hostname", "github.com"]
    assert calls == [
        ([*host, "-X", "DELETE", f"{LABELS_ENDPOINT}/size%2FXS"], "selected-token"),
        ([*host, "-X", "DELETE", f"{LABELS_ENDPOINT}/won%27t%20fix%3F"], "selected-token"),
        (
            [
                *host,
                "-X",
                "POST",
                LABELS_ENDPOINT,
                "-f",
                "labels[]=ready",
                "-f",
                "labels[]=a = b",
            ],
            "selected-token",
        ),
    ]


def test_removing_absent_labels_reads_the_current_labels(
    monkeypatch: pytest.MonkeyPatch, reference: PullRequestRef
) -> None:
    calls: list[GhCall] = []
    missing = (1, json.dumps({"message": "Label does not exist"}), "gh: Not Found (HTTP 404)")
    monkeypatch.setattr(
        github, "_gh", _fake_gh(calls, [missing, (0, json.dumps([_label("bug")]), "")])
    )

    result = github._edit_labels("/workspace", reference, add=[], remove=["stale"])

    assert result["labels"] == [{"name": "bug", "color": "ededed", "description": None}]
    assert [argv[-1] for argv, _token in calls] == [
        f"{LABELS_ENDPOINT}/stale",
        f"{LABELS_ENDPOINT}?per_page=100",
    ]


def test_an_absent_label_keeps_an_earlier_removal_result(
    monkeypatch: pytest.MonkeyPatch, reference: PullRequestRef
) -> None:
    calls: list[GhCall] = []
    missing = (1, json.dumps({"message": "Label does not exist"}), "gh: Not Found (HTTP 404)")
    monkeypatch.setattr(github, "_gh", _fake_gh(calls, [(0, "[]", ""), missing]))

    result = github._edit_labels("/workspace", reference, add=[], remove=["bug", "stale"])

    assert result["labels"] == []
    assert len(calls) == 2


@pytest.mark.parametrize(
    "response, message",
    [
        (
            (1, json.dumps({"message": "Must have admin rights"}), "gh: Must have (HTTP 403)"),
            "cannot edit this pull request's labels",
        ),
        (
            (1, json.dumps({"message": "Not Found"}), "gh: Not Found (HTTP 404)"),
            "cannot edit this pull request's labels",
        ),
        ((1, "", "gh: Bad credentials (HTTP 401)"), "cannot edit this pull request's labels"),
        (
            (1, json.dumps({"message": "Validation Failed"}), "gh: Validation (HTTP 422)"),
            "GitHub rejected the label change: Validation Failed",
        ),
        ((1, "", "gh: Server Error (HTTP 502)"), "could not update the labels"),
        ((None, "", "No such file or directory: 'gh'"), "could not update the labels"),
        ((0, json.dumps({"unexpected": True}), ""), "could not update the labels"),
    ],
)
def test_edit_label_failures_are_user_facing(
    monkeypatch: pytest.MonkeyPatch,
    reference: PullRequestRef,
    response: tuple[int | None, str, str],
    message: str,
) -> None:
    monkeypatch.setattr(github, "_gh", _fake_gh([], [response]))

    with pytest.raises(ValueError, match=message):
        github._edit_labels("/workspace", reference, add=["ready"], remove=[])


def test_every_gh_call_of_a_label_edit_shares_one_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github._config, "github_account_preference", lambda _repo: "work")
    monkeypatch.setattr(github, "time", SimpleNamespace(monotonic=lambda: 100.0))
    deadlines: list[tuple[str, float | None]] = []

    def gh(argv: list[str], **_kwargs: object) -> tuple[int, str, str]:
        deadlines.append((argv[1], github._gh_deadline.get()))
        return 0, "selected-token" if argv[:2] == ["auth", "token"] else "[]", ""

    monkeypatch.setattr(github, "_gh", gh)

    github._edit_labels(
        "/workspace", PullRequestRef.from_url(PR_URL), add=["ready"], remove=["bug"]
    )

    deadline = 100.0 + github._LABEL_REQUEST_SECONDS
    assert deadlines == [("token", deadline), ("--hostname", deadline), ("--hostname", deadline)]
    assert github._gh_deadline.get() is None


@pytest.mark.parametrize("slow", ["account lookup", "removal"])
def test_a_label_edit_past_its_deadline_reports_an_unknown_outcome(
    monkeypatch: pytest.MonkeyPatch, slow: str
) -> None:
    """The deadline cuts the slow call; nothing runs after it, least of all as another account."""
    monkeypatch.setattr(
        github._config,
        "github_account_preference",
        lambda _repo: "work" if slow == "account lookup" else None,
    )
    clock = [100.0]
    monkeypatch.setattr(github, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    commands: list[list[str]] = []

    def run(argv: list[str], *, cwd: str, timeout: float, env: object = None):
        del cwd, env
        commands.append(argv)
        assert timeout <= github._LABEL_REQUEST_SECONDS
        clock[0] += timeout
        return None, "", "timed out"

    monkeypatch.setattr(github, "_run", run)

    with pytest.raises(ValueError, match="took too long to update the labels"):
        github._edit_labels(
            "/workspace", PullRequestRef.from_url(PR_URL), add=["ready"], remove=["bug"]
        )

    first = ["gh", "auth", "token"] if slow == "account lookup" else ["gh", "api", "--hostname"]
    assert [command[:3] for command in commands] == [first]
    assert github._gh_timed_out.get() is False


@pytest.fixture
def tracked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(github, "_pr_token", lambda _root, _reference: None)
    SessionPrRegistry("session").record(
        [PullRequestRef.from_url(PR_URL), PullRequestRef.from_url(GITLAB_MR_URL)],
        relationship="created",
        source="test",
    )
    return str(tmp_path)


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"add": [], "remove": []}, "Expected labels to add or remove"),
        ({"add": "bug"}, "Expected add to be a list of label names"),
        ({"remove": ["bug", 7]}, "Expected remove to be a list of label names"),
        ({"add": ["  "]}, "Expected add to be a list of label names"),
        ({"add": ["bug"], "remove": ["bug"]}, "both added and removed"),
        ({"add": ["Bug"], "remove": ["bug"]}, "both added and removed"),
        ({"remove": [".."]}, "Remove labels named . or .. on GitHub"),
        ({"remove": ["."]}, "Remove labels named . or .. on GitHub"),
        ({"add": [f"label-{index}" for index in range(101)]}, "at most 100 labels"),
        ({"pr_url": None, "add": ["bug"]}, "Select a pull request"),
        ({"pr_url": PR_URL.replace("17", "18"), "add": ["bug"]}, "not associated"),
        ({"pr_url": GITLAB_MR_URL, "add": ["bug"]}, "GitLab merge request labels"),
    ],
)
def test_invalid_label_edits_never_reach_github(
    tracked: str, monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, object], message: str
) -> None:
    monkeypatch.setattr(github, "_gh", _fake_gh([], []))
    request: dict[str, object] = {"pr_url": PR_URL, **kwargs}

    with pytest.raises(ValueError, match=message):
        pr_resource.update_pr_labels(tracked, session_id="session", **request)  # type: ignore[arg-type]


def test_label_edit_names_are_deduplicated_ignoring_case(
    tracked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def edit(_root: str, _reference: PullRequestRef, *, add: list[str], remove: list[str]):
        seen.update(add=add, remove=remove)
        return {"object": "session.github.pr_labels", "labels": []}

    monkeypatch.setattr(github, "_edit_labels", edit)

    pr_resource.update_pr_labels(
        tracked,
        session_id="session",
        pr_url=PR_URL,
        add=["bug", "Bug", "bug"],
        remove=["x", "X"],
    )

    assert seen == {"add": ["bug"], "remove": ["x"]}


def test_repo_labels_need_a_label_editing_provider(tracked: str) -> None:
    with pytest.raises(ValueError, match="Select a pull request"):
        pr_resource.pr_labels(tracked, session_id="session")
    with pytest.raises(ValueError, match="GitLab merge request labels"):
        pr_resource.pr_labels(tracked, session_id="session", pr_url=GITLAB_MR_URL)


async def test_runner_routes_list_and_edit_the_selected_prs_labels(
    tracked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def gh(argv: list[str], **_kwargs: object) -> tuple[int, str, str]:
        calls.append(argv)
        if argv[:2] == ["label", "list"]:
            return 0, json.dumps([_label("bug", "D73A4A")]), ""
        return 0, json.dumps([_label("bug", "D73A4A")]), ""

    monkeypatch.setattr(github, "_gh", gh)
    app = create_runner_app(
        runner_workspace=Path(tracked),
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    bug = {"name": "bug", "color": "d73a4a", "description": None}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runner"
    ) as client:
        listed = await client.get(
            "/v1/sessions/session/resources/github/labels", params={"pr_url": PR_URL}
        )
        edited = await client.post(
            "/v1/sessions/session/resources/github/labels",
            json={"pr_url": PR_URL, "add": ["bug"]},
        )
        missing_url = await client.post(
            "/v1/sessions/session/resources/github/labels", json={"add": ["bug"]}
        )
        overlapping = await client.post(
            "/v1/sessions/session/resources/github/labels",
            json={"pr_url": PR_URL, "add": ["bug"], "remove": ["bug"]},
        )

    assert listed.status_code == 200, listed.text
    assert listed.json() == {"object": "list", "data": [bug], "has_more": False}
    assert edited.status_code == 200, edited.text
    assert edited.json() == {"object": "session.github.pr_labels", "labels": [bug]}
    assert (missing_url.status_code, missing_url.json()["detail"]) == (
        400,
        "Expected a pull request URL",
    )
    assert overlapping.status_code == 400
    assert "both added and removed" in overlapping.json()["detail"]
    assert [argv[:2] for argv in calls] == [["label", "list"], ["api", "--hostname"]]
