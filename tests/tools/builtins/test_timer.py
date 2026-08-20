"""
Unit tests for ``sys_timer_set`` and ``sys_timer_cancel`` (step 10
of the harness contract migration).

Covers the schema shape, argument validation, and ToolManager
registration gating — paths that don't require a running DBOS
workflow. The actual firing behavior (sleep + send +
auto-rendering) requires DBOS and is exercised by the server
integration suite.

Mirrors the structure of :mod:`tests.tools.builtins.test_async_inbox`,
which gates ``sys_call_async`` / ``sys_read_inbox`` /
``sys_cancel_async`` on ``async_enabled``. The timer family is
gated on ``timers``.
"""

from __future__ import annotations

import json

import pytest

from omnigent.spec import AgentSpec
from omnigent.tools.base import ToolContext
from omnigent.tools.builtins.timer import (
    SysTimerCancelTool,
    SysTimerSetTool,
    validate_timer_set_args,
)
from omnigent.tools.manager import ToolManager

# Stub :class:`ToolContext` for the validation paths below. All the
# parametrized invalid-argument tests hit early-exit error branches
# in ``SysTimerSetTool.invoke`` BEFORE the conversation_id check, so
# the conversation_id value here doesn't affect them. The dedicated
# ``test_set_missing_conversation_id_returns_error`` test exercises
# the conversation_id branch with a different stub.
_STUB_CTX = ToolContext(task_id="task_parent", agent_id="agent_x", conversation_id="conv_x")
_STUB_CTX_NO_CONV = ToolContext(task_id="task_parent", agent_id="agent_x", conversation_id=None)


# ─── Schema shape ────────────────────────────────────────────


