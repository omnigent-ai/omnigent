"""Clipped screenshots polled on a video-recorded page must leave the footage intact,
and the crop the e2e_ui conftest substitutes for Chromium's view-resizing clip path
must match Playwright's native clip pixel for pixel."""

from __future__ import annotations

import asyncio
import io
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageChops
from playwright.async_api import async_playwright
from playwright.sync_api import Browser, Error

from tests.helpers.ui_recording import extract_frames, fraction_near

# A flat blue page with a red block inside the probe clip and a ticking counter
# far away from it, so the screencast keeps emitting frames like a live pane.
_PAGE = """<html><body style="margin:0;background:#1d4ed8;color:#fff;font:32px monospace">
<div style="position:absolute;left:60px;top:50px;width:100px;height:40px;background:#dc2626"></div>
<div id="tick" style="position:absolute;left:400px;top:300px">0</div>
<script>let n=0;
setInterval(()=>{document.getElementById('tick').textContent=String(++n)},50)</script>
</body></html>"""
_CLIP = {"x": 40, "y": 40, "width": 160, "height": 48}
_PAGE_BLUE = (0x1D, 0x4E, 0xD8)
# Playwright pads screencast frames smaller than the video onto this grey.
_PAD_GREY = (128, 128, 128)


def _skip_unless_chromium(browser: Browser) -> None:
    if browser.browser_type.name != "chromium":
        pytest.skip("only Chromium resizes the view for clipped screenshots")


def _assert_same_pixels(native: bytes, cropped: bytes, scale: int) -> None:
    expected = Image.open(io.BytesIO(native)).convert("RGB")
    actual = Image.open(io.BytesIO(cropped)).convert("RGB")
    assert actual.size == expected.size == (_CLIP["width"] * scale, _CLIP["height"] * scale)
    assert ImageChops.difference(expected, actual).getbbox() is None


def test_clip_probes_keep_the_recording_intact(browser: Browser, tmp_path: Path) -> None:
    _skip_unless_chromium(browser)
    record_dir = tmp_path / "video"
    context = browser.new_context(record_video_dir=str(record_dir))
    page = context.new_page()
    page.set_content(_PAGE)
    page.wait_for_timeout(500)

    # Poll the probe back-to-back, the way a renderer-corruption test watches a
    # region of the terminal for the whole flood.
    probes = 0
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        shot = Image.open(io.BytesIO(page.screenshot(clip=_CLIP)))
        assert shot.size == (_CLIP["width"], _CLIP["height"])
        probes += 1
    page.wait_for_timeout(500)
    context.close()
    assert probes > 20, (
        f"only {probes} probes in 3s; the probe loop is not exercising the recorder"
    )

    video = next(record_dir.glob("*.webm"))
    frames = extract_frames(video, tmp_path / "frames")
    assert any(fraction_near(f, _PAGE_BLUE) > 0.5 for f in frames), (
        "the video never shows the page"
    )
    grey = {f.name: round(fraction_near(f, _PAD_GREY), 2) for f in frames}
    distorted = {name: share for name, share in grey.items() if share > 0.5}
    assert not distorted, (
        f"{len(distorted)}/{len(frames)} recorded frames are mostly pad-grey while the "
        f"clip probe polled: {distorted}"
    )


@pytest.mark.parametrize("device_scale_factor", [1, 2])
def test_recorded_clip_matches_native_clip(
    browser: Browser, tmp_path: Path, device_scale_factor: int
) -> None:
    _skip_unless_chromium(browser)
    plain = browser.new_context(device_scale_factor=device_scale_factor, record_video_dir=None)
    page = plain.new_page()
    page.set_content(_PAGE)
    native = page.screenshot(clip=_CLIP)
    plain.close()

    recorded = browser.new_context(
        record_video_dir=str(tmp_path / "video"), device_scale_factor=device_scale_factor
    )
    page = recorded.new_page()
    page.set_content(_PAGE)
    saved = tmp_path / "out" / "probe.png"
    cropped = page.screenshot(clip=_CLIP, path=str(saved))
    recorded.close()

    _assert_same_pixels(native, cropped, device_scale_factor)
    assert saved.read_bytes() == cropped


def test_async_recorded_clip_matches_native_clip(tmp_path: Path) -> None:
    shots: dict[str, bytes] = {}

    async def drive() -> None:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch()
            try:
                page = await browser.new_page(record_video_dir=None)
                await page.set_content(_PAGE)
                shots["native"] = await page.screenshot(clip=_CLIP)
                await page.context.close()

                page = await browser.new_page(record_video_dir=str(tmp_path / "video"))
                await page.set_content(_PAGE)
                shots["cropped"] = await page.screenshot(clip=_CLIP)
                await page.context.close()
            finally:
                await browser.close()

    # The sync Playwright fixtures keep a loop running on the main thread, so
    # the async API gets its own loop in a worker thread.
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(lambda: asyncio.run(drive())).result(timeout=120)
    _assert_same_pixels(shots["native"], shots["cropped"], scale=1)


@pytest.mark.parametrize("scale", ["css", "device"])
@pytest.mark.parametrize("no_viewport", [False, True])
def test_recorded_clip_scale_and_viewport(
    browser: Browser, tmp_path: Path, scale: str, no_viewport: bool
) -> None:
    shots = []
    for recorded in (False, True):
        context = browser.new_context(
            no_viewport=no_viewport,
            **({} if no_viewport else {"device_scale_factor": 2}),
            record_video_dir=str(tmp_path / "video") if recorded else None,
        )
        page = context.new_page()
        page.set_content(_PAGE)
        shots.append(page.screenshot(clip=_CLIP, scale=scale))
        context.close()
    _assert_same_pixels(*shots, scale=2 if scale == "device" and not no_viewport else 1)


