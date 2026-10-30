"""Inline multiple-choice selector state for :class:`TerminalHost`.

Agents ask questions with options (Claude's ``AskUserQuestion``, Pi dialogs,
Codex and Cursor questions). The host shows one question at a time below the
toolbar: arrows move the highlight, Space toggles an option in multi-select,
Enter answers, Esc dismisses. When typed answers are allowed, a
"Type something" row takes the text in the selector's answer line; a
single-select answer uses that text only while the row is highlighted.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass

from prompt_toolkit.utils import get_cwidth

Answers = dict[str, str | list[str]]

CUSTOM_ROW_LABEL = "Type something"
# Option rows shown at once; longer lists scroll with the highlight.
_MAX_VISIBLE_ROWS = 8
# Question rows in the selector; a longer question is shown in full above it.
_MAX_QUESTION_ROWS = 3


@dataclass(frozen=True)
class QuestionSpec:
    """One question for :meth:`TerminalHost.ask_questions`.

    :param key: Answer key, e.g. the question's ``id`` or its text.
    :param question: Question text, e.g. ``"Which environment?"``.
    :param header: Short badge, e.g. ``"Environment"``; empty when absent.
    :param options: ``(label, description)`` pairs in display order.
    :param multi_select: Whether several options may be chosen.
    :param allow_custom: Whether a typed answer is accepted.
    :param text_default: Prefill for an editor-style question (no options),
        whose exact text, empty included, is the answer; ``None`` otherwise.
    """

    key: str
    question: str
    header: str = ""
    options: tuple[tuple[str, str], ...] = ()
    multi_select: bool = False
    allow_custom: bool = True
    text_default: str | None = None


class QuestionPicker:
    """State of an open selector: the current question, highlight, and answers."""

    def __init__(self, questions: Sequence[QuestionSpec]) -> None:
        """:param questions: Questions to ask in order; at least one."""
        self.questions = list(questions)
        self.index = 0
        self.row = 0
        self.checked: set[int] = set()
        self.answers: Answers = {}
        self.future: asyncio.Future[Answers | None] = asyncio.get_running_loop().create_future()
        self._scroll = 0

    @property
    def current(self) -> QuestionSpec:
        """The question being asked."""
        return self.questions[self.index]

    @property
    def prefill(self) -> str:
        """Answer-line text to start the current question with: an editor's prefill."""
        question = self.current
        return (question.text_default or "") if not question.options else ""

    def row_count(self) -> int:
        """Selectable rows: the options plus the "Type something" row when allowed."""
        question = self.current
        return len(question.options) + (1 if question.options and question.allow_custom else 0)

    def on_custom_row(self) -> bool:
        """Whether the highlight is on the "Type something" row."""
        question = self.current
        return (
            bool(question.options) and question.allow_custom and self.row == len(question.options)
        )

    def can_toggle(self) -> bool:
        """Whether Space toggles the highlighted row (a multi-select option)."""
        return self.current.multi_select and self.row < len(self.current.options)

    def move(self, delta: int) -> None:
        """Move the highlight by *delta*, wrapping around."""
        count = self.row_count()
        if count:
            self.row = (self.row + delta) % count

    def toggle(self) -> None:
        """Check or uncheck the highlighted multi-select option."""
        if self.can_toggle():
            self.checked ^= {self.row}

    def note_typing(self, text: str) -> None:
        """Typing means a custom answer, so highlight its row (as the web form does)."""
        if text and self.current.options and self.current.allow_custom:
            self.row = len(self.current.options)

    def answer(self, text: str) -> bool:
        """Answer the current question from the highlight and the typed *text*.

        :returns: ``True`` if it was answered (the picker then moves to the
            next question or resolves :attr:`future`); ``False`` when nothing
            is chosen yet.
        """
        value = self._value(text)
        if value is None:
            return False
        self.answers[self.current.key] = value
        if self.index + 1 < len(self.questions):
            self.index += 1
            self.row = 0
            self.checked = set()
            self._scroll = 0
        elif not self.future.done():
            self.future.set_result(dict(self.answers))
        return True

    def cancel(self) -> None:
        """Dismiss the selector without answers."""
        if not self.future.done():
            self.future.set_result(None)

    def _value(self, text: str) -> str | list[str] | None:
        question = self.current
        if not question.options:
            if question.text_default is not None:
                # Editor: the exact text is the answer, or its fixed default.
                value = text if question.allow_custom else question.text_default
            else:
                value = text.strip()
                if not value or not question.allow_custom:
                    return None
            return [value] if question.multi_select else value
        if not question.allow_custom and text.strip():
            # Never turn typed text into the highlighted option: typing "No"
            # must not submit a "Yes" that happens to be highlighted.
            return None
        typed = text.strip() if question.allow_custom else ""
        if question.multi_select:
            picked = [question.options[i][0] for i in sorted(self.checked)]
            if typed and typed not in picked:
                picked.append(typed)
            if not picked and not self.on_custom_row():
                picked = [question.options[self.row][0]]
            return picked or None
        if self.on_custom_row():
            return typed or None
        return question.options[self.row][0]

    def render(self, width: int, max_rows: int | None = None) -> list[tuple[str, str]]:
        """Selector lines as prompt-toolkit ``(style, text)`` fragments, one per line.

        :param width: Terminal columns; long lines are wrapped or truncated.
        :param max_rows: Rows available, e.g. ``12``. The question and the
            option list shrink to fit (at least one row each), and the
            options scroll with the highlight; ``None`` uses the default caps.
        """
        question = self.current
        rows_left = (max_rows if max_rows is not None else 1 << 16) - 2  # title, hint
        visible = min(self.row_count(), _MAX_VISIBLE_ROWS, max(1, rows_left - 1))
        question_rows = max(1, min(_MAX_QUESTION_ROWS, rows_left - visible))
        lines: list[tuple[str, str]] = []
        title = f" ? question {self.index + 1} of {len(self.questions)}"
        if question.header:
            title += f" · {_one_line(question.header)}"
        lines.append(("class:prompt-marker", title))
        wrap_width = max(width - 4, 2)
        wrapped = _wrap(question.question, wrap_width)
        if len(wrapped) > question_rows:
            wrapped = wrapped[:question_rows]
            wrapped[-1] = _truncate(f"{wrapped[-1]} …", wrap_width)
        for text_line in wrapped:
            lines.append(("", f"   {text_line}"))
        lines.extend(self._option_lines(visible))
        lines.append(("class:model-name", f" {self._hint()}"))
        return [(style, _truncate(text, width) + "\n") for style, text in lines]

    def line_count(self, width: int, max_rows: int | None = None) -> int:
        """Number of terminal lines :meth:`render` produces."""
        return len(self.render(width, max_rows))

    def question_overflows(self, width: int) -> bool:
        """Whether the current question is too long for the selector at *width*."""
        return len(_wrap(self.current.question, max(width - 4, 2))) > _MAX_QUESTION_ROWS

    def _option_lines(self, visible: int) -> list[tuple[str, str]]:
        question = self.current
        count = self.row_count()
        if not count:
            return []
        self._scroll = min(self._scroll, max(0, count - visible))  # fill a grown window
        if self.row < self._scroll:
            self._scroll = self.row
        elif self.row >= self._scroll + visible:
            self._scroll = self.row - visible + 1
        lines: list[tuple[str, str]] = []
        for row in range(self._scroll, min(count, self._scroll + visible)):
            selected = row == self.row
            marker = "▸" if selected else " "
            if row < len(question.options):
                label, description = (_one_line(part) for part in question.options[row])
                box = ("[x] " if row in self.checked else "[ ] ") if question.multi_select else ""
                text = f" {marker} {box}{row + 1}. {label}"
                if description:
                    text += f" — {description}"
            else:
                text = f" {marker} {row + 1}. {CUSTOM_ROW_LABEL} (type it below)"
            lines.append(("class:prompt-marker" if selected else "class:model-name", text))
        return lines

    def _hint(self) -> str:
        question = self.current
        if not question.options:
            if not question.allow_custom:
                return "⏎ send the answer below · esc dismiss"
            if question.text_default is not None:
                return "edit the answer below · ⏎ send · esc dismiss"
            return "type the answer below · ⏎ send · esc dismiss"
        if question.multi_select:
            return "↑↓ move · space check · ⏎ send checked (or the highlighted one) · esc dismiss"
        return "↑↓ move · ⏎ choose · esc dismiss"


def _wrap(text: str, width: int) -> list[str]:
    """Wrap *text* into lines of at most *width* terminal cells, dropping nothing.

    Breaks at spaces, and inside a run wider than a line (e.g. CJK text).
    """
    lines: list[str] = []
    line = ""
    for word in text.split():
        candidate = f"{line} {word}" if line else word
        if get_cwidth(candidate) <= width:
            line = candidate
            continue
        if line:
            lines.append(line)
        line = ""
        for char in word:
            if line and get_cwidth(line + char) > width:
                lines.append(line)
                line = ""
            line += char
    lines.append(line)
    return lines


def _one_line(text: str) -> str:
    """Collapse newlines and runs of whitespace, so agent text stays on one row."""
    return " ".join(text.split())


def _truncate(text: str, width: int) -> str:
    """Cut *text* to *width* terminal cells, ending in ``…`` when cut."""
    if get_cwidth(text) <= width:
        return text
    out = ""
    for char in text:
        if get_cwidth(out + char + "…") > width:
            break
        out += char
    return out + "…"
