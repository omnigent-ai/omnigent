"""Codex app-server elicitation protocol adapters for session routes."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from omnigent.codex_native_elicitation import is_codex_request_id
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.schemas import ElicitationRequestParams, ElicitationResult

_CODEX_MCP_ELICITATION_REQUEST_METHOD = "mcpServer/elicitation/request"
_CODEX_TOOL_REQUEST_USER_INPUT_METHOD = "item/tool/requestUserInput"
_CODEX_COMMAND_EXECUTION_REQUEST_APPROVAL_METHOD = "item/commandExecution/requestApproval"
_CODEX_FILE_CHANGE_REQUEST_APPROVAL_METHOD = "item/fileChange/requestApproval"
_CODEX_PERMISSIONS_REQUEST_APPROVAL_METHOD = "item/permissions/requestApproval"
_CODEX_EXEC_COMMAND_APPROVAL_METHOD = "execCommandApproval"
_CODEX_APPLY_PATCH_APPROVAL_METHOD = "applyPatchApproval"

_CodexParamsBuilder = Callable[[Any, str, dict[str, Any]], ElicitationRequestParams]
_CodexResponseBuilder = Callable[[ElicitationResult, str, dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class _CodexElicitationAdapter:
    """
    Bidirectional adapter for one Codex app-server request method.

    :param build_params: Converts Codex JSON-RPC params into AP
        elicitation params, e.g. ``_codex_mcp_elicitation_params``.
    :param build_response: Converts an Omnigent web verdict into Codex's
        method-specific JSON-RPC result payload, e.g.
        ``_codex_mcp_elicitation_response``.
    """

    build_params: _CodexParamsBuilder
    build_response: _CodexResponseBuilder


@dataclass(frozen=True)
class CodexElicitationRequest:
    """
    Validated Codex request expressed in Omnigent elicitation terms.

    :param params: AP/MCP-shaped elicitation params for the web UI.
    :param method: Codex JSON-RPC method, e.g.
        ``"item/tool/requestUserInput"``.
    :param request_id: Codex JSON-RPC request id, e.g. ``12``.
    :param codex_params: Original Codex JSON-RPC params object.
    :param response_builder: Method-specific web-verdict adapter.
    """

    params: ElicitationRequestParams
    method: str
    request_id: int | str
    codex_params: dict[str, Any]
    response_builder: _CodexResponseBuilder

    def build_response(self, result: ElicitationResult) -> dict[str, Any]:
        """
        Convert a web verdict into this request's Codex response body.

        :param result: Web-submitted elicitation result.
        :returns: Codex JSON-RPC ``result`` payload.
        """
        return self.response_builder(result, self.method, self.codex_params)


def parse_codex_elicitation_request(payload: dict[str, Any]) -> CodexElicitationRequest:
    """
    Validate a Codex request envelope and build its Omnigent adapter object.

    :param payload: Codex JSON-RPC request envelope, e.g.
        ``{"id": 1, "method": "mcpServer/elicitation/request",
        "params": {...}}``.
    :returns: Validated request metadata plus Omnigent elicitation params.
    :raises OmnigentError: If the request shape is unsupported or
        missing required fields.
    """
    method = payload.get("method")
    params = payload.get("params")
    request_id = payload.get("id")
    if not isinstance(method, str) or not method:
        raise OmnigentError(
            "Codex elicitation request must include a non-empty method string.",
            code=ErrorCode.INVALID_INPUT,
        )
    if not isinstance(params, dict):
        raise OmnigentError(
            "Codex elicitation request params must be an object.",
            code=ErrorCode.INVALID_INPUT,
        )
    if not is_codex_request_id(request_id):
        raise OmnigentError(
            "Codex elicitation request must include a string or integer id.",
            code=ErrorCode.INVALID_INPUT,
        )
    adapter = _CODEX_ELICITATION_ADAPTERS.get(method)
    if adapter is None:
        raise OmnigentError(
            f"Unsupported Codex elicitation request method: {method!r}",
            code=ErrorCode.INVALID_INPUT,
        )
    return CodexElicitationRequest(
        params=adapter.build_params(request_id, method, params),
        method=method,
        request_id=request_id,
        codex_params=params,
        response_builder=adapter.build_response,
    )


def _structured_codex_request_user_input(
    params: dict[str, Any],
) -> dict[str, Any] | None:
    """
    Build a structured prompt payload from Codex ``requestUserInput``.

    The web UI already renders Claude's ``ask_user_question`` extra as
    a multi-question form. Codex's ``item/tool/requestUserInput`` uses
    the same user-facing shape (question text plus selectable options),
    but its response must be keyed by stable question id. Preserve that
    id on each question so the UI can post MCP ``content`` keyed by id.

    :param params: Codex ``item/tool/requestUserInput`` params, e.g.
        ``{"questions": [{"id": "framework", "question": "Pick",
        "options": [{"label": "React"}]}]}``.
    :returns: ``{"questions": [...]}`` on success, or ``None`` when
        no usable questions are present.
    """
    questions_raw = params.get("questions")
    if not isinstance(questions_raw, list) or not questions_raw:
        return None
    questions: list[dict[str, Any]] = []
    for entry in questions_raw:
        if not isinstance(entry, dict):
            continue
        question_id = entry.get("id")
        question_text = entry.get("question")
        if not isinstance(question_id, str) or not question_id:
            continue
        if not isinstance(question_text, str) or not question_text:
            continue
        options_raw = entry.get("options")
        options: list[dict[str, Any]] = []
        if isinstance(options_raw, list):
            for opt in options_raw:
                if not isinstance(opt, dict):
                    continue
                label = opt.get("label")
                if not isinstance(label, str) or not label:
                    continue
                option: dict[str, Any] = {"label": label}
                description = opt.get("description")
                if isinstance(description, str) and description:
                    option["description"] = description
                options.append(option)
        question: dict[str, Any] = {
            "id": question_id,
            "question": question_text,
            "options": options,
            "multiSelect": False,
        }
        header = entry.get("header")
        if isinstance(header, str) and header:
            question["header"] = header
        is_other = entry.get("isOther")
        if isinstance(is_other, bool):
            question["isOther"] = is_other
        is_secret = entry.get("isSecret")
        if isinstance(is_secret, bool):
            question["isSecret"] = is_secret
        questions.append(question)
    if not questions:
        return None
    return {"questions": questions}


def _string_list_answer(value: Any) -> list[str]:
    """
    Normalize one web-submitted answer to Codex's ``answers`` list.

    :param value: MCP ``ElicitResult.content`` value, e.g. ``"React"``
        or ``["React", "Vue"]``.
    :returns: List of string answers with empty strings removed.
    """
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [entry for entry in value if isinstance(entry, str) and entry]
    if value is None:
        return []
    return [str(value)]


def _codex_request_user_input_response(
    result: ElicitationResult,
    _method: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """
    Convert a web approval result into Codex ``requestUserInput`` output.

    :param result: Web-submitted elicitation result.
    :param _method: Codex app-server method, unused because this
        response shape is unique to ``requestUserInput``.
    :param params: Original Codex ``item/tool/requestUserInput`` params.
    :returns: Codex ``ToolRequestUserInputResponse`` payload.
    """
    response: dict[str, Any] = {"answers": {}}
    if result.action != "accept" or not isinstance(result.content, dict):
        return response
    questions_raw = params.get("questions")
    if not isinstance(questions_raw, list):
        return response
    answers: dict[str, dict[str, list[str]]] = {}
    for entry in questions_raw:
        if not isinstance(entry, dict):
            continue
        question_id = entry.get("id")
        question_text = entry.get("question")
        if not isinstance(question_id, str) or not question_id:
            continue
        value = result.content.get(question_id)
        if value is None and isinstance(question_text, str):
            value = result.content.get(question_text)
        normalized = _string_list_answer(value)
        if normalized:
            answers[question_id] = {"answers": normalized}
    response["answers"] = answers
    return response


def _codex_mcp_elicitation_response(
    result: ElicitationResult,
    _method: str,
    _params: dict[str, Any],
) -> dict[str, Any]:
    """
    Convert a web approval result into Codex MCP elicitation output.

    :param result: Web-submitted elicitation result.
    :param _method: Codex app-server method, unused because MCP
        elicitation has one response shape.
    :param _params: Original Codex request params, unused because the
        response depends only on the web verdict.
    :returns: Codex ``McpServerElicitationRequestResponse`` payload.
    """
    return {
        "action": result.action,
        "content": result.content if result.action == "accept" else None,
        "_meta": None,
    }


def _execpolicy_amendment(value: Any) -> list[str] | None:
    """
    Validate a Codex ``ExecPolicyAmendment`` value.

    Codex's v2 app-server schema defines ``ExecPolicyAmendment`` as a
    non-empty array of command-prefix strings. Return ``None`` only
    when the value is absent; malformed present values fail loudly.

    :param value: Candidate amendment, e.g.
        ``[".venv/bin/python", "-m", "pytest"]``.
    :returns: The amendment as a list of strings, or ``None``.
    :raises OmnigentError: If ``value`` is present but not a
        non-empty list of non-empty strings.
    """
    if value is None:
        return None
    if not isinstance(value, list) or not value:
        raise OmnigentError(
            "Codex execpolicy amendment must be a non-empty list of strings.",
            code=ErrorCode.INVALID_INPUT,
        )
    if not all(isinstance(entry, str) and entry for entry in value):
        raise OmnigentError(
            "Codex execpolicy amendment must be a non-empty list of strings.",
            code=ErrorCode.INVALID_INPUT,
        )
    return list(value)


def _decision_execpolicy_amendment(decision: Any) -> list[str] | None:
    """
    Extract an execpolicy amendment from one Codex decision option.

    :param decision: One entry from ``availableDecisions``, e.g.
        ``{"acceptWithExecpolicyAmendment": {"execpolicy_amendment":
        ["pytest"]}}``.
    :returns: The offered amendment, or ``None`` when this decision
        is not an ``acceptWithExecpolicyAmendment`` option.
    """
    if not isinstance(decision, dict):
        return None
    wrapped = decision.get("acceptWithExecpolicyAmendment")
    if wrapped is None:
        return None
    if not isinstance(wrapped, dict):
        raise OmnigentError(
            "Codex acceptWithExecpolicyAmendment decision must be an object.",
            code=ErrorCode.INVALID_INPUT,
        )
    return _execpolicy_amendment(wrapped.get("execpolicy_amendment"))


def _codex_available_execpolicy_amendment(params: dict[str, Any]) -> list[str] | None:
    """
    Return the execpolicy amendment Codex offered for this request.

    Codex documents ``availableDecisions`` as the exact set of choices
    clients should expose, so this helper only reads that field.

    :param params: Codex command approval params, e.g.
        ``{"availableDecisions": [...]}``.
    :returns: Offered amendment, or ``None`` when no such decision is
        available.
    """
    available = params.get("availableDecisions")
    if not isinstance(available, list):
        return None
    for decision in available:
        amendment = _decision_execpolicy_amendment(decision)
        if amendment is not None:
            return amendment
    return None


def _result_execpolicy_amendment(
    content: dict[str, str | int | float | bool | list[str] | None] | None,
) -> list[str] | None:
    """
    Extract a user-selected execpolicy amendment from Omnigent content.

    :param content: MCP ``ElicitResult.content`` submitted through the
        session approval event, e.g. ``{"execpolicy_amendment":
        ["pytest"]}``.
    :returns: Selected amendment, or ``None`` for ordinary accepts.
    """
    if not isinstance(content, dict):
        return None
    return _execpolicy_amendment(content.get("execpolicy_amendment"))


def _codex_command_approval_response(
    result: ElicitationResult,
    method: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """
    Convert a web approval result into Codex command-approval output.

    Codex has two command approval methods in the app-server schema:
    the newer v2 ``item/commandExecution/requestApproval`` expects
    ``accept`` / ``decline`` / ``cancel`` literals, while the older
    ``execCommandApproval`` expects ``approved`` / ``denied`` /
    ``abort``. The web UI speaks MCP-style ``accept`` / ``decline`` /
    ``cancel`` actions, so this function is the protocol adapter.

    :param result: Web-submitted elicitation result.
    :param method: Codex app-server request method.
    :param params: Original Codex request params. Used to verify any
        requested execpolicy amendment against Codex's offered
        decisions, e.g. ``{"availableDecisions": [...]}``.
    :returns: Codex command approval response payload.
    """
    decision: str | dict[str, dict[str, list[str]]]
    if method == _CODEX_COMMAND_EXECUTION_REQUEST_APPROVAL_METHOD:
        amendment = _result_execpolicy_amendment(result.content)
        if result.action == "accept" and amendment is not None:
            allowed = _codex_available_execpolicy_amendment(params)
            if allowed != amendment:
                raise OmnigentError(
                    "Codex execpolicy amendment approval did not match an available decision.",
                    code=ErrorCode.INVALID_INPUT,
                )
            decision = {
                "acceptWithExecpolicyAmendment": {
                    "execpolicy_amendment": amendment,
                }
            }
        else:
            decision = {
                "accept": "accept",
                "decline": "decline",
                "cancel": "cancel",
            }[result.action]
    elif method == _CODEX_EXEC_COMMAND_APPROVAL_METHOD:
        decision = {
            "accept": "approved",
            "decline": "denied",
            "cancel": "abort",
        }[result.action]
    else:
        raise OmnigentError(
            f"Unsupported Codex command approval method: {method!r}",
            code=ErrorCode.INVALID_INPUT,
        )
    return {"decision": decision}


def _codex_file_change_approval_response(
    result: ElicitationResult,
    _method: str,
    _params: dict[str, Any],
) -> dict[str, Any]:
    """
    Convert a web verdict into Codex file-change approval output.

    :param result: Web-submitted elicitation result.
    :param _method: Codex app-server method, unused because this
        response shape is unique to file-change approvals.
    :param _params: Original Codex request params, unused because the
        response depends only on the web verdict.
    :returns: Codex ``FileChangeRequestApprovalResponse`` payload.
    """
    return {
        "decision": {
            "accept": "accept",
            "decline": "decline",
            "cancel": "cancel",
        }[result.action]
    }


def _codex_apply_patch_approval_response(
    result: ElicitationResult,
    _method: str,
    _params: dict[str, Any],
) -> dict[str, Any]:
    """
    Convert a web verdict into legacy Codex patch approval output.

    :param result: Web-submitted elicitation result.
    :param _method: Codex app-server method, unused because this
        response shape is unique to legacy patch approvals.
    :param _params: Original Codex request params, unused because the
        response depends only on the web verdict.
    :returns: Codex ``ApplyPatchApprovalResponse`` payload.
    """
    return {
        "decision": {
            "accept": "approved",
            "decline": "denied",
            "cancel": "abort",
        }[result.action]
    }


def _codex_permissions_approval_response(
    result: ElicitationResult,
    _method: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    """
    Convert a web verdict into Codex permission-profile output.

    Codex's permission approval response has no explicit denial enum;
    a rejected web verdict returns an empty grant for the current turn.
    An accepted verdict grants exactly the requested profile, with
    ``null`` entries omitted to match the response schema's optional
    fields.

    :param result: Web-submitted elicitation result.
    :param _method: Codex app-server method, unused because this
        response shape is unique to permissions approvals.
    :param params: Original Codex permissions request params.
    :returns: Codex ``PermissionsRequestApprovalResponse`` payload.
    """
    permissions: dict[str, Any] = {}
    if result.action == "accept":
        requested = params.get("permissions")
        if isinstance(requested, dict):
            for key in ("network", "fileSystem"):
                value = requested.get(key)
                if value is not None:
                    permissions[key] = value
    return {"permissions": permissions, "scope": "turn"}


def _codex_mcp_elicitation_params(
    request_id: Any,
    method: str,
    params: dict[str, Any],
) -> ElicitationRequestParams:
    """
    Build Omnigent params for Codex ``mcpServer/elicitation/request``.

    :param request_id: Codex JSON-RPC request id, e.g. ``12``.
    :param method: Codex app-server method, e.g.
        ``"mcpServer/elicitation/request"``.
    :param params: Codex MCP elicitation params.
    :returns: AP/MCP-shaped elicitation params.
    :raises OmnigentError: If required MCP fields are malformed.
    """
    mode = params.get("mode")
    message = params.get("message")
    if mode not in {"form", "url"}:
        raise OmnigentError(
            "Codex MCP elicitation params.mode must be 'form' or 'url'.",
            code=ErrorCode.INVALID_INPUT,
        )
    if not isinstance(message, str) or not message:
        raise OmnigentError(
            "Codex MCP elicitation params.message must be a non-empty string.",
            code=ErrorCode.INVALID_INPUT,
        )
    server_name = params.get("serverName")
    thread_id = params.get("threadId")
    turn_id = params.get("turnId")
    extras: dict[str, Any] = {
        "codex_method": method,
        "codex_request_id": request_id,
    }
    if isinstance(server_name, str) and server_name:
        extras["server_name"] = server_name
    if isinstance(thread_id, str) and thread_id:
        extras["thread_id"] = thread_id
    if isinstance(turn_id, str) and turn_id:
        extras["turn_id"] = turn_id
    if "_meta" in params:
        extras["_meta"] = params.get("_meta")
    if mode == "form":
        requested_schema = params.get("requestedSchema")
        if not isinstance(requested_schema, dict):
            raise OmnigentError(
                "Codex MCP form elicitation requires object params.requestedSchema.",
                code=ErrorCode.INVALID_INPUT,
            )
        return ElicitationRequestParams(
            mode="form",
            message=message,
            requestedSchema=requested_schema,
            url=None,
            phase="codex_mcp_elicitation",
            policy_name="codex_native_mcp_elicitation",
            content_preview=_json_preview(params),
            **extras,
        )
    url = params.get("url")
    if not isinstance(url, str) or not url:
        raise OmnigentError(
            "Codex MCP URL elicitation requires non-empty params.url.",
            code=ErrorCode.INVALID_INPUT,
        )
    return ElicitationRequestParams(
        mode="url",
        message=message,
        requestedSchema=None,
        url=url,
        phase="codex_mcp_elicitation",
        policy_name="codex_native_mcp_elicitation",
        content_preview=_json_preview(params),
        **extras,
    )


def _codex_tool_request_user_input_params(
    request_id: Any,
    method: str,
    params: dict[str, Any],
) -> ElicitationRequestParams:
    """
    Build Omnigent params for Codex ``item/tool/requestUserInput``.

    :param request_id: Codex JSON-RPC request id, e.g. ``12``.
    :param method: Codex app-server method, e.g.
        ``"item/tool/requestUserInput"``.
    :param params: Codex request-user-input params.
    :returns: AP/MCP-shaped elicitation params.
    :raises OmnigentError: If no usable questions are present.
    """
    ask_payload = _structured_codex_request_user_input(params)
    if ask_payload is None:
        raise OmnigentError(
            "Codex requestUserInput requires at least one usable question.",
            code=ErrorCode.INVALID_INPUT,
        )
    thread_id = params.get("threadId")
    turn_id = params.get("turnId")
    item_id = params.get("itemId")
    extras: dict[str, Any] = {
        "codex_method": method,
        "codex_request_id": request_id,
        "ask_user_question": ask_payload,
    }
    if isinstance(thread_id, str) and thread_id:
        extras["thread_id"] = thread_id
    if isinstance(turn_id, str) and turn_id:
        extras["turn_id"] = turn_id
    if isinstance(item_id, str) and item_id:
        extras["item_id"] = item_id
    return ElicitationRequestParams(
        mode="form",
        message="Codex needs input",
        requestedSchema=None,
        url=None,
        phase="codex_request_user_input",
        policy_name="codex_native_request_user_input",
        content_preview=_json_preview(params),
        **extras,
    )


def _codex_command_approval_params(
    request_id: Any,
    method: str,
    params: dict[str, Any],
) -> ElicitationRequestParams:
    """
    Build Omnigent params for Codex command approval requests.

    :param request_id: Codex JSON-RPC request id, e.g. ``12``.
    :param method: Codex app-server method.
    :param params: Codex command approval params.
    :returns: AP/MCP-shaped elicitation params for a binary command
        approval card.
    """
    command = _codex_command_preview(params)
    cwd = params.get("cwd")
    reason = params.get("reason")
    thread_id = params.get("threadId") or params.get("conversationId")
    turn_id = params.get("turnId")
    item_id = params.get("itemId")
    call_id = params.get("callId")
    approval_id = params.get("approvalId")
    execpolicy_amendment = _codex_available_execpolicy_amendment(params)
    extras: dict[str, Any] = {
        "codex_method": method,
        "codex_request_id": request_id,
    }
    if isinstance(command, str) and command:
        extras["command"] = command
    if isinstance(cwd, str) and cwd:
        extras["cwd"] = cwd
    if isinstance(reason, str) and reason:
        extras["reason"] = reason
    if isinstance(thread_id, str) and thread_id:
        extras["thread_id"] = thread_id
    if isinstance(turn_id, str) and turn_id:
        extras["turn_id"] = turn_id
    if isinstance(item_id, str) and item_id:
        extras["item_id"] = item_id
    if isinstance(call_id, str) and call_id:
        extras["call_id"] = call_id
    if isinstance(approval_id, str) and approval_id:
        extras["approval_id"] = approval_id
    if execpolicy_amendment is not None:
        extras["execpolicy_amendment"] = execpolicy_amendment
    message = "Codex wants to run a command"
    if command:
        message = f"Codex wants to run **{command}**"
    return ElicitationRequestParams(
        mode="form",
        message=message,
        requestedSchema=None,
        url=None,
        phase="codex_command_approval",
        policy_name="codex_native_command_approval",
        content_preview=_json_preview(params),
        **extras,
    )


def _codex_file_change_approval_params(
    request_id: Any,
    method: str,
    params: dict[str, Any],
) -> ElicitationRequestParams:
    """
    Build Omnigent params for Codex file-change approval requests.

    :param request_id: Codex JSON-RPC request id, e.g. ``12``.
    :param method: Codex app-server method, e.g.
        ``"item/fileChange/requestApproval"``.
    :param params: Codex file-change approval params.
    :returns: AP/MCP-shaped elicitation params.
    """
    reason = params.get("reason")
    grant_root = params.get("grantRoot")
    thread_id = params.get("threadId")
    turn_id = params.get("turnId")
    item_id = params.get("itemId")
    extras: dict[str, Any] = {
        "codex_method": method,
        "codex_request_id": request_id,
    }
    if isinstance(reason, str) and reason:
        extras["reason"] = reason
    if isinstance(grant_root, str) and grant_root:
        extras["grant_root"] = grant_root
    if isinstance(thread_id, str) and thread_id:
        extras["thread_id"] = thread_id
    if isinstance(turn_id, str) and turn_id:
        extras["turn_id"] = turn_id
    if isinstance(item_id, str) and item_id:
        extras["item_id"] = item_id
    message = "Codex wants to modify files"
    if grant_root:
        message = f"Codex wants write access under **{grant_root}**"
    return ElicitationRequestParams(
        mode="form",
        message=message,
        requestedSchema=None,
        url=None,
        phase="codex_file_change_approval",
        policy_name="codex_native_file_change_approval",
        content_preview=_json_preview(params),
        **extras,
    )


def _codex_permissions_approval_params(
    request_id: Any,
    method: str,
    params: dict[str, Any],
) -> ElicitationRequestParams:
    """
    Build Omnigent params for Codex permission-profile approval requests.

    :param request_id: Codex JSON-RPC request id, e.g. ``12``.
    :param method: Codex app-server method, e.g.
        ``"item/permissions/requestApproval"``.
    :param params: Codex permissions approval params.
    :returns: AP/MCP-shaped elicitation params.
    """
    cwd = params.get("cwd")
    reason = params.get("reason")
    permissions = params.get("permissions")
    thread_id = params.get("threadId")
    turn_id = params.get("turnId")
    item_id = params.get("itemId")
    extras: dict[str, Any] = {
        "codex_method": method,
        "codex_request_id": request_id,
    }
    if isinstance(cwd, str) and cwd:
        extras["cwd"] = cwd
    if isinstance(reason, str) and reason:
        extras["reason"] = reason
    if isinstance(permissions, dict):
        extras["permissions"] = permissions
    if isinstance(thread_id, str) and thread_id:
        extras["thread_id"] = thread_id
    if isinstance(turn_id, str) and turn_id:
        extras["turn_id"] = turn_id
    if isinstance(item_id, str) and item_id:
        extras["item_id"] = item_id
    return ElicitationRequestParams(
        mode="form",
        message="Codex requests additional permissions",
        requestedSchema=None,
        url=None,
        phase="codex_permissions_approval",
        policy_name="codex_native_permissions_approval",
        content_preview=_json_preview(params),
        **extras,
    )


def _codex_apply_patch_approval_params(
    request_id: Any,
    method: str,
    params: dict[str, Any],
) -> ElicitationRequestParams:
    """
    Build Omnigent params for legacy Codex patch approval requests.

    :param request_id: Codex JSON-RPC request id, e.g. ``12``.
    :param method: Codex app-server method, e.g.
        ``"applyPatchApproval"``.
    :param params: Codex apply-patch approval params.
    :returns: AP/MCP-shaped elicitation params.
    """
    reason = params.get("reason")
    grant_root = params.get("grantRoot")
    thread_id = params.get("conversationId")
    call_id = params.get("callId")
    files = params.get("fileChanges")
    extras: dict[str, Any] = {
        "codex_method": method,
        "codex_request_id": request_id,
    }
    if isinstance(reason, str) and reason:
        extras["reason"] = reason
    if isinstance(grant_root, str) and grant_root:
        extras["grant_root"] = grant_root
    if isinstance(thread_id, str) and thread_id:
        extras["thread_id"] = thread_id
    if isinstance(call_id, str) and call_id:
        extras["call_id"] = call_id
    if isinstance(files, dict) and files:
        extras["files"] = sorted(str(key) for key in files)
    return ElicitationRequestParams(
        mode="form",
        message="Codex wants to apply a patch",
        requestedSchema=None,
        url=None,
        phase="codex_apply_patch_approval",
        policy_name="codex_native_apply_patch_approval",
        content_preview=_json_preview(params),
        **extras,
    )


def _codex_command_preview(params: dict[str, Any]) -> str | None:
    """
    Extract a displayable command string from Codex approval params.

    :param params: Codex command approval params.
    :returns: Command string, or ``None`` when absent/malformed.
    """
    command = params.get("command")
    if isinstance(command, str) and command:
        return command
    if isinstance(command, list) and command:
        parts = [part for part in command if isinstance(part, str)]
        if parts:
            return " ".join(parts)
    return None


def _json_preview(value: Any) -> str:
    """
    Return a bounded JSON preview for an elicitation payload.

    :param value: JSON-like value to preview.
    :returns: Preview string capped at 1024 characters.
    """
    try:
        preview = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        preview = repr(value)
    return preview[:1024]


_CODEX_ELICITATION_ADAPTERS: dict[str, _CodexElicitationAdapter] = {
    _CODEX_MCP_ELICITATION_REQUEST_METHOD: _CodexElicitationAdapter(
        build_params=_codex_mcp_elicitation_params,
        build_response=_codex_mcp_elicitation_response,
    ),
    _CODEX_TOOL_REQUEST_USER_INPUT_METHOD: _CodexElicitationAdapter(
        build_params=_codex_tool_request_user_input_params,
        build_response=_codex_request_user_input_response,
    ),
    _CODEX_COMMAND_EXECUTION_REQUEST_APPROVAL_METHOD: _CodexElicitationAdapter(
        build_params=_codex_command_approval_params,
        build_response=_codex_command_approval_response,
    ),
    _CODEX_EXEC_COMMAND_APPROVAL_METHOD: _CodexElicitationAdapter(
        build_params=_codex_command_approval_params,
        build_response=_codex_command_approval_response,
    ),
    _CODEX_FILE_CHANGE_REQUEST_APPROVAL_METHOD: _CodexElicitationAdapter(
        build_params=_codex_file_change_approval_params,
        build_response=_codex_file_change_approval_response,
    ),
    _CODEX_PERMISSIONS_REQUEST_APPROVAL_METHOD: _CodexElicitationAdapter(
        build_params=_codex_permissions_approval_params,
        build_response=_codex_permissions_approval_response,
    ),
    _CODEX_APPLY_PATCH_APPROVAL_METHOD: _CodexElicitationAdapter(
        build_params=_codex_apply_patch_approval_params,
        build_response=_codex_apply_patch_approval_response,
    ),
}
