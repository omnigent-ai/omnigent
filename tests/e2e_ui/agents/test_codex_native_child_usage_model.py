"""UI journey: a Codex Native child on another model must own its usage bucket.

Codex Native can run a parent thread and a built-in sub-agent (child thread) on
different models. Codex reports the child's model on the child thread's
``thread/settings/updated`` (or ``thread/resume``), but its per-thread
``thread/tokenUsage/updated`` frames carry only the child ``threadId`` and
cumulative counts -- no model. The forwarder must attribute the child's tokens
to that child's own reported model; seeding the child's coalescer with the
*parent* forwarder's model instead posts the child's usage to the child
session's ``external_session_usage`` under the parent model. Two user-visible
symptoms follow, both in the agent-info popover's "Token usage" per-model
breakdown:

* Facet 1 -- the child session's tokens sit under the *parent* model's bucket
  (priced at the parent's rates) instead of the child model the child ran on.
* Facet 2 -- the root subtree rollup folds parent + child into one parent-model
  bucket instead of one bucket per model.

Reproduction shape (why events are injected, not driven through a spawn):
Codex's native multi-agent spawn (``multi_agent_v1``/``spawn_agent``) requires
ChatGPT-backend agent-task registration and agent-identity JWKS, which the
offline mock cannot satisfy -- the CLI rejects a scripted spawn as
``unsupported call``. So, exactly like the repo's own codex child-thread
coverage (``test_codex_native_subagent_activity.py``), this drives the *real*
forwarder (``_handle_event`` + ``_SessionUsageCoalescer`` + the real child
coalescer seeding), the *real* server persistence, and the *real* web UI,
feeding the byte-faithful Codex app-server frames a genuine parent + child
would emit. The forwarder logic under test -- the model attribution -- runs
unchanged from production.
"""

from __future__ import annotations

import asyncio
import os
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from omnigent.harnesses.codex_native import forwarder as codex_native_forwarder

# Parent and child run on different Codex catalog models, mirroring a
# parent / built-in-child split.
PARENT_MODEL = "gpt-5.6-sol"
CHILD_MODEL = "gpt-5.6-luna"

# The child's cumulative usage (input is inclusive of cached).
CHILD_INPUT = 20_280
CHILD_CACHED = 9_984
CHILD_OUTPUT = 23
CHILD_TOTAL = CHILD_INPUT + CHILD_OUTPUT

# Distinct parent usage so the parent-model bucket is unambiguously present.
PARENT_INPUT = 5_000
PARENT_CACHED = 1_000
PARENT_OUTPUT = 400
PARENT_TOTAL = PARENT_INPUT + PARENT_OUTPUT

_PARENT_THREAD = "thread_parent"
_CHILD_THREAD = "thread_child"
_CONTEXT_WINDOW = 200_000

_CHILD_TIMEOUT_S = 60.0
_USAGE_TIMEOUT_S = 60.0


@pytest.fixture
def browser_context_args(browser_context_args: dict[str, Any]) -> dict[str, Any]:
    """Record the sync ``page`` fixture's journey when a record dir is set.

    The shared ``_record_video`` fixture only patches the async Browser API;
    this journey drives the sync ``page`` fixture, whose context is built from
    ``browser_context_args``, so the record dir is injected here instead.
    """
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    if not record_dir:
        return browser_context_args
    Path(record_dir).mkdir(parents=True, exist_ok=True)
    return {**browser_context_args, "record_video_dir": record_dir}


def _token_usage_params(
    thread_id: str, *, input_tokens: int, cached: int, output_tokens: int
) -> dict[str, Any]:
    """Build a Codex ``thread/tokenUsage/updated`` params for a thread.

    ``total`` carries the cumulative counts the forwarder forwards to the
    server; the frame itself carries no model (Codex sends usage and settings
    separately), which is what makes the child's model ambiguous.
    """
    return {
        "threadId": thread_id,
        "tokenUsage": {
            "total": {
                "inputTokens": input_tokens,
                "outputTokens": output_tokens,
                "cachedInputTokens": cached,
                "contextWindow": _CONTEXT_WINDOW,
            },
            "last": {"inputTokens": input_tokens},
            "modelContextWindow": _CONTEXT_WINDOW,
        },
    }


