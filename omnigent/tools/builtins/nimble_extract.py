"""Built-in tool: nimble_extract — Nimble Extract Templates → structured JSON.

Runs a Nimble extract template (``POST /v2/extract/templates/run``) and
returns the template's structured, parsed results as JSON in one synchronous
call. Extract templates are account-scoped: the ``template`` config value
names one of your account's templates, and the LLM supplies the template's
``params`` (each template declares its own input schema).

Configured in the agent spec::

    tools:
      builtins:
        - name: nimble_extract
          api_key: ${NIMBLE_API_KEY}
          template: my_template_name   # one of your account's extract templates
          # optional:
          # timeout_seconds: 120

List your templates (and each one's input schema)::

    curl -H "Authorization: Bearer $NIMBLE_API_KEY" \\
         https://sdk.nimbleway.com/v2/extract/templates
    curl -H "Authorization: Bearer $NIMBLE_API_KEY" \\
         https://sdk.nimbleway.com/v2/extract/templates/<name>

See https://docs.nimbleway.com/api-reference/extract-templates-api/run-extract-template
"""

from __future__ import annotations

import json
import logging
import os

# Any: Nimble's JSON response is a heterogeneous dict with string keys and
# mixed value types (str, dict, list, None).
from typing import Any

import httpx

from omnigent.tools.base import Tool, ToolContext
from omnigent.tools.builtins._arguments import parse_json_object_arguments

_logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://sdk.nimbleway.com"

# Identifies this integration to Nimble via the ``X-Client-Source`` header that
# Nimble's own SDKs send (same convention as the web_search nimble backend).
_CLIENT_SOURCE = "omnigent"

# A template run drives a browser task; allow a generous, configurable read
# timeout (spec ``timeout_seconds``, clamped).
_DEFAULT_TIMEOUT_S = 120.0
_MIN_TIMEOUT_S = 10.0
_MAX_TIMEOUT_S = 600.0

# Cap the returned JSON so a large extraction cannot blow the model context.
_MAX_CONTENT_CHARS = 50_000
# Per-string cap for any API-supplied value reflected into an error message,
# so an oversized error body cannot bloat the tool result either.
_MAX_FIELD_CHARS = 2_000

_DESCRIPTION = (
    "Run the configured Nimble extract template against the live web and "
    "return its structured, parsed results as JSON in one call. The template "
    "defines what gets extracted; 'params' fills in its declared inputs "
    '(e.g. {"url": "..."} for a page-scoped template).'
)


def _base_url() -> str:
    """Resolve the base URL; ``OMNIGENT_NIMBLE_EXTRACT_BASE_URL`` overrides for tests."""
    return os.environ.get("OMNIGENT_NIMBLE_EXTRACT_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")


def _resolve_timeout(config: dict[str, str]) -> float:
    """
    Read ``timeout_seconds`` from spec config, clamped to a sane range.

    :param config: Spec-level config; values arrive as strings from YAML.
    :returns: A usable read timeout in seconds.
    """
    raw = config.get("timeout_seconds")
    if raw is None:
        return _DEFAULT_TIMEOUT_S
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _DEFAULT_TIMEOUT_S
    if value != value:  # NaN guard: NaN breaks min/max clamping
        return _DEFAULT_TIMEOUT_S
    return max(_MIN_TIMEOUT_S, min(value, _MAX_TIMEOUT_S))


def _truncate(text: str) -> str:
    """Cap text to keep the model context bounded."""
    if len(text) > _MAX_CONTENT_CHARS:
        return text[:_MAX_CONTENT_CHARS] + "\n\n[truncated]"
    return text


def _cap_field(value: str) -> str:
    """Cap a single API-supplied string reflected into an error message."""
    return value if len(value) <= _MAX_FIELD_CHARS else value[:_MAX_FIELD_CHARS] + "…"


def _error_detail(resp: httpx.Response) -> str:
    """
    A `` : <message> (task: ...)`` suffix built from an error response body,
    when it carries one (e.g. ``{"message": "...", "task_id": "..."}``).

    :param resp: The non-2xx response.
    :returns: A human-readable suffix, or ``""``.
    """
    try:
        body = resp.json()
    except (json.JSONDecodeError, ValueError):
        return ""
    if not isinstance(body, dict):
        return ""
    message = body.get("message") or body.get("msg")
    if not isinstance(message, str) or not message.strip():
        return ""
    return f": {_cap_field(message.strip())}{_task_suffix(body)}"


def _task_suffix(payload: dict[str, Any]) -> str:
    """A `` (task: ...)`` suffix for support-friendly messages, when known."""
    task_id = payload.get("task_id")
    if isinstance(task_id, str) and task_id:
        return f" (task: {_cap_field(task_id)})"
    return ""


