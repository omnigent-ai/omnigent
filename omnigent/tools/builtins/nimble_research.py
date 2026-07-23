"""Built-in tool: nimble_research — Nimble Agent API v2 research runs.

Runs a research task on a Nimble Web Search Agent through the asynchronous
Agent API v2: start a run (``POST /v2/agents/{agent_id}/runs``), poll the run
to a terminal status, then fetch the cited result. Returns a bounded JSON
envelope with the run id, the output (prose text or structured JSON), and
trust metadata (overall confidence, consulted sources, per-claim citations).
A run can take minutes; the tool blocks its worker thread and enforces its
``timeout_seconds`` deadline internally (a dispatched call cannot be
interrupted mid-run — it returns by its clamped deadline).

Configured in the agent spec::

    tools:
      builtins:
        - name: nimble_research
          api_key: ${NIMBLE_API_KEY}
          agent_id: wsa_...            # a Web Search Agent instance you own
          # optional:
          # effort: medium             # low | medium | high | x-high | max
          # timeout_seconds: 300
          # poll_interval_seconds: 2

``agent_id`` references an agent instance on your Nimble account; the tool
never creates one. One-time bootstrap::

    # list templates, create an instance from one, then copy its wsa_... id
    curl -H "Authorization: Bearer $NIMBLE_API_KEY" \\
         https://sdk.nimbleway.com/v2/agents/templates
    curl -X POST -H "Authorization: Bearer $NIMBLE_API_KEY" \\
         -H "Content-Type: application/json" -d '{"template": "company-profile"}' \\
         https://sdk.nimbleway.com/v2/agents

Per-run ``output_schema`` / ``input_data`` / ``sources`` overrides and event
streaming are not exposed here.

See https://docs.nimbleway.com/
"""

from __future__ import annotations

import json
import logging
import os
import time

# Any: Nimble's JSON payloads are heterogeneous dicts with string keys and
# mixed value types (str, int, bool, dict, list, None).
from typing import Any

import httpx

from omnigent.tools.base import Tool, ToolContext
from omnigent.tools.builtins._arguments import parse_json_object_arguments

_logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://sdk.nimbleway.com"

# Identifies this integration to Nimble via the ``X-Client-Source`` header that
# Nimble's own SDKs send (same convention as the web_search nimble backend).
_CLIENT_SOURCE = "omnigent"

# Effort tiers accepted by the Agent API v2. Validated locally so a bad spec or
# tool call gets a clear error instead of an opaque API failure.
_VALID_EFFORTS = frozenset({"low", "medium", "high", "x-high", "max"})

# Run lifecycle statuses. Anything outside these sets is a protocol error.
_NONTERMINAL_STATUSES = frozenset({"queued", "running"})
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})

# Run ids have the form ``task_run_<uuid>``.
_RUN_ID_PREFIX = "task_run_"

# Overall deadline for one tool call (start + poll + result), and the pause
# between polls. Spec-configurable; clamped to keep a typo from hanging the
# worker thread (the runner cannot interrupt it).
_DEFAULT_TIMEOUT_S = 300.0
_MIN_TIMEOUT_S = 10.0
_MAX_TIMEOUT_S = 3600.0
_DEFAULT_POLL_INTERVAL_S = 2.0
_MIN_POLL_INTERVAL_S = 0.5
_MAX_POLL_INTERVAL_S = 30.0

# Per-request HTTP timeout (each start/poll/result call).
_HTTP_TIMEOUT_S = 30.0

# Consecutive transient failures (transport error, 408, 429, 5xx) tolerated
# while polling before giving up.
_MAX_TRANSIENT_RETRIES = 3

# Caps that keep the returned envelope from blowing the model context.
_MAX_CONTENT_CHARS = 50_000
_MAX_TRUST_SOURCES = 10
_MAX_TRUST_CLAIMS = 10
_MAX_CITATIONS_PER_CLAIM = 3

# Test seams: unit tests monkeypatch these module attributes with a fake clock
# instead of patching the process-global ``time`` module.
_monotonic = time.monotonic
_sleep = time.sleep

