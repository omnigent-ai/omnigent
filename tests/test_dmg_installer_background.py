"""macOS DMG installer window legibility and drop-target clarity.

The desktop installer is the Finder window of the mounted DMG that
electron-builder produces from ``web/electron/package.json`` (``build.dmg``)
and ``web/electron/dmg/background.tiff``. Finder composes that window
deterministically from these shipped artifacts:

- the background picture is drawn at its 1x point size, anchored top-left,
  and tiled if the window is larger;
- each ``dmg.contents`` entry is an icon *centered* at its ``x``/``y`` with
  ``iconSize`` points per side;
- Finder draws each icon's filename label in a short text band directly
  below the icon (black text in light appearance, white in dark appearance).

Artwork that runs under the label band makes the installer text unreadable,
and artwork that intrudes into an icon slot makes the drag source / drop
target ambiguous. Finder itself cannot run in CI (Linux), so these tests
guard the composition inputs — exactly the pixels Finder puts under the
labels and drop slots — rather than driving the mounted DMG. The artwork is
produced by ``web/electron/dmg/generate_background.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image

REPO_ROOT = Path(__file__).resolve().parents[1]
ELECTRON_DIR = REPO_ROOT / "web" / "electron"

# WCAG AA contrast for normal-size text; Finder labels are ~12pt.
MIN_LABEL_CONTRAST = 4.5

# Finder's label text band relative to the icon: centered under the icon,
# starting just below its bottom edge. Generous ±60pt half-width covers the
# "Applications" label.
LABEL_HALF_WIDTH = 60
LABEL_TOP_OFFSET = 2
LABEL_BOTTOM_OFFSET = 24

# A drop slot (the icon's bounding box plus a small margin) must read as an
# interactive element: at most this fraction of its background pixels may be
# "busy" (saturated or dark illustration rather than quiet backdrop).
SLOT_MARGIN = 4
MAX_SLOT_BUSY_FRACTION = 0.05
BUSY_SATURATION = 0.15
BUSY_LUMINANCE = 0.75


def _dmg_config() -> dict:
    pkg = json.loads((ELECTRON_DIR / "package.json").read_text())
    return pkg["build"]["dmg"]


def _background_1x(dmg: dict) -> Image.Image:
    """The 1x (point-size) frame of the DMG background picture.

    The TIFF carries 1x and 2x frames; Finder lays the window out in points,
    i.e. against the smallest frame.
    """
    tif = Image.open(ELECTRON_DIR / dmg["background"])
    sizes = []
    for i in range(getattr(tif, "n_frames", 1)):
        tif.seek(i)
        sizes.append((tif.size, i))
    _, idx = min(sizes)
    tif.seek(idx)
    return tif.convert("RGB")


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    def channel(v: int) -> float:
        c = v / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(lum_a: float, lum_b: float) -> float:
    lighter, darker = max(lum_a, lum_b), min(lum_a, lum_b)
    return (lighter + 0.05) / (darker + 0.05)


def _region_pixels(img: Image.Image, x0: int, y0: int, x1: int, y1: int) -> list:
    x0, y0 = max(x0, 0), max(y0, 0)
    x1, y1 = min(x1, img.width), min(y1, img.height)
    assert x0 < x1 and y0 < y1, "icon geometry no longer overlaps the background picture"
    px = img.load()
    assert px is not None
    return [px[x, y] for y in range(y0, y1) for x in range(x0, x1)]


def _slot_name(entry: dict) -> str:
    return entry.get("path", "app icon")


def test_icon_labels_have_legible_contrast() -> None:
    """Finder's icon labels must be readable where they land on the background.

    The band directly under each icon must support at least one Finder label
    color at WCAG AA: consistently light enough for black text (light
    appearance) or consistently dark enough for white text (dark appearance).
    Artwork whose luminance spans the band leaves neither label color legible.
    """
    dmg = _dmg_config()
    bg = _background_1x(dmg)
    icon_half = dmg["iconSize"] // 2

    failures = []
    for entry in dmg["contents"]:
        cx, cy = entry["x"], entry["y"]
        icon_bottom = cy + icon_half
        lums = sorted(
            _relative_luminance(p)
            for p in _region_pixels(
                bg,
                cx - LABEL_HALF_WIDTH,
                icon_bottom + LABEL_TOP_OFFSET,
                cx + LABEL_HALF_WIDTH,
                icon_bottom + LABEL_BOTTOM_OFFSET,
            )
        )
        # Robust worst-case pixels (5th/95th percentile): black text is
        # limited by the darkest band pixels, white text by the lightest.
        p05 = lums[len(lums) // 20]
        p95 = lums[19 * len(lums) // 20]
        vs_black = _contrast(p05, 0.0)
        vs_white = _contrast(p95, 1.0)
        if max(vs_black, vs_white) < MIN_LABEL_CONTRAST:
            failures.append(
                f"{_slot_name(entry)!r} label band under icon at ({cx},{cy}): "
                f"black-text contrast {vs_black:.2f}:1, white-text contrast "
                f"{vs_white:.2f}:1 (need {MIN_LABEL_CONTRAST}:1 for one of them)"
            )

    assert not failures, (
        "DMG installer icon labels are illegible against the background "
        "artwork: " + "; ".join(failures)
    )


def test_drop_target_slots_are_clear_of_artwork() -> None:
    """The two drop slots must not be occluded by the background illustration.

    A background that is itself a large illustration reaching into an icon
    slot leaves the user unable to tell the draggable app icon from the
    decoration. Each icon slot's backdrop must be visually quiet so the real
    drag source and drop target read as the interactive elements.
    """
    dmg = _dmg_config()
    bg = _background_1x(dmg)
    margin = dmg["iconSize"] // 2 + SLOT_MARGIN

    def is_busy(p: tuple[int, int, int]) -> bool:
        mx, mn = max(p), min(p)
        saturation = 0.0 if mx == 0 else (mx - mn) / mx
        return saturation > BUSY_SATURATION or _relative_luminance(p) < BUSY_LUMINANCE

    failures = []
    for entry in dmg["contents"]:
        cx, cy = entry["x"], entry["y"]
        pixels = _region_pixels(bg, cx - margin, cy - margin, cx + margin, cy + margin)
        busy_fraction = sum(is_busy(p) for p in pixels) / len(pixels)
        if busy_fraction > MAX_SLOT_BUSY_FRACTION:
            failures.append(
                f"{_slot_name(entry)!r} slot at ({cx},{cy}) is {busy_fraction:.0%} "
                f"covered by illustration (max {MAX_SLOT_BUSY_FRACTION:.0%})"
            )

    assert not failures, (
        "DMG installer drop slots are occluded by the background artwork, "
        "making the drag target ambiguous: " + "; ".join(failures)
    )


def test_window_matches_background_size() -> None:
    """The configured DMG window must match the background's 1x point size.

    Finder tiles a background picture smaller than the window, so a mismatch
    shows a repeated strip below the artwork and breaks the designed layout
    the icons and labels sit in.
    """
    dmg = _dmg_config()
    bg = _background_1x(dmg)
    window = (dmg["window"]["width"], dmg["window"]["height"])
    assert window == bg.size, (
        f"dmg.window {window} does not match the background picture's 1x size "
        f"{bg.size}; Finder will tile the picture across the uncovered strip"
    )


def test_background_carries_matching_retina_frame() -> None:
    """The background must ship paired 1x/2x frames with matching dpi tags.

    Finder picks the retina variant by pairing a 72 dpi frame with a 144 dpi
    frame of twice the pixel size (the layout ``tiffutil -cathidpicheck``
    produces); a missing or mis-tagged frame renders the installer blurry on
    Retina displays or breaks the point-size layout.
    """
    dmg = _dmg_config()
    tif = Image.open(ELECTRON_DIR / dmg["background"])
    assert getattr(tif, "n_frames", 1) == 2, (
        "background picture must carry exactly a 1x and a 2x frame"
    )
    frames = []
    for i in range(2):
        tif.seek(i)
        frames.append((tif.size, tif.info.get("dpi")))
    frames.sort()
    (one_x_size, one_x_dpi), (two_x_size, two_x_dpi) = frames
    assert two_x_size == (one_x_size[0] * 2, one_x_size[1] * 2), (
        f"2x frame {two_x_size} is not twice the 1x frame {one_x_size}"
    )
    assert one_x_dpi == (72.0, 72.0), f"1x frame dpi {one_x_dpi} != 72"
    assert two_x_dpi == (144.0, 144.0), f"2x frame dpi {two_x_dpi} != 144"