def _format_extract(payload: dict[str, Any], template: str) -> str:
    """
    Format a ``/v2/extract/templates/run`` response into structured JSON text.

    The response carries ``{"data": {"parsing": ..., "markdown"?, "html"?},
    "status", "task_id", ...}``. Prefer the parsed entities as JSON; surface
    parsing errors clearly; fall back to ``markdown``/``html``.

    :param payload: The parsed JSON response.
    :param template: The template that ran (for error messages).
    :returns: Structured JSON (entities) or text, truncated; or a clear
        status/error message.
    """
    data = payload.get("data")
    inner = data if isinstance(data, dict) else {}
    parsing = inner.get("parsing")
    if isinstance(parsing, dict):
        parsing_status = parsing.get("status")
        if parsing_status == "error":
            error = parsing.get("error")
            detail = error if isinstance(error, str) and error else "unknown parsing error"
            return (
                f"Nimble extract error: template {template!r} parsing failed: "
                f"{_cap_field(detail)}{_task_suffix(payload)}"
            )
        entities = parsing.get("entities")
        if entities or (parsing_status == "success" and isinstance(entities, dict | list)):
            return _truncate(json.dumps(entities, ensure_ascii=False))
        if parsing and parsing_status is None:
            # Free-form parsing object (no status envelope): return it as-is.
            return _truncate(json.dumps(parsing, ensure_ascii=False))
    body = inner.get("markdown") or inner.get("html") or ""
    if isinstance(body, str) and body.strip():
        return _truncate(body)
    status = payload.get("status", "unknown")
    return (
        f"No structured data returned by the Nimble extract run "
        f"(status: {_cap_field(str(status))}){_task_suffix(payload)}."
    )


def _run_extract(params: dict[str, Any], config: dict[str, str]) -> str:
    """
    Run a Nimble extract template and return its structured results.

    :param params: Template parameters (e.g. ``{"query": ...}`` for
        ``google_search``).
    :param config: Spec-level config; ``api_key`` (required), optional
        ``template`` and ``timeout_seconds``.
    :returns: Structured JSON (the template's parsed entities), or an error
        message. Never raises.
    """
    api_key = config.get("api_key")
    if not api_key:
        return "Error: api_key must be provided in the nimble_extract config in config.yaml."
    template = config.get("template")
    if not template:
        return (
            "Error: template must be provided in the nimble_extract config in config.yaml. "
            "List your account's extract templates via GET /v2/extract/templates."
        )
    try:
        resp = httpx.post(
            f"{_base_url()}/v2/extract/templates/run",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-Client-Source": _CLIENT_SOURCE,
            },
            json={"template": template, "params": params},
            timeout=_resolve_timeout(config),
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code == 404:
            return (
                f"Nimble extract error: template {template!r} was not found. "
                "List templates via GET /v2/extract/templates."
            )
        if code in (401, 403):
            return (
                f"Nimble extract error: HTTP {code} (authentication failed - check the "
                "api_key in the nimble_extract config)."
            )
        if code == 422:
            return (
                f"Nimble extract error: HTTP 422 (template {template!r} rejected the "
                "params - check the template's required parameters)."
            )
        detail = _error_detail(exc.response)
        return f"Nimble extract error: HTTP {code}{detail}"
    except httpx.RequestError as exc:
        return f"Nimble extract error: {exc}"
    try:
        payload = resp.json()
    except (json.JSONDecodeError, ValueError):
        return "Nimble extract error: the run returned a non-JSON response."
    if not isinstance(payload, dict):
        return "Nimble extract error: the run returned an unexpected response shape."
    return _format_extract(payload, template)


class NimbleExtractTool(Tool):
    """
    Nimble Extract Templates tool: template run → structured JSON.

    :param config: Spec-level config, e.g. ``{"api_key": "...", "template": "google_search"}``.
    """

    def __init__(self, config: dict[str, str] | None = None) -> None:
        """
        :param config: Spec-level config with ``api_key`` and optional
            ``template``/``timeout_seconds``.
        """
        self._config = config or {}

    @classmethod
    def name(cls) -> str:
        """:returns: ``"nimble_extract"``."""
        return "nimble_extract"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return _DESCRIPTION

    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI function schema for nimble_extract.

        :returns: A function tool schema with a required ``params`` object.
        """
        return {
            "type": "function",
            "function": {
                "name": "nimble_extract",
                "description": _DESCRIPTION,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "params": {
                            "type": "object",
                            "description": (
                                "Parameters for the configured extract template, "
                                'e.g. {"query": "nimbleway"} for google_search.'
                            ),
                        },
                    },
                    "required": ["params"],
                },
            },
        }

    def is_async(self, arguments: str | None = None) -> bool:
        """
        Run nimble_extract synchronously in the parent's tool loop.

        :param arguments: Ignored — async-ness is a property of this tool.
        :returns: ``False``.
        """
        del arguments
        return False

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """
        Execute a nimble_extract call.

        :param arguments: JSON-encoded dict with a ``params`` object.
        :param ctx: Tool execution context (unused).
        :returns: Structured JSON, or an error message.
        """
        del ctx
        parsed, error = parse_json_object_arguments(arguments)
        if error is not None:
            return f"Error: {error}"
        assert parsed is not None
        params = parsed.get("params")
        if not isinstance(params, dict):
            return (
                "Error: 'params' object is required (the template's parameters, "
                'e.g. {"query": "..."} for google_search).'
            )
        return _run_extract(params, self._config)