async def _forward_parent_child_usage(base_url: str, parent_session_id: str) -> None:
    """Drive the real forwarder through a parent turn + child spawn + usage.

    Feeds the app-server frames a genuine Codex parent-on-``PARENT_MODEL``
    plus a built-in child-on-``CHILD_MODEL`` would emit:

    1. parent ``thread/tokenUsage/updated`` -> parent usage under PARENT_MODEL
    2. ``subAgentActivity`` (kind ``started``) -> registers the child session
    3. child ``thread/settings/updated`` reporting CHILD_MODEL (the frame the
       forwarder must attribute the child's tokens by)
    4. child ``thread/tokenUsage/updated`` (model-less) -> child usage
    """
    state = codex_native_forwarder._CodexForwarderState(parent_session_id=parent_session_id)
    # The parent turn established the parent's model on the forwarder state.
    state.model = PARENT_MODEL
    tracker = codex_native_forwarder._CodexElicitationTaskTracker()
    async with httpx.AsyncClient(base_url=base_url) as client:
        parent_coalescer = codex_native_forwarder._SessionUsageCoalescer(
            client, parent_session_id, model=PARENT_MODEL
        )

        async def handle(event: dict[str, Any]) -> None:
            await codex_native_forwarder._handle_event(
                client,
                session_id=parent_session_id,
                bridge_dir=Path(),
                event=event,
                usage_coalescer=parent_coalescer,
                elicitation_tracker=tracker,
                expected_thread_id=_PARENT_THREAD,
                forwarder_state=state,
            )

        await handle(
            {
                "method": "thread/tokenUsage/updated",
                "params": _token_usage_params(
                    _PARENT_THREAD,
                    input_tokens=PARENT_INPUT,
                    cached=PARENT_CACHED,
                    output_tokens=PARENT_OUTPUT,
                ),
            }
        )
        await handle(
            {
                "method": "item/completed",
                "params": {
                    "threadId": _PARENT_THREAD,
                    "turnId": "turn_parent",
                    "item": {
                        "type": "subAgentActivity",
                        "id": "activity_1",
                        "kind": "started",
                        "agentThreadId": _CHILD_THREAD,
                        "agentPath": "root/child",
                        "agent_nickname": "child",
                    },
                },
            }
        )
        await handle(
            {
                "method": "thread/settings/updated",
                "params": {
                    "threadId": _CHILD_THREAD,
                    "threadSettings": {"model": CHILD_MODEL},
                },
            }
        )
        await handle(
            {
                "method": "thread/tokenUsage/updated",
                "params": _token_usage_params(
                    _CHILD_THREAD,
                    input_tokens=CHILD_INPUT,
                    cached=CHILD_CACHED,
                    output_tokens=CHILD_OUTPUT,
                ),
            }
        )
    await tracker.close()


def _child_session_id(base_url: str, parent_session_id: str) -> str:
    """Return the child session id the subAgent-activity spawn registered."""
    deadline = time.monotonic() + _CHILD_TIMEOUT_S
    while time.monotonic() < deadline:
        url = f"{base_url}/v1/sessions/{parent_session_id}/child_sessions"
        resp = httpx.get(url, timeout=10.0)
        resp.raise_for_status()
        children = list(resp.json().get("data") or [])
        if children:
            return str(children[0]["id"])
        time.sleep(1.0)
    raise AssertionError(
        "the Codex subAgentActivity spawn never registered a child session on the parent "
        "- the forwarder/server wiring broke, not the bug"
    )


def _usage_by_model(base_url: str, session_id: str) -> dict[str, dict[str, Any]]:
    resp = httpx.get(
        f"{base_url}/v1/sessions/{session_id}", params={"include_usage": "true"}, timeout=10.0
    )
    resp.raise_for_status()
    return dict(resp.json().get("usage_by_model") or {})


def _sum_field(usage_by_model: dict[str, dict[str, Any]], field: str) -> int:
    return sum(int(bucket.get(field) or 0) for bucket in usage_by_model.values())


