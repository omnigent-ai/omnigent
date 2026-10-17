"""Build a Geist Mono variant whose ``liga`` feature merges ``###`` into one glyph that
keeps a single cell's advance but draws its ink in the two cells to the left. Run from
the repo root with ``.venv/bin/python`` (needs ``fonttools`` and ``brotli``)."""

from __future__ import annotations

from pathlib import Path

from fontTools.feaLib.builder import addOpenTypeFeaturesFromString
from fontTools.pens.transformPen import TransformPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont

_SRC = (
    Path(__file__).resolve().parents[4]
    / "web/node_modules/@fontsource-variable/geist-mono/files/geist-mono-latin-wght-normal.woff2"
)
_OUT = Path(__file__).with_name("geist-mono-latin-hash-ligature.woff2")
_HASH = "numbersign"
_LIGA = "numbersign_numbersign_numbersign.liga"
_FEATURES = "\n".join(
    [
        "languagesystem DFLT dflt;",
        "languagesystem latn dflt;",
        "feature liga {",
        f"    sub {_HASH} {_HASH} {_HASH} by {_LIGA};",
        "} liga;",
    ]
)


def build() -> None:
    font = TTFont(_SRC, recalcTimestamp=False)
    instantiateVariableFont(font, {"wght": 400}, inplace=True)

    advance, _lsb = font["hmtx"][_HASH]
    glyph_set = font.getGlyphSet()
    pen = TTGlyphPen(glyph_set)
    # Three hashes ending in the ligature's own cell: cells -2, -1 and 0.
    for cell in (-2, -1, 0):
        glyph_set[_HASH].draw(TransformPen(pen, (1, 0, 0, 1, cell * advance, 0)))
    liga = pen.glyph()

    glyf = font["glyf"]
    font.setGlyphOrder([*font.getGlyphOrder(), _LIGA])
    glyf[_LIGA] = liga
    liga.recalcBounds(glyf)
    font["hmtx"][_LIGA] = (advance, liga.xMin)

    addOpenTypeFeaturesFromString(font, _FEATURES, tables=["GSUB"])

    font.flavor = "woff2"
    font.save(_OUT)
    print(
        f"wrote {_OUT} ({_OUT.stat().st_size} bytes); "
        f"{_LIGA} xMin={liga.xMin} xMax={liga.xMax} advance={advance}"
    )


if __name__ == "__main__":
    build()
