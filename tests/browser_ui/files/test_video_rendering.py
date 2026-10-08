"""Recording playback contracts using a real WebM and a sealed backend."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Browser, Locator, Page, Route, expect

from tests.browser_ui.files import test_file_line_navigation as session_contract
from tests.browser_ui.files.test_file_line_navigation import (
    BrowserSession,
    _post_message,
)

seeded_session = session_contract.seeded_session


@pytest.fixture
def recording(browser: Browser, tmp_path: Path) -> bytes:
    """Generate a decodable screen recording without a checked-in binary fixture."""
    context = browser.new_context(
        record_video_dir=tmp_path, viewport={"width": 320, "height": 180}
    )
    page = context.new_page()
    page.set_content(
        '<body style="background:#3454d1;color:white"><h1>Feature verified</h1></body>'
    )
    page.wait_for_timeout(1500)
    video = page.video
    assert video is not None
    context.close()
    return Path(video.path()).read_bytes()


def _seed_video(session: BrowserSession, recording: bytes, *, fail: bool = False) -> list[str]:
    api = f"/v1/sessions/{session.session_id}/resources/environments/default"
    listing = {
        "object": "list",
        "has_more": False,
        "data": [
            {"path": "demo.webm", "name": "demo.webm", "type": "file", "bytes": len(recording)}
        ],
    }
    session.contract.json(f"{api}/filesystem", listing)
    session.contract.json(
        f"{api}/changes",
        {
            **listing,
            "data": [
                {
                    "path": "demo.webm",
                    "name": "demo.webm",
                    "status": "added",
                    "bytes": len(recording),
                    "modified_at": 1,
                }
            ],
        },
    )
    session.contract.json(
        f"/v1/sessions/{session.session_id}",
        {
            "id": session.session_id,
            "object": "conversation",
            "title": "Video browser contract",
            "agent_id": "file-line-agent",
            "agent_name": "hello_world",
            "status": "idle",
            "created_at": 1,
            "updated_at": 1,
            "labels": {},
            "permission_level": 4,
        },
    )
    reads: list[str] = []

    def serve(route: Route) -> None:
        reads.append(route.request.url)
        assert parse_qs(urlparse(route.request.url).query).get("download") == ["true"]
        route.fulfill(status=503 if fail else 200, content_type="video/webm", body=recording)

    session.contract.route(f"{session.contract.base_url}{api}/filesystem/demo.webm?*", serve)
    session.contract.json(
        f"{api}/filesystem/demo.webm.chapters.vtt", {"detail": "Not found"}, status=404
    )
    return reads


@pytest.mark.parametrize("width", [1600, 390])
@pytest.mark.parametrize("entry", ["chat", "file-link", "deep-link", "files", "changes"])
def test_recording_action_navigation(
    page: Page, seeded_session: BrowserSession, recording: bytes, width: int, entry: str
) -> None:
    """Annotations seek a real recording and track native playback on each preview."""
    session = seeded_session
    reads = _seed_video(session, recording)
    api = f"/v1/sessions/{session.session_id}/resources/environments/default"
    content = (
        "WEBVTT\n\n"
        "00:00.000 --> 00:00.400\nOpen the app\n\n"
        "00:00.500 --> 00:10.000\nVerify the result\n\n"
        "00:16:39.000 --> 00:16:40.000\nOutside the recording\n"
    )
    session.contract.json(
        f"{api}/filesystem/demo.webm.chapters.vtt",
        {
            "object": "session.environment.filesystem.file_content",
            "path": "demo.webm.chapters.vtt",
            "encoding": "utf-8",
            "content_type": "text/vtt",
            "bytes": len(content),
            "content": content,
        },
    )
    _post_message(session, "[Screen recording](demo.webm)")
    page.set_viewport_size({"width": width, "height": 1000})
    page.goto(
        f"{session.contract.base_url}/c/{session.session_id}"
        + ("?file=demo.webm" if entry == "deep-link" else "")
    )
    if entry == "file-link":
        page.get_by_role("button", name="Screen recording", exact=True).click()
    elif entry in {"files", "changes"}:
        if width == 390:
            page.get_by_role("banner").get_by_role(
                "button", name="Conversation actions", exact=True
            ).click()
            page.get_by_role(
                "menuitem", name="Files" if entry == "files" else re.compile(r"^Changes")
            ).click()
        else:
            page.get_by_role("button", name="Expand right panel", exact=True).click()
            page.get_by_role(
                "tab", name="Files" if entry == "files" else re.compile(r"^Changes")
            ).click()
        page.get_by_role("button", name=re.compile(r"^demo.webm\b")).click()
    title = "Screen recording" if entry == "chat" else "demo.webm"
    actions = page.get_by_role("group", name=f"Recording actions: {title}", exact=True).filter(
        visible=True
    )
    verify = actions.get_by_role("button", name=re.compile("Verify the result"))
    expect(verify).to_be_visible()
    assert not reads, "Annotations must not download video bytes"
    verify.focus()
    verify.press("Enter")
    player = page.locator(f'video[aria-label="{title}"]:visible')
    page.wait_for_function(
        "el => el.readyState >= 2 && el.currentTime >= 0.5", arg=player.element_handle()
    )
    player.evaluate("el => el.pause()")
    expect(verify).to_have_attribute("aria-current", "step")
    expect(
        actions.get_by_role("button", name=re.compile("Outside the recording"))
    ).to_be_disabled()
    player.evaluate("el => {el.currentTime = 0.1;}")
    expect(actions.get_by_role("button", name=re.compile("Open the app"))).to_have_attribute(
        "aria-current", "step"
    )
    player.evaluate("el => {el.currentTime = 0.45;}")
    expect(actions.locator('[aria-current="step"]')).to_have_count(0)
    verify.click()
    page.wait_for_function(
        "el => !el.seeking && Math.abs(el.currentTime - 0.5) < 0.1", arg=player.element_handle()
    )
    assert player.evaluate("el => el.paused"), "Seeking preserves paused playback"
    vb, ab = player.bounding_box(), actions.bounding_box()
    assert vb is not None and ab is not None
    if width == 390:
        assert ab["y"] >= vb["y"] + vb["height"]
    elif entry == "chat":
        assert ab["x"] >= vb["x"] + vb["width"]


@pytest.mark.parametrize("truncated", [False, True])
def test_optional_recording_annotations(
    page: Page, seeded_session: BrowserSession, recording: bytes, truncated: bool
) -> None:
    """Malformed or truncated chapters preserve ordinary video playback."""
    session = seeded_session
    _seed_video(session, recording)
    api = f"/v1/sessions/{session.session_id}/resources/environments/default"
    session.contract.json(
        f"{api}/filesystem/demo.webm.chapters.vtt",
        {
            "object": "session.environment.filesystem.file_content",
            "path": "demo.webm.chapters.vtt",
            "encoding": "utf-8",
            "content_type": "text/vtt",
            "bytes": 8,
            "content": "WEBVTT\n\n00:00.000 --> 00:01.000\nPartial\n"
            if truncated
            else "bad header",
            "truncated": truncated,
        },
    )
    _post_message(session, "[Screen recording](demo.webm)")
    page.goto(f"{session.contract.base_url}/c/{session.session_id}")
    page.get_by_role("button", name="Play video: Screen recording").click()
    _assert_playback(page, page.locator('video[aria-label="Screen recording"]'))
    expect(page.get_by_role("group", name="Recording actions: Screen recording")).to_have_count(0)


def _assert_playback(page: Page, player: Locator) -> None:
    expect(player).to_be_visible()
    expect(player).to_have_attribute("controls", "")
    expect(player).to_have_attribute("playsinline", "")
    page.wait_for_function(
        "el => el.readyState >= 2 && el.videoWidth > 0", arg=player.element_handle()
    )
    player.evaluate("el => el.pause()")
    player.evaluate("el => { el.currentTime = el.duration / 2; }")
    page.wait_for_function("el => !el.seeking && el.currentTime > 0", arg=player.element_handle())
    player.evaluate("el => el.play()")
    page.wait_for_function("el => !el.paused", arg=player.element_handle())
    player.evaluate("el => el.pause()")


@pytest.mark.parametrize("width", [1600, 390])
def test_chat_recording_label_stays_inside_footer(
    page: Page, seeded_session: BrowserSession, recording: bytes, width: int
) -> None:
    """The file action stays below the video at desktop and mobile widths."""
    session = seeded_session
    _seed_video(session, recording)
    _post_message(session, "[Screen recording](demo.webm)")
    page.set_viewport_size({"width": width, "height": 1000})
    page.goto(f"{session.contract.base_url}/c/{session.session_id}")
    label = page.get_by_role("button", name="Screen recording", exact=True)
    expect(page.get_by_text("Screen recording", exact=True)).to_have_count(1)
    page.get_by_role("button", name="Play video: Screen recording", exact=True).click()
    video = page.locator('video[aria-label="Screen recording"]')
    _assert_playback(page, video)
    video_box, label_box = video.bounding_box(), label.bounding_box()
    assert video_box is not None and label_box is not None
    assert label_box["y"] >= video_box["y"] + video_box["height"]
    assert label_box["x"] >= video_box["x"]
    assert label_box["x"] + label_box["width"] <= video_box["x"] + video_box["width"]
    label.focus()
    label.press("Enter")
    expect(page.locator('[data-testid="file-viewer"]:visible')).to_be_visible()


@pytest.mark.parametrize("entry", ["chat", "file-link", "files", "changes", "deep-link", "mobile"])
def test_workspace_recording_playback(
    page: Page, seeded_session: BrowserSession, recording: bytes, entry: str
) -> None:
    """Chat, file links, file browsing, and mobile/deep links play complete bytes."""
    session = seeded_session
    reads = _seed_video(session, recording)
    _post_message(session, "Result: [Screen recording](demo.webm)")
    page.set_viewport_size({"width": 390 if entry == "mobile" else 1600, "height": 1000})
    base_url, sid = session
    suffix = "?file=demo.webm" if entry in {"deep-link", "mobile"} else ""
    page.goto(f"{base_url}/c/{sid}{suffix}")
    if entry == "file-link":
        page.get_by_role("button", name="Screen recording", exact=True).click()
    elif entry in {"files", "changes"}:
        page.get_by_role("button", name="Expand right panel", exact=True).click()
        page.get_by_role(
            "tab", name="Files" if entry == "files" else re.compile(r"^Changes")
        ).click()
        page.get_by_role("button", name=re.compile(r"^demo.webm\b")).click()
    # Chat and the file viewer each expose the same player with a different title.
    title = "Screen recording" if entry == "chat" else "demo.webm"
    play = page.get_by_role("button", name=f"Play video: {title}").filter(visible=True)
    expect(play).to_be_visible(timeout=15_000)
    assert not reads, "Rendering a recording must not download its bytes"
    play.click()
    player = page.locator(f'video[aria-label="{title}"]:visible')
    _assert_playback(page, player)
    expect(page.get_by_role("group", name=re.compile("^Recording actions:"))).to_have_count(0)
    expect(player).to_have_attribute("src", re.compile(r"^blob:"))
    assert len(reads) == 1


def test_remote_recording_playback(
    page: Page, seeded_session: BrowserSession, recording: bytes
) -> None:
    session = seeded_session

    def serve_range(route: Route) -> None:
        start, end = 0, len(recording) - 1
        value = route.request.headers.get("range")
        if value:
            parts = value.removeprefix("bytes=").split("-", 1)
            start = int(parts[0] or 0)
            end = min(int(parts[1]) if parts[1] else end, end)
        headers = {"Accept-Ranges": "bytes"}
        if value:
            headers["Content-Range"] = f"bytes {start}-{end}/{len(recording)}"
        route.fulfill(
            status=206 if value else 200,
            content_type="video/webm",
            headers=headers,
            body=recording[start : end + 1],
        )

    session.contract.route("https://recordings.example/demo.webm?token=abc", serve_range)
    _post_message(session, "[Remote demo](https://recordings.example/demo.webm?token=abc)")
    page.goto(f"{session.contract.base_url}/c/{session.session_id}")
    _assert_playback(page, page.locator('video[aria-label="Remote demo"]'))


def test_recording_download_failure(
    page: Page, seeded_session: BrowserSession, recording: bytes
) -> None:
    session = seeded_session
    _seed_video(session, recording, fail=True)
    page.goto(f"{session.contract.base_url}/c/{session.session_id}?file=demo.webm")
    page.get_by_role("button", name="Play video: demo.webm").filter(visible=True).click()
    viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(
        viewer.get_by_text("Unable to play this video. Download it to watch locally.")
    ).to_be_visible()
    expect(viewer.get_by_role("button", name="Download video: demo.webm")).to_be_visible()
    expect(viewer.get_by_role("button", name="Retry")).to_be_visible()