def test_set_schema_required_fields_and_no_extras() -> None:
    """
    ``sys_timer_set`` requires ``seconds`` and rejects unknown
    properties. ``repeat`` and ``note`` are optional.

    A regression that loosened the required list would let the LLM
    schedule a timer with no delay (the workflow would then fire
    immediately in a tight loop for repeat=true). Allowing extras
    would silently drop unknown keys instead of failing fast.
    """
    schema = SysTimerSetTool().get_schema()["function"]["parameters"]
    assert schema["required"] == ["seconds"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"].keys()) == {"seconds", "repeat", "note"}
    assert schema["properties"]["repeat"]["default"] is False


def test_cancel_schema_required_fields_and_no_extras() -> None:
    """
    ``sys_timer_cancel`` requires ``timer_id`` and rejects unknown
    properties.

    Without the required field, the LLM could call cancel with no
    args; the validation branch returns an error string but a
    schema-enforced reject is the cleaner front line.
    """
    schema = SysTimerCancelTool().get_schema()["function"]["parameters"]
    assert schema["required"] == ["timer_id"]
    assert schema["additionalProperties"] is False
    assert set(schema["properties"].keys()) == {"timer_id"}


def test_tools_are_synchronous() -> None:
    """
    Both timer tools return ``is_async() == False``.

    The CALL is synchronous (the LLM gets the timer_id back
    immediately as the tool result), even though the FIRING is
    asynchronous and arrives later as a persisted conversation
    item. A regression where ``is_async`` flipped to ``True``
    would route ``invoke`` to ``dispatch_async`` and the LLM
    would see a "task in progress" handle JSON instead of the
    timer_id it needs to later cancel. The cross-harness PATCH
    flow also doesn't propagate string returns from
    ``dispatch_async`` back to all harness types, so the sync
    ``invoke`` path is what produces a clean function_call_output
    in the conversation.
    """
    assert SysTimerSetTool().is_async() is False
    assert SysTimerCancelTool().is_async() is False


# ─── SysTimerSetTool argument validation ─────────────────────


@pytest.mark.parametrize(
    "args_json,expected_error_substring",
    [
        # Missing required field — passes empty object so the JSON
        # parser succeeds and the missing-seconds branch fires.
        ("{}", "seconds must be a number"),
        # Negative seconds — the underlying DBOS sleep would raise
        # at workflow time, but rejecting at the tool boundary
        # surfaces the error to the LLM with a clearer message.
        ('{"seconds": -1}', "seconds must be non-negative"),
        # Above the cap — guards against an LLM hallucination
        # parking a workflow indefinitely.
        ('{"seconds": 10000000}', "seconds must be <="),
        # Bool gets rejected explicitly because Python treats
        # ``isinstance(True, int)`` as True; without the bool
        # check, ``True`` would silently coerce to ``1.0``.
        ('{"seconds": true}', "seconds must be a number"),
        # ``repeat`` MUST be a real bool, not a truthy string —
        # YAML / LLM JSON often confuses these.
        ('{"seconds": 1, "repeat": "yes"}', "repeat must be a boolean"),
        # ``note`` MUST be a string when present.
        ('{"seconds": 1, "note": 5}', "note must be a string"),
        # Zero-delay repeating timers would busy-loop sleep(0)+POST.
        ('{"seconds": 0, "repeat": true}', "seconds must be > 0 when repeat is true"),
        # Non-standard JSON NaN/Inf are floats, but every comparison is
        # false — without an isfinite guard, repeat=true would hot-loop.
        ('{"seconds": NaN, "repeat": true}', "seconds must be a finite number"),
        ('{"seconds": Infinity}', "seconds must be a finite number"),
        ('{"seconds": -Infinity}', "seconds must be a finite number"),
    ],
)
def test_set_invalid_args_return_error(args_json: str, expected_error_substring: str) -> None:
    """
    Each malformed argument shape produces a structured ``{"error":
    ...}`` response without starting a workflow.

    Without these checks, a bad call would either crash inside the
    workflow (LLM sees an opaque "internal error") or silently
    coerce wrong types. The error path is the public surface for
    invalid input — must remain stable.
    """
    result_json = SysTimerSetTool().invoke(args_json, _STUB_CTX)
    result = json.loads(result_json)
    assert "error" in result, f"expected error key in {result!r}"
    assert expected_error_substring in result["error"]


def test_set_malformed_json_returns_parse_error() -> None:
    """
    Malformed JSON produces a structured ``{"error": "invalid
    arguments: ..."}`` response, not a 500-style crash.

    Mirrors :class:`SysCallAsyncTool`'s own JSON-decode error
    handling — the tool is on the LLM-facing boundary and any
    parse failure must round-trip as an error string.
    """
    result_json = SysTimerSetTool().invoke("{not json", _STUB_CTX)
    result = json.loads(result_json)
    assert "error" in result
    assert "invalid arguments" in result["error"]


def test_set_missing_conversation_id_returns_error() -> None:
    """
    Valid args + ``ctx.conversation_id is None`` returns a structured
    error (no workflow is started).

    The timer workflow appends firings to the conversation store,
    so it MUST have a destination conversation. Without that, the
    firings would have nowhere to land — the tool fails loud here
    rather than silently dropping them. A regression that omitted
    this guard would surface much later as a workflow-time
    exception or, worse, silently lost firings.
    """
    result_json = SysTimerSetTool().invoke('{"seconds": 1, "note": "x"}', _STUB_CTX_NO_CONV)
    result = json.loads(result_json)
    assert "error" in result
    assert "conversation" in result["error"].lower()


def test_set_valid_args_in_process_reports_no_schedule() -> None:
    """
    Fully valid args off the runner dispatch path return a structured
    error, never a raise or a false success.

    The runner intercepts ``sys_timer_set`` before this builtin is
    reached (see ``tool_dispatch._execute_timer_set``). This guards the
    in-process fallback: a future non-runner caller must fail cleanly
    with no ``timer_id`` (there is no scheduled timer to cancel), rather
    than hit the old ``NotImplementedError`` trap.
    """
    result_json = SysTimerSetTool().invoke('{"seconds": 5, "note": "x"}', _STUB_CTX)
    result = json.loads(result_json)
    assert "error" in result
    assert "timer_id" not in result
    assert result.get("status") != "scheduled"


# ─── Shared validation helper ────────────────────────────────


def test_validate_timer_set_args_accepts_valid_shapes() -> None:
    """
    ``validate_timer_set_args`` returns ``(seconds, repeat, note)`` for
    valid input — the tuple the runner firing loop and the builtin both
    consume.

    This is the single contract both surfaces share; a drift here would
    desync the runner path from the LLM-facing schema.
    """
    assert validate_timer_set_args({"seconds": 5}) == (5.0, False, None)
    assert validate_timer_set_args({"seconds": 5, "repeat": True, "note": "x"}) == (
        5.0,
        True,
        "x",
    )
    # Immediate one-shot remains valid; only repeating timers reject 0.
    assert validate_timer_set_args({"seconds": 0}) == (0.0, False, None)
    assert validate_timer_set_args({"seconds": 0, "repeat": False}) == (0.0, False, None)


@pytest.mark.parametrize(
    "args,expected_error_substring",
    [
        ({}, "seconds must be a number"),
        ({"seconds": -1}, "seconds must be non-negative"),
        ({"seconds": 10_000_000}, "seconds must be <="),
        ({"seconds": True}, "seconds must be a number"),
        ({"seconds": 1, "repeat": "yes"}, "repeat must be a boolean"),
        ({"seconds": 1, "note": 5}, "note must be a string"),
        ({"seconds": 0, "repeat": True}, "seconds must be > 0 when repeat is true"),
        ({"seconds": float("nan"), "repeat": True}, "seconds must be a finite number"),
        ({"seconds": float("inf")}, "seconds must be a finite number"),
        ({"seconds": float("-inf")}, "seconds must be a finite number"),
    ],
)
def test_validate_timer_set_args_rejects_bad_shapes(
    args: dict[str, object], expected_error_substring: str
) -> None:
    """
    Each malformed shape yields an error message (a ``str``), not a
    tuple — the same messages both timer surfaces return to the LLM.
    """
    result = validate_timer_set_args(args)
    assert isinstance(result, str)
    assert expected_error_substring in result


# ─── SysTimerCancelTool argument validation ──────────────────


def test_cancel_missing_timer_id_returns_error() -> None:
    """
    Missing ``timer_id`` returns ``{"error": "timer_id is required"}``.

    The tool can't address a workflow without an id — the cancel
    has nothing to act on. Returning an error is preferred over
    a silent ``not_found`` because the latter would mask LLM bugs
    where it forgot to thread the timer_id through.
    """
    result_json = SysTimerCancelTool().invoke("{}", _STUB_CTX)
    result = json.loads(result_json)
    assert result == {"error": "timer_id is required"}


def test_cancel_empty_string_timer_id_returns_error() -> None:
    """
    Empty-string ``timer_id`` is rejected with the same error as a
    missing key.

    Distinct empty-string handling matters because the LLM
    sometimes passes ``""`` for omitted fields rather than
    omitting them — without this branch, an empty string would
    flow into ``get_workflow_status`` and produce a confusing
    DBOS-internal error.
    """
    result_json = SysTimerCancelTool().invoke('{"timer_id": ""}', _STUB_CTX)
    result = json.loads(result_json)
    assert result == {"error": "timer_id is required"}


def test_cancel_malformed_json_returns_parse_error() -> None:
    """Malformed JSON produces a parse-error response on the cancel tool too."""
    result_json = SysTimerCancelTool().invoke("not json", _STUB_CTX)
    result = json.loads(result_json)
    assert "error" in result
    assert "invalid arguments" in result["error"]


# ─── ToolManager registration gating ─────────────────────────


def test_timers_false_does_not_register() -> None:
    """
    With ``timers=False`` (the default) the manager does NOT
    register either timer tool.

    The default-off behavior matches the inner stack
    (``AgentDef.timers`` defaults to False there too) — agents
    that don't declare ``timers: true`` get the same minimal tool
    surface they did pre-step-10. A regression that flipped the
    default to True would surprise existing agents.
    """
    spec = AgentSpec(spec_version=1)  # timers defaults to False
    manager = ToolManager(spec=spec)
    names = manager.get_tool_names()
    assert SysTimerSetTool.name() not in names
    assert SysTimerCancelTool.name() not in names


def test_timers_true_registers_both_tools_and_schemas() -> None:
    """
    With ``timers=True`` the manager registers both tools and
    surfaces them in ``get_tool_schemas``.

    The schema visibility is what the LLM sees in its function
    list — a regression where registration succeeded but the
    schema didn't appear would manifest as the LLM holding a
    tool name it can't actually use (the runtime would still
    dispatch on call_tool, but the LLM never knew to call it).
    """
    spec = AgentSpec(spec_version=1, timers=True)
    manager = ToolManager(spec=spec)
    names = manager.get_tool_names()
    assert SysTimerSetTool.name() in names
    assert SysTimerCancelTool.name() in names
    schema_names = {s["function"]["name"] for s in manager.get_tool_schemas()}
    assert SysTimerSetTool.name() in schema_names
    assert SysTimerCancelTool.name() in schema_names


def test_timers_independent_of_async_enabled() -> None:
    """
    The ``timers`` and ``async_enabled`` flags are independent — a
    spec with ``timers=True, async_enabled=False`` registers the
    timer tools but NOT the async-inbox tools.

    Step 10 is described as building on the async-inbox machinery,
    but the LLM's tool surface is gated separately. An agent that
    wants timers without sys_call_async / sys_read_inbox /
    sys_cancel_async should be able to declare that combination
    explicitly.
    """
    spec = AgentSpec(spec_version=1, timers=True, async_enabled=False)
    manager = ToolManager(spec=spec)
    names = manager.get_tool_names()
    assert SysTimerSetTool.name() in names
    assert SysTimerCancelTool.name() in names
    # async-inbox tools are NOT registered.
    assert "sys_call_async" not in names
    assert "sys_read_inbox" not in names
    assert "sys_cancel_async" not in names
