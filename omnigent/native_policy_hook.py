"""Shared conversion between native-harness hooks and Omnigent policy events.

Both Claude Code and Codex expose a command-hook system whose
``PreToolUse`` / ``PostToolUse`` payloads use the same field names
(``hook_event_name``, ``tool_name``, ``tool_input``, ``tool_output``)
and whose ``UserPromptSubmit`` payload carries the user prompt under
``prompt``. This module owns the harness-neutral translation between
that hook shape and the server's proto-compatible ``EvaluationRequest``
/ ``EvaluationResponse`` schema served by
``POST /v1/sessions/{id}/policies/evaluate``, so the per-harness hook
entrypoints (:mod:`omnigent.claude_native_hook`,
:mod:`omnigent.codex_native_hook`) share one implementation.

The output contract differs by hook event: ``PreToolUse`` enforces via
``hookSpecificOutput.permissionDecision``, while ``UserPromptSubmit``
enforces via the top-level ``decision`` / ``reason`` fields (both
harnesses parse ``decision: "block"`` to drop the prompt before the
model sees it).
"""

from __future__ import annotations

import json
import os
import secrets
import shlex
import sys
import time
from collections.abc import Callable

import httpx

# How long to keep retrying transient 5xx / connect errors on the
# policy evaluate POST before failing closed. Keeps the pre-execution
# gate from blocking long on a sick server while still absorbing brief
# DB hiccups on a hosted deployment.
_EVALUATE_POLICY_RETRY_BUDGET_S = 30.0
_EVALUATE_POLICY_RETRY_INITIAL_BACKOFF_S = 1.0
_EVALUATE_POLICY_RETRY_MAX_BACKOFF_S = 10.0
# Fast connect budget so an unreachable server fails into the retry
# loop quickly rather than blocking on the day-long read timeout.
_EVALUATE_POLICY_CONNECT_TIMEOUT_S = 5.0

# Hook event names that gate tool execution and therefore carry policy
# meaning. ``PreToolUse`` fires before the tool runs (can block);
# ``PostToolUse`` fires after (observational — can only warn).
_PRE_TOOL_USE = "PreToolUse"
_POST_TOOL_USE = "PostToolUse"
# ``UserPromptSubmit`` fires when a new user prompt reaches the harness —
# for native sessions this is the request-phase gate (the server-level
# ``_evaluate_input_policy`` is bypassed for native message events, so
# this hook is the sole REQUEST gate and covers both web-UI-injected and
# direct-terminal prompts). It can block the prompt before the model runs.
_USER_PROMPT_SUBMIT = "UserPromptSubmit"

# Reason surfaced when a tool call is denied because its policy verdict
# could not be obtained (server unreachable / non-2xx / empty or malformed
# body). Mirrors the runner-side fail-closed default in
# ``omnigent.runner.app._evaluate_policy_via_omnigent``.
_EVAL_UNAVAILABLE_REASON = (
    "Omnigent policy evaluation unavailable (could not reach or authenticate to the "
    "Omnigent server); failing closed for this tool call."
)
_EVAL_UNAVAILABLE_REQUEST_REASON = (
    "Omnigent policy evaluation unavailable (could not reach or authenticate to the "
    "Omnigent server); failing closed for this request."
)


# Env var carrying the one-shot auth + workspace-routing headers from the
# executor (which writes the hook wrapper) to the hook subprocess. The hook
# is import-free of the runner, so the headers are passed in rather than
# resolved in-process — the same reason ``ap_auth_headers`` is baked for the
# claude/codex/kimi hooks.
_AUTH_HEADERS_ENV = "_OMNIGENT_AUTH_HEADERS"


