"""Per-model cost/token attribution for the native cumulative usage path.

``_persist_native_cumulative_usage`` receives *cumulative* session totals from
native harnesses (claude-native / codex-native). It must attribute each
report's growth to the currently-active model so per-model buckets hold each
model's own spend and sum to the flat session total across mid-session model
switches — rather than SETting every bucket to the whole running total (which
double-counted the shared baseline once the active model changed).
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import pytest

from omnigent.errors import OmnigentError
from omnigent.llms.context_window import ModelPricing
from omnigent.runtime.policies.builder import load_session_usage
from omnigent.server.routes._sessions.orchestration import (
    _agent_costs,
    _persist_native_cumulative_usage,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)

# Agent ids are stored as 16-byte uuids, so tests use a valid 32-char hex id.
_AGENT_ID = "0123456789abcdef0123456789abcdef"
_CHILD = "c" * 32
_OTHER_CHILD = "d" * 32


def _usage(store: SqlAlchemyConversationStore, conv_id: str) -> dict:
    conv = store.get_conversation(conv_id)
    assert conv is not None
    return dict(conv.session_usage)


@pytest.mark.parametrize("coverage", [{"cost_complete": False}, {}])
@pytest.mark.parametrize("catalog_price", [None, 0.01])
def test_pi_incomplete_cost_does_not_become_a_resume_baseline(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, coverage: dict, catalog_price: float | None
) -> None:
    monkeypatch.setattr(
        "omnigent.llms.context_window.fetch_model_pricing_with_provider",
        lambda *a, **kw: (
            ModelPricing(input_per_token=catalog_price, output_per_token=0)
            if catalog_price is not None
            else None
        ),
    )
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="pi-cost", agent_id=_AGENT_ID)
    _persist_native_cumulative_usage(
        conv.id,
        {
            "cumulative_input_tokens": 100,
            "cumulative_cost_usd": 0.75,
            "cost_complete": True,
            "model": "unpriced-model",
        },
        store,
    )
    assert _usage(store, conv.id)["native_cost_complete"] is True
    _persist_native_cumulative_usage(
        conv.id,
        {"cumulative_input_tokens": 200, **coverage, "model": "unpriced-model"},
        store,
    )
    usage = _usage(store, conv.id)
    assert usage["total_cost_usd"] == (2.0 if catalog_price is not None else 0.75)
    assert usage["input_tokens"] == 200
    assert usage.get("native_cost_complete") is not True
    with pytest.raises(OmnigentError, match="cost_complete must be a bool"):
        _persist_native_cumulative_usage(
            conv.id, {"cumulative_input_tokens": 300, "cost_complete": "true"}, store
        )


@pytest.mark.parametrize("hide_cost", [False, True])
def test_subagent_cost_estimates_stay_display_only(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, hide_cost: bool
) -> None:
    monkeypatch.setenv("OMNIGENT_HIDE_COST", "1" if hide_cost else "0")
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation(title="claude", agent_id=_AGENT_ID)
    child = store.create_conversation(
        kind="sub_agent", title="Explore:a", parent_conversation_id=parent.id, agent_id=_AGENT_ID
    )
    _persist_native_cumulative_usage(
        parent.id, {"cumulative_cost_usd": 1.0, "subagent_costs_usd": {child.id: 0.4}}, store
    )
    # A lower estimate keeps the maximum; a new child merges in.
    _persist_native_cumulative_usage(
        parent.id,
        {"policy_cost_usd": 1.2, "subagent_costs_usd": {child.id: 0.3, _OTHER_CHILD: 0.1}},
        store,
    )

    usage = _usage(store, parent.id)
    assert usage["subagent_costs_usd"] == {child.id: 0.4, _OTHER_CHILD: 0.1}
    # The parent's bill already includes the child's spend; nothing adds it again.
    assert usage["total_cost_usd"] == 1.0
    assert usage["policy_cost_usd"] == 1.2
    assert load_session_usage(parent.id, store)["total_cost_usd"] == 1.0
    tree = store.list_spawn_tree_usage(parent.id, limit=10).data
    costs = {cost.id: cost for cost in _agent_costs(tree, parent.id)}
    assert (costs[parent.id].cost_usd, costs[parent.id].estimated_cost_usd) == (
        None if hide_cost else 1.0,
        None,
    )
    assert (costs[child.id].cost_usd, costs[child.id].estimated_cost_usd) == (
        None,
        None if hide_cost else 0.4,
    )

    _persist_native_cumulative_usage(child.id, {"cumulative_cost_usd": 0.3}, store)
    tree = store.list_spawn_tree_usage(parent.id, limit=10).data
    costs = {cost.id: cost for cost in _agent_costs(tree, parent.id)}
    assert (costs[child.id].cost_usd, costs[child.id].estimated_cost_usd) == (
        None if hide_cost else 0.3,
        None,
    )
    assert _usage(store, parent.id) == usage
    assert _usage(store, child.id)["total_cost_usd"] == 0.3
    totals = load_session_usage(parent.id, store)
    assert totals["total_cost_usd"] == 1.3
    assert totals["policy_cost_usd"] == 1.5


@pytest.mark.parametrize(
    ("estimates", "kept"),
    [
        ([0.1], {}),
        ({_OTHER_CHILD: -1, _CHILD: 0.1}, {_CHILD: 0.1}),
        ({_OTHER_CHILD: float("nan")}, {}),
        ({_OTHER_CHILD: True}, {}),
        # Too large for a float: dropped rather than raising OverflowError.
        ({_OTHER_CHILD: 10**400}, {}),
        # Only conversation ids are stored, which bounds each key's size.
        ({"conv_child": 0.1, "f" * 256: 0.1}, {}),
    ],
)
def test_malformed_subagent_estimates_never_block_authoritative_costs(
    db_uri: str, estimates: object, kept: dict[str, float]
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="claude", agent_id=_AGENT_ID)
    _persist_native_cumulative_usage(
        conv.id,
        {"cumulative_cost_usd": 1.0, "policy_cost_usd": 1.5, "subagent_costs_usd": estimates},
        store,
    )
    usage = _usage(store, conv.id)
    assert (usage["total_cost_usd"], usage["policy_cost_usd"]) == (1.0, 1.5)
    assert usage.get("subagent_costs_usd", {}) == kept


def test_subagent_estimates_past_the_cap_keep_costs_advancing(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="claude", agent_id=_AGENT_ID)
    # The largest stored entries: full-length ids and long fractional costs.
    full = {f"{index:032x}": 123456789.123456 for index in range(200)}
    _persist_native_cumulative_usage(
        conv.id, {"cumulative_cost_usd": 1.0, "subagent_costs_usd": full}, store
    )
    _persist_native_cumulative_usage(
        conv.id,
        {
            "cumulative_cost_usd": 2.0,
            "policy_cost_usd": 2.0,
            "subagent_costs_usd": {**full, f"{0:032x}": 123456790.5, _OTHER_CHILD: 0.2},
        },
        store,
    )
    usage = _usage(store, conv.id)
    assert (usage["total_cost_usd"], usage["policy_cost_usd"]) == (2.0, 2.0)
    assert len(usage["subagent_costs_usd"]) == 200
    assert usage["subagent_costs_usd"][f"{0:032x}"] == 123456790.5
    assert _OTHER_CHILD not in usage["subagent_costs_usd"]
    # Even uncompressed, a full map stays far inside MySQL's 65,535-byte usage column.
    assert len(json.dumps(usage)) < 20_000


def test_per_model_cost_sums_across_model_switch(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="switch", agent_id=_AGENT_ID)

    # Cumulative session cost climbs on opus, then keeps climbing after a
    # /model switch to sonnet (native /cost is a running session total).
    _persist_native_cumulative_usage(
        conv.id, {"cumulative_cost_usd": 3.0, "model": "claude-opus-4-8"}, store
    )
    _persist_native_cumulative_usage(
        conv.id, {"cumulative_cost_usd": 10.80, "model": "claude-opus-4-8"}, store
    )
    _persist_native_cumulative_usage(
        conv.id, {"cumulative_cost_usd": 11.91, "model": "claude-sonnet-4-5"}, store
    )

    usage = _usage(store, conv.id)
    by_model = usage["by_model"]
    # Each model holds only its own spend (opus's growth 0->10.80, sonnet's
    # 10.80->11.91), and the buckets sum to the flat total — the bug fixed here.
    assert usage["total_cost_usd"] == pytest.approx(11.91)
    assert by_model["claude-opus-4-8"]["total_cost_usd"] == pytest.approx(10.80)
    assert by_model["claude-sonnet-4-5"]["total_cost_usd"] == pytest.approx(1.11)
    assert sum(m["total_cost_usd"] for m in by_model.values()) == pytest.approx(11.91)


def test_per_model_cost_accumulates_when_switching_back(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="switch-back", agent_id=_AGENT_ID)

    # A -> B -> A: model A's bucket must accumulate across its two separate
    # active spans (not be overwritten on the second visit), so the returning
    # model's growth adds onto its earlier spend.
    _persist_native_cumulative_usage(
        conv.id, {"cumulative_cost_usd": 10.0, "model": "model-a"}, store
    )
    _persist_native_cumulative_usage(
        conv.id, {"cumulative_cost_usd": 12.50, "model": "model-b"}, store
    )
    _persist_native_cumulative_usage(
        conv.id, {"cumulative_cost_usd": 15.0, "model": "model-a"}, store
    )

    usage = _usage(store, conv.id)
    by_model = usage["by_model"]
    # A = 10.0 (first span) + 2.50 (15.0 - 12.50 on return); B = 12.50 - 10.0.
    assert usage["total_cost_usd"] == pytest.approx(15.0)
    assert by_model["model-a"]["total_cost_usd"] == pytest.approx(12.50)
    assert by_model["model-b"]["total_cost_usd"] == pytest.approx(2.50)
    assert sum(m["total_cost_usd"] for m in by_model.values()) == pytest.approx(15.0)


def test_single_model_bucket_equals_flat_total(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="single", agent_id=_AGENT_ID)

    _persist_native_cumulative_usage(
        conv.id, {"cumulative_cost_usd": 2.0, "model": "claude-opus-4-8"}, store
    )
    _persist_native_cumulative_usage(
        conv.id, {"cumulative_cost_usd": 5.5, "model": "claude-opus-4-8"}, store
    )

    usage = _usage(store, conv.id)
    assert usage["total_cost_usd"] == pytest.approx(5.5)
    assert usage["by_model"]["claude-opus-4-8"]["total_cost_usd"] == pytest.approx(5.5)


def test_lowered_report_does_not_claw_back_bucket(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="rebased", agent_id=_AGENT_ID)

    _persist_native_cumulative_usage(conv.id, {"cumulative_cost_usd": 10.0, "model": "m1"}, store)
    # A forged / rebased lower report: the flat total is monotonic-clamped, and
    # the per-model delta is clamped >= 0, so neither is clawed back.
    _persist_native_cumulative_usage(conv.id, {"cumulative_cost_usd": 2.0, "model": "m1"}, store)

    usage = _usage(store, conv.id)
    assert usage["total_cost_usd"] == pytest.approx(10.0)
    assert usage["by_model"]["m1"]["total_cost_usd"] == pytest.approx(10.0)


def test_per_model_tokens_accumulate_from_cumulative(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="tokens", agent_id=_AGENT_ID)

    # codex-native style: cumulative token totals plus an explicit cost.
    _persist_native_cumulative_usage(
        conv.id,
        {
            "cumulative_input_tokens": 100,
            "cumulative_output_tokens": 20,
            "cumulative_cost_usd": 1.0,
            "model": "gpt-5.6",
        },
        store,
    )
    _persist_native_cumulative_usage(
        conv.id,
        {
            "cumulative_input_tokens": 300,
            "cumulative_output_tokens": 50,
            "cumulative_cost_usd": 3.0,
            "model": "gpt-5.6",
        },
        store,
    )

    usage = _usage(store, conv.id)
    bucket = usage["by_model"]["gpt-5.6"]
    # The bucket accumulates deltas to match the flat cumulative session totals.
    assert bucket["input_tokens"] == usage["input_tokens"] == 300
    assert bucket["output_tokens"] == usage["output_tokens"] == 50
    assert bucket["total_tokens"] == usage["total_tokens"] == 350
    assert bucket["total_cost_usd"] == pytest.approx(3.0)


def test_per_model_tokens_sum_across_model_switch(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="switch-tokens", agent_id=_AGENT_ID)

    # codex-style: cumulative tokens (with explicit cost to avoid the catalog)
    # climb on opus, then keep climbing after a /model switch to sonnet.
    _persist_native_cumulative_usage(
        conv.id,
        {
            "cumulative_input_tokens": 100,
            "cumulative_output_tokens": 20,
            "cumulative_cost_usd": 3.0,
            "model": "claude-opus-4-8",
        },
        store,
    )
    _persist_native_cumulative_usage(
        conv.id,
        {
            "cumulative_input_tokens": 300,
            "cumulative_output_tokens": 60,
            "cumulative_cost_usd": 11.91,
            "model": "claude-sonnet-4-5",
        },
        store,
    )

    usage = _usage(store, conv.id)
    opus = usage["by_model"]["claude-opus-4-8"]
    sonnet = usage["by_model"]["claude-sonnet-4-5"]
    # Each model holds only its own token growth, and the per-model token +
    # cost buckets sum to the flat session totals across the switch.
    assert opus["input_tokens"] == 100
    assert sonnet["input_tokens"] == 200
    assert opus["input_tokens"] + sonnet["input_tokens"] == usage["input_tokens"] == 300
    assert opus["output_tokens"] + sonnet["output_tokens"] == usage["output_tokens"] == 60
    assert opus["total_tokens"] + sonnet["total_tokens"] == usage["total_tokens"] == 360
    assert opus["total_cost_usd"] + sonnet["total_cost_usd"] == pytest.approx(11.91)


def test_token_priced_bucket_matches_flat_cost(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="codex-priced", agent_id=_AGENT_ID)

    # codex-native reports cumulative tokens but NO explicit cost, so cost is
    # computed from the running token totals. Stub pricing so the test does not
    # depend on the live catalog; assert the per-model cost bucket still tracks
    # the flat token-priced total (the elif-has_tokens branch feeds by_model).
    monkeypatch.setattr(
        "omnigent.llms.context_window.fetch_model_pricing",
        lambda model: {"stub": True},
    )
    monkeypatch.setattr(
        "omnigent.llms.context_window.compute_llm_cost",
        lambda usage, pricing: float(usage.get("total_tokens", 0)) * 0.01,
    )

    _persist_native_cumulative_usage(
        conv.id,
        {"cumulative_input_tokens": 200, "cumulative_output_tokens": 50, "model": "gpt-5.6"},
        store,
    )

    usage = _usage(store, conv.id)
    # total_tokens = 200 (input) + 0 (cache) + 50 (output) = 250; cost = 2.50.
    assert usage["total_cost_usd"] == pytest.approx(2.50)
    assert usage["by_model"]["gpt-5.6"]["total_cost_usd"] == pytest.approx(2.50)


def test_token_report_without_cache_field_keeps_persisted_cache_split(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="codex-cache-miss", agent_id=_AGENT_ID)

    monkeypatch.setattr(
        "omnigent.llms.context_window.fetch_model_pricing",
        lambda model: ModelPricing(
            input_per_token=2.5e-6,
            output_per_token=10e-6,
            cache_read_per_token=0.25e-6,
            cache_write_per_token=None,
        ),
    )

    # Cache-heavy turn: 100K cumulative input, 90K of them cache reads.
    _persist_native_cumulative_usage(
        conv.id,
        {
            "cumulative_input_tokens": 100_000,
            "cumulative_cache_read_input_tokens": 90_000,
            "cumulative_output_tokens": 200,
            "model": "gpt-5.6",
        },
        store,
    )
    # Cache-miss turn: the cumulative cached count is unchanged, and the
    # report omits it. The persisted split must carry forward rather than
    # re-billing the 90K cache reads at the full input rate.
    _persist_native_cumulative_usage(
        conv.id,
        {"cumulative_input_tokens": 200_000, "cumulative_output_tokens": 400, "model": "gpt-5.6"},
        store,
    )

    usage = _usage(store, conv.id)
    expected_cost = (110_000 * 2.5 + 90_000 * 0.25 + 400 * 10) / 1_000_000
    assert usage["cache_read_input_tokens"] == 90_000
    assert usage["input_tokens"] == 110_000
    assert usage["total_tokens"] == 200_400
    assert usage["total_cost_usd"] == pytest.approx(expected_cost)
    bucket = usage["by_model"]["gpt-5.6"]
    assert bucket["input_tokens"] == 110_000
    assert bucket["cache_read_input_tokens"] == 90_000
    assert bucket["total_cost_usd"] == pytest.approx(expected_cost)


def test_explicit_zero_cache_count_replaces_persisted_split(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="codex-cache-zero", agent_id=_AGENT_ID)

    # Only token splits are asserted here; keep the token-pricing branch off
    # the live catalog so the test is deterministic and offline.
    monkeypatch.setattr(
        "omnigent.llms.context_window.fetch_model_pricing",
        lambda model: None,
    )

    _persist_native_cumulative_usage(
        conv.id,
        {
            "cumulative_input_tokens": 100_000,
            "cumulative_cache_read_input_tokens": 90_000,
            "cumulative_output_tokens": 200,
            "model": "gpt-5.6",
        },
        store,
    )
    # An explicit zero is a report, not an omission: it is honored as-is.
    _persist_native_cumulative_usage(
        conv.id,
        {
            "cumulative_input_tokens": 120_000,
            "cumulative_cache_read_input_tokens": 0,
            "cumulative_output_tokens": 300,
            "model": "gpt-5.6",
        },
        store,
    )

    usage = _usage(store, conv.id)
    assert usage["cache_read_input_tokens"] == 0
    assert usage["input_tokens"] == 120_000


def test_carried_cache_count_is_clamped_to_a_lowered_input_total(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="codex-cache-lowered", agent_id=_AGENT_ID)

    # Only token splits are asserted here; keep the token-pricing branch off
    # the live catalog so the test is deterministic and offline.
    monkeypatch.setattr(
        "omnigent.llms.context_window.fetch_model_pricing",
        lambda model: None,
    )

    _persist_native_cumulative_usage(
        conv.id,
        {
            "cumulative_input_tokens": 100_000,
            "cumulative_cache_read_input_tokens": 90_000,
            "cumulative_output_tokens": 200,
            "model": "gpt-5.6",
        },
        store,
    )
    # A lowered cumulative total without a cache count keeps the carried split
    # within the reported input, and later reports carry that clamped count.
    _persist_native_cumulative_usage(
        conv.id,
        {"cumulative_input_tokens": 50_000, "cumulative_output_tokens": 250, "model": "gpt-5.6"},
        store,
    )
    assert _usage(store, conv.id)["cache_read_input_tokens"] == 50_000
    assert _usage(store, conv.id)["input_tokens"] == 0

    _persist_native_cumulative_usage(
        conv.id,
        {"cumulative_input_tokens": 200_000, "cumulative_output_tokens": 400, "model": "gpt-5.6"},
        store,
    )
    usage = _usage(store, conv.id)
    assert usage["cache_read_input_tokens"] == 50_000
    assert usage["input_tokens"] == 150_000


def _conversation_point_reads(statements: list[str]) -> list[str]:
    """Filter captured SQL to point reads of a single conversation row."""
    matches: list[str] = []
    for raw in statements:
        normalized = " ".join(raw.split())
        if "FROM conversations" not in normalized or " WHERE " not in normalized:
            continue
        where = normalized.split(" WHERE ", 1)[1]
        if "conversations.id = " in where and "root_conversation_id" not in where:
            matches.append(normalized)
    return matches


def _conversation_tree_scans(statements: list[str]) -> list[str]:
    """Filter captured SQL to page loads of one spawn tree."""
    matches: list[str] = []
    for raw in statements:
        normalized = " ".join(raw.split())
        if "FROM conversations" not in normalized or " WHERE " not in normalized:
            continue
        if "root_conversation_id = " in normalized.split(" WHERE ", 1)[1]:
            matches.append(normalized)
    return matches


@pytest.mark.asyncio
async def test_flush_with_stale_row_keeps_monotonic_clamp(db_uri: str) -> None:
    """A stale caller-held row must not weaken the forged-low-report clamp.

    The events route reads the conversation at request start (authorization)
    and hands that row to the usage flush for the read-only tree walks. If a
    concurrent report persists a higher cost AFTER that read, a forged low
    report on the stale-row request must still be a no-op: the own-usage
    persist baselines its monotonic clamp on its own FRESH read, never on the
    caller's row (otherwise the replay could roll the persisted/enforced cost
    back and double-count the daily rollup delta).
    """
    from omnigent.server.routes._sessions.orchestration import (
        _persist_external_session_usage,
    )
    from omnigent.server.schemas import SessionEventInput

    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="stale-clamp", agent_id=_AGENT_ID)
    # The route's access-check row, read while the persisted cost was $0.
    stale_row = store.get_conversation(conv.id)
    assert stale_row is not None

    # A concurrent legitimate report lands after the access check.
    _persist_native_cumulative_usage(conv.id, {"cumulative_cost_usd": 10.0, "model": "m1"}, store)

    # The stale-row request then delivers a (forged or delayed) lower report.
    await _persist_external_session_usage(
        conv.id,
        SessionEventInput(
            type="external_session_usage",
            data={"cumulative_cost_usd": 2.0, "model": "m1"},
        ),
        store,
        stale_row,
    )

    assert _usage(store, conv.id)["total_cost_usd"] == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_flush_with_supplied_row_skips_tree_root_rereads(db_uri: str) -> None:
    """The flush's read-only tree walks must reuse the supplied row's root.

    With the route's row in hand, the only conversation point read a flush
    may issue is the own-usage persist's fresh clamp baseline — the subtree
    roll-up and the ancestor publish take the root from the supplied row.
    """
    from sqlalchemy import event as sa_event

    from omnigent.db.utils import _engine_cache
    from omnigent.server.routes._sessions.orchestration import (
        _persist_external_session_usage,
    )
    from omnigent.server.schemas import SessionEventInput

    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="flush-reads", agent_id=_AGENT_ID)
    row = store.get_conversation(conv.id)
    assert row is not None

    engine = _engine_cache[db_uri]
    statements: list[str] = []

    def _on_exec(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    sa_event.listen(engine, "before_cursor_execute", _on_exec)
    try:
        await _persist_external_session_usage(
            conv.id,
            SessionEventInput(
                type="external_session_usage",
                data={"cumulative_cost_usd": 3.0, "model": "m1"},
            ),
            store,
            row,
        )
    finally:
        sa_event.remove(engine, "before_cursor_execute", _on_exec)

    point_reads = _conversation_point_reads(statements)
    assert len(point_reads) == 1, (
        f"flush issued {len(point_reads)} conversation point reads (expected "
        f"1: the persist's fresh clamp baseline); statements={point_reads}"
    )
    # The subtree roll-up and the ancestor publish share one tree load; on a
    # large spawn tree each load pages the whole tree.
    tree_scans = _conversation_tree_scans(statements)
    assert len(tree_scans) == 1, (
        f"flush loaded the spawn tree {len(tree_scans)} times (expected 1); "
        f"statements={tree_scans}"
    )
    assert _usage(store, conv.id)["total_cost_usd"] == pytest.approx(3.0)


@dataclass
class _UsageTree:
    """root -> {mid -> leaf -> grandchild, sibling}; each node holds known spend."""

    root: str
    mid: str
    sibling: str
    leaf: str
    grandchild: str


def _seed_usage_tree(store: SqlAlchemyConversationStore) -> _UsageTree:
    root = store.create_conversation(title="root", agent_id=_AGENT_ID)
    mid = store.create_conversation(kind="sub_agent", parent_conversation_id=root.id)
    sibling = store.create_conversation(kind="sub_agent", parent_conversation_id=root.id)
    leaf = store.create_conversation(kind="sub_agent", parent_conversation_id=mid.id)
    grandchild = store.create_conversation(kind="sub_agent", parent_conversation_id=leaf.id)
    # Powers of two give every subtree a unique total, so summing the wrong
    # subtree (sibling included, grandchild dropped) changes the number.
    for conv, cost, tokens in (
        (root, 1.0, 10),
        (mid, 2.0, 20),
        (sibling, 8.0, 80),
        (leaf, 4.0, 40),
        (grandchild, 16.0, 160),
    ):
        store.set_session_usage(
            conv.id,
            {
                "total_cost_usd": cost,
                "input_tokens": tokens,
                "by_model": {"m1": {"input_tokens": tokens, "total_cost_usd": cost}},
            },
        )
    return _UsageTree(root.id, mid.id, sibling.id, leaf.id, grandchild.id)


class _UsageRecorder:
    """Stands in for ``session_stream``, keeping only ``session.usage`` events."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def publish(self, session_id: str, event: dict) -> None:
        if event.get("type") == "session.usage":
            assert event["conversation_id"] == session_id
            self.events.append(event)


