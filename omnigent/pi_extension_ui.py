"""Pi extension UI elicitation adapters."""

from __future__ import annotations

from typing import Any

from omnigent.server.schemas import ElicitationRequestParams, ElicitationResult

DIALOG_METHODS: frozenset[str] = frozenset({"confirm", "select", "input", "editor"})
FIRE_AND_FORGET_METHODS: frozenset[str] = frozenset(
    {"notify", "setStatus", "setWidget", "setTitle", "set_editor_text"}
)

POLICY_NAME = "pi_native_extension_ui"
PHASE = "pi_extension_ui"


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


def to_elicitation_params(
    req: dict[str, Any], *, harness: str = "pi-native"
) -> ElicitationRequestParams:
    """Convert a native extension dialog into safe web form controls."""
    method = req.get("method")
    omp = harness == "omp-native"
    extras: dict[str, Any] = {"omp_extension_ui" if omp else "pi_extension_ui": req}
    if method == "formDialog" and omp:
        return ElicitationRequestParams(
            mode="form",
            message=_nonempty_str(req.get("title")) or "OMP needs your input",
            requestedSchema=_native_form_schema(req),
            phase="omp_extension_ui",
            policy_name="omp_native_extension_ui",
            **extras,
        )
    if not isinstance(method, str) or not is_dialog_method(method):
        raise ValueError(f"Unsupported Pi extension UI method: {method!r}")
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
    *,
    timed_out: bool = False,
) -> dict[str, Any]:
    """Convert an elicitation verdict into a Pi ``extension_ui_response``."""
    response_id = req.get("id")
    req_id = response_id if isinstance(response_id, str) and response_id else ""
    method = req.get("method")
    accepted = result is not None and result.action == "accept"
    if method == "formDialog":
        if not accepted:
            return {
                "type": "extension_ui_response",
                "id": req_id,
                "cancelled": True,
                "timedOut": result is None and timed_out,
            }
        content = result.content or {}
        properties = _native_form_schema(req)["properties"]
        values: dict[str, Any] = {}
        for name, prop in properties.items():
            if name not in content:
                continue
            value = content[name]
            kind = prop["type"]
            valid = (
                (kind == "boolean" and isinstance(value, bool))
                or (kind == "string" and isinstance(value, str))
                or (
                    kind == "number"
                    and not isinstance(value, bool)
                    and isinstance(value, int | float)
                )
                or (
                    kind == "number"
                    and prop.get("x-preserve-empty") is True
                    and isinstance(value, str)
                    and not value.strip()
                )
            )
            if not valid:
                return {"type": "extension_ui_response", "id": req_id, "cancelled": True}
            values[name] = value
        return {"type": "extension_ui_response", "id": req_id, "value": values}
    if method == "confirm":
        return {
            "type": "extension_ui_response",
            "id": req_id,
            "confirmed": accepted,
        }
    if result is None or result.action != "accept":
        return {"type": "extension_ui_response", "id": req_id, "cancelled": True}
    value = _accepted_text(req, result)
    labels = _select_labels(req) if method == "select" else []
    if value is None or (labels and value not in labels):
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
        options = []
        header = "Editor"
    else:
        # A placeholder radio could be submitted without the user typing.
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
    if method == "editor":
        prefill = req.get("prefill")
        question["textInput"] = {"default": prefill if isinstance(prefill, str) else ""}
    return {"questions": [question]}


def _select_labels(req: dict[str, Any]) -> list[str]:
    raw_options = req.get("options")
    if not isinstance(raw_options, list):
        return []
    return [opt for opt in raw_options if isinstance(opt, str) and opt]


def _accepted_text(req: dict[str, Any], result: ElicitationResult) -> str | None:
    """Read the form answer, preserving an editor's exact text."""
    content = result.content
    if not isinstance(content, dict):
        return None
    answer: object = content.get("0")
    if answer is None:
        title = _nonempty_str(req.get("title"))
        if title:
            answer = content.get(title)
    if req.get("method") == "editor" and isinstance(answer, str):
        return answer
    labels = _answer_labels(answer)
    if not labels:
        return None
    return labels[0]


def _answer_labels(answer: object) -> list[str]:
    if isinstance(answer, str):
        return [answer] if answer else []
    if isinstance(answer, list):
        return [item for item in answer if isinstance(item, str) and item]
    return []


def _nonempty_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _native_form_schema(req: dict[str, Any]) -> dict[str, Any]:
    """Allow primitive schema controls only; HTML is parsed inside OMP, not here."""
    schema = req.get("requestedSchema")
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise ValueError("OMP formDialog requires an object requestedSchema")
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        raise ValueError("OMP formDialog requires schema properties")
    for name, prop in properties.items():
        if (
            not isinstance(name, str)
            or not isinstance(prop, dict)
            or prop.get("type") not in {"string", "number", "boolean"}
        ):
            raise ValueError("OMP formDialog supports primitive named schema controls")
    required = schema.get("required", [])
    if not isinstance(required, list) or any(
        not isinstance(name, str) or name not in properties for name in required
    ):
        raise ValueError("OMP formDialog required fields must name schema properties")
    return {**schema, "properties": properties, "required": required}
