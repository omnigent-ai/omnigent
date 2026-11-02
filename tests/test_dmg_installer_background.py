"""macOS DMG installer window legibility and drop-target clarity.

Finder composes the mounted-DMG window from ``build.dmg`` in
``web/electron/package.json`` and ``web/electron/dmg/background.tiff``: each
``contents`` entry is an icon centered at its ``x``/``y`` with ``iconSize``
points per side, and Finder draws the icon's filename label directly below it.
Finder cannot run in CI, so these tests check the background pixels Finder
puts under the labels and icon slots in every frame (1x and Retina) instead of
driving the mounted DMG. ``web/electron/dmg/generate_background.py`` produces
the picture.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

ELECTRON_DIR = Path(__file__).resolve().parents[1] / "web" / "electron"
DMG_CONFIG = json.loads((ELECTRON_DIR / "package.json").read_text())["build"]["dmg"]
ICON_HALF = DMG_CONFIG["iconSize"] // 2

# WCAG AA for normal-size text against Finder's black labels in the default
# Light appearance. Finder's Dark-appearance label colour is not modelled here.
MIN_LABEL_CONTRAST = 4.5
# Label band under the icon's bottom edge, wide enough for "Applications".
LABEL_HALF_WIDTH = 60
LABEL_BAND_HEIGHT = 24
SLOT_MARGIN = 4
# generate_background.py paints the whole window backdrop plain white.
WINDOW_BACKDROP = (255, 255, 255)
# Per-channel deviation from the backdrop that counts as artwork.
ARTWORK_TOLERANCE = 24


def _background_frames() -> list[tuple[int, Image.Image]]:
    """Every frame of the background picture with its scale relative to the 1x frame."""
    with Image.open(ELECTRON_DIR / DMG_CONFIG["background"]) as picture:
        frames = []
        for index in range(getattr(picture, "n_frames", 1)):
            picture.seek(index)
            frames.append(picture.convert("RGB"))
    one_x_width = min(frame.width for frame in frames)
    return [(frame.width // one_x_width, frame) for frame in frames]


def _contrast_with_black(pixel: tuple[int, int, int]) -> float:
    """WCAG contrast ratio between black text and this backdrop pixel."""
    channels = []
    for value in pixel:
        value /= 255
        channels.append(value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4)
    red, green, blue = channels
    return (0.2126 * red + 0.7152 * green + 0.0722 * blue + 0.05) / 0.05


def _pixels(frame: Image.Image, scale: int, box: tuple[int, int, int, int]) -> list:
    left, top, right, bottom = (edge * scale for edge in box)
    assert 0 <= left < right <= frame.width and 0 <= top < bottom <= frame.height, (
        f"{scale}x frame: icon geometry {box} falls outside the background picture"
    )
    loaded = frame.load()
    assert loaded is not None
    return [loaded[x, y] for y in range(top, bottom) for x in range(left, right)]


def _deviation(pixel: tuple[int, int, int], backdrop: tuple[int, int, int]) -> int:
    return max(abs(channel - base) for channel, base in zip(pixel, backdrop, strict=True))


def _item_id(item: dict) -> str:
    return item.get("path", "app")


@pytest.mark.parametrize("item", DMG_CONFIG["contents"], ids=_item_id)
def test_icon_label_band_is_readable_with_black_text(item: dict) -> None:
    band = (
        item["x"] - LABEL_HALF_WIDTH,
        item["y"] + ICON_HALF,
        item["x"] + LABEL_HALF_WIDTH,
        item["y"] + ICON_HALF + LABEL_BAND_HEIGHT,
    )
    for scale, frame in _background_frames():
        contrasts = [_contrast_with_black(pixel) for pixel in _pixels(frame, scale, band)]
        low_contrast = sum(contrast < MIN_LABEL_CONTRAST for contrast in contrasts)
        assert low_contrast == 0, (
            f"{scale}x frame: {low_contrast / len(contrasts):.1%} of the label band {band} "
            f"gives black label text less than {MIN_LABEL_CONTRAST}:1 contrast "
            f"(minimum {min(contrasts):.2f}:1)"
        )


@pytest.mark.parametrize("item", DMG_CONFIG["contents"], ids=_item_id)
def test_icon_slot_is_free_of_artwork(item: dict) -> None:
    margin = ICON_HALF + SLOT_MARGIN
    slot = (item["x"] - margin, item["y"] - margin, item["x"] + margin, item["y"] + margin)
    for scale, frame in _background_frames():
        pixels = _pixels(frame, scale, slot)
        artwork = sum(_deviation(pixel, WINDOW_BACKDROP) > ARTWORK_TOLERANCE for pixel in pixels)
        assert artwork == 0, (
            f"{scale}x frame: artwork covers {artwork / len(pixels):.1%} of the icon slot {slot}"
        )


def test_install_arrow_points_from_app_to_applications() -> None:
    app = next(entry for entry in DMG_CONFIG["contents"] if "path" not in entry)
    applications = next(entry for entry in DMG_CONFIG["contents"] if entry.get("type") == "link")
    assert app["x"] < applications["x"] and app["y"] == applications["y"], (
        "the installer must place the app left of /Applications on the same row"
    )
    margin = ICON_HALF + SLOT_MARGIN
    gap = (app["x"] + margin, app["y"] - margin, applications["x"] - margin, app["y"] + margin)
    for scale, frame in _background_frames():
        pixels = _pixels(frame, scale, gap)
        width = (gap[2] - gap[0]) * scale
        heights = [
            sum(
                _deviation(pixel, WINDOW_BACKDROP) > ARTWORK_TOLERANCE
                for pixel in pixels[column::width]
            )
            for column in range(width)
        ]
        drawn = [column for column, height in enumerate(heights) if height]
        assert len(drawn) >= width // 2, (
            f"{scale}x frame: no install arrow spans the gap {gap} between the icon slots"
        )
        tallest = max(range(width), key=heights.__getitem__)
        assert tallest > (drawn[0] + drawn[-1]) / 2, (
            f"{scale}x frame: the install arrow in {gap} does not point towards /Applications"
        )


def test_installer_shows_one_app_and_the_applications_link() -> None:
    """dmg-builder places the app bundle at the ``contents`` entry without a ``path``."""
    apps = [entry for entry in DMG_CONFIG["contents"] if "path" not in entry]
    links = [entry.get("path") for entry in DMG_CONFIG["contents"] if entry.get("type") == "link"]
    assert len(DMG_CONFIG["contents"]) == 2 and len(apps) == 1 and links == ["/Applications"], (
        "the installer must show exactly one app icon and one link to /Applications"
    )


def test_window_matches_background_size() -> None:
    """electron-builder sizes the window from the picture, so the config must describe it."""
    one_x = next(frame for scale, frame in _background_frames() if scale == 1)
    window = (DMG_CONFIG["window"]["width"], DMG_CONFIG["window"]["height"])
    assert window == one_x.size, (
        f"dmg.window {window} does not match the background picture's 1x size {one_x.size}"
    )


def test_background_carries_matching_retina_frame() -> None:
    """Finder pairs a 72 dpi frame with a 144 dpi frame of twice the pixel size."""
    with Image.open(ELECTRON_DIR / DMG_CONFIG["background"]) as picture:
        assert getattr(picture, "n_frames", 1) == 2, (
            "background picture must carry exactly a 1x and a 2x frame"
        )
        frames = []
        for index in range(2):
            picture.seek(index)
            frames.append((picture.size, picture.info.get("dpi")))
    (one_x_size, one_x_dpi), (two_x_size, two_x_dpi) = sorted(frames)
    assert two_x_size == (one_x_size[0] * 2, one_x_size[1] * 2), (
        f"2x frame {two_x_size} is not twice the 1x frame {one_x_size}"
    )
    assert one_x_dpi == (72.0, 72.0), f"1x frame dpi {one_x_dpi} != 72"
    assert two_x_dpi == (144.0, 144.0), f"2x frame dpi {two_x_dpi} != 144"
