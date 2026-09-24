"""UI journey: a Codex Native child thread on a different model than its parent
must attribute its token usage to the CHILD's model, not the parent's.

Codex Native can run a parent and its built-in sub-agent on different models.
Codex's ``thread/tokenUsage/updated`` notification for a child carries the
child ``threadId`` and cumulative counts but NO model, so the forwarder must
attribute the child's usage from the child's own ``thread/resume`` /
``thread/settings/updated`` model — never from the parent thread's model.
Otherwise the server prices and buckets the child's tokens under the PARENT
model.

User-visible surface (web SPA agent-info popover → "Token usage" per-model
breakdown, ``AgentInfo.tsx``):

* Child session panel: the child's tokens must appear under the child's own
  model id (``agent-info-model-<child>``), never the parent's.
* Root session panel: the subtree rollup must keep distinct per-model buckets
  instead of folding parent + child into a single parent-model bucket.

Journey (drives the REAL forwarder against a live server + runner, exactly the
Codex app-server frames Codex emits, then reads the persisted session snapshot
and renders the SPA panel):

1. a Codex Native parent runs a turn on ``PARENT_MODEL`` (its cumulative usage
   frame arrives) → parent usage bucketed under ``PARENT_MODEL``
2. the parent spawns one built-in Codex child (``subAgentActivity`` started) →
   a child session is registered
3. the child announces its own model via ``thread/settings/updated``
   (``CHILD_MODEL``) — the attribution source
4. the child completes; Codex emits the child's model-less cumulative
   ``thread/tokenUsage/updated`` (20,280 input / 9,984 cached / 23 output)
5. inspect the persisted child + root ``usage_by_model`` and the SPA panels
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import httpx
from playwright.sync_api import Page, expect

from omnigent.harnesses.codex_native import forwarder as codex_native_forwarder

# Two models with different rates: a parent (gpt-5.5) and a built-in child
# (gpt-5.5-codex-mini). Dash-only ids keep the ``agent-info-model-<id>`` test
# ids simple.
PARENT_MODEL = "databricks-gpt-5-5"
CHILD_MODEL = "databricks-gpt-5-5-codex-mini"

# The child's cumulative usage counts.
CHILD_INPUT = 20_280
CHILD_CACHED = 9_984
CHILD_OUTPUT = 23

# A small parent-turn usage so the parent bucket is unambiguously present.
PARENT_INPUT = 5_000
PARENT_CACHED = 1_000
PARENT_OUTPUT = 400


async def _drive_parent_child_journey(base_url: str, parent_session_id: str) -> None:
    """Drive the real Codex Native forwarder through the parent+child journey.

    Feeds the exact Codex app-server notification frames Codex emits: a parent
    usage frame (parent model), a child spawn, the child's own
    ``thread/settings/updated`` model, and the child's model-less cumulative
    ``thread/tokenUsage/updated``. Posts land on the live server via ``client``.
    """
    state = codex_native_forwarder._CodexForwarderState(parent_session_id=parent_session_id)
    # The parent thread's resolved model (learned from resume/config live).
    state.model = PARENT_MODEL
    tracker = codex_native_forwarder._CodexElicitationTaskTracker()
    async with httpx.AsyncClient(base_url=base_url) as client:
        parent_coalescer = codex_native_forwarder._SessionUsageCoalescer(client, parent_session_id)
        events: list[dict[str, Any]] = [
            # 1) parent turn cumulative usage (parent model)
            {
                "method": "thread/tokenUsage/updated",
                "params": {
                    "threadId": "thread_parent",
                    "tokenUsage": {
                        "modelContextWindow": 272_000,
                        "total": {
                            "inputTokens": PARENT_INPUT,
                            "cachedInputTokens": PARENT_CACHED,
                            "outputTokens": PARENT_OUTPUT,
                        },
                        "last": {"inputTokens": PARENT_INPUT},
                    },
                },
            },
            # 2) parent spawns a built-in Codex child
            {
                "method": "item/completed",
                "params": {
                    "threadId": "thread_parent",
                    "turnId": "turn_parent",
                    "item": {
                        "type": "subAgentActivity",
                        "id": "activity_1",
                        "kind": "started",
                        "agentThreadId": "thread_child",
                        "agentPath": "root/reviewer",
                    },
                },
            },
            # 3) child turn starts
            {
                "method": "turn/started",
                "params": {
                    "threadId": "thread_child",
                    "turn": {"id": "turn_child", "status": "inProgress"},
                },
            },
            # 4) child announces its OWN model (resume/settings model source)
            {
                "method": "thread/settings/updated",
                "params": {
                    "threadId": "thread_child",
                    "threadSettings": {"model": CHILD_MODEL},
                },
            },
            # 5) child's model-less cumulative usage frame
            {
                "method": "thread/tokenUsage/updated",
                "params": {
                    "threadId": "thread_child",
                    "tokenUsage": {
                        "modelContextWindow": 272_000,
                        "total": {
                            "inputTokens": CHILD_INPUT,
                            "cachedInputTokens": CHILD_CACHED,
                            "outputTokens": CHILD_OUTPUT,
                        },
                        "last": {"inputTokens": CHILD_INPUT},
                    },
                },
            },
            # 6) child turn completes
            {
                "method": "turn/completed",
                "params": {
                    "threadId": "thread_child",
                    "turn": {"id": "turn_child", "status": "completed", "items": []},
                },
            },
        ]
        for event in events:
            await codex_native_forwarder._handle_event(
                client,
                session_id=parent_session_id,
                bridge_dir=Path(),
                event=event,
                usage_coalescer=parent_coalescer,
                elicitation_tracker=tracker,
                expected_thread_id="thread_parent",
                forwarder_state=state,
            )
        await parent_coalescer.flush()
    await tracker.close()


def _run_journey(base_url: str, parent_session_id: str) -> str:
    """Drive the journey (off-loop) and return the registered child session id."""
    with ThreadPoolExecutor(max_workers=1) as executor:
        executor.submit(asyncio.run, _drive_parent_child_journey(base_url, parent_session_id)).result()
    children = httpx.get(
        f"{base_url}/v1/sessions/{parent_session_id}/child_sessions", timeout=10.0
    ).json()["data"]
    assert children, "the Codex child spawn did not register a child session"
    return str(children[0]["id"])


def _usage_by_model(base_url: str, session_id: str) -> dict[str, dict[str, Any]]:
    """Return the persisted per-model usage map for ``session_id`` (subtree)."""
    snap = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0).json()
    return snap.get("usage_by_model") or {}


def _open_token_usage(page: Page) -> None:
    """Open the agent-info popover and expand its per-model Token usage section."""
    trigger = page.get_by_test_id("agent-info-trigger")
    expect(trigger).to_be_visible(timeout=60_000)
    trigger.click()
    breakdown = page.get_by_test_id("agent-info-usage-by-model")
    expect(breakdown).to_be_visible(timeout=15_000)
    # Expand the collapsed <details> so the per-model rows are on screen.
    breakdown.locator("summary").first.click()


def test_codex_child_usage_attributed_to_child_model(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The child session's tokens must be attributed to CHILD_MODEL.

    A forwarder that seeds the child's usage from the parent thread's model
    buckets the child's tokens under ``agent-info-model-<PARENT_MODEL>`` and
    keys the snapshot's ``usage_by_model`` by the parent model.
    """
    base_url, parent_session_id = seeded_session
    child_session_id = _run_journey(base_url, parent_session_id)

    # Server truth: the child's own per-model bucket.
    child_usage = _usage_by_model(base_url, child_session_id)

    # Render the CHILD session's agent-info Token usage breakdown (this is the
    # user-visible surface; leave it on screen for the recording).
    page.goto(f"{base_url}/c/{child_session_id}")
    _open_token_usage(page)
    child_model_group = page.get_by_test_id(f"agent-info-model-{CHILD_MODEL}")
    parent_model_group = page.get_by_test_id(f"agent-info-model-{PARENT_MODEL}")

    # The child's usage must be keyed by the child's own model.
    assert PARENT_MODEL not in child_usage, (
        "child token usage is attributed to the PARENT model bucket "
        f"{PARENT_MODEL!r}: {child_usage!r}"
    )
    assert CHILD_MODEL in child_usage, (
        f"child token usage is not attributed to the child's own model {CHILD_MODEL!r}: "
        f"{child_usage!r}"
    )
    assert int(child_usage[CHILD_MODEL].get("total_tokens") or 0) == CHILD_INPUT + CHILD_OUTPUT, (
        f"child bucket total does not match the reported counts: {child_usage[CHILD_MODEL]!r}"
    )
    expect(child_model_group).to_be_visible(timeout=10_000)
    expect(parent_model_group).to_have_count(0)


def test_codex_root_rollup_splits_child_and_parent_models(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The root subtree rollup must show distinct per-model buckets.

    Child tokens mis-attributed to PARENT_MODEL fold parent + child into a
    single ``agent-info-model-<PARENT_MODEL>`` bucket; the rollup must instead
    list one bucket per model.
    """
    base_url, parent_session_id = seeded_session
    _run_journey(base_url, parent_session_id)

    root_usage = _usage_by_model(base_url, parent_session_id)

    # Render the ROOT (parent) session's agent-info Token usage breakdown.
    page.goto(f"{base_url}/c/{parent_session_id}")
    _open_token_usage(page)
    parent_model_group = page.get_by_test_id(f"agent-info-model-{PARENT_MODEL}")
    child_model_group = page.get_by_test_id(f"agent-info-model-{CHILD_MODEL}")

    # The rollup must keep one bucket per model.
    assert set(root_usage) == {PARENT_MODEL, CHILD_MODEL}, (
        "root subtree rollup does not split into distinct parent- and "
        f"child-model buckets: {root_usage!r}"
    )
    expect(parent_model_group).to_be_visible(timeout=10_000)
    expect(child_model_group).to_be_visible(timeout=10_000)
