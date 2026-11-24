"""Find, replace and diff-target markdown sections of a project mandate.

A section starts at a heading line (``#`` to ``######``) and runs to the next
heading of the same or a higher level. Headings inside fenced code blocks
don't count. Titles match case-insensitively with whitespace collapsed.
"""

from __future__ import annotations

import hashlib
import re

from omnigent.entities.project import MandateProposal

_HEADING = re.compile(r"^(#{1,6})[ \t]+(.*?)[ \t]*#*[ \t]*$")
_FENCE = re.compile(r"^[ \t]{0,3}(```|~~~)")


def normalize_title(title: str) -> str:
    """Return the comparable form of a heading title, e.g. ``"  How  Work flows"`` -> ``"how work flows"``."""
    return " ".join(title.split()).casefold()


def _headings(mandate: str) -> list[tuple[int, int, str]]:
    """Return ``(offset, level, title)`` for each heading line outside code fences."""
    found: list[tuple[int, int, str]] = []
    offset = 0
    fence: str | None = None
    for line in mandate.splitlines(keepends=True):
        stripped = line.rstrip("\r\n")
        fence_match = _FENCE.match(stripped)
        if fence_match:
            marker = fence_match.group(1)
            fence = None if fence == marker else (fence or marker)
        elif fence is None:
            heading = _HEADING.match(stripped)
            if heading:
                found.append((offset, len(heading.group(1)), heading.group(2)))
        offset += len(line)
    return found


def section_spans(mandate: str, title: str) -> list[tuple[int, int]]:
    """Return the ``(start, end)`` character spans of every section titled *title*."""
    wanted = normalize_title(title)
    headings = _headings(mandate)
    spans = []
    for index, (start, level, heading_title) in enumerate(headings):
        if normalize_title(heading_title) != wanted:
            continue
        end = next(
            (offset for offset, other, _ in headings[index + 1 :] if other <= level),
            len(mandate),
        )
        spans.append((start, end))
    return spans


def text_hash(text: str) -> str:
    """SHA-256 hex of *text* with surrounding whitespace removed."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def _join(*parts: str) -> str:
    return "\n\n".join(part.strip("\n") for part in parts if part.strip())


def proposal_target(mandate: str | None, proposal: MandateProposal) -> str | None:
    """Return the current text a replacement would remove, or ``None`` if its section is gone.

    Append proposals remove nothing, so they return ``""``.
    """
    current = mandate or ""
    if proposal.mode == "replace_all":
        return current.strip()
    if proposal.mode == "replace_section":
        spans = section_spans(current, proposal.section or "")
        if len(spans) != 1:
            return None
        start, end = spans[0]
        return current[start:end].strip()
    return ""


def proposal_is_stale(mandate: str | None, proposal: MandateProposal) -> bool:
    """Whether the text a replacement was written against has changed or disappeared."""
    if proposal.mode == "append":
        return False
    target = proposal_target(mandate, proposal)
    return target is None or text_hash(target) != proposal.base_hash


def apply_proposal(mandate: str | None, proposal: MandateProposal) -> str:
    """Return the mandate with *proposal* applied; the caller checks staleness first.

    :raises ValueError: If a section replacement no longer finds exactly one section.
    """
    current = mandate or ""
    if proposal.mode == "replace_all":
        return proposal.text.strip()
    if proposal.mode == "replace_section":
        spans = section_spans(current, proposal.section or "")
        if len(spans) != 1:
            raise ValueError(f"expected one section titled {proposal.section!r}, found {len(spans)}")
        start, end = spans[0]
        return _join(current[:start], proposal.text, current[end:])
    return _join(current, proposal.text)
