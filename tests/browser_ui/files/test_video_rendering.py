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
def test_recording_action_navigation(
    page: Page, seeded_session: BrowserSession, recording: bytes, width: int
) -> None:
    """File links open responsive actions that seek and track native playback."""
    session = seeded_session
    reads = _seed_video(session, recording)
    api = f"/v1/sessions/{session.session_id}/resources/environments/default"
    # Playwright can emit a 0.96s fallback clip when no compositor frame arrives.
    content = (
        "WEBVTT\n\n"
        "00:00.000 --> 00:00.400\nOpen the app\n\n"
        "00:00.500 --> 00:10.000\nVerify the result\n\n"
        + "".join(
            f"00:00.{600 + i * 10:03d} --> 00:00.{609 + i * 10:03d}\n"
            f"Recorded action {i + 1}: verify the application responds correctly\n\n"
            for i in range(20)
        )
        + "00:16:39.000 --> 00:16:40.000\nOutside the recording\n"
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
    page.goto(f"{session.contract.base_url}/c/{session.session_id}")
    page.get_by_role("button", name="Screen recording", exact=True).click()
    title = "demo.webm"
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
    player.evaluate("el => { el.pause(); el.currentTime = 0.5; }")
    page.wait_for_function("el => !el.seeking", arg=player.element_handle())
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
    scrolling = actions.get_by_role("button", name=re.compile("Recorded action 20:")).locator("..")
    assert scrolling.evaluate("el => el.scrollHeight > el.clientHeight")
    if ab["x"] >= vb["x"] + vb["width"] - 1:
        media_box = player.bounding_box()
        assert media_box is not None
        assert abs(ab["height"] - media_box["height"]) < 2
    else:
        assert scrolling.bounding_box()["height"] <= 288
    last = scrolling.get_by_role("button", name=re.compile("Recorded action 20:"))
    expect(last).to_be_enabled()
    last.focus()
    last.press("Enter")
    page.wait_for_function(
        "el => !el.seeking && Math.abs(el.currentTime - 0.79) < 0.05", arg=player.element_handle()
    )
    assert scrolling.evaluate("el => el.scrollTop > 0"), "Keyboard focus scrolls to later actions"
    expect(last).to_have_attribute("aria-current", "step")
    verify.click()
    page.wait_for_function(
        "el => !el.seeking && Math.abs(el.currentTime - 0.5) < 0.1", arg=player.element_handle()
    )
    media = player.element_handle()
    assert media is not None
    card = player.locator("../../..")
    hide = card.get_by_role("button", name="Hide recording actions", exact=True)
    assert not hide.evaluate("el => el.closest('[role=group]')")
    toggle_box = hide.bounding_box()
    header_offset = toggle_box["y"] - card.bounding_box()["y"]
    expect(hide).to_have_attribute("aria-expanded", "true")
    hide.focus()
    hide.press("Enter")
    expect(actions).not_to_be_visible()
    show = card.get_by_role("button", name="Show recording actions", exact=True)
    assert show.bounding_box()["x"] == toggle_box["x"]
    assert abs(show.bounding_box()["y"] - card.bounding_box()["y"] - header_offset) < 1
    expect(show).to_be_focused()
    expect(show).to_have_attribute("aria-expanded", "false")
    assert media.evaluate(
        "el => el.isConnected && el.paused && Math.abs(el.currentTime - 0.5) < 0.1"
    )
    show.press("Enter")
    expect(actions).to_be_visible()
    expect(hide).to_be_focused()
    expect(verify).to_have_attribute("aria-current", "step")
    assert media.evaluate(
        "el => el.isConnected && el.paused && Math.abs(el.currentTime - 0.5) < 0.1"
    )
    assert len(reads) == 1, "Collapsing actions must not reload the recording"


@pytest.mark.parametrize("width", [1600, 390])
def test_recording_header_size_and_optional_controls(
    page: Page, seeded_session: BrowserSession, recording: bytes, width: int
) -> None:
    """Shared controls stay aligned; optional chapters do not change card width."""
    session = seeded_session
    _seed_video(session, recording)
    api = f"/v1/sessions/{session.session_id}/resources/environments/default"
    content = "WEBVTT\n\n00:00.000 --> 00:01.000\nOpen the app\n"

    def annotations(route: Route) -> None:
        if "download=true" in route.request.url:
            route.fulfill(
                status=200,
                content_type="text/vtt",
                headers={"Content-Disposition": 'attachment; filename="demo.webm.chapters.vtt"'},
                body=content,
            )
        else:
            route.fulfill(
                status=200,
                json={"encoding": "utf-8", "content": content, "path": "demo.webm.chapters.vtt"},
            )

    session.contract.route(
        f"{session.contract.base_url}{api}/filesystem/demo.webm.chapters.vtt*", annotations
    )
    session.contract.json(
        f"{api}/filesystem/plain.webm.chapters.vtt", {"detail": "Not found"}, status=404
    )
    session.contract.route(
        f"{session.contract.base_url}{api}/filesystem/plain.webm?*",
        lambda route: route.fulfill(status=200, content_type="video/webm", body=recording),
    )
    session.contract.json(
        f"{api}/filesystem",
        {
            "object": "list",
            "has_more": False,
            "data": [
                {"path": f, "name": f, "type": "file", "bytes": len(recording)}
                for f in ["demo.webm", "plain.webm"]
            ],
        },
    )
    _post_message(session, "[Annotated](demo.webm)\n\n[Plain](plain.webm)")
    page.set_viewport_size({"width": width, "height": 1000})
    page.goto(f"{session.contract.base_url}/c/{session.session_id}")
    annotated = page.get_by_role("button", name="Annotated", exact=True)
    plain = page.get_by_role("button", name="Plain", exact=True)
    acard, pcard = annotated.locator("../../.."), plain.locator("../../..")
    expect(acard.get_by_role("button", name="Download recording: Annotated")).to_be_visible()
    expect(plain).to_be_visible()
    expect(
        page.get_by_role("button", name=re.compile(r"^(Show|Hide) recording actions$"))
    ).to_have_count(0)
    expect(acard.get_by_role("group")).to_have_count(0)
    expect(pcard.get_by_role("group")).to_have_count(0)
    assert acard.bounding_box()["width"] == pcard.bounding_box()["width"]
    assert acard.bounding_box()["width"] <= 320
    initial_height = acard.bounding_box()["height"]
    for title in ["Annotated", "Plain"]:
        label = page.get_by_role("button", name=title, exact=True)
        expect(page.get_by_text(title, exact=True)).to_have_count(1)
        page.get_by_role("button", name=f"Play video: {title}", exact=True).click()
        video = page.locator(f'video[aria-label="{title}"]')
        _assert_playback(page, video)
        video_box, label_box = video.bounding_box(), label.bounding_box()
        assert video_box is not None and label_box is not None
        assert label_box["y"] + label_box["height"] <= video_box["y"]
        assert label_box["x"] >= video_box["x"]
        assert label_box["x"] + label_box["width"] <= video_box["x"] + video_box["width"]
    avideo, pvideo = acard.locator("video").bounding_box(), pcard.locator("video").bounding_box()
    assert avideo["height"] == pvideo["height"]
    assert avideo["height"] <= 200
    assert abs(avideo["width"] / avideo["height"] - 1.6) < 0.01
    assert acard.bounding_box()["height"] == initial_height
    media = acard.locator("video").element_handle()
    annotated.focus()
    annotated.press("Enter")
    viewer = page.locator('[data-testid="file-viewer"]:visible')
    expect(viewer).to_be_visible()
    actions = viewer.get_by_role("group", name="Recording actions: demo.webm", exact=True)
    expect(actions).to_be_visible()
    expect(acard.get_by_role("group")).to_have_count(0)
    if width == 1600:
        assert acard.bounding_box()["height"] == initial_height
    page.wait_for_function("el => el.isConnected && el.paused", arg=media)
    actions.get_by_role("button", name=re.compile("Open the app")).click()
    _assert_playback(page, viewer.locator("video"))
    expect(viewer.get_by_role("button", name="Download recording: demo.webm")).to_be_visible()


def test_optional_recording_annotations(
    page: Page, seeded_session: BrowserSession, recording: bytes
) -> None:
    """Malformed native chapters leave viewer playback available without navigation."""
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
            "bytes": len("bad header"),
            "content": "bad header",
            "truncated": False,
        },
    )
    page.add_init_script("""
        document.addEventListener('error', event => {
            if (event.target instanceof HTMLTrackElement && event.target.kind === 'chapters') {
                window.recordingAnnotationsRejected = true;
            }
        }, true);
    """)
    page.goto(f"{session.contract.base_url}/c/{session.session_id}?file=demo.webm")
    viewer = page.get_by_test_id("file-viewer")
    expect(viewer).to_be_visible()
    page.wait_for_function("window.recordingAnnotationsRejected === true")
    viewer.get_by_role("button", name="Play video: demo.webm", exact=True).click()
    _assert_playback(page, viewer.locator("video"))
    expect(viewer.get_by_role("group", name="Recording actions: demo.webm")).to_have_count(0)
    expect(
        viewer.get_by_role("button", name=re.compile(r"^(Show|Hide) recording actions$"))
    ).to_have_count(0)
    expect(viewer.get_by_role("button", name="Download video: demo.webm")).to_be_visible()


