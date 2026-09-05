"""Generate the DMG installer background picture (``background.tiff``).

Finder composes the mounted-DMG window from ``build.dmg`` in
``web/electron/package.json`` and this picture: the picture is drawn at its
1x point size (so ``dmg.window`` must match that size or Finder tiles it),
each ``contents`` entry is an icon centered at its ``x``/``y`` with
``iconSize`` points per side, and Finder draws the icon's filename label in
a text band directly below the icon.

The layout keeps every icon slot and its label band on a plain white
backdrop so the labels stay legible and the drag source / drop target read
as the interactive elements, draws an install arrow between the app icon
and the Applications link, and keeps the mascot as a small accent flush
with the bottom edge, clear of the interactive area. ``mascot@2x.png`` is
the mascot artwork at 2x resolution.

Regenerate with::

    .venv/bin/python web/electron/dmg/generate_background.py

The output is a two-frame HiDPI TIFF (1x at 72 dpi, 2x at 144 dpi) — the
layout ``tiffutil -cathidpicheck`` produces. ``tests/test_dmg_installer_background.py``
guards the composed geometry.
"""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

DMG_DIR = Path(__file__).resolve().parent
ELECTRON_DIR = DMG_DIR.parent

# Sampled from the app icon tile (icons/icon.png) so the arrow and headline
# match the icon the user drags.
BRAND_NAVY = (28, 39, 69)

HEADLINE = "Drag Omnigent into Applications to install"
HEADLINE_SIZE = 15  # 1x points
HEADLINE_TOP = 20

# Install arrow between the two icon slots (1x points).
ARROW_MARGIN = 34  # gap between an icon slot's edge and the arrow
ARROW_SHAFT_HALF = 5
ARROW_HEAD_LENGTH = 26
ARROW_HEAD_HALF_HEIGHT = 17

MASCOT_HEIGHT = 120  # 1x points; drawn flush with the bottom edge

FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
]


def _dmg_config() -> dict:
    pkg = json.loads((ELECTRON_DIR / "package.json").read_text())
    return pkg["build"]["dmg"]


def _font(size: int) -> ImageFont.FreeTypeFont:
    for candidate in FONT_CANDIDATES:
        if Path(candidate).exists():
            return ImageFont.truetype(candidate, size)
    msg = f"no headline font found; looked for {FONT_CANDIDATES}"
    raise FileNotFoundError(msg)


def _render(scale: int, dmg: dict, mascot: Image.Image) -> Image.Image:
    width = dmg["window"]["width"] * scale
    height = dmg["window"]["height"] * scale
    img = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(img)

    draw.text(
        (width // 2, HEADLINE_TOP * scale),
        HEADLINE,
        font=_font(HEADLINE_SIZE * scale),
        fill=BRAND_NAVY,
        anchor="ma",
    )

    # Arrow from the app icon slot to the Applications drop slot, kept out of
    # both slots' bounding boxes.
    app, applications = sorted(dmg["contents"], key=lambda entry: entry["x"])
    icon_half = dmg["iconSize"] // 2
    x0 = (app["x"] + icon_half + ARROW_MARGIN) * scale
    x1 = (applications["x"] - icon_half - ARROW_MARGIN) * scale
    y = app["y"] * scale
    head = ARROW_HEAD_LENGTH * scale
    draw.rectangle(
        (x0, y - ARROW_SHAFT_HALF * scale, x1 - head, y + ARROW_SHAFT_HALF * scale),
        fill=BRAND_NAVY,
    )
    draw.polygon(
        [
            (x1 - head, y - ARROW_HEAD_HALF_HEIGHT * scale),
            (x1, y),
            (x1 - head, y + ARROW_HEAD_HALF_HEIGHT * scale),
        ],
        fill=BRAND_NAVY,
    )

    # Mascot accent, centered and flush with the bottom edge (its bottom is
    # the artwork's own cut edge), well below the icons and their labels.
    mascot_height = MASCOT_HEIGHT * scale
    mascot_width = round(mascot.width * mascot_height / mascot.height)
    scaled = mascot.resize((mascot_width, mascot_height), Image.Resampling.LANCZOS)
    img.paste(scaled, ((width - mascot_width) // 2, height - mascot_height))
    return img


def main() -> None:
    dmg = _dmg_config()
    mascot = Image.open(DMG_DIR / "mascot@2x.png").convert("RGB")
    one_x = _render(1, dmg, mascot)
    two_x = _render(2, dmg, mascot)
    # Per-frame dpi tags let Finder pair the frames as 1x/2x of one picture.
    two_x.encoderinfo = {"dpi": (144.0, 144.0)}
    one_x.save(
        DMG_DIR / "background.tiff",
        save_all=True,
        append_images=[two_x],
        dpi=(72.0, 72.0),
        compression="tiff_lzw",
    )


if __name__ == "__main__":
    main()