def policy_hook_request_headers() -> dict[str, str]:
    """Build the headers for a policy-hook subprocess's POST to the server.

    Always carries ``Content-Type``, and merges the one-shot auth +
    workspace-routing headers the executor baked into :data:`_AUTH_HEADERS_ENV`
    at launch (see :func:`policy_hook_wrapper_script`). Without them the POST
    is unauthenticated and unrouted — it 401s on an authenticated server and
    misroutes to the account on a unified-account workspace. Missing or
    malformed env → just ``Content-Type`` (a local unauthenticated server
    needs no auth).

    :returns: Request headers for ``post_evaluate_with_retry``.
    """
    headers = {"Content-Type": "application/json"}
    raw = os.environ.get(_AUTH_HEADERS_ENV, "")
    if raw:
        try:
            extra = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            extra = None
        if isinstance(extra, dict):
            headers.update({str(k): str(v) for k, v in extra.items()})
    return headers


def policy_hook_wrapper_script(server_url: str, session_id: str, hook_script_path: str) -> str:
    """Build the ``/bin/sh`` wrapper a native policy hook is launched as.

    Resolves a one-shot Omnigent-server token and bakes the auth +
    workspace-routing headers (via
    :func:`omnigent.cli_auth.databricks_request_headers`) into
    :data:`_AUTH_HEADERS_ENV`, so the hook's POST authenticates and routes to
    the workspace. The token is a secret, so callers MUST write the returned
    wrapper ``0o700`` (owner-only) — it is never world-readable.

    :param server_url: Omnigent server base URL the hook posts to.
    :param session_id: Session / conversation id for policy evaluation.
    :param hook_script_path: Absolute path to the hook's Python entrypoint.
    :returns: Shell-script text for the wrapper (write it ``0o700``).
    """
    from omnigent.cli_auth import databricks_request_headers
    from omnigent.runner._entry import _make_auth_token_factory

    factory = _make_auth_token_factory(server_url=server_url)
    token = factory() if factory is not None else None
    auth_headers = databricks_request_headers(server_url, bearer_token=token)
    return (
        "#!/bin/sh\n"
        f"export _OMNIGENT_SERVER_URL={shlex.quote(server_url)}\n"
        f"export _OMNIGENT_SESSION_ID={shlex.quote(session_id)}\n"
        f"export {_AUTH_HEADERS_ENV}={shlex.quote(json.dumps(auth_headers))}\n"
        f"exec {shlex.quote(sys.executable)} {shlex.quote(hook_script_path)}\n"
    )


class PolicyHookReauth:
    """Callable that re-mints the Omnigent bearer for a policy hook subprocess.

    The baked one-shot token dies with the ~1h Databricks OAuth lifetime; on a
    lapsed-token signal (401 or Apps ``302→/oidc/``) ``post_evaluate_with_retry``
    calls this once to mint a fresh bearer through the same factory the
    refresh-capable runtime auth uses, keeping the other headers (e.g.
    ``X-Databricks-Org-Id``) so routing survives.

    The ``failure_reason`` attribute is set to a short diagnostic string when
    the re-mint fails so callers can surface it in the fail-closed message shown
    to the user — stderr from hook subprocesses is discarded by the harness, so
    this is the only channel that reaches the UI.
    """

    failure_reason: str | None

    def __init__(self, server_url: str, headers: dict[str, str]) -> None:
        self._server_url = server_url
        self._headers = headers
        self.failure_reason = None

    def __call__(self) -> dict[str, str] | None:
        # Lazy import: paid only on the rare re-auth path, off the hot path.
        try:
            from omnigent.runner._entry import _make_auth_token_factory
        except Exception as exc:  # noqa: BLE001 — best-effort; fail closed if unavailable
            self.failure_reason = f"auth factory unavailable: {exc}"
            return None
        factory = _make_auth_token_factory(self._server_url)
        if factory is None:
            self.failure_reason = (
                "no credential resolved "
                f"(no stored token and no Databricks SDK auth for {self._server_url!r})"
            )
            return None
        try:
            token = factory()
        except Exception as exc:  # noqa: BLE001 — transient mint failure; fail closed
            self.failure_reason = f"token mint failed: {exc}"
            return None
        if not token:
            self.failure_reason = "auth factory returned empty token"
            return None
        self.failure_reason = None
        return {**self._headers, "Authorization": f"Bearer {token}"}