def _assert_playback(page: Page, player: Locator) -> None:
    expect(player).to_be_visible()
    expect(player).to_have_attribute("controls", "")
    expect(player).to_have_attribute("playsinline", "")
    page.wait_for_function(
        "el => el.readyState >= 2 && el.videoWidth > 0 && Number.isFinite(el.duration)",
        arg=player.element_handle(),
    )
    player.evaluate("el => el.pause()")
    player.evaluate("el => { el.currentTime = el.duration / 2; }")
    page.wait_for_function("el => !el.seeking && el.currentTime > 0", arg=player.element_handle())
    start = player.evaluate("el => el.currentTime")
    player.evaluate("el => el.play()")
    page.wait_for_function(
        "({el, start}) => !el.paused && el.currentTime > start + 0.05",
        arg={"el": player.element_handle(), "start": start},
    )
    player.evaluate("el => el.pause()")


@pytest.mark.parametrize(
    "entry",
    [
        "chat",
        "file-link",
        "files",
        "changes",
        "deep-link",
        "mobile",
        "mobile-files",
        "mobile-changes",
    ],
)
def test_workspace_recording_playback(
    page: Page, seeded_session: BrowserSession, recording: bytes, entry: str
) -> None:
    """Chat, file links, file browsing, and mobile/deep links play complete bytes."""
    session = seeded_session
    reads = _seed_video(session, recording)
    _post_message(session, "Result: [Screen recording](demo.webm)")
    mobile = entry.startswith("mobile")
    page.set_viewport_size({"width": 390 if mobile else 1600, "height": 1000})
    base_url, sid = session
    suffix = "?file=demo.webm" if entry in {"deep-link", "mobile"} else ""
    page.goto(f"{base_url}/c/{sid}{suffix}")
    if entry == "file-link":
        page.get_by_role("button", name="Screen recording", exact=True).click()
    elif entry in {"files", "changes", "mobile-files", "mobile-changes"}:
        tab = "Files" if entry.endswith("files") else re.compile(r"^Changes")
        if mobile:
            page.get_by_role("banner").get_by_role(
                "button", name="Conversation actions", exact=True
            ).click()
            page.get_by_role("menuitem", name=tab).click()
        else:
            page.get_by_role("button", name="Expand right panel", exact=True).click()
            page.get_by_role("tab", name=tab).click()
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
    """Remote media makes no requests until Play, then decodes and advances."""
    session = seeded_session
    requests: list[str] = []

    def serve_range(route: Route) -> None:
        requests.append(route.request.url)
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
    play = page.get_by_role("button", name="Play video: Remote demo", exact=True)
    expect(play).to_be_visible()
    expect(page.locator('video[aria-label="Remote demo"][src]')).to_have_count(0)
    # Give native preloading time to expose any request triggered by rendering.
    page.wait_for_timeout(300)
    assert requests == [], "Rendering a remote recording must not contact its origin"
    play.click()
    _assert_playback(page, page.locator('video[aria-label="Remote demo"]'))
    assert requests, "Playback must request the remote recording"


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