@pytest.fixture
def usage_events(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    from omnigent.server.routes import sessions as sessions_facade

    recorder = _UsageRecorder()
    # Orchestration reaches ``session_stream`` through the facade at call time.
    monkeypatch.setattr(sessions_facade, "session_stream", recorder)
    return recorder.events


@contextmanager
def _captured_sql(db_uri: str) -> Iterator[list[str]]:
    from sqlalchemy import event as sa_event

    from omnigent.db.utils import _engine_cache

    engine = _engine_cache[db_uri]
    statements: list[str] = []

    def _on_exec(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    sa_event.listen(engine, "before_cursor_execute", _on_exec)
    try:
        yield statements
    finally:
        sa_event.remove(engine, "before_cursor_execute", _on_exec)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("caller_row", "expected_tree_scans"),
    [
        pytest.param("fresh", 1, id="fresh-row"),
        pytest.param("absent", 1, id="no-row"),
        # The row predates a re-parent and claims the child is top-level, so its
        # root hint names a tree the child is not in: the verified load
        # re-resolves the root, and the ancestors must come from the fresh tree.
        pytest.param("stale-top-level", 2, id="stale-row-claims-top-level"),
    ],
)
async def test_child_flush_publishes_every_ancestor_from_one_tree_load(
    db_uri: str,
    usage_events: list[dict],
    caller_row: str,
    expected_tree_scans: int,
) -> None:
    """A sub-agent's flush re-publishes each ancestor's subtree from its own tree read."""
    from omnigent.server.routes._sessions.orchestration import (
        _persist_external_session_usage,
    )
    from omnigent.server.schemas import SessionEventInput

    store = SqlAlchemyConversationStore(db_uri)
    tree = _seed_usage_tree(store)
    row = store.get_conversation(tree.leaf)
    assert row is not None
    conv_arg = {
        "fresh": row,
        "absent": None,
        "stale-top-level": dataclasses.replace(
            row, root_conversation_id=row.id, parent_conversation_id=None
        ),
    }[caller_row]

    with _captured_sql(db_uri) as statements:
        await _persist_external_session_usage(
            tree.leaf,
            SessionEventInput(
                type="external_session_usage",
                data={"context_tokens": 1234, "cumulative_cost_usd": 4.5, "model": "m1"},
            ),
            store,
            conv_arg,
        )

    tree_scans = _conversation_tree_scans(statements)
    assert len(tree_scans) == expected_tree_scans, tree_scans
    # leaf = 4.5 + grandchild 16; mid adds 2; root adds 1 + sibling 8. The
    # sibling gets no event, and context_tokens rides only on the leaf's own.
    assert [(e["conversation_id"], e["total_cost_usd"]) for e in usage_events] == [
        (tree.leaf, 20.5),
        (tree.mid, 22.5),
        (tree.root, 31.5),
    ]
    assert [e.get("context_tokens") for e in usage_events] == [1234, None, None]
    assert [e["usage_by_model"]["m1"]["input_tokens"] for e in usage_events] == [200, 220, 310]


@pytest.mark.asyncio
async def test_relay_turn_completion_publishes_every_ancestor_from_one_tree_load(
    db_uri: str,
    usage_events: list[dict],
) -> None:
    """A relayed ``response.completed`` rolls usage up its ancestors from one tree read."""
    import httpx

    from omnigent.server.routes._sessions.orchestration import _relay_runner_stream_once

    store = SqlAlchemyConversationStore(db_uri)
    tree = _seed_usage_tree(store)
    events = [
        {"type": "response.in_progress", "response": {"id": "turn-1", "model": "m1"}},
        {
            "type": "response.completed",
            "response": {
                "id": "turn-1",
                "usage": {"input_tokens": 100, "output_tokens": 0, "cost_usd": 0.5, "model": "m1"},
            },
        },
    ]
    body = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"
    # Only the runner is faked; event handling and the store stay real.
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=body))

    with _captured_sql(db_uri) as statements:
        async with httpx.AsyncClient(
            base_url="http://runner.example", transport=transport
        ) as runner:
            await _relay_runner_stream_once(tree.leaf, runner, store)

    tree_scans = _conversation_tree_scans(statements)
    assert len(tree_scans) == 1, tree_scans
    assert [(e["conversation_id"], e["total_cost_usd"]) for e in usage_events] == [
        (tree.leaf, 20.5),
        (tree.mid, 22.5),
        (tree.root, 31.5),
    ]
    assert [e["usage_by_model"]["m1"]["input_tokens"] for e in usage_events] == [300, 320, 410]


