"""Read a completed tool call's result: helpers for the PR observer and provider hooks.

A result can be a harness envelope (``stdout``, ``content``, ``structuredContent``,
and so on), JSON text, or plain text, nested in any mix. The observer imports this
module on every tool completion, so it imports only the PR reference model.
"""

from __future__ import annotations

import json

from omnigent.runner.session_prs import PullRequestRef


def pr_reference(value: object) -> PullRequestRef | None:
    """Return the PR that a URL names, ignoring trailing punctuation.

    :param value: Any value; only a string that a registered provider parses as a
        PR URL names a PR, e.g. ``"https://github.com/o/r/pull/7)."``.
    :returns: The normalized reference, or ``None``.
    """
    if not isinstance(value, str):
        return None
    try:
        return PullRequestRef.from_url(value.rstrip(".,);]"))
    except ValueError:
        return None


def _result_parts(result: object, depth: int = 0) -> list[dict[str, object] | str]:
    """Unwrap tool envelopes and JSON text, excluding body/description fields."""
    if depth > 6:
        return []
    if isinstance(result, str):
        # Compound shell output can interleave JSON responses, URL lines, and logs.
        parts: list[dict[str, object] | str] = []
        decoder = json.JSONDecoder()
        index = 0
        while index < len(result):
            if result[index].isspace():
                index += 1
                continue
            position = index
            if result[index] in '{["':
                try:
                    value, position = decoder.raw_decode(result, index)
                except ValueError as error:
                    # Keep incomplete JSON together instead of re-parsing its nested lines.
                    position = error.pos if isinstance(error, json.JSONDecodeError) else index
                else:
                    line_end = result.find("\n", position)
                    if not result[position : line_end if line_end != -1 else len(result)].strip():
                        parts.extend(_result_parts(value, depth + 1))
                        index = position
                        continue
            end = result.find("\n", position)
            if end == -1:
                end = len(result)
            parts.append(result[index:end])
            index = end
        return parts
    if isinstance(result, list):
        return [part for item in result[:100] for part in _result_parts(item, depth + 1)]
    if not isinstance(result, dict):
        return []
    found: list[dict[str, object] | str] = [result]
    for key in (
        "content",
        "structuredContent",
        "text",
        "result",
        "data",
        "pull_request",
        "stdout",
        "stderr",
        "output",
        "aggregatedOutput",
        "metadata",
    ):
        if key in result:
            found.extend(_result_parts(result[key], depth + 1))
    return found


def result_objects(result: object) -> list[dict[str, object]]:
    """Return every JSON object in a tool result, the envelope objects included.

    JSON in text counts only when it ends its line. Only envelope fields such as
    ``content``, ``stdout``, and ``result`` are unwrapped, so JSON quoted in a
    body or description field is not.
    """
    return [part for part in _result_parts(result) if isinstance(part, dict)]


def output_text(result: object) -> str:
    """Return the plain-text lines of a tool result; JSON that ends its line is left out."""
    return "\n".join(part for part in _result_parts(result) if isinstance(part, str))
