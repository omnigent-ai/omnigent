#!/usr/bin/env python3
"""Add PR-template scaffolding without deleting the author's text."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from validate import TEST_LABELS, TYPE_LABELS

_HEADING_RE = re.compile(r"(?im)^\s*##\s+(.+?)\s*$")


def _has_heading(body: str, heading: str) -> bool:
    wanted = heading.strip().lower()
    return any(match.group(1).strip().lower() == wanted for match in _HEADING_RE.finditer(body))


def _append_section(body: str, heading: str, content: str) -> str:
    if _has_heading(body, heading):
        return body
    return body.rstrip() + f"\n\n## {heading}\n\n{content.rstrip()}\n"


def _checkbox_block(labels: tuple[str, ...]) -> str:
    return "\n".join(f"- [ ] {label}" for label in labels) + "\n"


def format_body(body: str) -> str:
    """Return *body* with missing PR-template sections appended.

    Existing prose is preserved verbatim. When the body has no Summary
    heading, the existing text is placed under Summary so it remains the
    main description instead of being pushed below the template.
    """
    body = body.strip()
    if not body:
        body = "## Summary\n\n"
    elif not _has_heading(body, "Summary"):
        body = f"## Summary\n\n{body}"

    body = _append_section(
        body,
        "Test Plan",
        "How was this change tested? Describe the steps, commands, or scenarios "
        "used to verify it (autoformat added this section — please replace it).",
    )
    body = _append_section(
        body,
        "Demo",
        "<!-- Video or images demonstrating the change. Mandatory for UI / "
        "frontend changes; use 'N/A' otherwise. -->",
    )
    body = _append_section(
        body,
        "ELI5",
        "<!-- Optional: explain the change in plain language. -->",
    )
    body = _append_section(
        body,
        "Diagram",
        "```mermaid\nflowchart LR\n  A[Before] --> B[Change]\n  B --> C[After]\n```",
    )
    body = _append_section(body, "Type of change", _checkbox_block(TYPE_LABELS))
    body = _append_section(body, "Test coverage", _checkbox_block(TEST_LABELS))
    body = _append_section(
        body,
        "Coverage notes",
        "<!-- Optional; required if you checked 'Manual verification completed' "
        "or 'Not applicable' above. -->",
    )
    return body.rstrip() + "\n"


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: format_body.py INPUT OUTPUT", file=sys.stderr)
        return 2
    source = Path(sys.argv[1])
    dest = Path(sys.argv[2])
    dest.write_text(format_body(source.read_text()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
