"""Built-in tool: nimble_research — Nimble Agent API v2 research runs.

Runs a research task on a Nimble Web Search Agent through the asynchronous
Agent API v2: start a run, poll it to a terminal status, then fetch the cited
result. Returns a bounded JSON envelope with both run identifiers, the output
(prose text or structured JSON), and trust metadata (overall confidence,
consulted sources, per-claim citations). A run can take minutes; the tool
blocks its worker thread and enforces its ``timeout_seconds`` deadline
internally (a dispatched call cannot be interrupted mid-run — it returns by
its clamped deadline).

``agent_id`` is optional, and selects which of the two published create routes
is used:

* omitted — ``POST /v2/agents/runs``. Nimble provisions the agent for this run
  and returns its id. No account agent has to exist first.
* provided — ``POST /v2/agents/{agent_id}/runs``, against an agent you own.

Either way the ``202`` carries both a run ``id`` and a ``web_search_agent_id``.
The **returned** agent id — never the configured one — is what the subsequent
status and result requests are addressed to, because on the generic route it
is the only id that exists. When both are present and disagree, the run is
rejected rather than silently re-pointed at the configured agent.

Run creation is billable and not idempotent, and the API exposes no idempotency
key, so the create call is issued exactly once and never retried — on any
outcome, including transport errors, 408, 409, 429, and 5xx. Retries are
confined to the read-only calls, each within a bounded budget: polling retries
transport errors, 408, 429, and 5xx, while the result call re-checks only the
documented 409 (completed, but the result is not materialized yet).

Some create failures leave the billing outcome unknown — a transport error or
timeout may still have been received, and 408/5xx reached Nimble before failing
— while a 202 carrying an unusable body means the run was definitely created.
Those errors say so explicitly and tell the caller not to resubmit, pointing at
the run id when one survived and at the account's run history when none did. A
clear rejection (401/403/404/422) created nothing and carries no such warning, and a
create-time 429 is treated the same way: a rate limiter refuses the request before a run
is started, so there is nothing to reconcile. Once creation succeeds the run exists and is
billed, so every later failure — timeout, polling, result fetch — carries the same
do-not-resubmit guidance, keyed to the run id.

Effort is optional. When omitted, Nimble applies the selected agent/template
default (the documented product default is ``high``; template defaults vary).
The generally available ``low``, ``medium``, ``high``, and ``x-high`` tiers
can be selected per run. The schema-visible ``max`` value is a coming-soon
custom-budget capability and is not offered as a selectable tier. If supplied
directly, it stops before create with product-team guidance and is never
silently substituted.

Requires the optional ``nimble`` extra (``pip install 'omnigent[nimble]'``);
the nimble-python client is imported lazily, so the base install never
needs it.

Configured in the agent spec::

    tools:
      builtins:
        - name: nimble_research
          api_key: ${NIMBLE_API_KEY}
          # optional:
          # agent_id: wsa_...          # omit to let Nimble provision the agent
          # timeout_seconds: 300
          # poll_interval_seconds: 10

Per-call ``input_data``, ``output_schema``, and ``sources`` are exposed as tool
arguments and forwarded unchanged on both create routes, for enrichment,
structured output, and source guidance respectively.

Released ``nimble-python`` 1.2 typed fields ``skill``, ``use_case``, and
``agent_name`` are exposed per run and forwarded through the SDK on both
create routes. No ``extra_body`` escape hatch is used.

Event streaming (``enable_events``) and ``previous_interaction_id`` are not
exposed here.

See https://docs.nimbleway.com/
"""

from __future__ import annotations

import json
import logging
import os
import re
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

_SELECTABLE_EFFORTS = frozenset({"low", "medium", "high", "x-high"})
_VALID_USE_CASES = frozenset({"research", "enrichment", "dataset_building"})
_MAX_CONTACT = "https://www.nimbleway.com/contact"

# Run lifecycle statuses. Anything outside these sets is a protocol error.
_NONTERMINAL_STATUSES = frozenset({"queued", "running"})
_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})

# Run ids have the form ``task_run_<uuid>``.
_RUN_ID_PREFIX = "task_run_"