def test_closing_workspace_releases_recording(
    page: Page, seeded_session: BrowserSession, recording: bytes
) -> None:
    """One visible player survives resizing; closing the rail releases its media."""
    session = seeded_session
    reads = _seed_video(session, recording)
    _post_message(session, "[Screen recording](demo.webm)")
    page.set_viewport_size({"width": 390, "height": 1000})
    page.goto(f"{session.contract.base_url}/c/{session.session_id}?file=demo.webm")
    viewer = page.get_by_test_id("file-viewer")
    viewer.get_by_role("button", name="Play video: demo.webm", exact=True).click()
    video = viewer.locator("video")
    _assert_playback(page, video)
    video.evaluate("el => { el.loop = true; return el.play(); }")
    media, source = video.element_handle(), video.get_attribute("src")
    assert media is not None and source is not None
    expect(video).to_have_attribute("src", re.compile(r"^blob:"))
    for width in (1600, 390, 1600):
        page.set_viewport_size({"width": width, "height": 1000})
        expect(viewer).to_have_count(1)
        expect(video).to_be_in_viewport()
        # Include hidden elements so duplicate players cannot keep playing offscreen.
        expect(page.locator("video")).to_have_count(1)
        assert video.evaluate("(el, original) => el === original", media)
        expect(video).to_have_attribute("src", source)
        # Native reparenting may pause the same element; it remains playable.
        video.evaluate("el => el.play()")
        start = video.evaluate("el => el.currentTime")
        page.wait_for_function(
            "({el, start}) => !el.paused && el.currentTime !== start",
            arg={"el": media, "start": start},
        )
        assert len(reads) == 1, "Resizing must not download a second copy of the recording"
    page.get_by_role("button", name="Collapse right panel", exact=True).click()
    expect(page.locator('[data-testid="file-viewer"] video')).to_have_count(0)
    page.wait_for_function("el => !el.isConnected && el.paused", arg=media)
    page.wait_for_function(
        "async url => {try {await fetch(url); return false;} catch {return true;}}", arg=source
    )
    page.get_by_role("button", name="Expand right panel", exact=True).click()
    expect(viewer.get_by_role("button", name="Play video: demo.webm", exact=True)).to_be_visible()
    expect(viewer.locator("video")).to_have_count(0)
    assert len(reads) == 1, "Reopening must wait for Play before downloading again"
