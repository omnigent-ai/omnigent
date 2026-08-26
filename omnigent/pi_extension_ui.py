"""Pi extension UI elicitation adapters.

Pure shape-mapping — no I/O. Converts Pi ``ctx.ui.confirm|select|input|editor``
requests (RPC ``extension_ui_request`` events, or the same fields synthesized
by the pi-native TUI wrap) into the web UI's existing ApprovalCard /
AskUserQuestion form, and maps the user's ``ElicitationResult`` back to a Pi
``extension_ui_response``.

Pi's ``ExtensionUIContext`` options are always ``string[]``. There is no
multi-select. ``custom()`` is TUI-only and out of scope.
"""

from __future__ import annotations

from typing import Any

from omnigent.server.schemas import ElicitationRequestParams, ElicitationResult

DIALOG_METHODS: frozenset[str] = frozenset({"confirm", "select", "input", "editor"})
FIRE_AND_FORGET_METHODS: frozenset[str] = frozenset(
    {"notify", "setStatus", "setWidget", "setTitle", "set_editor_text"}
)

POLICY_NAME = "pi_native_extension_ui"
PHASE = "pi_extension_ui"

_KEEP_AS_IS_LABEL = "Keep as-is"


def is_dialog_method(method: object) -> bool:
    """Return whether *method* is a Pi dialog that needs a client response."""
    return method in DIALOG_METHODS


def is_fire_and_forget_method(method: object) -> bool:
    """Return whether *method* is a Pi chrome update with no response."""
    return method in FIRE_AND_FORGET_METHODS


def timeout_seconds(req: dict[str, Any]) -> float | None:
    """Pi's optional dialog timeout (milliseconds) as seconds, or ``None``."""
    raw = req.get("timeout")
    if isinstance(raw, bool) or not isinstance(raw, int | float) or raw <= 0:
        return None
    return float(raw) / 1000.0


def to_elicitation_params(req: dict[str, Any]) -> ElicitationRequestParams:
    """
    Convert a Pi extension UI request into elicitation params for the web UI.

    :param req: Pi RPC ``extension_ui_request`` fields (``method``, ``title``,
        optional ``message`` / ``options`` / ``placeholder`` / ``prefill`` /
        ``timeout``), or the same shape synthesized by the pi-native wrap.
    :returns: Form-mode params. ``confirm`` is a binary card; ``select`` /
        ``input`` / ``editor`` stamp ``ask_user_question`` so the existing
        form renders. Empty ``select`` options are treated as ``input``
        (custom-row only — no dummy radio).
    :raises ValueError: When ``method`` is not a dialog method.
    """
    method = req.get("method")
    if not is_dialog_method(method):
        raise ValueError(f"Unsupported Pi extension UI method: {method!r}")

    extras: dict[str, Any] = {"pi_extension_ui": req}
    if method == "confirm":
        return ElicitationRequestParams(
            mode="form",
            message=_confirm_message(req),
            requestedSchema=None,
            url=None,
            phase=PHASE,
            policy_name=POLICY_NAME,
            **extras,
        )

    extras["ask_user_question"] = _ask_user_question(req, method)
    title = _nonempty_str(req.get("title")) or "Pi needs your input"
    return ElicitationRequestParams(
        mode="form",
        message=title,
        requestedSchema=None,
        url=None,
        phase=PHASE,
        policy_name=POLICY_NAME,
        **extras,
    )


def to_ui_response(
    req: dict[str, Any],
    result: ElicitationResult | None,
) -> dict[str, Any]:
    """
    Convert an elicitation verdict into a Pi ``extension_ui_response``.

    :param req: The original request (needs ``id`` and ``method``).
    :param result: Web verdict, or ``None`` on timeout / missing handler.
    :returns: JSON object to write on Pi's stdin (RPC) or to interpret as
        the wrap return value.
    """
    response_id = req.get("id")
    req_id = response_id if isinstance(response_id, str) and response_id else ""
    method = req.get("method")
    accepted = result is not None and result.action == "accept"
    if method == "confirm":
        return {
            "type": "extension_ui_response",
            "id": req_id,
            "confirmed": accepted,
        }
    if result is None or result.action != "accept":
        return {"type": "extension_ui_response", "id": req_id, "cancelled": True}
    value = _accepted_text(req, result)
    if value is None:
        return {"type": "extension_ui_response", "id": req_id, "cancelled": True}
    return {"type": "extension_ui_response", "id": req_id, "value": value}


def _confirm_message(req: dict[str, Any]) -> str:
    """Compose the binary-card prompt from title + message, without markdown."""
    title = _nonempty_str(req.get("title"))
    message = _nonempty_str(req.get("message"))
    if title and message:
        return f"{title}\n\n{message}"
    return title or message or "Pi needs your confirmation"


def _ask_user_question(req: dict[str, Any], method: object) -> dict[str, Any]:
    """Build a one-question AskUserQuestion payload for select/input/editor."""
    title = _nonempty_str(req.get("title")) or "Pi needs your input"
    option_labels = _select_labels(req) if method == "select" else []
    header: str | None = None
    options: list[dict[str, Any]]
    if option_labels:
        options = [{"label": label} for label in option_labels]
    elif method == "editor":
        prefill = req.get("prefill")
        prefill_text = prefill if isinstance(prefill, str) else ""
        option: dict[str, Any] = {"label": _KEEP_AS_IS_LABEL}
        if prefill_text:
            option["preview"] = prefill_text
        options = [option]
        header = "Editor"
    else:
        # Custom-row only. A dummy radio (placeholder / "Enter text") was
        # selectable and submitted as the Pi value without the user typing.
        options = []
        header = _nonempty_str(req.get("placeholder")) or "Input"
    question: dict[str, Any] = {
        "id": "0",
        "question": title,
        "options": options,
        "multiSelect": False,
        # Pi ``select`` only accepts one of its supplied labels. Input,
        # editor, and an empty select still need the form's custom row.
        "isOther": not bool(option_labels),
    }
    if header:
        question["header"] = header
    return {"questions": [question]}


def _select_labels(req: dict[str, Any]) -> list[str]:
    raw_options = req.get("options")
    if not isinstance(raw_options, list):
        return []
    return [opt for opt in raw_options if isinstance(opt, str) and opt]


def _accepted_text(req: dict[str, Any], result: ElicitationResult) -> str | None:
    """First selected / typed label from the form, remapping editor Keep as-is."""
    content = result.content
    if not isinstance(content, dict):
        return None
    answer: object = content.get("0")
    if answer is None:
        title = _nonempty_str(req.get("title"))
        if title:
            answer = content.get(title)
    labels = _answer_labels(answer)
    if not labels:
        return None
    text = labels[0]
    if req.get("method") == "editor" and text == _KEEP_AS_IS_LABEL:
        prefill = req.get("prefill")
        return prefill if isinstance(prefill, str) else ""
    return text


def _answer_labels(answer: object) -> list[str]:
    if isinstance(answer, str):
        return [answer] if answer else []
    if isinstance(answer, list):
        return [item for item in answer if isinstance(item, str) and item]
    return []


def _nonempty_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