@pytest.mark.parametrize("extension", ["jpg", "jpe"])
@pytest.mark.parametrize("quality", [None, 0, 80, 80.0, 100])
def test_recorded_jpeg_clip_preserves_quality_and_path(
    browser: Browser, tmp_path: Path, quality: int | float | None, extension: str
) -> None:
    _skip_unless_chromium(browser)
    context = browser.new_context(record_video_dir=str(tmp_path / "video"))
    page = context.new_page()
    page.set_content(_PAGE)
    path = tmp_path / "out" / f"clip.{extension}"
    options = {} if quality is None else {"quality": quality}
    data = page.screenshot(clip=_CLIP, path=path, **options)
    context.close()
    actual = Image.open(io.BytesIO(data))
    assert actual.format == "JPEG"
    assert actual.size == (_CLIP["width"], _CLIP["height"])
    assert path.read_bytes() == data
    # JPEG quantization tables expose the requested encoder quality, including zero.
    expected = io.BytesIO()
    Image.new("RGB", actual.size).save(
        expected, "JPEG", quality=80 if quality is None else int(quality)
    )
    with Image.open(expected) as expected_image:
        expected_quantization = expected_image.quantization
    assert actual.quantization == expected_quantization


@pytest.mark.parametrize("api", ["sync", "async"])
@pytest.mark.parametrize(
    ("options", "exact_message"),
    [
        pytest.param({"type": "png", "quality": 80}, True, id="png-quality"),
        pytest.param({"quality": 0}, True, id="implicit-png-quality"),
        pytest.param({"type": "webp"}, True, id="unsupported-format"),
        pytest.param({"type": ""}, True, id="empty-format"),
        pytest.param({"path": "probe.webp"}, True, id="unsupported-extension"),
        pytest.param({"path": "probe"}, True, id="missing-extension"),
        pytest.param({"type": "jpeg", "quality": -1}, True, id="negative-quality"),
        pytest.param({"type": "jpeg", "quality": 101}, True, id="excessive-quality"),
        pytest.param({"type": "jpeg", "quality": 80.5}, True, id="fractional-quality"),
        pytest.param({"type": "jpeg", "quality": "80"}, True, id="string-quality"),
        pytest.param({"type": "jpeg", "quality": True}, True, id="boolean-quality"),
        pytest.param({"clip": {"x": 0, "y": 0, "height": 10}}, True, id="missing-width"),
        pytest.param({"clip": []}, True, id="non-object-clip"),
        pytest.param({"clip": {**_CLIP, "x": "40"}}, True, id="string-coordinate"),
        pytest.param({"clip": {**_CLIP, "x": True}}, True, id="boolean-coordinate"),
        pytest.param({"clip": {**_CLIP, "width": 0}}, True, id="zero-width"),
        pytest.param(
            {"clip": {"x": 2000, "y": 2000, "width": 20, "height": 20}},
            False,
            id="outside-image",
        ),
    ],
)
def test_invalid_screenshot_options_match_native(
    browser: Browser, tmp_path: Path, api: str, options: dict[str, Any], exact_message: bool
) -> None:
    """Preserve Playwright errors, including exact messages for native validation."""
    _skip_unless_chromium(browser)
    kwargs = {"clip": _CLIP, **options}
    if "path" in kwargs:
        kwargs["path"] = tmp_path / kwargs["path"]
    errors = []

    async def drive() -> None:
        async with async_playwright() as playwright:
            async_browser = await playwright.chromium.launch()
            try:
                for recorded in (False, True):
                    context = await async_browser.new_context(
                        record_video_dir=str(tmp_path / "video") if recorded else None
                    )
                    try:
                        page = await context.new_page()
                        await page.set_content(_PAGE)
                        with pytest.raises(Error) as error:
                            await page.screenshot(**kwargs)
                        errors.append(str(error.value).split("\nCall log:")[0])
                    finally:
                        await context.close()
            finally:
                await async_browser.close()

    if api == "async":
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(lambda: asyncio.run(drive())).result(timeout=120)
    else:
        for recorded in (False, True):
            context = browser.new_context(
                record_video_dir=str(tmp_path / "video") if recorded else None
            )
            try:
                page = context.new_page()
                page.set_content(_PAGE)
                with pytest.raises(Error) as error:
                    page.screenshot(**kwargs)
                errors.append(str(error.value).split("\nCall log:")[0])
            finally:
                context.close()

    if exact_message:
        assert errors[0] == errors[1]
    else:
        # Locally cropped geometry errors lack Playwright's API-call prefix.
        assert all("Clipped area is either empty or outside" in message for message in errors)
    if "path" in kwargs:
        assert not kwargs["path"].exists()


@pytest.mark.parametrize("device_scale_factor", [1, 2])
@pytest.mark.parametrize(
    "clip",
    [
        {"x": 40.25, "y": 40.25, "width": 160.5, "height": 48.5},
        {"x": 40.75, "y": 40.5, "width": 160.9, "height": 48.9},
        {"x": -10, "y": -10, "width": 160, "height": 100},
        {"x": 1200, "y": 680, "width": 160, "height": 100},
    ],
)
def test_recorded_clip_bounds_match_native(
    browser: Browser, tmp_path: Path, clip: dict[str, float], device_scale_factor: int
) -> None:
    shots = []
    for recorded in (False, True):
        context = browser.new_context(
            device_scale_factor=device_scale_factor,
            record_video_dir=str(tmp_path / "video") if recorded else None,
        )
        page = context.new_page()
        page.set_content(_PAGE)
        shots.append(Image.open(io.BytesIO(page.screenshot(clip=clip))).convert("RGB"))
        context.close()
    assert shots[0].size == shots[1].size
    assert ImageChops.difference(*shots).getbbox() is None
