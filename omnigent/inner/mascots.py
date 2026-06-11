"""Static terminal mascot art for Omnigent startup banners."""

from __future__ import annotations

import hashlib
from typing import TypedDict


class MascotPayload(TypedDict):
    """
    Stable mascot art plus hex color for a given identity.

    :param lines: Multi-line ASCII mascot art.
    :param color: Hex color used to render the mascot,
        e.g. ``"#F43BA6"``.
    """

    lines: list[str]
    color: str


MASCOT_ART_LINES: tuple[str, ...] = (
    "( ◉ ‿ ◉ )",
    " ~)|)|(~ ",
)

MASCOT_ART_COL_WIDTH = max(len(line) for line in MASCOT_ART_LINES)

# Truecolor hex: must stay in sync with the interactive welcome ``Panel`` border in
# ``omnigent.cli``. Octopus magenta-pink — the Omnigent brand accent.
MASCOT_ART_COLOR = "#F43BA6"


def random_mascot_color() -> str:
    """
    Return the brand color used for mascot glyphs.

    :returns: Hex color string for the Omnigent accent,
        e.g. ``"#F43BA6"``.
    """

    return MASCOT_ART_COLOR


def random_mascot_lines() -> list[str]:
    """
    Return the startup mascot ASCII art.

    The function name is kept for compatibility with the old procedural
    mascot API, but the TUI now uses the single static Omnigent mascot.

    :returns: Centered two-row mascot art.
    """

    return list(MASCOT_ART_LINES)


def mascot_payload_for_identity(agent_identity: str) -> MascotPayload:
    """
    Return stable mascot art and color for an arbitrary identity.

    The art is static. The color remains identity-derived for callers that
    use this payload outside the startup banner.

    :param agent_identity: Stable identity seed, e.g.
        ``"demo\\x00tool_a,tool_b\\x00You are a helper."``.
    :returns: Mascot payload containing static art and a hex color.
    """

    digest = hashlib.sha256(agent_identity.encode("utf-8")).digest()
    # Octopus magenta-pink range from hash bytes, centered on the brand
    # accent ``#F43BA6`` (244, 59, 166).
    r = 223 + (digest[8] % 33)
    g = 39 + (digest[9] % 40)
    b = 146 + (digest[10] % 40)
    color = f"#{r:02x}{g:02x}{b:02x}"
    return {"lines": list(MASCOT_ART_LINES), "color": color}
