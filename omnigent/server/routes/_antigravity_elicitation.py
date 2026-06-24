"""Agy (antigravity) elicitation protocol adapters.

Pure shape-mapping — no I/O, no RPC calls.  The bridge (Task 8) and
the endpoint (Task 9) handle all network interaction.

Two functions are exported:

* :func:`to_elicitation_params` — converts a :class:`PendingInteraction`
  dict (produced by :func:`omnigent.antigravity_native_steps.pending_interaction`)
  into an :class:`~omnigent.server.schemas.ElicitationRequestParams` that
  the web UI can render.

* :func:`to_interaction_payload` — converts the user's
  :class:`~omnigent.server.schemas.ElicitationResult` back into the
  ``payload`` dict that ``HandleCascadeUserInteraction`` expects.
"""

from __future__ import annotations

import logging
from typing import Any

from omnigent.server.schemas import ElicitationRequestParams, ElicitationResult

_logger = logging.getLogger(__name__)


def to_elicitation_params(pending: dict[str, Any]) -> ElicitationRequestParams:
    """
    Convert an agy WAITING pending-interaction dict into elicitation params.

    :param pending: A ``PendingInteraction`` dict with keys ``kind``,
        ``trajectory_id``, ``step_index``, and ``spec``.
    :returns: AP/MCP-shaped elicitation params for the web UI.
    :raises ValueError: When ``kind`` is not ``"ask_question"`` or
        ``"permission"``.
    """
    kind: object = pending.get("kind")
    trajectory_id: object = pending.get("trajectory_id")
    step_index: object = pending.get("step_index")
    spec: object = pending.get("spec")

    if kind == "ask_question":
        return _agy_ask_question_params(
            trajectory_id=trajectory_id,
            step_index=step_index,
            spec=spec,
        )
    if kind == "permission":
        return _agy_permission_params(
            trajectory_id=trajectory_id,
            step_index=step_index,
            spec=spec,
        )
    raise ValueError(f"Unsupported agy interaction kind: {kind!r}")


def _agy_questions_to_ask_user_question(spec: object) -> list[dict[str, Any]]:
    """
    Convert an agy ``askQuestion`` spec to the web UI's AskUserQuestion shape.

    The web UI already ships an interactive ``AskUserQuestionForm`` keyed on the
    ``ask_user_question`` params extra (Claude Code's built-in tool); reusing it
    is what makes an agy question render with selectable options AND round-trip a
    real answer (the SPA's generic approve/reject card cannot). The mapping:

    * each agy ``questions[i]`` → one Claude question with a synthetic string
      ``id`` equal to its index ``i``, so the form keys its answer by that id
      (robust to duplicate question text) and :func:`_agy_ask_question_response`
      can line answers back up to agy questions positionally;
    * agy ``is_multi_select`` → Claude ``multiSelect`` (radios vs checkboxes);
    * each agy option's ``text`` → the Claude option ``label`` (the human-
      readable choice the form displays and returns). The agy option ``id`` is
      NOT sent to the SPA — the form returns the chosen *label*, which
      :func:`_agy_ask_question_response` maps back to the agy id by ``text``.

    :param spec: The ``requestedInteraction.askQuestion`` block.
    :returns: A list of Claude-shaped question dicts (possibly empty).
    """
    out: list[dict[str, Any]] = []
    if not isinstance(spec, dict):
        return out
    questions = spec.get("questions")
    if not isinstance(questions, list):
        return out
    for i, question in enumerate(questions):
        if not isinstance(question, dict):
            continue
        text = question.get("question")
        if not isinstance(text, str) or not text:
            continue
        options: list[dict[str, Any]] = []
        raw_options = question.get("options")
        if isinstance(raw_options, list):
            for opt in raw_options:
                if isinstance(opt, dict):
                    label = opt.get("text")
                    if isinstance(label, str) and label:
                        options.append({"label": label})
        out.append(
            {
                "id": str(i),
                "question": text,
                "options": options,
                "multiSelect": bool(question.get("is_multi_select")),
            }
        )
    return out


