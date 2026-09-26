"""Structured extraction of Claude's AskUserQuestion tool input.

Shared by the server permission/elicitation routes and the claude-sdk
elicitation bridge (``runtime/harnesses/_executor_adapter.py``). Both ship
the returned structure to the UI as the ``ask_user_question`` extra on
``ElicitationRequestParams`` so the front-end renders an interactive option
form instead of the truncated ``content_preview`` blob.

Kept dependency-free (stdlib/typing only) so runtime code can import it
without pulling in server-route modules.
"""

from __future__ import annotations

from typing import Any


def structured_ask_user_question(tool_input: Any) -> dict[str, Any] | None:
    """Build a structured AskUserQuestion payload for elicitation extras.

    The returned shape is the same one the UI's
    :file:`web/src/lib/askUserQuestion.ts` consumes, so the front-end can
    render an interactive option form directly.

    :param tool_input: The ``tool_input`` field from the tool call.
    :returns: ``{"questions": [...]}`` on success, or ``None`` when the
        input doesn't carry a usable AskUserQuestion shape (no questions,
        malformed options, etc.) — caller falls back to the binary
        preview-only render.
    """
    if not isinstance(tool_input, dict):
        return None
    questions_raw = tool_input.get("questions")
    if not isinstance(questions_raw, list) or not questions_raw:
        return None
    questions: list[dict[str, Any]] = []
    for entry in questions_raw:
        if not isinstance(entry, dict):
            continue
        question_text = entry.get("question")
        if not isinstance(question_text, str) or not question_text:
            continue
        options_raw = entry.get("options")
        if not isinstance(options_raw, list):
            continue
        options: list[dict[str, Any]] = []
        for opt in options_raw:
            if isinstance(opt, dict):
                label = opt.get("label")
                if not isinstance(label, str) or not label:
                    continue
                option: dict[str, Any] = {"label": label}
                description = opt.get("description")
                if isinstance(description, str) and description:
                    option["description"] = description
                # ``preview`` is an optional richer snippet some
                # Claude builds attach to an option (rendered as a
                # <pre> below the option list when selected). Ride
                # it through verbatim so the UI can surface it.
                preview = opt.get("preview")
                if isinstance(preview, str) and preview:
                    option["preview"] = preview
                options.append(option)
            elif isinstance(opt, str) and opt:
                options.append({"label": opt})
        if not options:
            continue
        question: dict[str, Any] = {
            "question": question_text,
            "options": options,
            "multiSelect": entry.get("multiSelect") is True,
        }
        header = entry.get("header")
        if isinstance(header, str) and header:
            question["header"] = header
        questions.append(question)
    if not questions:
        return None
    return {"questions": questions}