def policy_hook_reauth(server_url: str, headers: dict[str, str]) -> PolicyHookReauth:
    """Build a :class:`PolicyHookReauth` callable for *server_url*.

    :param server_url: Omnigent server base URL the hook POSTs to.
    :param headers: Current (lapsed) headers; the fresh bearer is merged over
        a copy so routing headers survive.
    :returns: A :class:`PolicyHookReauth` instance. Call it to attempt a
        re-mint; check ``.failure_reason`` afterwards when it returns ``None``.
    """
    return PolicyHookReauth(server_url, headers)


def _is_login_redirect_or_unauthorized(response: httpx.Response) -> bool:
    """
    Return ``True`` when a response means "the bearer is no good — re-auth".

    Mirrors :func:`omnigent.runner._entry._is_login_redirect_or_unauthorized`,
    duplicated here so the dependency-light hook need not import the runner
    package. The Databricks Apps front door bounces an *expired* bearer with a
    ``302`` to the OAuth login flow (``/oidc/`` or ``/.auth/``) — **not** a
    ``401`` — so a hook that only treats ``401`` as auth failure silently fails
    closed once the one-shot ``ap_auth_headers`` token (snapshotted at launch by
    ``build_hook_settings``) lapses with the ~1h Databricks OAuth lifetime.
    Treat the 401, 403 "Invalid Token", and the OAuth-login redirect as
    re-auth signals.

    Unrelated 3xx (an application-level redirect to another resource) return
    ``False`` so the caller does not waste a token round-trip on every redirect.

    Note: Databricks Apps returns 403 (not 401) with body "Invalid Token"
    when a bearer has expired, in addition to the 302→``/oidc/`` bounce. A
    caller that only watches for 401 and the redirect silently fails closed
    on sessions older than ~1h.

    :param response: The hook's POST response to classify.
    :returns: ``True`` when the caller should re-mint a token and retry.
    """
    if response.status_code in (401, 403):
        return True
    if not response.is_redirect:
        return False
    location = response.headers.get("location", "")
    return "/oidc/" in location or "/.auth/" in location


def hook_payload_to_evaluation_request(
    hook_event: str,
    payload: dict[str, object],
) -> dict[str, object] | None:
    """
    Convert a native-harness tool-hook payload into a proto ``EvaluationRequest``.

    Maps ``PreToolUse`` to a ``PHASE_TOOL_CALL`` event, ``PostToolUse``
    to a ``PHASE_TOOL_RESULT`` event, and ``UserPromptSubmit`` to a
    ``PHASE_REQUEST`` event (the prompt text from the payload's
    ``prompt`` field becomes the request content). Omnigent MCP tools
    (``mcp__omnigent__*``) are skipped because they are already
    policy-checked by the relay path (``ProxyMcpManager`` → Omnigent
    ``/mcp`` endpoint → ``_evaluate_tool_call_policy``); evaluating
    them here would double-count. Connector-native MCP tools
    (for example ``mcp__github__*``) still need this pre-call gate.

    :param hook_event: Hook event name from the payload's
        ``hook_event_name`` field, e.g. ``"PreToolUse"``,
        ``"PostToolUse"``, or ``"UserPromptSubmit"``.
    :param payload: Raw hook JSON from the harness, e.g.
        ``{"hook_event_name": "PreToolUse", "tool_name": "Bash",
        "tool_input": {"command": "rm -rf /"}}``.
    :returns: An ``EvaluationRequest`` dict suitable for POSTing to
        ``/policies/evaluate``, or ``None`` when the event is not
        policy-relevant (unknown event or an ``mcp__omnigent__*`` tool).
    """
    if hook_event == _USER_PROMPT_SUBMIT:
        # Request-phase gate for native sessions. The server reads REQUEST
        # content from ``data.text`` (see ``_build_evaluation_context``).
        prompt = payload.get("prompt", "")
        return {
            "event": {
                "type": "PHASE_REQUEST",
                "target": "",
                "data": {
                    "text": prompt if isinstance(prompt, str) else json.dumps(prompt),
                },
                "context": {},
            },
        }
    tool_name = payload.get("tool_name", "")
    # Omnigent MCP tools are already policy-checked by the relay path
    # (ProxyMcpManager → Omnigent /mcp endpoint → _evaluate_tool_call_policy).
    # Skip only those here to avoid double evaluation; connector-native MCP
    # tools such as mcp__github__* must still go through this hook.
    if isinstance(tool_name, str) and tool_name.startswith("mcp__omnigent__"):
        return None
    tool_input = payload.get("tool_input") or {}
    if hook_event == _PRE_TOOL_USE:
        return {
            "event": {
                "type": "PHASE_TOOL_CALL",
                "target": "",
                "data": {
                    "name": tool_name,
                    "arguments": tool_input,
                },
                "context": {},
            },
        }
    if hook_event == _POST_TOOL_USE:
        tool_output = payload.get("tool_output", "")
        return {
            "event": {
                "type": "PHASE_TOOL_RESULT",
                "target": "",
                "data": {
                    "result": tool_output,
                },
                "context": {},
                "request_data": {
                    "name": tool_name,
                    "arguments": tool_input,
                },
            },
        }
    return None


