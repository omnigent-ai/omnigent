"""Agy (antigravity) elicitation protocol adapters.

Pure shape-mapping — no I/O, no RPC calls.  The bridge (Task 8) and
the endpoint (Task 9) handle all network interaction.

Two functions are exported:

* :func:`to_elicitation_params` — converts a :class:`PendingInteraction`
  dict (produced by :func:`omnigent.harnesses.antigravity_native.steps.pending_interaction`)
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


# agy marks a permission prompt as carrying an "always allow" choice with this
# ``persistSuggestionType`` value plus a concrete ``suggestedPersistPattern``
# (the pattern its own TUI menu offers to remember, e.g. the command word).
_PERSIST_SUGGESTED = "PERSIST_SUGGESTION_TYPE_SUGGESTED"


def _suggested_persist_pattern(spec: object) -> str | None:
    """
    Return the always-allow persist pattern a permission spec advertises.

    agy offers an always-allow menu entry only when the spec carries
    ``persistSuggestionType == PERSIST_SUGGESTION_TYPE_SUGGESTED`` AND a
    non-empty ``suggestedPersistPattern``. Both are required here so the web
    card never offers (and the TUI mapper never types) a persist choice agy's
    own prompt does not have.

    :param spec: The ``requestedInteraction.permission`` block.
    :returns: The advertised persist pattern, or ``None`` when the prompt
        offers no always-allow choice.
    """
    if not isinstance(spec, dict):
        return None
    if spec.get("persistSuggestionType") != _PERSIST_SUGGESTED:
        return None
    pattern = spec.get("suggestedPersistPattern")
    if isinstance(pattern, str) and pattern:
        return pattern
    return None


def _agy_permission_params(
    trajectory_id: object,
    step_index: object,
    spec: object,
) -> ElicitationRequestParams:
    """
    Build elicitation params for a ``permission`` pending interaction.

    Besides the binary approve/reject shape, the params surface the two parts
    of agy's own prompt the card must not drop:

    * ``action_description`` — the spec's ``actionDescription`` (what agy says
      it wants to do, e.g. "Running pwd command").
    * ``always_allow_pattern`` — the advertised persist pattern when agy's
      prompt offers an "always allow" choice (see
      :func:`_suggested_persist_pattern`); the web card renders it as an
      "Always allow <pattern>" button whose accept verdict carries
      ``_meta.persist == "always"``.

    :param trajectory_id: agy trajectory id string.
    :param step_index: Step index integer (0 when absent).
    :param spec: The ``requestedInteraction.permission`` block; carries
        ``resource.{action, target}``, ``actionDescription``, and the persist
        suggestion fields.
    :returns: ``ElicitationRequestParams`` for the command-approval card.
    """
    command: str | None = None
    action_description: str | None = None
    if isinstance(spec, dict):
        resource = spec.get("resource")
        if isinstance(resource, dict):
            target = resource.get("target")
            if isinstance(target, str) and target:
                command = target
        described = spec.get("actionDescription")
        if isinstance(described, str) and described:
            action_description = described

    message = "Antigravity wants to run a command"
    if command:
        message = f"Antigravity wants to run: {command}"

    extras: dict[str, Any] = {
        "permission_spec": spec,
    }
    if command:
        extras["command"] = command
    if action_description:
        extras["action_description"] = action_description
    always_allow_pattern = _suggested_persist_pattern(spec)
    if always_allow_pattern:
        extras["always_allow_pattern"] = always_allow_pattern
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

    An always-allow accept (``_meta.persist == "always"``) still delivers the
    plain ``allow: True`` here: the live-verified RPC variant carries only
    ``allow``, and the persist side of the verdict rides the TUI channel
    (:func:`to_tui_selection_keys` selects agy's own always-allow menu entry,
    which owns recording the pattern).

    :param result: Web-submitted elicitation verdict.
    :returns: ``{"permission": {"allow": <bool>}}``
    """
    return {"permission": {"allow": result.action == "accept"}}


# agy's attended-TUI permission prompt is a numbered list: option 1 is the bare
# "Yes" (approve once), option 2 is the always-allow entry carrying the spec's
# advertised ``suggestedPersistPattern`` (options 2/3 are the persist
# variants), and the LAST option is "No" (4). The web card's Approve drives the
# always-safe "Yes" (1), its "Always allow <pattern>" choice drives the
# advertised persist entry (2), and Reject drives "No" (4). These are the
# digits typed into the pane, each followed by Enter to confirm the selection.
_AGY_TUI_PERMISSION_APPROVE_OPTION = "1"
_AGY_TUI_PERMISSION_ALWAYS_ALLOW_OPTION = "2"
_AGY_TUI_PERMISSION_REJECT_OPTION = "4"
_AGY_TUI_CONFIRM_KEY = "Enter"