def _agy_ask_question_params(
    trajectory_id: object,
    step_index: object,
    spec: object,
) -> ElicitationRequestParams:
    """
    Build elicitation params for an ``ask_question`` pending interaction.

    Stamps the question under ``ask_user_question`` (Claude Code's built-in
    AskUserQuestion key) so the web UI's existing interactive form renders the
    options and returns a real selection. The raw agy ``askQuestion`` block is
    ALSO forwarded under ``ask_question`` (carrying option ids + verbatim
    question text) so the response mapper can translate the form's option
    *labels* back to agy option *ids*.

    :param trajectory_id: agy trajectory id string.
    :param step_index: Step index integer (0 when absent).
    :param spec: The ``requestedInteraction.askQuestion`` block; carries
        ``questions[].{question, is_multi_select, options[].{id, text}}``.
    :returns: ``ElicitationRequestParams`` for an ask-question form.
    """
    extras: dict[str, Any] = {
        "ask_user_question": {"questions": _agy_questions_to_ask_user_question(spec)},
        "ask_question": spec,
    }
    if isinstance(trajectory_id, str) and trajectory_id:
        extras["trajectory_id"] = trajectory_id
    if isinstance(step_index, int):
        extras["step_index"] = step_index
    return ElicitationRequestParams(
        mode="form",
        message="Antigravity needs your input",
        requestedSchema=None,
        url=None,
        phase="agy_ask_question",
        policy_name="agy_native_ask_question",
        **extras,
    )


def _agy_permission_params(
    trajectory_id: object,
    step_index: object,
    spec: object,
) -> ElicitationRequestParams:
    """
    Build elicitation params for a ``permission`` pending interaction.

    :param trajectory_id: agy trajectory id string.
    :param step_index: Step index integer (0 when absent).
    :param spec: The ``requestedInteraction.permission`` block; carries
        ``resource.{action, target}`` and ``actionDescription``.
    :returns: ``ElicitationRequestParams`` for a binary command-approval card.
    """
    command: str | None = None
    if isinstance(spec, dict):
        resource = spec.get("resource")
        if isinstance(resource, dict):
            target = resource.get("target")
            if isinstance(target, str) and target:
                command = target

    message = "Antigravity wants to run a command"
    if command:
        message = f"Antigravity wants to run **{command}**"

    extras: dict[str, Any] = {
        "permission_spec": spec,
    }
    if command:
        extras["command"] = command
    if isinstance(trajectory_id, str) and trajectory_id:
        extras["trajectory_id"] = trajectory_id
    if isinstance(step_index, int):
        extras["step_index"] = step_index
    return ElicitationRequestParams(
        mode="form",
        message=message,
        requestedSchema=None,
        url=None,
        phase="agy_permission",
        policy_name="agy_native_permission",
        **extras,
    )


def to_interaction_payload(
    kind: str,
    result: ElicitationResult,
    spec: dict[str, Any],
) -> dict[str, Any]:
    """
    Convert an elicitation result into a ``HandleCascadeUserInteraction`` payload.

    :param kind: Interaction kind — ``"ask_question"`` or ``"permission"``.
    :param result: The web-submitted elicitation verdict.
    :param spec: The original ``requestedInteraction.askQuestion`` or
        ``requestedInteraction.permission`` block (used to look up verbatim
        question text for ask_question responses).
    :returns: The variant dict that becomes the ``interaction`` body:
        ``{"askQuestion": {"responses": [...]}}`` or
        ``{"permission": {"allow": <bool>}}``.
    :raises ValueError: When ``kind`` is not ``"ask_question"`` or
        ``"permission"``.
    """
    if kind == "ask_question":
        return _agy_ask_question_response(result, spec)
    if kind == "permission":
        return _agy_permission_response(result)
    raise ValueError(f"Unsupported agy interaction kind: {kind!r}")