_DESCRIPTION = (
    "Run a Nimble research agent (Agent API v2) on a task and return its cited "
    "result as JSON: {run_id, status, output: {type: text|json, content}, "
    "trust: {confidence, sources, claims}}. The agent researches the live web; "
    "a run can take minutes. Use for questions that need fresh, sourced "
    "research rather than a quick search."
)


def _base_url() -> str:
    """Resolve the base URL; ``OMNIGENT_NIMBLE_RESEARCH_BASE_URL`` overrides for tests."""
    return os.environ.get("OMNIGENT_NIMBLE_RESEARCH_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")


def _headers(api_key: str) -> dict[str, str]:
    """Build the headers sent on every Agent API v2 request."""
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-Client-Source": _CLIENT_SOURCE,
    }


def _clamp_seconds(
    config: dict[str, str], key: str, default: float, lo: float, hi: float
) -> float:
    """
    Read a seconds value from spec config, clamped to ``[lo, hi]``.

    :param config: Spec-level config; values arrive as strings from YAML.
    :param key: Config key to read.
    :param default: Value when the key is missing or not a number.
    :param lo: Minimum allowed value.
    :param hi: Maximum allowed value.
    :returns: A usable duration in seconds.
    """
    raw = config.get(key)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    if value != value:  # NaN guard: NaN breaks min/max clamping
        return default
    return max(lo, min(value, hi))


def _resolve_effort(
    config: dict[str, str], arg_value: str | None
) -> tuple[str | None, str | None]:
    """
    Resolve the effort for this run: the tool-call value overrides the spec
    default; neither set means the field is omitted so the agent instance's
    own default effort applies server-side.

    :param config: Spec-level config (optional ``effort``).
    :param arg_value: Effort from the tool call, if any.
    :returns: ``(effort, None)`` on success (``effort`` may be ``None`` =
        omit), or ``(None, error_message)``.
    """
    raw = arg_value if arg_value is not None else config.get("effort")
    if raw is None or not str(raw).strip():
        return None, None
    value = str(raw).strip().lower()
    if value not in _VALID_EFFORTS:
        return None, (
            f"Error: unsupported effort {str(raw)!r}. "
            f"Use one of: {', '.join(sorted(_VALID_EFFORTS))}."
        )
    return value, None


def _parse_json_dict(resp: httpx.Response) -> dict[str, Any] | None:
    """Parse a response body as a JSON object; ``None`` when it isn't one."""
    try:
        data = resp.json()
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _retry_after_seconds(resp: httpx.Response) -> float | None:
    """Parse a numeric ``Retry-After`` header; ``None`` when absent/non-numeric."""
    raw = resp.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


def _http_error_detail(resp: httpx.Response) -> str:
    """
    A `` : <message> (task: ...)`` suffix built from an error response body,
    when it carries one (e.g. ``{"message": "...", "task_id": "..."}``).

    :param resp: The non-2xx response.
    :returns: A human-readable suffix, or ``""``.
    """
    body = _parse_json_dict(resp)
    if body is None:
        return ""
    message = body.get("message") or body.get("msg")
    if not isinstance(message, str) or not message.strip():
        return ""
    detail = f": {message.strip()}"
    task_id = body.get("task_id")
    if isinstance(task_id, str) and task_id:
        detail += f" (task: {task_id})"
    return detail


def _run_error_message(run_id: str, status: str, error: object) -> str:
    """
    Build the failure message for a terminal ``failed``/``cancelled`` run,
    preserving the run id.

    :param run_id: The ``task_run_...`` id.
    :param status: The terminal status.
    :param error: The run's structured error (``{ref_id, message}``), if any.
    :returns: A one-line error message.
    """
    message = ""
    if isinstance(error, dict):
        raw = error.get("message")
        if isinstance(raw, str):
            message = raw.strip()
    if message:
        return f"Nimble research run {run_id} {status}: {message}"
    return f"Nimble research run {run_id} {status} without an error message."