# Run and agent ids are interpolated into request paths, so they are restricted
# to the characters their documented forms actually use. This is an allowlist on
# purpose: a value like ``../../admin`` contains no control characters, but
# httpx resolves the dot segments and would silently retarget an authenticated
# request at a different endpoint. Ids arrive from config *and* from API
# responses, so neither source is trusted to be path-safe.
_ID_SAFE_RE = re.compile(r"\A[A-Za-z0-9_-]+\Z")

# Overall deadline for one tool call (start + poll + result), and the pause
# between polls. Spec-configurable; clamped to keep a typo from hanging the
# worker thread (the runner cannot interrupt it).
_DEFAULT_TIMEOUT_S = 300.0
_MIN_TIMEOUT_S = 10.0
_MAX_TIMEOUT_S = 3600.0
_DEFAULT_POLL_INTERVAL_S = 10.0
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
# Per-string cap for any API-supplied value reflected into the envelope or an
# error message, so a single oversized field can't inflate either past its bound.
_MAX_FIELD_CHARS = 2_000
# Per-field caps still multiply across sources x claims x citations, so the
# trust section is bounded as a whole too.
_MAX_TRUST_CHARS = 20_000
# A run id is "task_run_" + a uuid (~45 chars); anything far longer is a
# protocol violation, not a value to echo back or build a URL from. The same
# bound applies to the agent id, which is likewise interpolated into a URL.
_MAX_RUN_ID_CHARS = 200
_MAX_AGENT_ID_CHARS = 200

# Test seams: unit tests monkeypatch these module attributes with a fake clock
# instead of patching the process-global ``time`` module.
_monotonic = time.monotonic
_sleep = time.sleep

