"""``sys_ask_user_question`` protocol adapter for session routes.

Pure functions that bridge the cross-harness ``sys_ask_user_question``
builtin tool to the platform's existing elicitation engine — the same
``ElicitationRequestParams`` / ``ElicitationResult`` machinery the
Claude-native ``PermissionRequest`` ASK gate uses (see
:func:`omnigent.server.routes._sessions.orchestration._publish_and_wait_for_harness_elicitation`).
No new plumbing: this module only shapes the request going in and
reconstructs the answer coming out.

Modeled closely on Claude Code's own ``AskUserQuestionInput`` /
``AskUserQuestionOutput`` contract (see ``sdk-tools.d.ts`` in the
``@anthropic-ai/claude-code`` package), with one deliberate addition:
a boolean ``recommended`` flag on individual options, which neither
Claude Code's tool nor this platform's existing ``ElicitationRequestParams``
/ ``ElicitationResult`` schemas carry. It rides through as an ordinary
JSON field — no schema change needed, since ``ElicitationRequestParams``
already allows extra fields.

The wire format in between is flat: ``ElicitationResult.content`` is a
``dict[str, str | int | float | bool | list[str] | None]`` (MCP's
``ElicitResult.content`` shape — no nested objects). The web form
(``AskUserQuestionForm.tsx``) submits one key per question (the question
text), valued as the selected option label(s) — a list for multi-select,
a scalar for single-select. :func:`build_ask_user_question_params` shapes
the *request* side of that flat contract; :func:`reconstruct_ask_user_question_output`
reverses it, rebuilding the rich, Claude-Code-shaped output the calling
agent expects — including a top-level ``response`` free-text fallback for
a reply that doesn't key off any known question (e.g. a client that
answers with one blob of typed text instead of the structured form).
"""

from __future__ import annotations

import json
from typing import Any

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.schemas import ElicitationRequestParams, ElicitationResult

MIN_QUESTIONS = 1
MAX_QUESTIONS = 4
MIN_OPTIONS = 2
MAX_OPTIONS = 4

_PHASE = "tool_call"
_POLICY_NAME = "sys_ask_user_question"
_PREVIEW_PREFIX = "AskUserQuestion("


