"""Canvas entity — the agent-authored artifact rendered in the right pane.

A canvas is a single rendered artifact per conversation (Cursor-style): the
agent sets HTML or Markdown content via the ``set_canvas`` tool, and the web
UI renders it in a right-rail Canvas tab. One canvas per conversation; setting
it again overwrites.
"""

from __future__ import annotations

from dataclasses import dataclass

# Allowed canvas content types. HTML renders in a sandboxed iframe; Markdown
# renders via the app's markdown viewer.
CANVAS_CONTENT_TYPES: frozenset[str] = frozenset({"html", "markdown"})

# Upper bound on canvas content, measured as UTF-8 bytes. A canvas is a single
# rendered artifact, not a file store — cap it so a runaway agent (or a hostile
# caller hitting the REST endpoint) can't persist an unbounded blob per
# conversation. 1 MB comfortably fits a rich report / interactive widget.
MAX_CANVAS_CONTENT_BYTES: int = 1_000_000


@dataclass
class Canvas:
    """
    A conversation's canvas artifact.

    :param id: Opaque primary key, e.g. ``"cnv_a1b2c3..."``.
    :param conversation_id: The conversation this canvas belongs to (unique).
    :param title: Short human-readable title shown in the tab header.
    :param content: The artifact body — HTML or Markdown source.
    :param content_type: ``"html"`` or ``"markdown"`` (see
        :data:`CANVAS_CONTENT_TYPES`).
    :param created_at: Unix epoch seconds at row creation.
    :param updated_at: Unix epoch seconds of the last write, or ``None`` if
        never updated since creation.
    """

    id: str
    conversation_id: str
    title: str
    content: str
    content_type: str
    created_at: int
    updated_at: int | None = None