def _wait_for_usage(
    base_url: str, session_id: str, *, min_total_tokens: int
) -> dict[str, dict[str, Any]]:
    """Poll persisted per-model usage until it covers *min_total_tokens*.

    The server splits cached input out of the displayed ``input_tokens`` field,
    so the stable coverage check is ``total_tokens`` (full input incl. cache +
    output).
    """
    deadline = time.monotonic() + _USAGE_TIMEOUT_S
    usage: dict[str, dict[str, Any]] = {}
    while time.monotonic() < deadline:
        usage = _usage_by_model(base_url, session_id)
        if _sum_field(usage, "total_tokens") >= min_total_tokens:
            return usage
        time.sleep(1.0)
    raise AssertionError(
        f"session {session_id} never persisted usage covering {min_total_tokens} total tokens; "
        f"last usage_by_model={usage!r} - the usage never arrived, not the bug"
    )


def _drive(base_url: str, parent_session_id: str) -> str:
    """Run the forwarder injection off-thread; return the child session id.

    Also asserts the preconditions that hold before and after a fix: the child
    genuinely ran (its session registered) and its cumulative counts reached
    the child session.
    """
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(
            asyncio.run, _forward_parent_child_usage(base_url, parent_session_id)
        ).result()

    child_session_id = _child_session_id(base_url, parent_session_id)
    child_usage = _wait_for_usage(base_url, child_session_id, min_total_tokens=CHILD_TOTAL)
    assert _sum_field(child_usage, "output_tokens") >= CHILD_OUTPUT, child_usage
    assert _sum_field(child_usage, "cache_read_input_tokens") >= CHILD_CACHED, child_usage
    return child_session_id


def _open_token_usage(page: Page) -> Locator:
    """Open the agent-info popover and expand its Token usage breakdown."""
    trigger = page.get_by_test_id("agent-info-trigger")
    expect(trigger).to_be_visible(timeout=60_000)
    trigger.focus()
    trigger.press("Enter")
    usage_section = page.get_by_test_id("agent-info-usage-by-model")
    expect(usage_section).to_be_visible(timeout=30_000)
    usage_section.locator("summary").press("Enter")
    expect(usage_section.locator('[data-testid^="agent-info-model-"]').first).to_be_visible(
        timeout=30_000
    )
    return usage_section


def test_codex_native_child_usage_is_attributed_to_child_model(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Facet 1: the child session's tokens must sit under CHILD_MODEL, not PARENT_MODEL."""
    base_url, parent_session_id = seeded_session
    child_session_id = _drive(base_url, parent_session_id)

    page.goto(f"{base_url}/c/{child_session_id}")
    usage_section = _open_token_usage(page)

    child_usage = _usage_by_model(base_url, child_session_id)
    assert set(child_usage) == {CHILD_MODEL}, (
        f"child token usage is bucketed under {sorted(child_usage)} instead of "
        f"the child's own model {CHILD_MODEL!r}: {child_usage!r}"
    )
    expect(usage_section.get_by_test_id(f"agent-info-model-{CHILD_MODEL}")).to_be_visible(
        timeout=10_000
    )
    expect(usage_section.get_by_test_id(f"agent-info-model-{PARENT_MODEL}")).to_have_count(0)


def test_codex_native_root_rollup_keeps_parent_and_child_model_buckets(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Facet 2: the root subtree rollup must list one bucket per model."""
    base_url, parent_session_id = seeded_session
    _drive(base_url, parent_session_id)
    root_usage = _wait_for_usage(
        base_url, parent_session_id, min_total_tokens=PARENT_TOTAL + CHILD_TOTAL
    )

    page.goto(f"{base_url}/c/{parent_session_id}")
    usage_section = _open_token_usage(page)

    assert {PARENT_MODEL, CHILD_MODEL} <= set(root_usage), (
        "root subtree rollup folds the child's tokens into the parent-model bucket "
        f"instead of keeping distinct parent- and child-model buckets: {root_usage!r}"
    )
    expect(usage_section.get_by_test_id(f"agent-info-model-{PARENT_MODEL}")).to_be_visible(
        timeout=10_000
    )
    expect(usage_section.get_by_test_id(f"agent-info-model-{CHILD_MODEL}")).to_be_visible(
        timeout=10_000
    )