def validate_ask_user_question_questions(raw: Any) -> list[dict[str, Any]]:
    """
    Validate and normalize the tool call's ``questions`` argument.

    Mirrors the shape Claude Code's own ``AskUserQuestionInput`` enforces
    (1-4 questions, 2-4 options each) plus the platform-specific
    ``recommended`` option flag. Unlike the permissive server-side
    parsers for harness-originated payloads (e.g.
    ``_structured_ask_user_question``, which silently drops malformed
    entries because Claude already validated them client-side), this
    function is the FIRST validation the tool's own arguments see, so it
    fails loudly on a malformed shape rather than quietly dropping
    questions the agent asked for.

    At most one option per question should be marked ``recommended`` —
    if the caller marks more than one (a model mistake, not a contract
    violation), only the first is kept; the rest are normalized to
    ``False`` rather than failing the call.

    :param raw: The tool call's ``questions`` argument, e.g.
        ``[{"question": "...", "header": "...", "options": [...],
        "multiSelect": False}]``.
    :returns: Normalized question dicts, each with ``question``,
        ``header``, ``options`` (each with ``label``, ``description``,
        optional ``preview``, ``recommended``), and ``multiSelect``.
    :raises OmnigentError: If the shape doesn't satisfy the contract.
    """
    if not isinstance(raw, list) or not (MIN_QUESTIONS <= len(raw) <= MAX_QUESTIONS):
        raise OmnigentError(
            f"sys_ask_user_question requires {MIN_QUESTIONS}-{MAX_QUESTIONS} questions.",
            code=ErrorCode.INVALID_INPUT,
        )
    questions: list[dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise OmnigentError(
                "sys_ask_user_question: each question must be an object.",
                code=ErrorCode.INVALID_INPUT,
            )
        question_text = entry.get("question")
        if not isinstance(question_text, str) or not question_text:
            raise OmnigentError(
                "sys_ask_user_question: each question requires a non-empty 'question' string.",
                code=ErrorCode.INVALID_INPUT,
            )
        header = entry.get("header")
        if not isinstance(header, str) or not header:
            raise OmnigentError(
                "sys_ask_user_question: each question requires a non-empty 'header' string.",
                code=ErrorCode.INVALID_INPUT,
            )
        options_raw = entry.get("options")
        valid_option_count = isinstance(options_raw, list) and (
            MIN_OPTIONS <= len(options_raw) <= MAX_OPTIONS
        )
        if not valid_option_count:
            raise OmnigentError(
                f"sys_ask_user_question: each question requires "
                f"{MIN_OPTIONS}-{MAX_OPTIONS} options.",
                code=ErrorCode.INVALID_INPUT,
            )
        options: list[dict[str, Any]] = []
        seen_recommended = False
        for opt in options_raw:
            if not isinstance(opt, dict):
                raise OmnigentError(
                    "sys_ask_user_question: each option must be an object.",
                    code=ErrorCode.INVALID_INPUT,
                )
            label = opt.get("label")
            description = opt.get("description")
            if not isinstance(label, str) or not label:
                raise OmnigentError(
                    "sys_ask_user_question: each option requires a non-empty 'label' string.",
                    code=ErrorCode.INVALID_INPUT,
                )
            if not isinstance(description, str) or not description:
                raise OmnigentError(
                    "sys_ask_user_question: each option requires a "
                    "non-empty 'description' string.",
                    code=ErrorCode.INVALID_INPUT,
                )
            option: dict[str, Any] = {"label": label, "description": description}
            preview = opt.get("preview")
            if isinstance(preview, str) and preview:
                option["preview"] = preview
            recommended = opt.get("recommended") is True and not seen_recommended
            if recommended:
                seen_recommended = True
            option["recommended"] = recommended
            options.append(option)
        questions.append(
            {
                "question": question_text,
                "header": header,
                "options": options,
                "multiSelect": entry.get("multiSelect") is True,
            }
        )
    return questions


def build_ask_user_question_params(questions: list[dict[str, Any]]) -> ElicitationRequestParams:
    """
    Build ``ElicitationRequestParams`` for a ``sys_ask_user_question`` call.

    Reuses the exact ``ask_user_question`` extra the web UI's
    ``ApprovalCard`` / ``AskUserQuestionForm`` already know how to render
    (see ``_structured_ask_user_question`` in
    ``omnigent/server/routes/_sessions/helpers.py`` and
    ``@/lib/askUserQuestion.ts``), so no new UI render mode is needed —
    only the ``recommended`` field is new. ``requestedSchema`` is left
    ``None`` (matching the Claude-native AskUserQuestion PermissionRequest
    stamp and Codex's ``requestUserInput`` params): the structured
    ``ask_user_question`` extra is the authoritative payload, not the MCP
    form schema.

    :param questions: Normalized questions from
        :func:`validate_ask_user_question_questions`.
    :returns: Params ready for
        :func:`~omnigent.server.routes._sessions.orchestration._publish_and_wait_for_harness_elicitation`.
    """
    ask_payload = {"questions": questions}
    message = (
        "Claude wants to ask you a question"
        if len(questions) == 1
        else f"Claude wants to ask you {len(questions)} questions"
    )
    content_preview = f"{_PREVIEW_PREFIX}{json.dumps(ask_payload, ensure_ascii=False)[:1000]})"
    return ElicitationRequestParams(
        mode="form",
        message=message,
        requestedSchema=None,
        url=None,
        phase=_PHASE,
        policy_name=_POLICY_NAME,
        content_preview=content_preview,
        ask_user_question=ask_payload,
    )


def _question_key(question: dict[str, Any]) -> str:
    """Stable answer key for one question — matches the web form's ``questionKey``."""
    return question["question"]


def reconstruct_ask_user_question_output(
    questions: list[dict[str, Any]],
    result: ElicitationResult | None,
) -> dict[str, Any]:
    """
    Reconstruct the tool's return value from a flat elicitation verdict.

    The inverse of the flatten step in :func:`build_ask_user_question_params`:
    ``ElicitationResult.content`` is a flat ``{question_text: answer}``
    map (MCP's ``ElicitResult.content`` shape — no nested objects,
    matching what ``AskUserQuestionForm.handleSubmit`` posts). This
    rebuilds the rich, Claude-Code-``AskUserQuestionOutput``-shaped
    object the calling agent expects: the echoed ``questions``, an
    ``answers`` map keyed by question text (list value for multi-select,
    scalar for single-select — richer than Claude Code's own
    comma-joined-string convention, since our wire format natively
    supports string lists), and an optional top-level ``response`` for a
    free-text reply that doesn't key off any known question (e.g. a
    minimal client that replies with one blob of text instead of driving
    the structured form).

    :param questions: The same normalized questions passed to
        :func:`build_ask_user_question_params` — echoed back to the
        agent so it doesn't have to recall its own call arguments.
    :param result: The verdict from
        :func:`~omnigent.server.routes._sessions.orchestration._publish_and_wait_for_harness_elicitation`,
        or ``None`` on timeout / upstream disconnect.
    :returns: ``{"questions": [...], "answers": {...}}`` on a normal
        accept, with an optional ``response`` key; ``{"questions": [...],
        "answers": {}, "error": "..."}`` on timeout / decline / cancel.
    """
    if result is None:
        return {"questions": questions, "answers": {}, "error": "timed out waiting for a response"}
    if result.action == "decline":
        return {"questions": questions, "answers": {}, "error": "the user declined to answer"}
    if result.action != "accept":
        return {
            "questions": questions,
            "answers": {},
            "error": "the user cancelled without answering",
        }

    content = result.content or {}
    known_keys = {_question_key(q) for q in questions}
    answers: dict[str, str | list[str]] = {}
    response: str | None = None
    for key, value in content.items():
        if key in known_keys:
            answers[key] = _normalize_answer_value(value)
        else:
            # A reply that doesn't key off any known question — e.g. a
            # minimal/legacy client that submits ``{"response": "..."}``
            # instead of driving the structured per-question form.
            response = _stringify(value) if response is None else response
    output: dict[str, Any] = {"questions": questions, "answers": answers}
    if response is not None:
        output["response"] = response
    return output


def _normalize_answer_value(value: object) -> str | list[str]:
    """Normalize one flat content value to an answer (string or string list)."""
    if isinstance(value, list):
        return [str(v) for v in value]
    return _stringify(value)


def _stringify(value: object) -> str:
    """Render a flat content scalar as a display string."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)
