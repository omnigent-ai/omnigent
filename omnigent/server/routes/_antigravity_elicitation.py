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


def _agy_ask_question_params(
    trajectory_id: object,
    step_index: object,
    spec: object,
) -> ElicitationRequestParams:
    """
    Build elicitation params for an ``ask_question`` pending interaction.

    :param trajectory_id: agy trajectory id string.
    :param step_index: Step index integer (0 when absent).
    :param spec: The ``requestedInteraction.askQuestion`` block; carries
        ``questions[].{question, is_multi_select, options[].{id, text}}``.
    :returns: ``ElicitationRequestParams`` for an ask-question form.
    """
    extras: dict[str, Any] = {
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


def _agy_ask_question_response(
    result: ElicitationResult,
    spec: dict[str, Any],
) -> dict[str, Any]:
    """
    Build the ``askQuestion`` interaction payload from a web result.

    On ``accept``, ``content`` must carry ``selectedOptionIds`` (a list of
    option id strings, e.g. ``["2"]``).  An optional ``writeInResponse``
    string is forwarded verbatim when present and non-empty.

    On ``decline`` or ``cancel``, an empty ``responses`` list is returned
    so the caller can forward it without special-casing the verdict.

    MULTI-QUESTION LIMITATION (minimal-safe guard): agy's ``askQuestion`` can
    carry several ``questions[i]`` (each with its own option ids and
    ``is_multi_select`` flag — see ``_merge_is_multi_select`` in
    ``antigravity_native_steps``), and the agy wire wants one response entry PER
    question. But :class:`ElicitationResult.content` is flat — it carries ONE
    ``selectedOptionIds`` / ``writeInResponse``, with no per-question key — so the
    SPA can only collect a single answer end-to-end. Broadcasting that single
    answer to EVERY question (the prior behaviour) is semantically wrong: those
    option ids belong to the first question and rarely map onto the others.

    So we answer ONLY the first question with the verdict and leave the remaining
    questions unanswered (logging the limitation), letting agy handle the rest on
    its own terms rather than mis-answering them. A full per-question fix requires
    a schema + SPA-form change (a per-question ``content`` shape) and is tracked
    as a follow-up; single-question — the dominant, fully-working case — stays
    correct because there is no "rest" to drop.

    :param result: Web-submitted elicitation verdict.
    :param spec: The ``askQuestion`` spec block; used to recover verbatim
        question text for the answered response entry.
    :returns: ``{"askQuestion": {"responses": [...]}}`` with at most one entry.
    """
    if result.action != "accept" or not isinstance(result.content, dict):
        return {"askQuestion": {"responses": []}}

    questions_raw = spec.get("questions")
    if not isinstance(questions_raw, list):
        return {"askQuestion": {"responses": []}}

    # Only the first question with usable text can be answered: the flat result
    # shape carries a single answer. Skip leading non-dict / text-less entries so
    # a malformed first entry does not waste the one answer we can represent.
    answerable = [
        entry
        for entry in questions_raw
        if isinstance(entry, dict) and isinstance(entry.get("question"), str)
    ]
    if not answerable:
        return {"askQuestion": {"responses": []}}

    if len(answerable) > 1:
        # Genuine multi-question: the flat verdict cannot represent per-question
        # answers, so answer the first and leave the rest to agy. Do NOT broadcast.
        _logger.warning(
            "agy askQuestion carried %d questions but the elicitation result "
            "represents a single answer; answering only the first question and "
            "leaving the remaining %d to agy (flat ElicitationResult.content has "
            "no per-question structure — full per-question support is a follow-up)",
            len(answerable),
            len(answerable) - 1,
        )

    selected_ids: list[str] = []
    raw_ids = result.content.get("selectedOptionIds")
    if isinstance(raw_ids, list):
        selected_ids = [s for s in raw_ids if isinstance(s, str)]

    write_in: str | None = None
    raw_write_in = result.content.get("writeInResponse")
    if isinstance(raw_write_in, str) and raw_write_in:
        write_in = raw_write_in

    response: dict[str, Any] = {
        "question": answerable[0]["question"],
        "selectedOptionIds": selected_ids,
    }
    if write_in is not None:
        response["writeInResponse"] = write_in

    return {"askQuestion": {"responses": [response]}}


def _agy_permission_response(result: ElicitationResult) -> dict[str, Any]:
    """
    Build the ``permission`` interaction payload from a web result.

    ``accept`` → ``allow: True``; ``decline`` or ``cancel`` → ``allow: False``.

    :param result: Web-submitted elicitation verdict.
    :returns: ``{"permission": {"allow": <bool>}}``
    """
    return {"permission": {"allow": result.action == "accept"}}