def to_tui_selection_keys(
    kind: str,
    result: ElicitationResult,
    spec: dict[str, Any],
) -> list[str]:
    """
    Map an elicitation result to the tmux keys that answer agy's TUI prompt.

    The attended agy TUI maintains its own permission / question prompt IN
    PARALLEL with the RPC trajectory step (live-verified; see
    ``docs/claude/antigravity-rpc-spike-notes.md``). Delivering the verdict over
    ``HandleCascadeUserInteraction`` flips the backend step but can leave that TUI
    prompt open, stranding the terminal (and folding the next typed turn into the
    stale prompt's buffer — #1200). So the bridge ALSO types the selection into
    the pane via
    :func:`omnigent.harnesses.antigravity_native.bridge.send_interaction_keys_via_tui`,
    mirroring cursor-native. This is the pure shape-mapper for those keys.

    * **permission** — Approve → option ``"1"`` ("Yes"); an always-allow accept
      (``_meta.persist == "always"``, offered only when the spec advertises a
      persist pattern) → option ``"2"`` (agy's own always-allow menu entry);
      Reject → option ``"4"`` ("No") — each followed by ``Enter``.
    * **ask_question** — type the selected option id(s) ("1".."N") then ``Enter``;
      agy's TUI numbers questions' options the same way its RPC ``selectedOptionIds``
      do. A decline/cancel (or no usable selection) presses ``Escape`` to dismiss.

    :param kind: Interaction kind — ``"ask_question"`` or ``"permission"``.
    :param result: The web-submitted elicitation verdict.
    :param spec: The original ``askQuestion`` / ``permission`` block (used to map
        an ask_question answer's option labels back to ids).
    :returns: Ordered tmux key arguments to send into the agy pane (possibly
        empty when no keystroke is warranted, e.g. an unsupported kind).
    :raises ValueError: When ``kind`` is not ``"ask_question"`` or ``"permission"``.
    """
    if kind == "permission":
        if result.action != "accept":
            return [_AGY_TUI_PERMISSION_REJECT_OPTION, _AGY_TUI_CONFIRM_KEY]
        persist = result.meta.get("persist") if result.meta is not None else None
        if persist == "always" and _suggested_persist_pattern(spec) is not None:
            # The always-allow menu entry exists only when the spec advertised
            # a persist pattern; an unadvertised persist request falls back to
            # the plain approve so a stale or crafted verdict can never select
            # a menu entry agy's prompt does not have.
            return [_AGY_TUI_PERMISSION_ALWAYS_ALLOW_OPTION, _AGY_TUI_CONFIRM_KEY]
        return [_AGY_TUI_PERMISSION_APPROVE_OPTION, _AGY_TUI_CONFIRM_KEY]
    if kind == "ask_question":
        return _agy_ask_question_tui_keys(result, spec)
    raise ValueError(f"Unsupported agy interaction kind: {kind!r}")


def _agy_ask_question_tui_keys(
    result: ElicitationResult,
    spec: dict[str, Any],
) -> list[str]:
    """
    Map an ``ask_question`` result to the tmux keys answering agy's TUI prompt.

    Reuses :func:`_agy_ask_question_response` to resolve the web answer back to
    agy's option ids (``"1".."N"``), then types the FIRST question's selected
    option id(s) followed by ``Enter``. agy's TUI shows one question's options as
    a numbered list, so the option id is exactly the digit to press. A decline /
    cancel — or any answer that resolves to no concrete option id — presses
    ``Escape`` to dismiss the prompt (the RPC ``askQuestion`` skip already tells
    the backend; the keystroke clears the TUI surface).

    :param result: The web-submitted elicitation verdict.
    :param spec: The ``askQuestion`` spec block (option id↔text map).
    :returns: Ordered tmux key arguments (digits + ``Enter``, or ``["Escape"]``).
    """
    payload = _agy_ask_question_response(result, spec)
    responses = payload.get("askQuestion", {}).get("responses", [])
    selected: list[str] = []
    if isinstance(responses, list) and responses:
        first = responses[0]
        if isinstance(first, dict):
            ids = first.get("selectedOptionIds")
            if isinstance(ids, list):
                selected = [oid for oid in ids if isinstance(oid, str) and oid]
    if not selected:
        return ["Escape"]
    return [*selected, _AGY_TUI_CONFIRM_KEY]