def evaluation_response_to_hook_output(
    hook_event: str,
    eval_response: dict[str, object],
) -> dict[str, object] | None:
    """
    Convert an ``EvaluationResponse`` into native-harness hook output JSON.

    For ``PreToolUse`` the policy layer only *enforces* — it emits a
    ``hookSpecificOutput.permissionDecision`` solely for verdicts that
    constrain the tool: ``POLICY_ACTION_DENY`` → ``"deny"`` (with
    ``permissionDecisionReason``). ``POLICY_ACTION_ASK`` is resolved
    server-side now (URL-based elicitation: ``POST /policies/evaluate``
    holds the gate and returns a hard ALLOW/DENY), so the hook should
    never see ASK; if it does, it fails closed with ``"deny"`` rather
    than the old ``"defer"`` — ``defer`` handed control back to the
    harness's ``permission_mode``, which ``acceptEdits`` /
    ``bypassPermissions`` would auto-approve, bypassing the human.
    ``POLICY_ACTION_ALLOW`` — which is the engine's default verdict when
    no policy matches a tool call, not just an explicit author allow —
    returns ``None`` ("no opinion") so the harness's *own* permission
    system still runs. Emitting ``"allow"`` here would auto-approve the
    tool and suppress the harness's native permission prompt (and, for
    Claude Code, the ``PermissionRequest`` hook that routes that prompt
    to the web UI), collapsing two independent gates — the deployment's
    policy gate and the user's own consent gate — into one. The policy
    layer may block (DENY) or demand approval (ASK); it must not silence
    the user's consent. For ``PostToolUse`` a ``DENY`` is surfaced as
    ``additionalContext`` because the tool result is already committed
    — PostToolUse hooks cannot block.

    For ``UserPromptSubmit`` the output uses the top-level ``decision`` /
    ``reason`` contract (not ``permissionDecision``): ``DENY`` → ``{"decision":
    "block", "reason": ...}``, which drops the prompt before the model sees
    it. ASK is resolved server-side (``_hold_native_ask_gate`` collapses it
    to a hard ALLOW/DENY before the response reaches the hook), so the hook
    should never see ASK; if it somehow does, it fails closed by blocking.
    ALLOW (and the engine's no-match default) returns ``None`` so the prompt
    proceeds. Unlike ``PreToolUse``, there is no separate user-consent gate
    on a prompt, so ALLOW need not preserve one.

    Both Claude Code and Codex consume these exact output shapes, so the
    ``hookEventName`` echoed back is the harness-supplied ``hook_event``.

    :param hook_event: Hook event name, e.g. ``"PreToolUse"``,
        ``"PostToolUse"``, or ``"UserPromptSubmit"``.
    :param eval_response: Parsed ``EvaluationResponse`` from AP, e.g.
        ``{"result": "POLICY_ACTION_DENY", "reason": "blocked by policy"}``.
    :returns: Hook output dict for the harness to read on stdout, or
        ``None`` when there is no verdict to express (allow with no
        rewrite on PostToolUse, or an unknown action).
    """
    action = eval_response.get("result", "POLICY_ACTION_UNSPECIFIED")
    reason = eval_response.get("reason")

    if hook_event == _USER_PROMPT_SUBMIT:
        # DENY blocks the prompt; a stray ASK fails closed (also block) since
        # ASK is meant to be resolved server-side before reaching the hook.
        # ALLOW / no-match → None so the prompt proceeds. A non-empty reason
        # is required for the block to take effect (both harnesses drop a
        # block with an empty reason), so default one in.
        if action in ("POLICY_ACTION_DENY", "POLICY_ACTION_ASK"):
            return {
                "decision": "block",
                "reason": reason or "Denied by policy",
            }
        return None

    if hook_event == _PRE_TOOL_USE:
        # ALLOW (the engine default when no policy matches) is omitted → None,
        # so the harness's own permission prompt still fires; see docstring.
        decision_map = {
            "POLICY_ACTION_DENY": "deny",
            # ASK is resolved server-side now (URL-based elicitation:
            # POST /policies/evaluate holds the gate and returns a hard
            # ALLOW/DENY), so the hook should never see ASK here. If it
            # somehow does, fail closed with ``deny`` rather than the old
            # ``defer`` — ``defer`` returns control to the harness's
            # permission_mode, which acceptEdits / bypassPermissions would
            # auto-approve, re-opening the very bypass this closes.
            "POLICY_ACTION_ASK": "deny",
        }
        decision = decision_map.get(str(action))
        if decision is None:
            return None
        output: dict[str, object] = {
            "hookEventName": _PRE_TOOL_USE,
            "permissionDecision": decision,
        }
        if decision == "deny" and reason:
            output["permissionDecisionReason"] = reason
        return {"hookSpecificOutput": output}

    if hook_event == _POST_TOOL_USE:
        if action == "POLICY_ACTION_DENY" and reason:
            return {
                "hookSpecificOutput": {
                    "hookEventName": _POST_TOOL_USE,
                    "additionalContext": f"[Policy violation] {reason}",
                },
            }
        return None

    return None