@pytest.mark.asyncio
async def test_flush_telemetry_reports_the_callers_installation_id(db_uri: str) -> None:
    """The usage event names the posting machine from its request header only.

    The header is what makes the attribution free: the server never looks the
    host up, so a flush from a client that sends no header leaves the field
    unset rather than costing a read.
    """
    from unittest.mock import patch

    from omnigent.server.routes._sessions.orchestration import (
        _persist_external_session_usage,
    )
    from omnigent.server.schemas import SessionEventInput

    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(title="flush-telemetry", agent_id=_AGENT_ID)
    row = store.get_conversation(conv.id)
    assert row is not None

    events: list = []
    with patch(
        "omnigent.server.routes._sessions.orchestration._tel_emit",
        side_effect=events.append,
    ):
        await _persist_external_session_usage(
            conv.id,
            SessionEventInput(
                type="external_session_usage",
                data={"cumulative_cost_usd": 3.0, "model": "m1"},
            ),
            store,
            row,
            "alice@example.com",
            "inst-host-9",
        )
        await _persist_external_session_usage(
            conv.id,
            SessionEventInput(
                type="external_session_usage",
                data={"cumulative_cost_usd": 4.0, "model": "m1"},
            ),
            store,
            row,
            "alice@example.com",
        )

    assert [e.host_installation_id for e in events] == ["inst-host-9", None]