_DESCRIPTION = (
    "Run a Nimble research agent (Agent API v2) on a task and return its cited "
    "result as JSON: {run_id, web_search_agent_id, status, "
    "output: {type: text|json, content}, trust: {confidence, sources, claims}}. "
    "The agent researches the live web; a run can take minutes. Use for "
    "questions that need fresh, sourced research rather than a quick search. "
    "Pass output_schema to get structured output, input_data to enrich given "
    "records, and sources to steer which sites are used."
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
    Resolve an optional effort override. An omission stays ``None`` so the
    selected agent/template default applies.

    A requested tier is either honored or refused: there is no substitution, so
    a resolved effort is always exactly what the caller asked for.

    :param config: Spec-level config (optional ``effort``).
    :param arg_value: Effort from the tool call, if any.
    :returns: ``(effective_effort, error_message)``.
    """
    raw = arg_value if arg_value is not None else config.get("effort")
    if raw is None or not str(raw).strip():
        return None, None
    value = str(raw).strip().lower()
    if value == "max":
        return (
            None,
            (
                "Max effort is a coming-soon custom-budget capability and is not "
                "generally selectable. Requested effort='max'; nothing was sent "
                "and no run was created. Contact the Nimble product team about "
                f"enabling Max: {_MAX_CONTACT}. To continue now, explicitly choose "
                "x-high; it will never be substituted silently."
            ),
        )
    if value not in _SELECTABLE_EFFORTS:
        return (
            None,
            (f"Error: unsupported effort {str(raw)!r}. Choose low, medium, high, or x-high."),
        )
    return value, None


def _created_run_guidance(run_id: str) -> str:
    """
    Guidance for a failure that happens *after* the run was created.

    Once creation succeeds the run exists and has been billed, so resubmitting
    the task creates and pays for a second one. Every post-create failure ends
    with this, mirroring the create-time rule rather than leaving the caller to
    guess which side of the boundary it is on.

    :param run_id: The run that already exists.
    :returns: A sentence telling the caller to reconcile, not resubmit.
    """
    return (
        f" Do not resubmit this task automatically: run {_cap_field(run_id)} was already "
        "created and billed — reconcile it by run id before trying again."
    )


def _unresolved_create_error(message: str, run_id: str | None = None) -> str:
    """
    Add do-not-resubmit guidance to a create failure whose billing outcome is
    unknown or already settled.

    A create that fails in transport, times out, or returns 408/5xx may still
    have been accepted server-side, and a create that returns an unusable body
    definitely was. In both cases the run can be live and billed while this
    call reports an error, so the caller is told to reconcile rather than
    resubmit — a resubmission would pay for the task twice.

    :param message: The underlying error message.
    :param run_id: A durable run id to reconcile against, when one survived.
    :returns: The message with reconciliation guidance appended.
    """
    if run_id is not None:
        follow_up = f"reconcile run {_cap_field(run_id)} before trying again"
    else:
        follow_up = (
            "check the account's recent Agent API run history for a matching run "
            "before trying again"
        )
    # Messages arrive with and without trailing punctuation; normalize so the
    # guidance always reads as its own sentence.
    lead = message if message.endswith((".", "!", "?")) else f"{message}."
    return (
        f"{lead} Do not retry or resubmit this task automatically: the run may "
        f"already have been created and billed — {follow_up}."
    )


def _validate_agent_id(agent_id: object, source: str) -> str | None:
    """
    Reject an agent id that is unsafe to interpolate into a request URL.

    Applied to the configured id and to the one returned by run creation: both
    end up in a path, and the returned one is not under our control at all.

    :param agent_id: The candidate id.
    :param source: Where it came from, for the error message.
    :returns: An error message, or ``None`` when the id is usable.
    """
    if not isinstance(agent_id, str) or not agent_id.strip():
        return f"Nimble research error: {source} agent id is missing or not a string."
    if len(agent_id) > _MAX_AGENT_ID_CHARS:
        return f"Nimble research error: {source} agent id is implausibly long."
    if not _ID_SAFE_RE.match(agent_id):
        return (
            f"Nimble research error: {source} agent id "
            f"{_cap_field(repr(agent_id))} is not a valid agent id."
        )
    return None


def _resolve_controls(parsed: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """
    Collect the per-run controls the published run-create schema accepts, and
    type-check them here so a malformed one is a clear tool error rather than
    an opaque 422 on a billable request.

    Values are forwarded unchanged — their shapes are the API's contract, not
    this tool's, so remapping them would only lose fidelity.

    :param parsed: The decoded tool-call arguments.
    :returns: ``(controls, None)``, possibly empty, or ``(None, error_message)``.
    """
    controls: dict[str, Any] = {}

    input_data = parsed.get("input_data")
    if input_data is not None:
        is_object = isinstance(input_data, dict)
        is_object_array = isinstance(input_data, list) and all(
            isinstance(row, dict) for row in input_data
        )
        if not (is_object or is_object_array):
            return None, "Error: 'input_data' must be a JSON object or an array of JSON objects."
        controls["input_data"] = input_data

    output_schema = parsed.get("output_schema")
    if output_schema is not None:
        if not isinstance(output_schema, dict):
            return None, "Error: 'output_schema' must be a JSON object."
        controls["output_schema"] = output_schema

    sources = parsed.get("sources")
    if sources is not None:
        if not isinstance(sources, dict):
            return None, "Error: 'sources' must be a JSON object."
        allowed_keys = {"allow", "avoid", "block", "prioritize"}
        unknown_keys = set(sources) - allowed_keys
        if unknown_keys:
            return None, (
                "Error: 'sources' contains unsupported keys: "
                + ", ".join(sorted(str(key) for key in unknown_keys))
                + "."
            )
        for key in ("prioritize", "avoid"):
            value = sources.get(key)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                return None, f"Error: 'sources.{key}' must be a non-empty string."
        for key in ("allow", "block"):
            entries = sources.get(key)
            if entries is None:
                continue
            if not isinstance(entries, list):
                return None, f"Error: 'sources.{key}' must be an array."
            for index, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    return None, f"Error: 'sources.{key}[{index}]' must be a JSON object."
                title = entry.get("title")
                domains = entry.get("domains")
                if not isinstance(title, str) or not title.strip():
                    return None, (
                        f"Error: 'sources.{key}[{index}].title' must be a non-empty string."
                    )
                if (
                    not isinstance(domains, list)
                    or not domains
                    or not all(isinstance(domain, str) and domain.strip() for domain in domains)
                ):
                    return None, (
                        f"Error: 'sources.{key}[{index}].domains' must be a non-empty "
                        "array of non-empty strings."
                    )
                order = entry.get("order")
                if order is not None and (not isinstance(order, int) or isinstance(order, bool)):
                    return None, f"Error: 'sources.{key}[{index}].order' must be an integer."
        controls["sources"] = sources

    for key in ("agent_name", "skill"):
        value = parsed.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            return None, f"Error: {key!r} must be a non-empty string."
        controls[key] = value

    use_case = parsed.get("use_case")
    if use_case is not None:
        # isinstance first: a list/dict operand would make the frozenset
        # membership test raise TypeError (unhashable) instead of erroring.
        if not isinstance(use_case, str) or use_case not in _VALID_USE_CASES:
            return None, ("Error: 'use_case' must be research, enrichment, or dataset_building.")
        controls["use_case"] = use_case

    return controls, None


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


def _request_timeout(deadline: float) -> float:
    """
    Per-request HTTP timeout, clamped to the time left before *deadline* so a
    single call cannot overrun the tool's overall budget.

    :param deadline: Absolute ``_monotonic()`` deadline.
    :returns: Seconds for this request, at most ``_HTTP_TIMEOUT_S``.
    """
    return min(_HTTP_TIMEOUT_S, max(deadline - _monotonic(), 0.001))


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
    detail = f": {_cap_field(message.strip())}"
    task_id = body.get("task_id")
    if isinstance(task_id, str) and task_id:
        detail += f" (task: {_cap_field(task_id)})"
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
        return (
            f"Nimble research run {_cap_field(run_id)} {_cap_field(status)}: {_cap_field(message)}"
        )
    return (
        f"Nimble research run {_cap_field(run_id)} {_cap_field(status)} without an error message."
    )


def _start_run(
    base: str,
    headers: dict[str, str],
    agent_id: str | None,
    task: str,
    effort: str | None,
    controls: dict[str, Any],
    deadline: float,
) -> tuple[dict[str, Any] | None, str | None, str | None]:
    """
    Start a run, on whichever create route the caller's identity selects:
    ``POST /v2/agents/{agent_id}/runs`` when an agent is configured, else
    ``POST /v2/agents/runs``, which has Nimble provision one.

    Issued exactly once and never retried, on any outcome: creation is billable
    and not idempotent, and the API offers no idempotency key, so a retry risks
    paying for — and orphaning — a second run.

    :param base: API base URL.
    :param headers: Request headers.
    :param agent_id: A Web Search Agent instance id, or ``None`` for a
        generated agent.
    :param task: The research task (the run's ``input``).
    :param effort: Optional effort override. Omitted to use the agent/template
        default.
    :param controls: Validated SDK 1.2 per-run controls (``input_data``,
        ``output_schema``, ``sources``, ``agent_name``, ``skill``, and
        ``use_case``) to forward unchanged.
    :param deadline: Absolute ``_monotonic()`` deadline; clamps the create
        request's timeout so it cannot outlive the tool's budget.
    :returns: ``(run, agent_id, None)`` with a validated ``task_run_...`` id
        and the agent id to address the rest of the lifecycle to, or
        ``(None, None, error_message)``.
    """
    # Lazy: nimble-python is the optional `nimble` extra, so the base install
    # never needs it. Checked before anything is sent, so nothing is billed.
    try:
        from nimble_python import (  # pyrefly: ignore[missing-import]
            APIConnectionError,
            APIError,
            APIStatusError,
            Nimble,
        )
    except ImportError:
        return (
            None,
            None,
            (
                "Nimble research error: the nimble-python client is not "
                "installed. Install the 'nimble' extra (e.g. "
                "pip install 'omnigent[nimble]') to use nimble_research."
            ),
        )
    body: dict[str, Any] = {"input": task}
    if effort is not None:
        body["effort"] = effort
    body.update(controls)
    api_key = headers["Authorization"].removeprefix("Bearer ")
    client = Nimble(
        api_key=api_key,
        base_url=base,
        default_headers={"X-Client-Source": _CLIENT_SOURCE},
        max_retries=0,
    )
    created: Any
    try:
        request_timeout = _request_timeout(deadline)
        if agent_id:
            created = client.agents.runs.create(agent_id, **body, timeout=request_timeout)
        else:
            created = client.agents.run(**body, timeout=request_timeout)
    except APIStatusError as exc:
        code = exc.status_code
        if code in (401, 403):
            return (
                None,
                None,
                (
                    f"Nimble research error: HTTP {code} (authentication failed — check the "
                    "api_key in the nimble_research config)."
                ),
            )
        if code == 404:
            if agent_id:
                return (
                    None,
                    None,
                    (
                        "Nimble research error: HTTP 404 (agent not found — verify the agent_id "
                        "in the nimble_research config)."
                    ),
                )
            return (
                None,
                None,
                ("Nimble research error: HTTP 404 (the run-create endpoint was not found)."),
            )
        if code == 422:
            return (
                None,
                None,
                "Nimble research error: HTTP 422 (the run request was rejected as invalid).",
            )
        # 408 and 5xx are ambiguous: the request reached Nimble, so the run may
        # have been created before the failure was reported.
        detail = f"Nimble research error: HTTP {code}{_http_error_detail(exc.response)}"
        if code == 408 or code >= 500:
            return None, None, _unresolved_create_error(detail)
        return None, None, detail
    except APIConnectionError as exc:
        # Includes timeouts: the request may have been received and started a
        # run even though no response came back.
        return None, None, _unresolved_create_error(f"Nimble research error: {exc}")
    except APIError as exc:
        # e.g. APIResponseValidationError: a 2xx arrived but its body failed
        # the SDK's validation, so the run may exist and be billed with
        # nothing durable to reconcile against except the account history.
        return None, None, _unresolved_create_error(f"Nimble research error: {exc}")
    finally:
        client.close()
    run = created.model_dump(mode="json")
    run_id = run.get("id")
    if (
        not isinstance(run_id, str)
        or not run_id.startswith(_RUN_ID_PREFIX)
        or len(run_id) > _MAX_RUN_ID_CHARS
        or not _ID_SAFE_RE.match(run_id)
    ):
        # The run was accepted — and billed — but its id is unusable, so there
        # is nothing durable to reconcile against except the account history.
        return (
            None,
            None,
            _unresolved_create_error(
                f"Nimble research error: expected a '{_RUN_ID_PREFIX}...' run id, "
                f"got {_cap_field(repr(run_id))}."
            ),
        )
    returned_agent_id = run.get("web_search_agent_id")
    identity_error = _validate_agent_id(returned_agent_id, "the returned")
    if identity_error is not None:
        # Same situation, but the run id survived and identifies the live run.
        return None, None, _unresolved_create_error(identity_error, run_id=run_id)
    assert isinstance(returned_agent_id, str)
    if agent_id is not None and returned_agent_id != agent_id:
        # The run belongs to a different agent than the one it was requested
        # on. Addressing the rest of the lifecycle to either id would be a
        # guess, so surface it instead of silently picking one.
        return (
            None,
            None,
            _unresolved_create_error(
                f"Nimble research run {_cap_field(run_id)} was created on agent "
                f"{_cap_field(returned_agent_id)} but was requested on "
                f"{_cap_field(agent_id)}; refusing to continue with a mismatched agent.",
                run_id=run_id,
            ),
        )
    return run, returned_agent_id, None


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
                f"Nimble research run {run_id} returned unknown status "
                f"{_cap_field(repr(status))} "
                f"(agent {agent_id}); treating this as a protocol error."
            )
        if status in _TERMINAL_STATUSES:
            return current, None
        remaining = deadline - _monotonic()
        if remaining <= 0:
            return None, (
                f"Nimble research run {run_id} timed out after {timeout_s:g}s "
                f"(last status: {status}). The run may still complete server-side."
                + _created_run_guidance(run_id)
            )
        _sleep(min(interval_s, remaining))
        if deadline - _monotonic() <= 0:
            return None, (
                f"Nimble research run {run_id} timed out after {timeout_s:g}s "
                f"(last status: {status}). The run may still complete server-side."
                + _created_run_guidance(run_id)
            )
        try:
            resp = httpx.get(
                f"{base}/v2/agents/{agent_id}/runs/{run_id}",
                headers=headers,
                timeout=_request_timeout(deadline),
            )
        except httpx.InvalidURL as exc:
            return None, (
                f"Nimble research run {run_id} polling failed: {exc}"
                + _created_run_guidance(run_id)
            )
        except httpx.RequestError as exc:
            transient_failures += 1
            if transient_failures > _MAX_TRANSIENT_RETRIES:
                return None, (
                    f"Nimble research run {run_id} polling failed: {exc}"
                    + _created_run_guidance(run_id)
                )
            continue
        if resp.status_code == 429:
            transient_failures += 1
            if transient_failures > _MAX_TRANSIENT_RETRIES:
                return None, (
                    f"Nimble research run {run_id} polling was rate-limited "
                    f"(HTTP 429{_http_error_detail(resp)})." + _created_run_guidance(run_id)
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
                    f"HTTP {resp.status_code}{_http_error_detail(resp)}"
                    + _created_run_guidance(run_id),
                )
            continue
        if resp.status_code >= 400:
            return None, (
                f"Nimble research run {run_id} polling failed: "
                f"HTTP {resp.status_code}{_http_error_detail(resp)}"
                + _created_run_guidance(run_id)
            )
        data = _parse_json_dict(resp)
        if data is None:
            return None, (
                f"Nimble research run {run_id} polling returned a non-JSON response."
                + _created_run_guidance(run_id)
            )
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
                timeout=_request_timeout(deadline),
            )
        except (httpx.RequestError, httpx.InvalidURL) as exc:
            return None, (
                f"Nimble research run {run_id} result fetch failed: {exc}"
                + _created_run_guidance(run_id)
            )
        if resp.status_code == 409:
            # The run was observed completed, so a 409 here is a brief
            # consistency race; re-check within the deadline.
            conflict_retries += 1
            remaining = deadline - _monotonic()
            if conflict_retries > _MAX_TRANSIENT_RETRIES or remaining <= 0:
                return None, (
                    f"Nimble research run {run_id} completed but its result was not yet "
                    "available (HTTP 409)." + _created_run_guidance(run_id)
                )
            _sleep(min(interval_s, remaining))
            continue
        if resp.status_code == 422:
            failed = _parse_json_dict(resp) or {}
            run = failed.get("run")
            status = "failed"
            if isinstance(run, dict) and run.get("status") in _TERMINAL_STATUSES:
                status = str(run["status"])
            return None, _run_error_message(run_id, status, failed.get("error"))
        if resp.status_code >= 400:
            return (
                None,
                f"Nimble research run {run_id} result fetch failed: "
                f"HTTP {resp.status_code}{_http_error_detail(resp)}"
                + _created_run_guidance(run_id),
            )
        data = _parse_json_dict(resp)
        if data is None:
            return None, (
                f"Nimble research run {run_id} result was not valid JSON."
                + _created_run_guidance(run_id)
            )
        output = data.get("output")
        if isinstance(output, dict):
            return data, None
        error = data.get("error")
        if isinstance(error, dict):
            # Defensive: the API schema allows a failed-result body on HTTP 200.
            run = data.get("run")
            status = "failed"
            if isinstance(run, dict) and run.get("status") in _TERMINAL_STATUSES:
                status = str(run["status"])
            return None, _run_error_message(run_id, status, error)
        return None, f"Nimble research run {run_id} result had an unexpected shape."


def _truncate(text: str, limit: int) -> str:
    """Cap text to keep the model context bounded."""
    if len(text) > limit:
        return text[:limit] + "\n\n[truncated]"
    return text


def _cap_field(value: str) -> str:
    """Cap a single API-supplied trust string to keep the envelope bounded."""
    return value if len(value) <= _MAX_FIELD_CHARS else value[:_MAX_FIELD_CHARS] + "…"


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
        mapped["confidence"] = _cap_field(confidence)
    reasoning = trust.get("reasoning")
    if isinstance(reasoning, str):
        mapped["reasoning"] = _cap_field(reasoning)

    raw_sources = trust.get("sources")
    sources = raw_sources if isinstance(raw_sources, list) else []
    kept_sources: list[dict[str, Any]] = []
    for source in sources[:_MAX_TRUST_SOURCES]:
        if not isinstance(source, dict):
            continue
        url = source.get("url")
        entry: dict[str, Any] = {"url": _cap_field(url) if isinstance(url, str) else None}
        if isinstance(source.get("type"), str):
            entry["type"] = _cap_field(source["type"])
        if isinstance(source.get("title"), str):
            entry["title"] = _cap_field(source["title"])
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
            entry["path"] = _cap_field(claim["path"])
        if isinstance(claim.get("confidence"), str):
            entry["confidence"] = _cap_field(claim["confidence"])
        raw_citations = claim.get("citations")
        citations = raw_citations if isinstance(raw_citations, list) else []
        kept_citations: list[dict[str, Any]] = []
        for citation in citations[:_MAX_CITATIONS_PER_CLAIM]:
            if not isinstance(citation, dict):
                continue
            curl = citation.get("url")
            cite: dict[str, Any] = {"url": _cap_field(curl) if isinstance(curl, str) else None}
            if isinstance(citation.get("title"), str):
                cite["title"] = _cap_field(citation["title"])
            kept_citations.append(cite)
        entry["citations"] = kept_citations
        kept_claims.append(entry)
    mapped["claims"] = kept_claims
    if len(claims) > _MAX_TRUST_CLAIMS:
        mapped["claims_omitted"] = len(claims) - _MAX_TRUST_CLAIMS

    # Per-field caps multiply across sources x claims x citations, so bound the
    # section as a whole. Claims carry the most text, so they go first; the
    # omitted counts stay so the reader knows data was withheld.
    for section, raw in (("claims", claims), ("sources", sources)):
        if len(json.dumps(mapped, ensure_ascii=False)) <= _MAX_TRUST_CHARS:
            break
        mapped[section] = []
        mapped[f"{section}_omitted"] = len(raw)
        mapped["trust_truncated"] = True
    return mapped


def _build_envelope(run_id: str, agent_id: str, output: dict[str, Any]) -> str:
    """
    Serialize the completed run's output into the tool's bounded JSON
    envelope. Caps are applied before the single ``json.dumps`` so the
    result is always valid JSON. When structured output is too large,
    ``content`` falls back to a truncated string while ``type`` stays
    ``json`` — ``content_truncated: true`` marks that case.

    Both identifiers are reported: on a generated run the agent id is only
    knowable from the run that created it, so dropping it would strand the
    run for any later follow-up.

    :param run_id: The ``task_run_...`` id.
    :param agent_id: The agent id the run actually executed on.
    :param output: The result's ``output`` object (``type``/``content``/``trust``).
    :returns: The envelope as a JSON string.
    """
    raw_type = output.get("type")
    content = output.get("content")
    output_type = (
        _cap_field(raw_type)
        if isinstance(raw_type, str)
        else ("text" if isinstance(content, str) else "json")
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
        "web_search_agent_id": agent_id,
        "status": "completed",
        "output": {"type": output_type, "content": content},
        "trust": _map_trust(output.get("trust")),
    }
    if content_truncated:
        envelope["output"]["content_truncated"] = True
    return json.dumps(envelope, ensure_ascii=False)


def _run_agent_v2(
    task: str,
    effort_arg: str | None,
    controls: dict[str, Any],
    config: dict[str, str],
) -> str:
    """
    Run the full Agent API v2 lifecycle for one task: start, poll to a
    terminal status, fetch the result, and build the envelope.

    :param task: The research task.
    :param effort_arg: Effort from the tool call, if any.
    :param controls: Validated per-run controls to forward on create.
    :param config: Spec-level config (``api_key``, optional ``agent_id``,
        ``effort``/``timeout_seconds``/``poll_interval_seconds``).
    :returns: The JSON envelope, or an error message. Never raises.
    """
    api_key = config.get("api_key")
    if not api_key:
        return "Error: api_key must be provided in the nimble_research config in config.yaml."
    # Optional: absent means the generic create route provisions the agent.
    configured_agent_id = config.get("agent_id") or None
    if configured_agent_id is not None:
        config_error = _validate_agent_id(configured_agent_id, "the configured")
        if config_error is not None:
            return config_error
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

    return _run_lifecycle(
        base, headers, configured_agent_id, task, effort, controls, deadline, interval_s, timeout_s
    )


def _run_lifecycle(
    base: str,
    headers: dict[str, str],
    configured_agent_id: str | None,
    task: str,
    effort: str | None,
    controls: dict[str, Any],
    deadline: float,
    interval_s: float,
    timeout_s: float,
) -> str:
    """
    Drive create → poll → result and render the outcome.

    :param base: API base URL.
    :param headers: Request headers.
    :param configured_agent_id: The spec's agent id, or ``None``.
    :param task: The research task.
    :param effort: Optional effort override.
    :param controls: Validated per-run controls.
    :param deadline: Absolute ``_monotonic()`` deadline.
    :param interval_s: Pause between polls.
    :param timeout_s: Total budget, used in the timeout message.
    :returns: The JSON envelope, or an error message. Never raises.
    """
    run, agent_id, error = _start_run(
        base, headers, configured_agent_id, task, effort, controls, deadline
    )
    if error is not None or run is None or agent_id is None:
        return error or "Nimble research error: run creation returned no data."
    run_id = str(run.get("id"))

    # From here on the agent id is the one the run reported, which is the only
    # one that exists when the run was created without a configured agent.
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
    return _build_envelope(run_id, agent_id, output)


class NimbleResearchTool(Tool):
    """
    Nimble Agent API v2 tool: asynchronous research runs with cited results.

    :param config: Spec-level config, e.g. ``{"api_key": "..."}``, optionally
        with ``agent_id`` to run on an agent you already own.
    """

    def __init__(self, config: dict[str, str] | None = None) -> None:
        """
        :param config: Spec-level config with ``api_key`` and optional
            ``agent_id``/``effort``/``timeout_seconds``/
            ``poll_interval_seconds``.
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

        :returns: A function tool schema with a required ``task`` parameter,
            the optional per-run controls, and supported ``effort`` choices.
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
                            "enum": sorted(_SELECTABLE_EFFORTS),
                            "description": (
                                "Optional effort override. Omit it to use the "
                                "agent/template default (documented product "
                                "default: high; template defaults vary)."
                            ),
                        },
                        "agent_name": {
                            "type": "string",
                            "description": "Optional SDK 1.2 name hint for the run agent.",
                        },
                        "skill": {
                            "type": "string",
                            "description": "Optional SDK 1.2 skill identifier.",
                        },
                        "use_case": {
                            "type": "string",
                            "enum": sorted(_VALID_USE_CASES),
                            "description": "SDK 1.2 run mode.",
                        },
                        "input_data": {
                            "type": ["object", "array"],
                            "description": (
                                "Records to enrich: a JSON object, or an array of "
                                "JSON objects, passed to the agent as input."
                            ),
                        },
                        "output_schema": {
                            "type": "object",
                            "description": (
                                "JSON Schema the agent's output must conform to. "
                                "Use for structured research or dataset building."
                            ),
                        },
                        "sources": {
                            "type": "object",
                            # Mirrors what _resolve_controls actually enforces, so a
                            # schema-conformant call is never rejected at runtime.
                            "additionalProperties": False,
                            "properties": {
                                "prioritize": {"type": "string", "minLength": 1},
                                "avoid": {"type": "string", "minLength": 1},
                                "allow": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "properties": {
                                            "title": {"type": "string", "minLength": 1},
                                            "domains": {
                                                "type": "array",
                                                "items": {"type": "string", "minLength": 1},
                                                "minItems": 1,
                                            },
                                            "order": {"type": "integer"},
                                        },
                                        "required": ["title", "domains"],
                                    },
                                },
                                "block": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "additionalProperties": False,
                                        "properties": {
                                            "title": {"type": "string", "minLength": 1},
                                            "domains": {
                                                "type": "array",
                                                "items": {"type": "string", "minLength": 1},
                                                "minItems": 1,
                                            },
                                            "order": {"type": "integer"},
                                        },
                                        "required": ["title", "domains"],
                                    },
                                },
                            },
                            "description": (
                                "Source guidance. prioritize/avoid are free-text "
                                "instructions; allow/block are arrays of named "
                                "domain groups with {title, domains, order?}."
                            ),
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

        :param arguments: JSON-encoded dict with a ``task`` key and optional
            ``effort``, ``input_data``, ``output_schema``, ``sources``,
            ``agent_name``, ``skill``, and ``use_case`` keys.
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
        controls, controls_error = _resolve_controls(parsed)
        if controls_error is not None or controls is None:
            return controls_error or "Error: invalid per-run controls."
        effort_arg = parsed.get("effort")
        return _run_agent_v2(
            str(task),
            str(effort_arg) if effort_arg is not None else None,
            controls,
            self._config,
        )