def fail_closed_hook_output(
    hook_event: str, detail: str | None = None
) -> dict[str, object] | None:
    """
    Build the fail-closed hook output for an unobtainable policy verdict.

    Called by the per-harness hooks when the ``/policies/evaluate``
    round-trip cannot produce a usable verdict for an *already-governed*
    session — the server is unreachable, returns a non-2xx status, or
    returns an empty / malformed body. Without this the hooks emitted "no
    opinion" on those paths, silently letting the gated tool run: for
    native harnesses this hook is the sole enforcement point (it gates
    Bash / Write / Edit / the native Skill tool / connector-native
    ``mcp__*`` tools), so a transient outage disabled all DENY/ASK
    enforcement.

    The default is phase-aware, matching
    :data:`omnigent.policies.types.FAIL_CLOSED_PHASES` (the runner-side
    precedent from PR #163) — but expressed in hook-event terms so the
    lightweight hook subprocess need not import the policy package:

    - ``PreToolUse`` (``PHASE_TOOL_CALL``) fails CLOSED → ``deny``. This is
      the authoritative pre-execution gate; an unevaluable policy must not
      let the call through.
    - ``UserPromptSubmit`` (``PHASE_REQUEST``) fails CLOSED →
      ``{"decision": "block", ...}``. This is the sole pre-turn enforcement
      point for native sessions; a server hiccup must not let an over-budget
      or otherwise-blocked request proceed.
    - ``PostToolUse`` (``PHASE_TOOL_RESULT``) fails OPEN → ``None``. By the
      result phase the tool has already executed, so denying would only block
      an already-incurred side effect.

    :param hook_event: Hook event name, e.g. ``"PreToolUse"``.
    :param detail: Optional short diagnostic string appended to the reason
        shown in the UI, e.g. a reauth failure message from
        :attr:`PolicyHookReauth.failure_reason`. Omit when no detail is
        available.
    :returns: A ``permissionDecision: "deny"`` hook output for
        ``PreToolUse``; a ``decision: "block"`` output for
        ``UserPromptSubmit``; ``None`` for every other event (fail open).
    """
    tool_reason = (
        f"{_EVAL_UNAVAILABLE_REASON} Detail: {detail}" if detail else _EVAL_UNAVAILABLE_REASON
    )
    request_reason = (
        f"{_EVAL_UNAVAILABLE_REQUEST_REASON} Detail: {detail}"
        if detail
        else _EVAL_UNAVAILABLE_REQUEST_REASON
    )
    if hook_event == _PRE_TOOL_USE:
        return {
            "hookSpecificOutput": {
                "hookEventName": _PRE_TOOL_USE,
                "permissionDecision": "deny",
                "permissionDecisionReason": tool_reason,
            },
        }
    if hook_event == _USER_PROMPT_SUBMIT:
        return {
            "decision": "block",
            "reason": request_reason,
        }
    return None


