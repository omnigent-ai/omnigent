"""GitHub PR title and description editing, and description attachment uploads."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from omnigent.runner import create_runner_app, pr_resource
from omnigent.runner import github_resource as github
from omnigent.runner.session_prs import PullRequestRef, SessionPrRegistry
from tests.runner.helpers import NullServerClient

PR_URL = "https://github.com/acme/widgets/pull/17"
ENTERPRISE_PR_URL = "https://ghe.example.com/acme/widgets/pull/17"
GITLAB_MR_URL = "https://gitlab.com/acme/widgets/-/merge_requests/17"
UPLOAD_URL = "https://uploads.github.com/user-attachments/assets"
ASSET_URL = "https://github.com/user-attachments/assets/0f1e2d3c-aaaa-bbbb-cccc-1234567890ab"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 16

GhResponse = tuple[int | None, str, str]


class GhCall:
    """One recorded ``_gh`` call, with the ``--input`` file read while it existed."""

    def __init__(self, argv: list[str], token: str | None, timeout: float | None) -> None:
        self.argv = argv
        self.token = token
        self.timeout = timeout
        self.input = (
            Path(argv[argv.index("--input") + 1]).read_bytes() if "--input" in argv else None
        )


def _fake_gh(calls: list[GhCall], responses: list[GhResponse]) -> Callable[..., GhResponse]:
    """A ``_gh`` stand-in that records each call and answers in order."""

    def gh(
        argv: list[str], *, cwd: str, token: str | None = None, timeout: float | None = None
    ) -> GhResponse:
        assert cwd == "/workspace"
        calls.append(GhCall(argv, token, timeout))
        return responses.pop(0)

    return gh


@pytest.fixture
def reference(monkeypatch: pytest.MonkeyPatch) -> PullRequestRef:
    monkeypatch.setattr(github, "_pr_token", lambda _root, _reference: "selected-token")
    return PullRequestRef.from_url(PR_URL)


@pytest.mark.parametrize(
    "title, body, sent",
    [
        ("New title", None, {"title": "New title"}),
        (None, "", {"body": ""}),
        (
            "New title",
            "## Summary\n<!-- kept -->",
            {"title": "New title", "body": "## Summary\n<!-- kept -->"},
        ),
    ],
)
def test_edit_details_patches_only_the_given_fields_as_the_pr_account(
    monkeypatch: pytest.MonkeyPatch,
    reference: PullRequestRef,
    title: str | None,
    body: str | None,
    sent: dict[str, str],
) -> None:
    calls: list[GhCall] = []
    answer = {"title": "GitHub title", "body": None, "number": 17}
    monkeypatch.setattr(github, "_gh", _fake_gh(calls, [(0, json.dumps(answer), "")]))

    result = github._edit_details("/workspace", reference, title=title, body=body)

    assert result == {"object": "session.github.pr_details", "title": "GitHub title", "body": ""}
    [call] = calls
    assert call.argv[:6] == [
        "api",
        "--hostname",
        "github.com",
        "-X",
        "PATCH",
        "repos/acme/widgets/pulls/17",
    ]
    assert call.token == "selected-token"
    assert call.input is not None and json.loads(call.input) == sent


@pytest.mark.parametrize(
    "response, message",
    [
        ((1, "", "gh: Resource not accessible (HTTP 403)"), "cannot edit this pull request"),
        (
            (1, json.dumps({"message": "Validation Failed"}), "gh: Validation Failed (HTTP 422)"),
            "GitHub rejected the change: Validation Failed",
        ),
        ((1, "", "gh: Server Error (HTTP 502)"), "could not update the pull request"),
        ((0, "not json", ""), "could not update the pull request"),
    ],
)
def test_edit_details_failures_are_user_facing(
    monkeypatch: pytest.MonkeyPatch,
    reference: PullRequestRef,
    response: GhResponse,
    message: str,
) -> None:
    monkeypatch.setattr(github, "_gh", _fake_gh([], [response]))

    with pytest.raises(ValueError, match=message):
        github._edit_details("/workspace", reference, title="New title", body=None)


def test_upload_attachment_posts_the_bytes_to_user_attachments(
    monkeypatch: pytest.MonkeyPatch, reference: PullRequestRef
) -> None:
    calls: list[GhCall] = []
    monkeypatch.setattr(
        github,
        "_gh",
        _fake_gh(calls, [(0, "1266212515\n", ""), (0, json.dumps({"url": ASSET_URL}), "")]),
    )

    result = github._upload_attachment(
        "/workspace", reference, name="login error.png", content_type="image/png", content=PNG
    )

    assert result == {
        "object": "session.github.pr_attachment",
        "url": ASSET_URL,
        "name": "login error.png",
        "content_type": "image/png",
    }
    lookup, upload = calls
    assert lookup.argv == ["api", "--hostname", "github.com", "repos/acme/widgets", "--jq", ".id"]
    assert upload.argv[:4] == [
        "api",
        "-X",
        "POST",
        f"{UPLOAD_URL}?content_type=image%2Fpng&name=login%20error.png&repository_id=1266212515",
    ]
    assert upload.input == PNG
    assert {lookup.token, upload.token} == {"selected-token"}
    assert upload.timeout == github._ATTACHMENT_REQUEST_SECONDS


def test_enterprise_uploads_go_to_the_hosts_upload_api(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github, "_pr_token", lambda _root, _reference: None)
    monkeypatch.setattr(
        github, "_list_accounts", lambda _root: (True, [{"host": "ghe.example.com"}])
    )
    enterprise_asset = ASSET_URL.replace("github.com", "ghe.example.com")
    calls: list[GhCall] = []
    monkeypatch.setattr(
        github,
        "_gh",
        _fake_gh(calls, [(0, "7", ""), (0, json.dumps({"url": enterprise_asset}), "")]),
    )

    result = github._upload_attachment(
        "/workspace",
        PullRequestRef.from_url(ENTERPRISE_PR_URL),
        name="clip.mp4",
        content_type="video/mp4",
        content=MP4,
    )

    assert (
        calls[1]
        .argv[3]
        .startswith(
            "https://ghe.example.com/api/uploads/user-attachments/assets?content_type=video%2Fmp4"
        )
    )
    assert result["url"] == enterprise_asset


@pytest.mark.parametrize(
    "responses, message",
    [
        ([(1, "", "gh: Not Found (HTTP 404)")], "could not find this pull request's repository"),
        ([(0, "7", ""), (1, "", "gh: Not Found (HTTP 404)")], "needs push access"),
        (
            [
                (0, "7", ""),
                (
                    1,
                    json.dumps(
                        {
                            "message": "Validation Failed",
                            "errors": [{"message": "content_type is not included in the list"}],
                        }
                    ),
                    "gh: Validation Failed (HTTP 422)",
                ),
            ],
            "GitHub rejected the file: content_type is not included",
        ),
        ([(0, "7", ""), (0, json.dumps({"url": "javascript:alert(1)"}), "")], "could not upload"),
        (
            [(0, "7", ""), (0, json.dumps({"url": "https://evil.example/assets/1"}), "")],
            "could not upload",
        ),
    ],
)
def test_upload_attachment_failures_are_user_facing(
    monkeypatch: pytest.MonkeyPatch,
    reference: PullRequestRef,
    responses: list[GhResponse],
    message: str,
) -> None:
    monkeypatch.setattr(github, "_gh", _fake_gh([], responses))

    with pytest.raises(ValueError, match=message):
        github._upload_attachment(
            "/workspace", reference, name="a.png", content_type="image/png", content=PNG
        )


def test_upload_attachment_stops_when_the_token_lookup_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def token_lookup_times_out(_root: str, _reference: PullRequestRef) -> None:
        github._gh_timed_out.set(True)

    monkeypatch.setattr(github, "_pr_token", token_lookup_times_out)
    monkeypatch.setattr(github, "_gh", _fake_gh([], []))

    with pytest.raises(ValueError, match="took too long"):
        github._upload_attachment(
            "/workspace",
            PullRequestRef.from_url(PR_URL),
            name="a.png",
            content_type="image/png",
            content=PNG,
        )


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
        ({}, "Expected a title or body"),
        ({"title": "   "}, "title cannot be empty"),
        ({"title": "x" * 257}, "at most 256 characters"),
        ({"body": "x" * 65537}, "at most 65536 characters"),
        ({"title": 7}, "Expected title to be a string"),
        ({"body": ["text"]}, "Expected body to be a string"),
        ({"pr_url": None, "title": "T"}, "Select a pull request to edit its title"),
        ({"pr_url": PR_URL.replace("17", "18"), "title": "T"}, "not associated"),
        ({"pr_url": GITLAB_MR_URL, "title": "T"}, "GitLab merge request titles"),
    ],
)
def test_invalid_details_edits_never_reach_github(
    tracked: str, monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, object], message: str
) -> None:
    monkeypatch.setattr(github, "_gh", _fake_gh([], []))
    request: dict[str, object] = {"pr_url": PR_URL, **kwargs}

    with pytest.raises(ValueError, match=message):
        pr_resource.update_pr_details(tracked, session_id="session", **request)  # type: ignore[arg-type]


def test_details_edits_trim_the_title_and_keep_the_body_verbatim(
    tracked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, object] = {}

    def edit(_root: str, _reference: PullRequestRef, *, title: str | None, body: str | None):
        seen.update(title=title, body=body)
        return {"object": "session.github.pr_details", "title": title, "body": body}

    monkeypatch.setattr(github, "_edit_details", edit)

    pr_resource.update_pr_details(
        tracked, session_id="session", pr_url=PR_URL, title="  Fix it \n", body="  body\n"
    )

    assert seen == {"title": "Fix it", "body": "  body\n"}


@pytest.mark.parametrize(
    "content, name, expected",
    [
        (PNG, "Screen Shot.PNG", ("Screen-Shot.png", "image/png")),
        (b"\xff\xd8\xff\xe0" + b"\x00" * 8, "photo.jpeg", ("photo.jpg", "image/jpeg")),
        (b"GIF89a" + b"\x00" * 8, "anim.gif", ("anim.gif", "image/gif")),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "pic", ("pic.webp", "image/webp")),
        (MP4, "demo.mov", ("demo.mp4", "video/mp4")),
        (
            b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 8,
            "Recording.mov",
            ("Recording.mov", "video/quicktime"),
        ),
        (b"\x1a\x45\xdf\xa3" + b"\x00" * 8, "../../clip", ("clip.webm", "video/webm")),
        (PNG, "", ("attachment.png", "image/png")),
    ],
)
def test_attachments_take_their_type_and_extension_from_the_content(
    tracked: str,
    monkeypatch: pytest.MonkeyPatch,
    content: bytes,
    name: str,
    expected: tuple[str, str],
) -> None:
    seen: dict[str, object] = {}

    def upload(
        _root: str, _reference: PullRequestRef, *, name: str, content_type: str, content: bytes
    ):
        seen.update(name=name, content_type=content_type)
        return {"object": "session.github.pr_attachment", "url": ASSET_URL}

    monkeypatch.setattr(github, "_upload_attachment", upload)

    pr_resource.upload_pr_attachment(
        tracked, session_id="session", pr_url=PR_URL, name=name, content=content
    )

    assert seen == {"name": expected[0], "content_type": expected[1]}


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"content": b""}, "attachment is empty"),
        ({"content": b"<svg xmlns='http://www.w3.org/2000/svg'/>"}, "Attach a PNG, JPEG"),
        ({"content": b"%PDF-1.7"}, "Attach a PNG, JPEG"),
        ({"content": b"\x00\x00\x00\x18ftypheic" + b"\x00" * 8}, "Attach a PNG, JPEG"),
        ({"content": b"\x00\x00\x00\x1cftypavif" + b"\x00" * 8}, "Attach a PNG, JPEG"),
        ({"content": b"ABCD\x00\x00\x00\x00WEBPVP8 "}, "Attach a PNG, JPEG"),
        ({"content": PNG + b"\x00" * (10 * 1024 * 1024)}, "An image can be at most 10 MB"),
        ({"content": MP4 + b"\x00" * (70 * 1024 * 1024)}, "A video can be at most 70 MB"),
        ({"pr_url": GITLAB_MR_URL, "content": PNG}, "Attaching files to a GitLab merge request"),
        ({"pr_url": None, "content": PNG}, "Select a pull request to attach files"),
    ],
)
def test_invalid_attachments_never_reach_github(
    tracked: str, monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, object], message: str
) -> None:
    monkeypatch.setattr(github, "_gh", _fake_gh([], []))
    request: dict[str, object] = {"pr_url": PR_URL, "name": "a.png", **kwargs}

    with pytest.raises(ValueError, match=message):
        pr_resource.upload_pr_attachment(tracked, session_id="session", **request)  # type: ignore[arg-type]


async def test_runner_routes_edit_details_and_upload_attachments(
    tracked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[GhCall] = []

    def gh(argv: list[str], **kwargs: object) -> GhResponse:
        calls.append(GhCall(argv, None, None))
        if "PATCH" in argv:
            return 0, json.dumps({"title": "Renamed", "body": "Body"}), ""
        if argv[-2:] == ["--jq", ".id"]:
            return 0, "7", ""
        return 0, json.dumps({"url": ASSET_URL}), ""

    monkeypatch.setattr(github, "_gh", gh)
    app = create_runner_app(
        runner_workspace=Path(tracked),
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runner"
    ) as client:
        edited = await client.post(
            "/v1/sessions/session/resources/github/details",
            json={"pr_url": PR_URL, "title": "Renamed"},
        )
        missing_url = await client.post(
            "/v1/sessions/session/resources/github/details", json={"title": "Renamed"}
        )
        uploaded = await client.post(
            "/v1/sessions/session/resources/github/attachments",
            params={"pr_url": PR_URL, "name": "shot.png"},
            content=PNG,
        )
        unsupported = await client.post(
            "/v1/sessions/session/resources/github/attachments",
            params={"pr_url": PR_URL, "name": "notes.txt"},
            content=b"plain text",
        )

    assert edited.status_code == 200, edited.text
    assert edited.json() == {
        "object": "session.github.pr_details",
        "title": "Renamed",
        "body": "Body",
    }
    assert (missing_url.status_code, missing_url.json()["detail"]) == (
        400,
        "Expected a pull request URL",
    )
    assert uploaded.status_code == 200, uploaded.text
    assert uploaded.json()["url"] == ASSET_URL
    assert calls[-1].input == PNG
    assert unsupported.status_code == 400
    assert "Attach a PNG" in unsupported.json()["detail"]


@pytest.mark.parametrize("url", [PR_URL, ENTERPRISE_PR_URL])
def test_create_comment_posts_literal_markdown_as_selected_account(
    monkeypatch: pytest.MonkeyPatch, reference: PullRequestRef, url: str
) -> None:
    reference = PullRequestRef.from_url(url)
    monkeypatch.setattr(github, "_list_accounts", lambda _root: (True, [{"host": reference.host}]))
    calls: list[GhCall] = []
    body = "  **Thanks**\n`$(literal)` <!-- preserved -->"
    answer = {
        "id": 99,
        "body": body,
        "html_url": url + "#issuecomment-99",
        "user": {"login": "octocat"},
        "created_at": "2026-10-26T12:00:00Z",
    }
    monkeypatch.setattr(github, "_gh", _fake_gh(calls, [(0, json.dumps(answer), "")]))
    result = github._create_comment("/workspace", reference, body=body)
    [call] = calls
    assert call.argv[:6] == [
        "api",
        "--hostname",
        reference.host,
        "-X",
        "POST",
        "repos/acme/widgets/issues/17/comments",
    ]
    assert call.token == "selected-token"
    assert call.input is not None and json.loads(call.input) == {"body": body}
    assert result["comment"] == {
        "author": "octocat",
        "body": body,
        "created_at": answer["created_at"],
        "url": answer["html_url"],
    }


@pytest.mark.parametrize(
    "response,message",
    [
        ((1, "", "gh: Forbidden (HTTP 403)"), "cannot post"),
        ((1, "", "gh: Validation Failed (HTTP 422)"), "rejected the comment"),
        ((None, "", "timed out"), "Check GitHub before retrying"),
        ((1, "", "HTTP 502"), "Check GitHub before retrying"),
        ((0, "{}", ""), "Check GitHub before retrying"),
        ((0, "not json", ""), "Check GitHub before retrying"),
    ],
)
def test_create_comment_does_not_retry_unknown_outcomes(
    monkeypatch: pytest.MonkeyPatch,
    reference: PullRequestRef,
    response: GhResponse,
    message: str,
) -> None:
    calls: list[GhCall] = []
    monkeypatch.setattr(github, "_gh", _fake_gh(calls, [response]))
    with pytest.raises(ValueError, match=message):
        github._create_comment("/workspace", reference, body="Hello")
    assert len(calls) == 1


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"body": None}, "Expected body"),
        ({"body": []}, "Expected body"),
        ({"body": " \n"}, "cannot be empty"),
        ({"body": "x" * 65537}, "at most 65536"),
        ({"pr_url": None}, "Select a pull request"),
        ({"pr_url": PR_URL.replace("17", "18")}, "not associated"),
        ({"pr_url": GITLAB_MR_URL}, "not supported"),
    ],
)
def test_invalid_comments_never_reach_github(
    tracked: str,
    monkeypatch: pytest.MonkeyPatch,
    kwargs: dict[str, object],
    message: str,
) -> None:
    monkeypatch.setattr(github, "_gh", _fake_gh([], []))
    request: dict[str, object] = {"pr_url": PR_URL, "body": "Hello", **kwargs}
    with pytest.raises(ValueError, match=message):
        pr_resource.create_pr_comment(tracked, session_id="session", **request)  # type: ignore[arg-type]


async def test_runner_posts_comment_once(tracked: str, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[GhCall] = []

    def gh(argv: list[str], **kwargs: object) -> GhResponse:
        calls.append(GhCall(argv, None, None))
        return (
            0,
            json.dumps({"id": 99, "body": "  Hello\n", "html_url": PR_URL + "#issuecomment-99"}),
            "",
        )

    monkeypatch.setattr(github, "_gh", gh)
    app = create_runner_app(runner_workspace=Path(tracked), server_client=NullServerClient())  # type: ignore[arg-type]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runner"
    ) as client:
        response = await client.post(
            "/v1/sessions/session/resources/github/comments",
            json={"pr_url": PR_URL, "body": "  Hello\n"},
        )
    assert response.status_code == 200, response.text
    assert response.json()["comment"]["body"] == "  Hello\n"
    assert len(calls) == 1
    assert calls[0].input is not None and json.loads(calls[0].input) == {"body": "  Hello\n"}


@pytest.mark.parametrize("stage", ["authentication", "post"])
def test_comment_deadline_distinguishes_unsent_from_uncertain(
    monkeypatch: pytest.MonkeyPatch, reference: PullRequestRef, stage: str
) -> None:
    calls = []

    def token(_root: str, _reference: PullRequestRef) -> str:
        if stage == "authentication":
            github._gh_timed_out.set(True)
        return "selected-token"

    def post(argv: list[str], **kwargs: object) -> GhResponse:
        calls.append(argv)
        github._gh_timed_out.set(True)
        return None, "", "timed out"

    monkeypatch.setattr(github, "_pr_token", token)
    monkeypatch.setattr(github, "_gh", post)
    message = "was not sent" if stage == "authentication" else "Check GitHub before retrying"
    with pytest.raises(ValueError, match=message):
        github._create_comment("/workspace", reference, body="Hello")
    assert len(calls) == (0 if stage == "authentication" else 1)
