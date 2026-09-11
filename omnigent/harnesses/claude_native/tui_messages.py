"""Read-only extraction of Claude dialogs absent from its transcript."""

from __future__ import annotations

import re

_OPTION = re.compile(r"^\s*(?P<caret>[❯›>])?\s*(?P<number>\d+)\.\s+(?P<label>\S.*)$")
_BORDER = re.compile(r"^\s*[╭╰┌└┏┗╔╚]?[─━═]{8,}[╮╯┐┘┓┛╗╝]?\s*$")
_SIDES = re.compile(r"^\s*[│┃║] ?|\s*[│┃║]\s*$")
_FOOTER = re.compile(r"\b(?:Esc|Enter|Tab) to (?:cancel|confirm|select|continue|amend)\b")
_TITLE = re.compile(r"^\s*(?:Tool use|Bash command|Permission request)\s*$")


def terminal_message_from_pane(pane: str) -> str | None:
    """Return the visible dialog, including context and choices, without driving it."""
    lines = [_SIDES.sub("", line) for line in pane.splitlines()]
    selected = next(
        (
            index
            for index in range(len(lines) - 1, -1, -1)
            if (match := _OPTION.match(lines[index])) and match.group("caret")
        ),
        None,
    )
    if selected is None:
        return None
    start = next(
        (index + 1 for index in range(selected - 1, -1, -1) if _BORDER.match(lines[index])),
        0,
    )
    title = next((index for index in range(start, selected) if _TITLE.match(lines[index])), None)
    if title is not None:
        start = title
    footer = next(
        (index for index in range(selected + 1, len(lines)) if _FOOTER.search(lines[index])),
        None,
    )
    end = footer + 1 if footer is not None else len(lines)
    region = lines[start:end]
    options = [match for line in region if (match := _OPTION.match(line))]
    if len(options) < 2 or len({match.group("number") for match in options}) != len(options):
        return None
    if footer is None and not (
        title is not None and any(line.strip().endswith("?") for line in region)
    ):
        return None
    if any(
        line.lstrip().startswith(("❯", "›", ">")) and not _OPTION.match(line)
        for line in lines[selected + 1 :]
    ):
        return None
    normalized = [
        f"{match.group('number')}. {match.group('label')}"
        if (match := _OPTION.match(line))
        else line.strip()
        for line in region
        if not _BORDER.match(line)
    ]
    return "\n".join(normalized).strip() or None