def _answer_labels(answer: object) -> list[str]:
    """
    Normalize one web-form answer value to a list of selected option labels.

    The ``AskUserQuestionForm`` posts a single-select answer as a string label
    and a multi-select answer as a list of string labels (plus, in either case,
    any free-form custom text as one more label). This flattens both to a list,
    dropping non-string / empty entries.

    :param answer: The per-question value from ``ElicitationResult.content``.
    :returns: Selected labels (possibly empty).
    """
    if isinstance(answer, str):
        return [answer] if answer else []
    if isinstance(answer, list):
        return [a for a in answer if isinstance(a, str) and a]
    return []


def _agy_ask_question_response(
    result: ElicitationResult,
    spec: dict[str, Any],
) -> dict[str, Any]:
    """
    Build the ``askQuestion`` interaction payload from a web result.

    The web UI renders the question via its ``AskUserQuestionForm`` (see
    :func:`_agy_questions_to_ask_user_question`) and, on submit, posts a flat
    ``content`` map keyed by each question's synthetic id (its index) — the
    value being the selected option *label(s)* (single-select → one string,
    multi-select → list of strings), or free-form custom text. agy's
    ``HandleCascadeUserInteraction`` instead wants option *ids* per question, so
    this maps each selected label back to its agy option id by matching option
    ``text``; any label with no matching option is forwarded as
    ``writeInResponse`` (the user's custom answer). EVERY question is answered
    (the form requires it), so multi-question prompts now round-trip fully.

    On ``decline`` / ``cancel`` (or a non-dict content), an empty ``responses``
    list is returned so the caller can forward it without special-casing.

    :param result: Web-submitted elicitation verdict.
    :param spec: The ``askQuestion`` spec block; supplies the option id↔text map
        and verbatim question text.
    :returns: ``{"askQuestion": {"responses": [...]}}`` (one entry per question).
    """
    if result.action != "accept" or not isinstance(result.content, dict):
        return {"askQuestion": {"responses": []}}

    questions_raw = spec.get("questions")
    if not isinstance(questions_raw, list):
        return {"askQuestion": {"responses": []}}

    content = result.content
    responses: list[dict[str, Any]] = []
    for i, question in enumerate(questions_raw):
        if not isinstance(question, dict):
            continue
        question_text = question.get("question")
        if not isinstance(question_text, str) or not question_text:
            continue

        # The form keys each answer by the synthetic question id (its index);
        # fall back to the verbatim question text for robustness.
        answer = content.get(str(i))
        if answer is None:
            answer = content.get(question_text)
        labels = _answer_labels(answer)

        # Map selected labels back to agy option ids by matching option text.
        text_to_id: dict[str, str] = {}
        raw_options = question.get("options")
        if isinstance(raw_options, list):
            for opt in raw_options:
                if isinstance(opt, dict):
                    opt_text = opt.get("text")
                    opt_id = opt.get("id")
                    if isinstance(opt_text, str) and isinstance(opt_id, str):
                        text_to_id.setdefault(opt_text, opt_id)

        selected_ids = [text_to_id[label] for label in labels if label in text_to_id]
        write_ins = [label for label in labels if label not in text_to_id]

        response: dict[str, Any] = {
            "question": question_text,
            "selectedOptionIds": selected_ids,
        }
        if write_ins:
            response["writeInResponse"] = (
                write_ins[0] if len(write_ins) == 1 else ", ".join(write_ins)
            )
        responses.append(response)

    return {"askQuestion": {"responses": responses}}


def _agy_permission_response(result: ElicitationResult) -> dict[str, Any]:
    """
    Build the ``permission`` interaction payload from a web result.

    ``accept`` → ``allow: True``; ``decline`` or ``cancel`` → ``allow: False``.

    :param result: Web-submitted elicitation verdict.
    :returns: ``{"permission": {"allow": <bool>}}``
    """
    return {"permission": {"allow": result.action == "accept"}}