def _start_run(
    base: str,
    headers: dict[str, str],
    agent_id: str,
    task: str,
    effort: str | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """
    Start a run (``POST /v2/agents/{agent_id}/runs``). Never retried — run
    creation is not idempotent.

    :param base: API base URL.
    :param headers: Request headers.
    :param agent_id: The Web Search Agent instance id.
    :param task: The research task (the run's ``input``).
    :param effort: Optional effort override; omitted from the body when ``None``.
    :returns: ``(run, None)`` with a validated ``task_run_...`` id, or
        ``(None, error_message)``.
    """
    body: dict[str, Any] = {"input": task}
    if effort is not None:
        body["effort"] = effort
    try:
        resp = httpx.post(
            f"{base}/v2/agents/{agent_id}/runs",
            headers=headers,
            json=body,
            timeout=_HTTP_TIMEOUT_S,
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code in (401, 403):
            return None, (
                f"Nimble research error: HTTP {code} (authentication failed — check the "
                "api_key in the nimble_research config)."
            )
        if code == 404:
            return None, (
                "Nimble research error: HTTP 404 (agent not found — verify the agent_id "
                "in the nimble_research config)."
            )
        if code == 422:
            return (
                None,
                "Nimble research error: HTTP 422 (the run request was rejected as invalid).",
            )
        return None, f"Nimble research error: HTTP {code}{_http_error_detail(exc.response)}"
    except httpx.RequestError as exc:
        return None, f"Nimble research error: {exc}"
    run = _parse_json_dict(resp)
    if run is None:
        return None, "Nimble research error: run creation returned a non-JSON response."
    run_id = run.get("id")
    if not isinstance(run_id, str) or not run_id.startswith(_RUN_ID_PREFIX):
        return None, (
            f"Nimble research error: expected a '{_RUN_ID_PREFIX}...' run id, got {run_id!r}."
        )
    return run, None


def _poll_until_terminal(
    base: str,
    headers: dict[str, str],
    agent_id: str,
    run: dict[str, Any],
    deadline: float,
    interval_s: float,
    timeout_s: float,
) -> tuple[dict[str, Any] | None, str | None]:
    """
    Poll ``GET /v2/agents/{agent_id}/runs/{run_id}`` until a terminal status
    or the monotonic deadline. The run dict from creation is the first
    observation, so an already-terminal run costs zero sleeps.

    :param base: API base URL.
    :param headers: Request headers.
    :param agent_id: The Web Search Agent instance id.
    :param run: The last observed run object (from creation).
    :param deadline: Absolute ``_monotonic()`` deadline.
    :param interval_s: Pause between polls.
    :param timeout_s: Total budget, used in the timeout message.
    :returns: ``(terminal_run, None)`` or ``(None, error_message)``. Every
        error message carries the run id.
    """
    run_id = str(run.get("id"))
    current = run
    transient_failures = 0
    while True:
        status = current.get("status")
        if not isinstance(status, str) or status not in (
            _NONTERMINAL_STATUSES | _TERMINAL_STATUSES
        ):
            return None, (
                f"Nimble research run {run_id} returned unknown status {status!r} "
                f"(agent {agent_id}); treating this as a protocol error."
            )
        if status in _TERMINAL_STATUSES:
            return current, None
        remaining = deadline - _monotonic()
        if remaining <= 0:
            return None, (
                f"Nimble research run {run_id} timed out after {timeout_s:g}s "
                f"(last status: {status}). The run may still complete server-side."
            )
        _sleep(min(interval_s, remaining))
        if deadline - _monotonic() <= 0:
            return None, (
                f"Nimble research run {run_id} timed out after {timeout_s:g}s "
                f"(last status: {status}). The run may still complete server-side."
            )
        try:
            resp = httpx.get(
                f"{base}/v2/agents/{agent_id}/runs/{run_id}",
                headers=headers,
                timeout=_HTTP_TIMEOUT_S,
            )
        except httpx.RequestError as exc:
            transient_failures += 1
            if transient_failures > _MAX_TRANSIENT_RETRIES:
                return None, f"Nimble research run {run_id} polling failed: {exc}"
            continue
        if resp.status_code == 429:
            transient_failures += 1
            if transient_failures > _MAX_TRANSIENT_RETRIES:
                return None, (
                    f"Nimble research run {run_id} polling was rate-limited "
                    f"(HTTP 429{_http_error_detail(resp)})."
                )
            retry_after = _retry_after_seconds(resp)
            if retry_after is not None:
                _sleep(min(retry_after, max(deadline - _monotonic(), 0.0)))
            continue
        if resp.status_code == 408 or resp.status_code >= 500:
            transient_failures += 1
            if transient_failures > _MAX_TRANSIENT_RETRIES:
                return (
                    None,
                    f"Nimble research run {run_id} polling failed: "
                    f"HTTP {resp.status_code}{_http_error_detail(resp)}",
                )
            continue
        if resp.status_code >= 400:
            return None, (
                f"Nimble research run {run_id} polling failed: "
                f"HTTP {resp.status_code}{_http_error_detail(resp)}"
            )
        data = _parse_json_dict(resp)
        if data is None:
            return None, f"Nimble research run {run_id} polling returned a non-JSON response."
        current = data
        transient_failures = 0


def _fetch_result(
    base: str,
    headers: dict[str, str],
    agent_id: str,
    run_id: str,
    deadline: float,
    interval_s: float,
) -> tuple[dict[str, Any] | None, str | None]:
    """
    Fetch ``GET /v2/agents/{agent_id}/runs/{run_id}/result`` after observing
    ``completed``. Success and failure are keyed off the body shape (an
    ``output`` vs an ``error``), not only the HTTP code.

    :param base: API base URL.
    :param headers: Request headers.
    :param agent_id: The Web Search Agent instance id.
    :param run_id: The completed run's id.
    :param deadline: Absolute ``_monotonic()`` deadline (bounds 409 re-checks).
    :param interval_s: Pause before re-checking after a 409.
    :returns: ``(result, None)`` where ``result`` has ``run`` + ``output``,
        or ``(None, error_message)``. Every error message carries the run id.
    """
    conflict_retries = 0
    while True:
        try:
            resp = httpx.get(
                f"{base}/v2/agents/{agent_id}/runs/{run_id}/result",
                headers=headers,
                timeout=_HTTP_TIMEOUT_S,
            )
        except httpx.RequestError as exc:
            return None, f"Nimble research run {run_id} result fetch failed: {exc}"
        if resp.status_code == 409:
            # The run was observed completed, so a 409 here is a brief
            # consistency race; re-check within the deadline.
            conflict_retries += 1
            remaining = deadline - _monotonic()
            if conflict_retries > _MAX_TRANSIENT_RETRIES or remaining <= 0:
                return None, (
                    f"Nimble research run {run_id} completed but its result was not yet "
                    "available (HTTP 409). Retry the task to fetch it."
                )
            _sleep(min(interval_s, remaining))
            continue
        if resp.status_code == 422:
            failed = _parse_json_dict(resp) or {}
            run = failed.get("run")
            status = "failed"
            if isinstance(run, dict) and isinstance(run.get("status"), str):
                status = str(run["status"])
            return None, _run_error_message(run_id, status, failed.get("error"))
        if resp.status_code >= 400:
            return (
                None,
                f"Nimble research run {run_id} result fetch failed: "
                f"HTTP {resp.status_code}{_http_error_detail(resp)}",
            )
        data = _parse_json_dict(resp)
        if data is None:
            return None, f"Nimble research run {run_id} result was not valid JSON."
        output = data.get("output")
        if isinstance(output, dict):
            return data, None
        error = data.get("error")
        if isinstance(error, dict):
            # Defensive: the API schema allows a failed-result body on HTTP 200.
            run = data.get("run")
            status = "failed"
            if isinstance(run, dict) and isinstance(run.get("status"), str):
                status = str(run["status"])
            return None, _run_error_message(run_id, status, error)
        return None, f"Nimble research run {run_id} result had an unexpected shape."


def _truncate(text: str, limit: int) -> str:
    """Cap text to keep the model context bounded."""
    if len(text) > limit:
        return text[:limit] + "\n\n[truncated]"
    return text


def _map_trust(trust: object) -> dict[str, Any]:
    """
    Map the API's trust metadata into a bounded envelope section: overall
    confidence + reasoning, consulted sources (capped), and per-claim
    citations (capped; verbatim excerpts dropped for size).

    :param trust: The ``trust`` object from the run output, if any.
    :returns: A bounded dict; empty when the input isn't a dict.
    """
    if not isinstance(trust, dict):
        return {}
    mapped: dict[str, Any] = {}
    confidence = trust.get("confidence")
    if isinstance(confidence, str):
        mapped["confidence"] = confidence
    reasoning = trust.get("reasoning")
    if isinstance(reasoning, str):
        mapped["reasoning"] = reasoning

    raw_sources = trust.get("sources")
    sources = raw_sources if isinstance(raw_sources, list) else []
    kept_sources: list[dict[str, Any]] = []
    for source in sources[:_MAX_TRUST_SOURCES]:
        if not isinstance(source, dict):
            continue
        entry: dict[str, Any] = {"url": source.get("url")}
        if isinstance(source.get("type"), str):
            entry["type"] = source["type"]
        if isinstance(source.get("title"), str):
            entry["title"] = source["title"]
        kept_sources.append(entry)
    mapped["sources"] = kept_sources
    if len(sources) > _MAX_TRUST_SOURCES:
        mapped["sources_omitted"] = len(sources) - _MAX_TRUST_SOURCES

    raw_claims = trust.get("claims")
    claims = raw_claims if isinstance(raw_claims, list) else []
    kept_claims: list[dict[str, Any]] = []
    for claim in claims[:_MAX_TRUST_CLAIMS]:
        if not isinstance(claim, dict):
            continue
        entry = {}
        if isinstance(claim.get("callout"), int):
            entry["callout"] = claim["callout"]
        if isinstance(claim.get("path"), str):
            entry["path"] = claim["path"]
        if isinstance(claim.get("confidence"), str):
            entry["confidence"] = claim["confidence"]
        raw_citations = claim.get("citations")
        citations = raw_citations if isinstance(raw_citations, list) else []
        kept_citations: list[dict[str, Any]] = []
        for citation in citations[:_MAX_CITATIONS_PER_CLAIM]:
            if not isinstance(citation, dict):
                continue
            cite: dict[str, Any] = {"url": citation.get("url")}
            if isinstance(citation.get("title"), str):
                cite["title"] = citation["title"]
            kept_citations.append(cite)
        entry["citations"] = kept_citations
        kept_claims.append(entry)
    mapped["claims"] = kept_claims
    if len(claims) > _MAX_TRUST_CLAIMS:
        mapped["claims_omitted"] = len(claims) - _MAX_TRUST_CLAIMS
    return mapped


def _build_envelope(run_id: str, output: dict[str, Any]) -> str:
    """
    Serialize the completed run's output into the tool's bounded JSON
    envelope. Caps are applied before the single ``json.dumps`` so the
    result is always valid JSON. When structured output is too large,
    ``content`` falls back to a truncated string while ``type`` stays
    ``json`` — ``content_truncated: true`` marks that case.

    :param run_id: The ``task_run_...`` id.
    :param output: The result's ``output`` object (``type``/``content``/``trust``).
    :returns: The envelope as a JSON string.
    """
    raw_type = output.get("type")
    content = output.get("content")
    output_type = (
        raw_type if isinstance(raw_type, str) else ("text" if isinstance(content, str) else "json")
    )
    content_truncated = False
    if isinstance(content, str):
        capped = _truncate(content, _MAX_CONTENT_CHARS)
        content_truncated = capped is not content
        content = capped
    else:
        serialized = json.dumps(content, ensure_ascii=False)
        if len(serialized) > _MAX_CONTENT_CHARS:
            # Structured output too large to pass through intact; fall back to
            # a truncated string form so the envelope stays bounded.
            content = _truncate(serialized, _MAX_CONTENT_CHARS)
            content_truncated = True
    envelope: dict[str, Any] = {
        "run_id": run_id,
        "status": "completed",
        "output": {"type": output_type, "content": content},
        "trust": _map_trust(output.get("trust")),
    }
    if content_truncated:
        envelope["output"]["content_truncated"] = True
    return json.dumps(envelope, ensure_ascii=False)


def _run_agent_v2(task: str, effort_arg: str | None, config: dict[str, str]) -> str:
    """
    Run the full Agent API v2 lifecycle for one task: start, poll to a
    terminal status, fetch the result, and build the envelope.

    :param task: The research task.
    :param effort_arg: Effort from the tool call, if any.
    :param config: Spec-level config (``api_key``, ``agent_id``, optional
        ``effort``/``timeout_seconds``/``poll_interval_seconds``).
    :returns: The JSON envelope, or an error message. Never raises.
    """
    api_key = config.get("api_key")
    if not api_key:
        return "Error: api_key must be provided in the nimble_research config in config.yaml."
    agent_id = config.get("agent_id")
    if not agent_id:
        return (
            "Error: agent_id must be provided in the nimble_research config in config.yaml. "
            "List your agents (GET /v2/agents) or create one from a template "
            "(POST /v2/agents) — see the nimble_research module docstring."
        )
    effort, effort_error = _resolve_effort(config, effort_arg)
    if effort_error is not None:
        return effort_error
    timeout_s = _clamp_seconds(
        config, "timeout_seconds", _DEFAULT_TIMEOUT_S, _MIN_TIMEOUT_S, _MAX_TIMEOUT_S
    )
    interval_s = _clamp_seconds(
        config,
        "poll_interval_seconds",
        _DEFAULT_POLL_INTERVAL_S,
        _MIN_POLL_INTERVAL_S,
        _MAX_POLL_INTERVAL_S,
    )
    base = _base_url()
    headers = _headers(api_key)
    deadline = _monotonic() + timeout_s

    run, error = _start_run(base, headers, agent_id, task, effort)
    if error is not None or run is None:
        return error or "Nimble research error: run creation returned no data."
    run_id = str(run.get("id"))

    terminal, error = _poll_until_terminal(
        base, headers, agent_id, run, deadline, interval_s, timeout_s
    )
    if error is not None or terminal is None:
        return error or f"Nimble research run {run_id} polling returned no data."
    status = str(terminal.get("status"))
    if status in ("failed", "cancelled"):
        return _run_error_message(run_id, status, terminal.get("error"))

    result, error = _fetch_result(base, headers, agent_id, run_id, deadline, interval_s)
    if error is not None or result is None:
        return error or f"Nimble research run {run_id} returned no result data."
    output = result.get("output")
    if not isinstance(output, dict):
        return f"Nimble research run {run_id} result had no output object."
    return _build_envelope(run_id, output)


class NimbleResearchTool(Tool):
    """
    Nimble Agent API v2 tool: asynchronous research runs with cited results.

    :param config: Spec-level config, e.g. ``{"api_key": "...", "agent_id": "wsa_..."}``.
    """

    def __init__(self, config: dict[str, str] | None = None) -> None:
        """
        :param config: Spec-level config with ``api_key`` + ``agent_id`` and
            optional ``effort``/``timeout_seconds``/``poll_interval_seconds``.
        """
        self._config = config or {}

    @classmethod
    def name(cls) -> str:
        """:returns: ``"nimble_research"``."""
        return "nimble_research"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return _DESCRIPTION

    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI function schema for nimble_research.

        :returns: A function tool schema with a required ``task`` parameter
            and an optional ``effort`` enum.
        """
        return {
            "type": "function",
            "function": {
                "name": "nimble_research",
                "description": _DESCRIPTION,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task": {
                            "type": "string",
                            "description": "The research task or question for the agent.",
                        },
                        "effort": {
                            "type": "string",
                            "enum": sorted(_VALID_EFFORTS),
                            "description": "Optional effort override for this run.",
                        },
                    },
                    "required": ["task"],
                },
            },
        }

    def is_async(self, arguments: str | None = None) -> bool:
        """
        Run nimble_research synchronously in the parent's tool loop; the runner
        bridges the blocking lifecycle off the event loop.

        :param arguments: Ignored — async-ness is a property of this tool.
        :returns: ``False``.
        """
        del arguments
        return False

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """
        Execute a nimble_research call.

        :param arguments: JSON-encoded dict with a ``task`` key and an
            optional ``effort`` key.
        :param ctx: Tool execution context (unused).
        :returns: The JSON envelope, or an error message.
        """
        del ctx
        parsed, error = parse_json_object_arguments(arguments)
        if error is not None:
            return f"Error: {error}"
        assert parsed is not None
        task = parsed.get("task")
        if task is None or not str(task).strip():
            return "Error: 'task' parameter is required."
        effort_arg = parsed.get("effort")
        return _run_agent_v2(
            str(task), str(effort_arg) if effort_arg is not None else None, self._config
        )
