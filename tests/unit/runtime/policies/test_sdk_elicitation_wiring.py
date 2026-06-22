"""
Tests for the client-side elicitation wiring.

Covers:

- :func:`omnigent_client._sse._parse_event` —
  ``response.elicitation_request`` events parse to
  :class:`ElicitationRequest` with the MCP-shape ``params``
  block surfaced as flat fields on the dataclass.
- :func:`omnigent_client._sse._parse_output_item` — regular
  ``function_call`` items still parse to :class:`ToolCall`
  (guards against accidental re-introduction of a
  reserved-name carve-out).
- :func:`omnigent_client._responses._handle_elicitation_request`
  — calls the registered hook, POSTs the verdict to the
  elicitation's dedicated resolve URL, and fail-closes when no hook
  is registered.
- REPL ``_make_elicitation_prompt`` — renders the elicitation
  and returns ``True`` / ``False`` based on the user's y/n
  answer.

Real HTTP is stubbed via a minimal ``_FakeHttpClient`` — these
tests exercise the SDK's branching logic, not the server.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

# The editable-install of ``omnigent_client`` points at the
# sibling worktree. Load this worktree's copy under a distinct
# module name so we're actually testing the code we just
# edited. Same pattern the e2e suite uses via PYTHONPATH.
_SDK_ROOT = (
    Path(__file__).resolve().parents[3].parent / "sdks" / "python-client" / "omnigent_client"
)


def _load_sdk_module(name: str) -> Any:
    """
    Load ``omnigent_client.<name>`` from this worktree
    regardless of which ``omnigent_client`` is resolved
    globally. Registering under ``_apc_under_test.<name>``
    so parent-package resolution works for helpers that
    reference sibling submodules.

    :param name: The module's basename without the ``.py``,
        e.g. ``"_events"``.
    :returns: The freshly-loaded module object.
    """
    parent_name = "_apc_under_test"
    if parent_name not in sys.modules:
        parent_spec = importlib.util.spec_from_file_location(
            parent_name,
            _SDK_ROOT / "__init__.py",
            submodule_search_locations=[str(_SDK_ROOT)],
        )
        assert parent_spec is not None and parent_spec.loader is not None
        parent_mod = importlib.util.module_from_spec(parent_spec)
        sys.modules[parent_name] = parent_mod
        parent_spec.loader.exec_module(parent_mod)
    full = f"{parent_name}.{name}"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(full, _SDK_ROOT / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    return mod


_events = _load_sdk_module("_events")
_sse = _load_sdk_module("_sse")
_tool_handler = _load_sdk_module("_tool_handler")
_responses = _load_sdk_module("_responses")

ElicitationRequest = _events.ElicitationRequest
ToolCall = _events.ToolCall
MCP_ELICITATION_METHOD = _events.MCP_ELICITATION_METHOD
ElicitationRequestCtx = _tool_handler.ElicitationRequestCtx
StreamHooks = _tool_handler.StreamHooks


class _FakeResponse:
    """Minimal httpx.Response stand-in for POST result checking."""

    def __init__(self, status_code: int = 202) -> None:
        """Initialize with the simulated status code.

        :param status_code: Status the fake response reports.
            Default 202 mirrors the session event endpoint's
            success contract.
        """
        self.status_code = status_code
        self.text = ""


class _FakeHttpClient:
    """
    Minimal async HTTP stub — records POSTs without opening
    a socket.

    Only ``post`` is exercised by the elicitation flow; PATCH
    is not used here, and any other method would raise
    ``AttributeError`` on access — fail-loud rather than
    silently hitting the real server.
    """

    def __init__(self) -> None:
        """Initialize with no recorded calls."""
        self.post_calls: list[dict[str, Any]] = []

    async def post(
        self,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> _FakeResponse:
        """Record the POST and return a fake 202.

        :param url: The full URL the SDK posted to.
        :param json: The JSON body (kwarg-only on httpx; keyword
            here too so the test signature mirrors httpx exactly).
        :param timeout: The per-call timeout the SDK sets.
        :returns: A 202 :class:`_FakeResponse`.
        """
        self.post_calls.append({"url": url, "json": json, "timeout": timeout})
        return _FakeResponse(status_code=202)


# ── SSE parse: response.elicitation_request ────────────────


def test_sse_parses_elicitation_request_event() -> None:
    """
    A ``response.elicitation_request`` event with an MCP-shape
    ``params`` block parses to :class:`ElicitationRequest` with
    every field surfaced as a flat dataclass attribute. This is
    what the streaming path keys off of to dispatch the hook.
    """
    raw = {
        "type": "response.elicitation_request",
        "elicitation_id": "elicit_abc",
        "method": "elicitation/create",
        "params": {
            "mode": "form",
            "message": "approve web search?",
            "requestedSchema": {},
            "phase": "tool_call",
            "policy_name": "ask_search",
            "content_preview": "q=classified",
        },
    }
    event = _sse._parse_event("response.elicitation_request", raw)
    assert isinstance(event, ElicitationRequest)
    assert event.elicitation_id == "elicit_abc"
    assert event.message == "approve web search?"
    assert event.requested_schema == {}
    assert event.mode == "form"
    assert event.phase == "tool_call"
    assert event.policy_name == "ask_search"
    assert event.content_preview == "q=classified"


def test_sse_elicitation_request_tolerates_missing_extras() -> None:
    """
    The MCP-spec required fields are ``mode`` / ``message`` /
    ``requestedSchema``; producer extras (``phase``,
    ``policy_name``, ``content_preview``) may be absent. The
    parser must coerce missing extras to empty string rather
    than raising — defensive shape keeps a stray protocol skew
    from crashing the whole stream.
    """
    raw = {
        "type": "response.elicitation_request",
        "elicitation_id": "elicit_bare",
        "method": "elicitation/create",
        "params": {
            "mode": "form",
            "message": "are you sure?",
            "requestedSchema": {},
        },
    }
    event = _sse._parse_event("response.elicitation_request", raw)
    assert isinstance(event, ElicitationRequest)
    assert event.message == "are you sure?"
    # Extras default to empty string when absent.
    assert event.phase == ""
    assert event.policy_name == ""
    assert event.content_preview == ""


def test_sse_elicitation_request_skips_when_id_missing() -> None:
    """
    Without an ``elicitation_id`` there's no correlation key
    for the reply POST — the parser returns None and logs
    rather than fabricating an id. Forward-compat: an upstream
    that emits the event with a different correlation surface
    won't crash the stream; it just gets dropped.
    """
    raw = {
        "type": "response.elicitation_request",
        # No elicitation_id key.
        "method": "elicitation/create",
        "params": {"mode": "form", "message": "?", "requestedSchema": {}},
    }
    event = _sse._parse_event("response.elicitation_request", raw)
    assert event is None


def test_sse_elicitation_request_skips_when_params_not_dict() -> None:
    """
    ``params`` must be a dict per MCP spec. Anything else is
    malformed; parser drops it with a log line rather than
    accessing fields on a non-dict and crashing.
    """
    raw = {
        "type": "response.elicitation_request",
        "elicitation_id": "elicit_x",
        "method": "elicitation/create",
        "params": "this is not a dict",
    }
    event = _sse._parse_event("response.elicitation_request", raw)
    assert event is None


def test_sse_parses_regular_function_call_as_toolcall() -> None:
    """
    A ``function_call`` with ANY name still parses to
    :class:`ToolCall`. Regression guard for the deleted
    reserved-name carve-out: pre-refactor, a function_call
    named ``request_approval`` was diverted to a separate
    event; post-refactor that's gone. If a future regression
    re-introduces a carve-out, this test catches it because
    the ``request_approval`` name returns a regular ToolCall.
    """
    raw = {
        "item": {
            "type": "function_call",
            "call_id": "call_real",
            "name": "request_approval",
            "status": "action_required",
            "arguments": json.dumps({"path": "x.py"}),
        },
    }
    event = _sse._parse_output_item(raw)
    assert isinstance(event, ToolCall)
    # The name is now just data — no special semantics.
    assert event.name == "request_approval"


# ── _handle_elicitation_request: hook + POST wiring ────────


@pytest.mark.asyncio
async def test_hook_accept_posts_accept_action() -> None:
    """
    A hook that returns ``True`` → SDK POSTs ``{"action": "accept"}``
    to the elicitation's dedicated resolve URL
    ``/v1/sessions/{session_id}/elicitations/{elicitation_id}/resolve``
    (URL-based elicitation).
    """
    http = _FakeHttpClient()
    seen: list[ElicitationRequestCtx] = []

    async def _hook(ctx: ElicitationRequestCtx) -> bool:
        seen.append(ctx)
        return True

    hooks = StreamHooks(on_elicitation_request=_hook)
    event = ElicitationRequest(
        elicitation_id="elicit_accept",
        message="tainted",
        requested_schema={},
        mode="form",
        phase="tool_call",
        policy_name="deny_tainted",
        content_preview="args",
    )
    await _responses._handle_elicitation_request(
        http,  # type: ignore[arg-type]
        "http://localhost:8000",
        hooks,
        event,
        response_id="resp_1",
        session_id="conv_1",
    )

    # The hook fired exactly once with the right context.
    # 1 = guards against the future being awaited twice or
    # the dispatcher invoking the hook in a loop.
    assert len(seen) == 1, (
        f"Expected hook to fire exactly once; got {len(seen)} "
        f"calls. >1 would mean the dispatcher is double-firing."
    )
    assert seen[0].elicitation_id == "elicit_accept"
    assert seen[0].policy_name == "deny_tainted"

    # Exactly one POST.
    assert len(http.post_calls) == 1
    call = http.post_calls[0]
    assert call["url"] == (
        "http://localhost:8000/v1/sessions/conv_1/elicitations/elicit_accept/resolve"
    )
    # Verdict body is the bare MCP ElicitResult — the elicitation id
    # rides in the URL path, not the body.
    assert call["json"] == {"action": "accept"}


@pytest.mark.asyncio
async def test_hook_decline_posts_decline_action() -> None:
    """
    Hook returns ``False`` → SDK POSTs ``{"action": "decline"}`` to
    the elicitation's resolve URL. Server treats this identically to
    timeout / cancel — the parked workflow wakes and the enforcement
    site short-circuits with a DENY sentinel.
    """
    http = _FakeHttpClient()

    async def _hook(ctx: ElicitationRequestCtx) -> bool:
        return False

    hooks = StreamHooks(on_elicitation_request=_hook)
    event = ElicitationRequest(
        elicitation_id="elicit_decline",
        message="",
        requested_schema={},
        mode="form",
        phase="response",
        policy_name="p",
        content_preview="",
    )
    await _responses._handle_elicitation_request(
        http,  # type: ignore[arg-type]
        "http://localhost:8000",
        hooks,
        event,
        response_id="resp_2",
        session_id="conv_2",
    )
    assert http.post_calls[0]["url"] == (
        "http://localhost:8000/v1/sessions/conv_2/elicitations/elicit_decline/resolve"
    )
    assert http.post_calls[0]["json"] == {"action": "decline"}


@pytest.mark.asyncio
async def test_no_hook_fails_closed() -> None:
    """
    No hook registered → SDK resolves the elicitation with
    ``{"action": "decline"}``. POLICIES.md §7.2: an unhandled
    elicitation must DENY. Silently swallowing it would stall the
    parked workflow until ``ask_timeout`` expired — declining
    fail-closed is the right default.
    """
    http = _FakeHttpClient()
    hooks = StreamHooks()  # no on_elicitation_request
    event = ElicitationRequest(
        elicitation_id="elicit_nohook",
        message="",
        requested_schema={},
        mode="form",
        phase="request",
        policy_name="p",
        content_preview="",
    )
    await _responses._handle_elicitation_request(
        http,  # type: ignore[arg-type]
        "http://localhost:8000",
        hooks,
        event,
        response_id="resp_3",
        session_id="conv_3",
    )
    assert http.post_calls[0]["url"] == (
        "http://localhost:8000/v1/sessions/conv_3/elicitations/elicit_nohook/resolve"
    )
    assert http.post_calls[0]["json"] == {"action": "decline"}


@pytest.mark.asyncio
async def test_hook_exception_fails_closed() -> None:
    """
    Hook raises → SDK catches, logs, resolves the elicitation with
    ``{"action": "decline"}``. A buggy elicitation handler must not
    crash the stream or stall the workflow; fail-closed keeps the
    invariant.
    """
    http = _FakeHttpClient()

    async def _hook(ctx: ElicitationRequestCtx) -> bool:
        raise RuntimeError("bug in handler")

    hooks = StreamHooks(on_elicitation_request=_hook)
    event = ElicitationRequest(
        elicitation_id="elicit_bug",
        message="",
        requested_schema={},
        mode="form",
        phase="tool_call",
        policy_name="p",
        content_preview="",
    )
    await _responses._handle_elicitation_request(
        http,  # type: ignore[arg-type]
        "http://localhost:8000",
        hooks,
        event,
        response_id="resp_4",
        session_id="conv_4",
    )
    assert http.post_calls[0]["url"] == (
        "http://localhost:8000/v1/sessions/conv_4/elicitations/elicit_bug/resolve"
    )
    assert http.post_calls[0]["json"] == {"action": "decline"}


@pytest.mark.asyncio
async def test_hook_accepts_sync_callable() -> None:
    """
    Hooks can be sync or async. A sync ``def hook(ctx) -> bool``
    must work too — the client awaits only when the return is
    awaitable.
    """
    http = _FakeHttpClient()

    def _hook(ctx: ElicitationRequestCtx) -> bool:
        return True

    hooks = StreamHooks(on_elicitation_request=_hook)
    event = ElicitationRequest(
        elicitation_id="elicit_sync",
        message="",
        requested_schema={},
        mode="form",
        phase="response",
        policy_name="p",
        content_preview="",
    )
    await _responses._handle_elicitation_request(
        http,  # type: ignore[arg-type]
        "http://localhost:8000",
        hooks,
        event,
        response_id="resp_5",
        session_id="conv_5",
    )
    assert http.post_calls[0]["url"] == (
        "http://localhost:8000/v1/sessions/conv_5/elicitations/elicit_sync/resolve"
    )
    assert http.post_calls[0]["json"] == {"action": "accept"}


# ── REPL elicitation prompt wiring ────────────────────────
#
# The REPL's flow doesn't call ``input()`` — that path fought
# prompt_toolkit's ``patch_stdout`` (characters vanishing
# mid-type, auto-delete jank). Instead, the hook creates an
# :class:`asyncio.Future` that the main input loop resolves
# when the user types ``y`` / ``n`` at the pinned prompt.
# These tests drive the future directly; the main-loop
# wiring in :func:`run_repl` is exercised via the e2e harness.


def _load_repl_module() -> Any:
    """
    Reload ``omnigent.repl._repl`` so these tests see the
    edited source. Multiple tests in this file touch the
    module; a stale import cache would silently test the old
    API.

    :returns: The freshly-reloaded ``omnigent.repl._repl``
        module.
    """
    import importlib

    import omnigent.repl._repl as repl_mod

    importlib.reload(repl_mod)
    return repl_mod


class _FakeHost:
    """TerminalHost stub — records everything the hook prints."""

    def __init__(self) -> None:
        """Initialize with an empty output log."""
        self.outputs: list[Any] = []

    def output(self, item: Any) -> None:
        """Record the item.

        :param item: Whatever the hook handed to
            ``host.output(...)`` — usually a Rich
            :class:`Text` instance, sometimes a plain string.
        """
        self.outputs.append(item)


class _FakeFmt:
    """Formatter stub — the hook reads style names off it."""

    warning = "yellow"
    muted = "dim"
    accent = "cyan"


def _make_ctx(
    *,
    elicitation_id: str = "c1",
    message: str = "needs review",
    policy_name: str = "gatekeeper",
    phase: str = "tool_call",
    content_preview: str = '{"tool": "search"}',
    response_id: str = "r1",
) -> ElicitationRequestCtx:
    """Build an :class:`ElicitationRequestCtx` for hook tests.

    Producer extras (phase / policy_name / content_preview)
    are required dataclass fields, so the tests must supply
    every one — empty strings stand in for "not applicable"
    where the test doesn't care.

    :param elicitation_id: Unique id; tests use synthetic
        ``c1``/``c2`` rather than real ``elicit_...`` prefixes
        to keep the values readable in failure messages.
    :param message: Human-readable prompt the renderer should
        display.
    :param policy_name: Producer extra — deciding ASK policy.
    :param phase: Producer extra — phase that produced the ASK.
    :param content_preview: Producer extra — truncated content
        snapshot.
    :param response_id: Audit-only response id; the hook never
        posts on behalf of this id (the elicitation_id is the
        correlation key).
    :returns: A fully-populated context.
    """
    return ElicitationRequestCtx(
        elicitation_id=elicitation_id,
        message=message,
        requested_schema={},
        mode="form",
        phase=phase,
        policy_name=policy_name,
        content_preview=content_preview,
        response_id=response_id,
    )


@pytest.mark.asyncio
async def test_repl_hook_renders_and_awaits_future() -> None:
    """
    The hook writes the elicitation preview to the host,
    creates a pending future on the shared
    :class:`_ApprovalState`, and awaits it. It must NOT touch
    stdin — previously calling :func:`input` inside a thread
    fought ``patch_stdout``.

    We drive the future manually to assert the shape without
    spinning up a full REPL.
    """
    repl_mod = _load_repl_module()
    host = _FakeHost()
    state = repl_mod._ApprovalState()
    prompt_fn = repl_mod._make_elicitation_prompt(host, _FakeFmt(), state)
    ctx = _make_ctx()
    # Kick off the hook; don't await yet.
    task = asyncio.create_task(prompt_fn(ctx))
    # Give the event loop a turn so the hook renders and
    # registers the pending future.
    await asyncio.sleep(0)
    assert state.pending is True
    assert host.outputs, "elicitation hook rendered nothing"

    # Resolve via the same path the main input loop takes.
    resolved = state.resolve_verdict(repl_mod._ApprovalVerdict.APPROVE_ONCE)
    assert resolved is True
    result = await task
    assert result is True


@pytest.mark.asyncio
async def test_repl_state_resolve_on_refuse() -> None:
    """
    Resolving the future with REFUSE must yield ``False``
    from the hook — the fail-closed path for POLICIES.md §13.
    The SDK collapses ``False`` to MCP ``"decline"`` when
    POSTing.
    """
    repl_mod = _load_repl_module()
    state = repl_mod._ApprovalState()
    prompt_fn = repl_mod._make_elicitation_prompt(_FakeHost(), _FakeFmt(), state)
    ctx = _make_ctx(
        elicitation_id="c2",
        message="",
        policy_name="p",
        phase="response",
        content_preview="",
        response_id="r2",
    )
    task = asyncio.create_task(prompt_fn(ctx))
    await asyncio.sleep(0)
    state.resolve_verdict(repl_mod._ApprovalVerdict.REFUSE)
    assert await task is False


@pytest.mark.asyncio
async def test_repl_state_cancel_refuses_closed() -> None:
    """
    Cancelling an in-flight elicitation (user ^C during
    stream) must resolve the future to ``False``. Leaking an
    unresolved future would stall the next elicitation
    forever.
    """
    repl_mod = _load_repl_module()
    state = repl_mod._ApprovalState()
    prompt_fn = repl_mod._make_elicitation_prompt(_FakeHost(), _FakeFmt(), state)
    ctx = _make_ctx(
        elicitation_id="c3",
        message="",
        policy_name="",
        phase="request",
        content_preview="",
        response_id="r3",
    )
    task = asyncio.create_task(prompt_fn(ctx))
    await asyncio.sleep(0)
    state.cancel()
    assert await task is False
    # And cancel() clears state so no future is left pending.
    assert state.pending is False


def test_repl_state_replaces_stale_future() -> None:
    """
    If a second elicitation arrives while a prior one is still
    pending (defense-in-depth — the server should only park
    one at a time, but bugs happen), the prior future is
    resolved fail-closed and a fresh one is installed.
    """
    repl_mod = _load_repl_module()

    async def _body() -> None:
        state = repl_mod._ApprovalState()
        first = state.begin("p1", "request")
        second = state.begin("p1", "request")
        # Old future was resolved False so the first
        # elicitation's hook wakes with a refusal (never
        # leaks).
        assert first.done() and first.result() is False
        # New future is still open, waiting for the verdict.
        assert not second.done()
        state.resolve_verdict(repl_mod._ApprovalVerdict.APPROVE_ONCE)
        assert second.result() is True

    asyncio.run(_body())


# ── Three-way verdict parser ─────────────────────────────
#
# The parser is the precise seam between user keystrokes and
# the elicitation state. It must: (a) accept the short forms
# users reach for (``y``, ``a``, ``n``), (b) disambiguate
# ``a`` as ALWAYS (not as a random non-``y`` character that
# falls through to refuse), and (c) fail-closed on anything
# outside the vocabulary.


@pytest.mark.parametrize(
    "text,expected",
    [
        # APPROVE_ONCE
        ("y", "APPROVE_ONCE"),
        ("Y", "APPROVE_ONCE"),
        ("yes", "APPROVE_ONCE"),
        ("YES", "APPROVE_ONCE"),
        ("approve", "APPROVE_ONCE"),
        ("ok", "APPROVE_ONCE"),
        (" y ", "APPROVE_ONCE"),
        # APPROVE_ALWAYS
        ("a", "APPROVE_ALWAYS"),
        ("A", "APPROVE_ALWAYS"),
        ("always", "APPROVE_ALWAYS"),
        ("ALWAYS", "APPROVE_ALWAYS"),
        ("approve always", "APPROVE_ALWAYS"),
        (" a ", "APPROVE_ALWAYS"),
        # REFUSE
        ("", "REFUSE"),
        ("n", "REFUSE"),
        ("no", "REFUSE"),
        ("anything else", "REFUSE"),
        ("yolo", "REFUSE"),  # near-miss — explicit refusal
    ],
)
def test_repl_parse_approval_input(text: str, expected: str) -> None:
    """
    Three-way verdict parser matches Claude Code muscle memory
    (``y`` / ``a`` / ``n``) and fails closed on anything
    outside the vocabulary. The enum comparison guards against
    regressions that'd silently demote APPROVE_ALWAYS to
    APPROVE_ONCE (or vice-versa).
    """
    repl_mod = _load_repl_module()
    verdict = repl_mod._parse_approval_input(text)
    assert verdict.name == expected


# ── Session auto-approve cache ────────────────────────────


@pytest.mark.asyncio
async def test_repl_always_caches_and_auto_approves() -> None:
    """
    End-to-end for the "approve always" path.

    First elicitation: user types "a" (mapped to
    APPROVE_ALWAYS). The state caches
    ``(policy_name, phase)`` and the hook returns ``True``
    for this one. Host received the full approval-required
    banner.

    Second elicitation for the same pair: hook checks the
    cache FIRST, returns True immediately, and prints a muted
    ``auto-approved`` audit line. Critically: no
    ``⚠ approval required`` banner is rendered — the whole
    point of caching is zero UI friction once you've said yes.
    """
    repl_mod = _load_repl_module()
    host = _FakeHost()
    state = repl_mod._ApprovalState()
    prompt_fn = repl_mod._make_elicitation_prompt(host, _FakeFmt(), state)

    # First elicitation — prompts, user says "always".
    ctx1 = _make_ctx(
        elicitation_id="c1",
        message="",
        policy_name="always_ask_on_input",
        phase="request",
        content_preview="hello",
        response_id="r1",
    )
    task1 = asyncio.create_task(prompt_fn(ctx1))
    await asyncio.sleep(0)
    assert state.pending is True
    # Find the banner in the first-elicitation outputs.
    first_texts = [getattr(o, "plain", str(o)) for o in host.outputs]
    assert any("approval required" in t for t in first_texts), (
        "First elicitation must render the banner"
    )
    outputs_before_always = len(host.outputs)
    state.resolve_verdict(repl_mod._ApprovalVerdict.APPROVE_ALWAYS)
    assert await task1 is True
    # Cache now has the pair.
    assert state.is_pre_approved("always_ask_on_input", "request")

    # Second elicitation — same pair. Must auto-approve
    # without rendering the banner.
    ctx2 = _make_ctx(
        elicitation_id="c2",
        message="",
        policy_name="always_ask_on_input",
        phase="request",
        content_preview="follow-up",
        response_id="r2",
    )
    task2 = asyncio.create_task(prompt_fn(ctx2))
    await asyncio.sleep(0)
    # Future NEVER gets created because the hook short-circuits.
    assert state.pending is False
    assert await task2 is True

    # Outputs added by elicitation #2: ONLY the muted
    # auto-approve audit line — no banner, no
    # policy/reason/preview lines. Banner would be
    # ``approval required``, which must not appear for the
    # second elicitation.
    second_outputs = host.outputs[outputs_before_always:]
    second_texts = [getattr(o, "plain", str(o)) for o in second_outputs]
    assert all("approval required" not in t for t in second_texts), (
        f"Second elicitation rendered a banner despite auto-approval cache:\n{second_texts}"
    )
    assert any("auto-approved" in t for t in second_texts), (
        f"Auto-approve path must print an audit line:\n{second_texts}"
    )


@pytest.mark.asyncio
async def test_repl_always_is_scoped_to_policy_and_phase() -> None:
    """
    The cache key is ``(policy_name, phase)`` — a different
    policy OR a different phase still prompts. Granularity
    prevents a blanket "always" from accidentally approving a
    different gate the user never consented to.
    """
    repl_mod = _load_repl_module()
    state = repl_mod._ApprovalState()
    # User said "always" for policy_a at REQUEST.
    state.remember_always("policy_a", "request")

    assert state.is_pre_approved("policy_a", "request") is True
    # Different policy — still prompts.
    assert state.is_pre_approved("policy_b", "request") is False
    # Same policy, different phase — still prompts.
    assert state.is_pre_approved("policy_a", "tool_call") is False


def test_repl_once_does_not_populate_cache() -> None:
    """
    APPROVE_ONCE must leave the cache empty. Otherwise the
    next elicitation would silently auto-approve, which is
    NOT what the user asked for — "once" means once.
    """
    repl_mod = _load_repl_module()

    async def _body() -> None:
        state = repl_mod._ApprovalState()
        state.begin("policy_x", "request")
        state.resolve_verdict(repl_mod._ApprovalVerdict.APPROVE_ONCE)
        assert state.is_pre_approved("policy_x", "request") is False

    asyncio.run(_body())


def test_repl_refuse_does_not_populate_cache() -> None:
    """
    REFUSE must also leave the cache untouched. Caching a
    refusal would make the next elicitation silently fail
    without the user getting a chance to reconsider.
    """
    repl_mod = _load_repl_module()

    async def _body() -> None:
        state = repl_mod._ApprovalState()
        state.begin("policy_x", "request")
        state.resolve_verdict(repl_mod._ApprovalVerdict.REFUSE)
        assert state.is_pre_approved("policy_x", "request") is False

    asyncio.run(_body())
