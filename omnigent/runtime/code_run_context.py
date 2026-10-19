"""The notice that tells the agent about code blocks the user ran.

The owner can run a code block from an agent message. Each run saves a
``started`` and a ``finished`` ``code_run`` item. With the user's next message
the agent gets a summary of those runs as a framework notice, so the stored
user message stays exactly as typed.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence

from omnigent.entities.conversation import (
    CANCELLATION_MARKER_TEXT,
    UNDELIVERED_PROMPT_ERROR_CODES,
)
from omnigent.inner.native_attachments import framework_text_notice_block

OUTPUT_LIMIT_CHARS = 10_000

_HEADER = (
    "The user ran code blocks from your messages since their previous message. "
    "Use these results as context for their message:"
)


def code_run_notice(
    items: Sequence[Mapping[str, object]],
    *,
    active_run_ids: Collection[str] = frozenset(),
) -> str:
    """
    Build the code-run notice for the user message about to be sent.

    Reports the latest run of each block: finished runs whose ``finished``
    item comes after the user's previous message (all of them on the first
    message), and runs with no ``finished`` item yet as still running while
    the runner reports them active. A finished run whose ``started`` item is
    missing, and a started run the runner does not report, are left out.

    :param items: Session items before the message being sent, in store
        order, as flat API dicts, e.g. ``{"type": "code_run", "run_id":
        "run_a", "phase": "started", "command": "ls", ...}``.
    :param active_run_ids: Run ids the runner reports as running; empty
        when unknown, e.g. after a runner restart.
    :returns: The notice text, or ``""`` when there is nothing to report.
    """
    # Known gap: a native message is stored only once the harness echoes it, so
    # a second native message sent before that echo reports the same runs again.
    # A message claude-native holds until a pending prompt is answered keeps the
    # notice built at forward time, so a run finishing during the hold is never
    # reported; the runner's pending-prompt buffer behaves the same way.
    previous_user_index = -1
    started: dict[str, Mapping[str, object]] = {}
    finished: dict[str, tuple[int, Mapping[str, object]]] = {}
    for index, item in enumerate(items):
        if _is_user_message(items, index):
            previous_user_index = index
        elif item.get("type") == "code_run":
            run_id = str(item.get("run_id"))
            if item.get("phase") == "started":
                started[run_id] = item
            elif item.get("phase") == "finished":
                finished[run_id] = (index, item)

    latest_by_block: dict[tuple[object, object], str] = {}
    for run_id, item in started.items():
        latest_by_block[(item.get("message_id"), item.get("block_hash"))] = run_id

    sections: list[str] = []
    for run_id in latest_by_block.values():
        if run_id not in finished:
            if run_id in active_run_ids:
                sections.append(_render_running(started[run_id]))
            continue
        finished_index, finished_item = finished[run_id]
        if finished_index > previous_user_index:
            sections.append(_render_finished(started[run_id], finished_item))
    if not sections:
        return ""
    return "\n\n".join([_HEADER, *sections])


def has_unfinished_runs(items: Sequence[Mapping[str, object]]) -> bool:
    """Whether some ``code_run`` in *items* has a ``started`` item but no ``finished`` one."""
    phases: dict[object, set[object]] = {}
    for item in items:
        if item.get("type") == "code_run":
            phases.setdefault(item.get("run_id"), set()).add(item.get("phase"))
    return any(run_phases == {"started"} for run_phases in phases.values())


def with_code_run_notice(
    content: object,
    items: Sequence[Mapping[str, object]],
    *,
    active_run_ids: Collection[str] = frozenset(),
) -> object:
    """
    Append the code-run notice for *items* to user message content.

    :param content: User message content, a string or a block list, e.g.
        ``[{"type": "input_text", "text": "why?"}]``.
    :param items: Session items before this message, as for
        :func:`code_run_notice`.
    :param active_run_ids: Run ids the runner reports as running.
    :returns: *content* plus a text framework notice block, or *content*
        unchanged when there is nothing to report.
    """
    notice = code_run_notice(items, active_run_ids=active_run_ids)
    blocks = [{"type": "input_text", "text": content}] if isinstance(content, str) else content
    if not notice or not isinstance(blocks, list):
        return content
    return [*blocks, framework_text_notice_block(notice)]


def _is_user_message(items: Sequence[Mapping[str, object]], index: int) -> bool:
    """Whether ``items[index]`` is a message the user sent and the agent received."""
    item = items[index]
    if item.get("type") != "message" or item.get("role") != "user" or item.get("is_meta"):
        return False
    if _message_text(item.get("content")) == CANCELLATION_MARKER_TEXT:
        return False
    following = items[index + 1] if index + 1 < len(items) else None
    return not (
        following is not None
        and following.get("type") == "error"
        and following.get("code") in UNDELIVERED_PROMPT_ERROR_CODES
    )


def _message_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        text
        for block in content
        if isinstance(block, Mapping) and isinstance(text := block.get("text"), str)
    )


def _render_running(started: Mapping[str, object]) -> str:
    return "\n".join(
        [
            f"Run of a {started.get('language')} block: still running, no output yet.",
            "Command:",
            _fenced(str(started.get("command") or "")),
        ]
    )


def _render_finished(started: Mapping[str, object], finished: Mapping[str, object]) -> str:
    exit_code = finished.get("exit_code")
    duration_ms = finished.get("duration_ms")
    duration = f"{duration_ms / 1000:.1f} s" if isinstance(duration_ms, int) else "unknown"
    lines = [
        f"Run of a {started.get('language')} block: {finished.get('status')}, "
        f"exit code {exit_code if exit_code is not None else 'none'}, took {duration}.",
        "Command:",
        _fenced(str(started.get("command") or "")),
    ]
    output = _output_text(finished.get("output"))
    if not output:
        lines.append("Output: none.")
        return "\n".join(lines)
    notes: list[str] = []
    if finished.get("truncated"):
        notes.append("the saved output was already cut to its start and end")
    if len(output) > OUTPUT_LIMIT_CHARS:
        notes.append(
            f"showing {OUTPUT_LIMIT_CHARS:,} of {len(output):,} characters, start and end kept"
        )
        half = OUTPUT_LIMIT_CHARS // 2
        output = f"{output[:half]}\n[...]\n{output[-half:]}"
    lines.append(f"Output ({'; '.join(notes)}):" if notes else "Output:")
    lines.append(_fenced(output))
    return "\n".join(lines)


def _output_text(segments: object) -> str:
    if not isinstance(segments, list):
        return ""
    return "".join(
        text
        for segment in segments
        if isinstance(segment, Mapping) and isinstance(text := segment.get("text"), str)
    )


def _fenced(text: str) -> str:
    """Fence *text* with more backticks than any run inside it."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}\n{text.rstrip(chr(10))}\n{fence}"