def post_evaluate_with_retry(
    url: str,
    headers: dict[str, str],
    eval_request: dict[str, object],
    read_timeout: float,
    hook_label: str,
    reauth: Callable[[], dict[str, str] | None] | None = None,
) -> tuple[httpx.Response, None] | tuple[None, str]:
    """
    POST to the Omnigent policy evaluate endpoint, retrying on transient errors.

    Retries on 5xx HTTP responses and connection-level errors
    (:class:`httpx.ConnectError`, :class:`httpx.ConnectTimeout`) within
    :data:`_EVALUATE_POLICY_RETRY_BUDGET_S`. Returns the successful response,
    or ``None`` if the budget is exhausted or a non-retryable error occurs.

    A stable ``_omnigent_elicitation_id`` is minted once and stamped on
    every attempt. When the server parks an ASK gate and the connection
    drops (5xx or :class:`httpx.ConnectError`), the retry re-POSTs the
    same id so the server re-attaches to the existing elicitation rather
    than minting a new one — mirroring the ``_post_hook_with_reattach``
    idiom used by the ``PermissionRequest`` hook. This prevents a
    second approval card from appearing when the first was already
    published before the error.

    4xx responses are final — a bad request won't succeed on retry. Other
    mid-stream errors (e.g. :class:`httpx.ReadTimeout`) are also not retried:
    a read timeout fires *after* the server received the request and may
    mean the long-polling ASK gate was severed mid-wait; retrying with the
    same id will re-park the existing elicitation (no duplicate card), but
    the caller's fail-closed path is equivalent and simpler. The caller is
    responsible for fail-closed handling on ``None``.

    :param url: Absolute URL of the evaluate endpoint.
    :param headers: Auth headers for the Omnigent server.
    :param eval_request: ``EvaluationRequest`` JSON body to POST.
    :param read_timeout: Per-attempt read timeout in seconds. Should be
        large (e.g. one day) to accommodate long-polling ASK gates.
    :param hook_label: Diagnostic label used in stderr messages,
        e.g. ``"evaluate-policy hook"`` or ``"codex evaluate-policy hook"``.
    :param reauth: Optional callable that re-mints fresh auth headers when
        the server bounces the request to its OAuth login flow (the Apps
        front door 302→``/oidc/``) or returns ``401`` — i.e. the one-shot
        ``ap_auth_headers`` token lapsed. Called at most once; returning new
        headers triggers an immediate retry with them, mirroring the runner's
        refresh-capable :class:`~omnigent.runner._entry._RunnerDatabricksAuth`.
        ``None`` (the default) keeps the legacy behavior for callers that have
        no token source. Returning ``None`` from it falls through to the
        normal failure handling (the caller fails closed).
    :returns: ``(response, error)`` — on success, ``(response, None)``; on
        failure, ``(None, short_error_string)`` describing the last error so
        callers can surface it in the deny/block reason shown to the user.
    """
    # Mint one stable id for the whole retry sequence. Each retry re-sends
    # it so the server can re-park the SAME elicitation rather than opening
    # a second approval card. The ``elicit_evaluate_`` namespace is validated
    # server-side by ``_EVALUATE_HOOK_ELICITATION_ID_RE``.
    elicitation_id = f"elicit_evaluate_{secrets.token_hex(16)}"
    request_body = {**eval_request, "_omnigent_elicitation_id": elicitation_id}
    deadline = time.monotonic() + _EVALUATE_POLICY_RETRY_BUDGET_S
    backoff_s = _EVALUATE_POLICY_RETRY_INITIAL_BACKOFF_S
    timeout = httpx.Timeout(read_timeout, connect=_EVALUATE_POLICY_CONNECT_TIMEOUT_S)
    reauthed = False
    last_error: str = "unknown error"
    while True:
        try:
            with httpx.Client(headers=headers, timeout=timeout) as client:
                resp = client.post(url, json=request_body)
                if (
                    reauth is not None
                    and not reauthed
                    and _is_login_redirect_or_unauthorized(resp)
                ):
                    # The one-shot ``ap_auth_headers`` token lapsed (~1h
                    # Databricks OAuth lifetime): the Apps front door bounces
                    # an expired bearer with a 302→/oidc/ (or a 401). Re-mint
                    # and retry once with the fresh token instead of failing
                    # closed — exactly as ``_RunnerDatabricksAuth`` does for the
                    # runner's own callbacks. Without this, every tool call on a
                    # session older than the token lifetime fails CLOSED while
                    # chat (refresh-capable) keeps working.
                    refreshed = reauth()
                    if refreshed:
                        headers = refreshed
                        reauthed = True
                        print(
                            f"omnigent {hook_label}: Omnigent auth expired "
                            "(login redirect/401); re-minted token and retrying",
                            file=sys.stderr,
                        )
                        continue
                resp.raise_for_status()
                return resp, None
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            body_preview = exc.response.text[:200] if exc.response.content else ""
            last_error = f"server returned {status}" + (
                f": {body_preview}" if body_preview else ""
            )
            if status < 500:
                print(
                    f"omnigent {hook_label}: Omnigent returned {status}"
                    + (f": {body_preview}" if body_preview else ""),
                    file=sys.stderr,
                )
                return None, last_error
            print(
                f"omnigent {hook_label}: Omnigent returned {status}; retrying",
                file=sys.stderr,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            last_error = f"connection error: {exc}"
            print(
                f"omnigent {hook_label}: Omnigent request failed; retrying: {exc}",
                file=sys.stderr,
            )
        except httpx.HTTPError as exc:
            # Other HTTP errors (ReadTimeout while a long ASK poll is in flight,
            # etc.) are not retried — retrying a severed ASK would open a new
            # elicitation and prompt the human twice.
            last_error = f"request error: {exc}"
            print(
                f"omnigent {hook_label}: Omnigent request failed: {exc}",
                file=sys.stderr,
            )
            return None, last_error
        if time.monotonic() + backoff_s >= deadline:
            print(
                f"omnigent {hook_label}: retry budget exhausted",
                file=sys.stderr,
            )
            return None, f"retry budget exhausted (last error: {last_error})"
        # Two-step backoff; not worth a retry library in this dependency-light hook.
        time.sleep(backoff_s)
        backoff_s = min(backoff_s * 2, _EVALUATE_POLICY_RETRY_MAX_BACKOFF_S)
