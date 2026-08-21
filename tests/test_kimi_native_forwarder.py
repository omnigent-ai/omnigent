"""Unit tests for the kimi-native transcript forwarder.

Covers the pure parsing/discovery helpers against kimi's real ``wire.jsonl``
event schema (turn.prompt + content.part + usage.record + llm.request), the
line-offset state round-trip, workspace/recency-based session discovery, and
the usage/model mirroring sync. The live POST loop is exercised by the e2e
gate, not here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from omnigent.kimi_native_forwarder import (
    KimiWireItem,
    _discover_wire,
    _ForwardState,
    _KimiUsageSync,
    _read_state,
    _read_usage_state,
    _row_to_item,
    _UsageState,
    _write_state,
    _write_usage_state,
    clear_kimi_bridge_state,
    forward_kimi_wire_to_session,
    read_kimi_wire_items,
)

#: Sentinel marking "remove this key" in test-row builders.
_ABSENT = object()


class TestRowToItem:
    def test_turn_prompt_is_user(self) -> None:
        row = {
            "type": "turn.prompt",
            "input": [{"type": "text", "text": "what is in this repo?"}],
            "origin": {"kind": "user"},
        }
        item = _row_to_item(4, row)
        assert item is not None
        assert item.role == "user"
        assert item.text == "what is in this repo?"
        assert item.response_id == "kimi:turn:4"

    def test_content_part_text_is_assistant(self) -> None:
        row = {
            "type": "context.append_loop_event",
            "event": {
                "type": "content.part",
                "uuid": "67ce67f7",
                "part": {"type": "text", "text": "This is **Omnigent**."},
            },
        }
        item = _row_to_item(9, row)
        assert item is not None
        assert item.role == "assistant"
        assert item.text == "This is **Omnigent**."
        assert item.response_id == "kimi:67ce67f7"

    def test_think_part_is_reasoning(self) -> None:
        # Reasoning lives in part["think"] (not part["text"]) and is mirrored as a
        # reasoning item, not skipped — the kimi analogue of codex-native #1254.
        row = {
            "type": "context.append_loop_event",
            "event": {
                "type": "content.part",
                "uuid": "abc123",
                "part": {"type": "think", "think": "Let me reason about this."},
            },
        }
        item = _row_to_item(5, row)
        assert item is not None
        assert item.kind == "reasoning"
        assert item.role == "assistant"
        assert item.text == "Let me reason about this."
        assert item.response_id == "kimi:abc123"

    def test_tool_call_and_metadata_skipped(self) -> None:
        for row in (
            {"type": "context.append_loop_event", "event": {"type": "tool.call", "name": "Read"}},
            {"type": "metadata", "protocol_version": 1},
            {"type": "usage.record", "usage": {}},
            {"type": "context.append_message", "message": {"role": "user", "content": []}},
        ):
            assert _row_to_item(0, row) is None

    def test_step_end_with_end_turn_is_turn_end(self) -> None:
        """``end_turn`` is the edge that reports terminal status to the parent."""
        row = {
            "type": "context.append_loop_event",
            "event": {
                "type": "step.end",
                "turnId": "0",
                "step": 3,
                "finishReason": "end_turn",
            },
        }
        item = _row_to_item(28, row)
        assert item is not None
        assert item.kind == "turn_end"
        assert item.response_id == "kimi:turn_end:28"

    def test_step_end_with_tool_use_is_skipped(self) -> None:
        """A step that stopped to call a tool is mid-turn, not a completion."""
        row = {
            "type": "context.append_loop_event",
            "event": {
                "type": "step.end",
                "turnId": "0",
                "step": 1,
                "finishReason": "tool_use",
            },
        }
        assert _row_to_item(12, row) is None

    def test_non_user_turn_prompt_skipped(self) -> None:
        row = {
            "type": "turn.prompt",
            "input": [{"type": "text", "text": "x"}],
            "origin": {"kind": "system"},
        }
        assert _row_to_item(0, row) is None

    def test_usage_record_maps_counts(self) -> None:
        """Pinned Kimi Code 0.34.0 ``usage.record`` shape → a usage item."""
        row = {
            "type": "usage.record",
            "model": "kimi-k3-databricks",
            "usage": {
                "inputOther": 2975,
                "output": 76,
                "inputCacheRead": 17920,
                "inputCacheCreation": 0,
            },
            "usageScope": "turn",
            "time": 1786275843173,
        }
        item = _row_to_item(7, row)
        assert item is not None
        assert item.kind == "usage"
        assert item.usage == {
            "input_other": 2975,
            "output": 76,
            "cache_read": 17920,
            "cache_creation": 0,
        }
        assert item.model == "kimi-k3-databricks"
        assert item.time_ms == 1786275843173
        assert item.response_id == "kimi:usage:7"

    def test_usage_record_non_turn_scope_skipped(self) -> None:
        """Only turn-scoped records carry the session's own spend."""
        row = {
            "type": "usage.record",
            "model": "kimi-k3-databricks",
            "usage": {"inputOther": 10, "output": 1, "inputCacheRead": 0, "inputCacheCreation": 0},
            "usageScope": "aggregate",
            "time": 1786275843173,
        }
        assert _row_to_item(0, row) is None

    def test_usage_record_drift_skips_whole_record(self) -> None:
        """Any deviation from the pinned schema skips the ENTIRE record.

        The cursor advances irreversibly, so partial/zeroed accounting from
        schema drift must never be emitted — emit nothing instead.
        """

        def _row(**overrides: object) -> dict[str, object]:
            usage: dict[str, object] = {
                "inputOther": 10,
                "output": 1,
                "inputCacheRead": 7,
                "inputCacheCreation": 0,
            }
            row: dict[str, object] = {
                "type": "usage.record",
                "model": "kimi-k3-databricks",
                "usage": usage,
                "usageScope": "turn",
                "time": 1786275843173,
            }
            for key, value in overrides.items():
                target = usage if key in usage else row
                if value is _ABSENT:
                    target.pop(key, None)
                else:
                    target[key] = value
            return row

        assert _row_to_item(0, _row()) is not None
        drifted = [
            _row(inputOther="lots"),  # non-int count
            _row(output=-5),  # negative count
            _row(inputCacheRead=True),  # boolean masquerading as a count
            _row(inputCacheCreation=_ABSENT),  # missing count field
            _row(model=_ABSENT),  # missing model
            _row(model=""),  # empty model
            _row(time=_ABSENT),  # missing time
            _row(time="yesterday"),  # invalid time
        ]
        for row in drifted:
            assert _row_to_item(0, row) is None, row

    def test_llm_request_prefers_provider_resolved_model(self) -> None:
        """Pinned 0.34.0 ``llm.request`` shape → a model item on the resolved id."""
        row = {
            "type": "llm.request",
            "kind": "loop",
            "provider": "openai",
            "model": "system.ai.kimi-k3",
            "modelAlias": "kimi-k3-databricks",
            "maxTokens": 65536,
            "time": 1786190562670,
        }
        item = _row_to_item(2, row)
        assert item is not None
        assert item.kind == "model"
        assert item.model == "system.ai.kimi-k3"

    def test_llm_request_falls_back_to_alias(self) -> None:
        row: dict[str, object] = {"type": "llm.request", "modelAlias": "kimi-k3-databricks"}
        item = _row_to_item(0, row)
        assert item is not None
        assert item.model == "kimi-k3-databricks"

    def test_llm_request_without_any_model_skipped(self) -> None:
        assert _row_to_item(0, {"type": "llm.request", "kind": "loop"}) is None


class TestReadNewItems:
    def _wire(self, tmp_path: Path) -> Path:
        def _part(uuid: str, part_type: str, text: str) -> dict[str, object]:
            return {
                "type": "context.append_loop_event",
                "event": {
                    "type": "content.part",
                    "uuid": uuid,
                    "part": {"type": part_type, "text": text},
                },
            }

        rows = [
            {"type": "metadata", "protocol_version": 1},
            {
                "type": "turn.prompt",
                "input": [{"type": "text", "text": "hi"}],
                "origin": {"kind": "user"},
            },
            _part("u1", "think", "…"),
            _part("u2", "text", "hello!"),
        ]
        p = tmp_path / "wire.jsonl"
        p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        return p

    def test_parses_user_and_assistant_only(self, tmp_path: Path) -> None:
        items = read_kimi_wire_items(self._wire(tmp_path), 0)
        assert [(i.role, i.text) for i in items] == [("user", "hi"), ("assistant", "hello!")]

    def test_offset_skips_already_seen(self, tmp_path: Path) -> None:
        wire = self._wire(tmp_path)
        # last_line past the user prompt (line 1) → only the assistant text (line 3).
        items = read_kimi_wire_items(wire, 2)
        assert [(i.role, i.text) for i in items] == [("assistant", "hello!")]
        assert items[0].line_no == 3

    def test_missing_file_is_empty(self, tmp_path: Path) -> None:
        assert read_kimi_wire_items(tmp_path / "nope.jsonl", 0) == []


class TestState:
    def test_cursor_round_trip_and_clear(self, tmp_path: Path) -> None:
        assert _read_state(tmp_path) is None
        _write_state(tmp_path, _ForwardState(wire_path="/x/wire.jsonl", last_line=7))
        loaded = _read_state(tmp_path)
        assert loaded is not None
        assert loaded.wire_path == "/x/wire.jsonl"
        assert loaded.last_line == 7
        clear_kimi_bridge_state(tmp_path)
        assert _read_state(tmp_path) is None

    def test_usage_state_round_trip(self, tmp_path: Path) -> None:
        state = _UsageState(
            totals={"input_other": 100, "output": 20, "cache_read": 50, "cache_creation": 5},
            model="system.ai.kimi-k3",
            posted_model="system.ai.kimi-k3",
            context_tokens=150,
            billed_wire="/x/wire.jsonl",
            billed_line=42,
        )
        _write_usage_state(tmp_path, state)
        loaded, trusted = _read_usage_state(tmp_path)
        assert trusted is True
        assert loaded == state

    def test_usage_state_survives_terminal_recreation(self, tmp_path: Path) -> None:
        """clear_kimi_bridge_state resets only the wire cursor.

        The cumulative usage state belongs to the Omnigent session, not the
        terminal: zeroing it on terminal recreation would make every later
        cumulative post a server-ignored decrease.
        """
        _write_state(tmp_path, _ForwardState(wire_path="/x/wire.jsonl", last_line=7))
        totals = {"input_other": 100, "output": 20, "cache_read": 0, "cache_creation": 0}
        _write_usage_state(tmp_path, _UsageState(totals=dict(totals)))

        clear_kimi_bridge_state(tmp_path)

        assert _read_state(tmp_path) is None
        loaded, _trusted = _read_usage_state(tmp_path)
        assert loaded is not None
        assert loaded.totals == totals


class _RecordingClient:
    """Async httpx-client stub that records POST bodies and returns HTTP 200."""

    def __init__(self, status_code: int = 200) -> None:
        self.posts: list[tuple[str, dict]] = []
        self._status_code = status_code
        self.fail_with: Exception | None = None

    async def post(self, url: str, *, headers: dict, json: dict) -> httpx.Response:
        del headers
        if self.fail_with is not None:
            raise self.fail_with
        self.posts.append((url, json))
        return httpx.Response(self._status_code, request=httpx.Request("POST", url))


def _usage_item(
    line_no: int,
    *,
    input_other: int = 0,
    output: int = 0,
    cache_read: int = 0,
    cache_creation: int = 0,
    model: str = "kimi-k3-databricks",
    time_ms: int = 1786275843173,
) -> KimiWireItem:
    row: dict[str, object] = {
        "type": "usage.record",
        "model": model,
        "usage": {
            "inputOther": input_other,
            "output": output,
            "inputCacheRead": cache_read,
            "inputCacheCreation": cache_creation,
        },
        "usageScope": "turn",
        "time": time_ms,
    }
    item = _row_to_item(line_no, row)
    assert item is not None
    return item


def _sync(
    bridge_dir: Path,
    state: _UsageState | None = None,
    *,
    trusted: bool = True,
    billing_floor_ms: int = 0,
) -> _KimiUsageSync:
    return _KimiUsageSync(
        base_url="http://ap",
        headers={},
        session_id="conv_k",
        bridge_dir=bridge_dir,
        state=state,
        trusted=trusted,
        billing_floor_ms=billing_floor_ms,
    )


def _valid_usage_payload() -> dict:
    """The canonical on-disk usage-state shape the writer emits."""
    return {
        "totals": {"input_other": 10, "output": 2, "cache_read": 3, "cache_creation": 0},
        "model": "system.ai.kimi-k3",
        "posted_model": "system.ai.kimi-k3",
        "context_tokens": 13,
        "billed_wire": "/w/a",
        "billed_line": 5,
    }


def _no_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the usage sync off the real model-catalog/litellm lookup."""
    from omnigent.llms import context_window

    monkeypatch.setattr(context_window, "find_model_context_window", lambda _m, **_kw: None)


def _usage_posts(client: _RecordingClient) -> list[dict]:
    return [b for _u, b in client.posts if b["type"] == "external_session_usage"]


def _model_posts(client: _RecordingClient) -> list[dict]:
    return [b for _u, b in client.posts if b["type"] == "external_model_change"]


class TestUsageSync:
    @pytest.mark.asyncio
    async def test_accumulates_cumulative_totals(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Per-call records sum into cumulative SET-semantics fields.

        ``cumulative_input_tokens`` is INCLUSIVE of cache reads (the server
        splits ``cumulative_cache_read_input_tokens`` back out to price them
        at the cache-read rate) and folds cache-creation into input (no
        dedicated server field). ``context_tokens`` is the LATEST record's
        occupancy, not a sum.
        """
        _no_window(monkeypatch)
        client = _RecordingClient()
        sync = _sync(tmp_path)

        sync.record(_usage_item(1, input_other=20559, output=160), wire="/w/a")
        await sync.sync(client)
        sync.record(_usage_item(2, input_other=2975, output=76, cache_read=17920), wire="/w/a")
        await sync.sync(client)

        posts = _usage_posts(client)
        assert len(posts) == 2
        assert posts[1]["data"] == {
            "cumulative_input_tokens": 20559 + 2975 + 17920,
            "cumulative_cache_read_input_tokens": 17920,
            "cumulative_output_tokens": 160 + 76,
            "context_tokens": 2975 + 17920,
            "model": "kimi-k3-databricks",
        }

    @pytest.mark.asyncio
    async def test_model_rides_along_on_every_usage_post(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The effective model is attached to every token post (the server
        reprices cumulative totals per post and needs it each time), and the
        llm.request-resolved id wins over the record alias."""
        _no_window(monkeypatch)
        client = _RecordingClient()
        sync = _sync(tmp_path)
        sync.note_model("system.ai.kimi-k3")

        sync.record(_usage_item(1, input_other=10, output=1), wire="/w/a")
        await sync.sync(client)
        sync.record(_usage_item(2, input_other=20, output=2), wire="/w/a")
        await sync.sync(client)

        posts = _usage_posts(client)
        assert len(posts) == 2
        assert all(b["data"]["model"] == "system.ai.kimi-k3" for b in posts)

    @pytest.mark.asyncio
    async def test_usage_record_alias_fills_model_until_llm_request(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _no_window(monkeypatch)
        client = _RecordingClient()
        sync = _sync(tmp_path)

        sync.record(
            _usage_item(1, input_other=10, output=1, model="kimi-k3-databricks"), wire="/w/a"
        )
        await sync.sync(client)

        assert _usage_posts(client)[0]["data"]["model"] == "kimi-k3-databricks"

    @pytest.mark.asyncio
    async def test_sync_dedupes_unchanged_totals(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _no_window(monkeypatch)
        client = _RecordingClient()
        sync = _sync(tmp_path)

        sync.record(_usage_item(1, input_other=10, output=1), wire="/w/a")
        await sync.sync(client)
        await sync.sync(client)

        assert len(_usage_posts(client)) == 1

    @pytest.mark.asyncio
    async def test_sync_posts_nothing_before_any_usage(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The per-poll sync must not SET the server's token fields to zero
        before anything was accumulated."""
        _no_window(monkeypatch)
        client = _RecordingClient()
        sync = _sync(tmp_path)

        await sync.sync(client)

        assert client.posts == []

    @pytest.mark.asyncio
    async def test_model_change_posts_once_and_is_not_seeded_at_spawn(
        self, tmp_path: Path
    ) -> None:
        """The FIRST llm.request mirrors the spawn model (baseline never
        seeded — the codex pattern, so the cost gate sees the real model),
        and an unchanged model is not re-posted."""
        client = _RecordingClient()
        sync = _sync(tmp_path)

        sync.note_model("system.ai.kimi-k3")
        await sync.sync(client)
        sync.note_model("system.ai.kimi-k3")
        await sync.sync(client)

        assert client.posts == [
            (
                "http://ap/v1/sessions/conv_k/events",
                {"type": "external_model_change", "data": {"model": "system.ai.kimi-k3"}},
            )
        ]

    @pytest.mark.asyncio
    async def test_model_change_posts_again_on_switch(self, tmp_path: Path) -> None:
        client = _RecordingClient()
        sync = _sync(tmp_path)

        sync.note_model("system.ai.kimi-k3")
        await sync.sync(client)
        sync.note_model("system.ai.kimi-k3-mini")
        await sync.sync(client)

        assert [b["data"]["model"] for b in _model_posts(client)] == [
            "system.ai.kimi-k3",
            "system.ai.kimi-k3-mini",
        ]

    @pytest.mark.asyncio
    async def test_context_window_included_when_resolvable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from omnigent.llms import context_window

        monkeypatch.setattr(context_window, "find_model_context_window", lambda _m, **_kw: 262_144)
        client = _RecordingClient()
        sync = _sync(tmp_path)
        sync.note_model("system.ai.kimi-k3")

        sync.record(_usage_item(1, input_other=10, output=1), wire="/w/a")
        await sync.sync(client)

        assert _usage_posts(client)[-1]["data"]["context_window"] == 262_144

    @pytest.mark.asyncio
    async def test_context_window_omitted_when_unresolvable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An unknown model must OMIT the window — a guessed default would
        draw a wrong context ring."""
        _no_window(monkeypatch)
        client = _RecordingClient()
        sync = _sync(tmp_path)
        sync.note_model("system.ai.kimi-k3")

        sync.record(_usage_item(1, input_other=10, output=1), wire="/w/a")
        await sync.sync(client)

        assert "context_window" not in _usage_posts(client)[-1]["data"]

    @pytest.mark.asyncio
    async def test_post_failure_is_swallowed_and_retried_by_next_sync(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A failed post never raises (it must not stall transcript
        mirroring); the per-poll sync re-posts without new wire records."""
        _no_window(monkeypatch)
        client = _RecordingClient()
        client.fail_with = httpx.ConnectError("boom")
        sync = _sync(tmp_path)

        sync.note_model("system.ai.kimi-k3")
        sync.record(_usage_item(1, input_other=10, output=1), wire="/w/a")
        await sync.sync(client)
        assert client.posts == []

        client.fail_with = None
        await sync.sync(client)
        assert [b["data"]["model"] for b in _model_posts(client)] == ["system.ai.kimi-k3"]
        assert _usage_posts(client)[0]["data"]["cumulative_input_tokens"] == 10

    @pytest.mark.asyncio
    async def test_state_restored_across_restart(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A restarted forwarder resumes the counters AND the model baselines.

        Totals must not reset to zero (later cumulative posts would look like
        decreases); the effective model must not downgrade to the record alias
        when the restart lands between llm.request and usage.record; and an
        unchanged, already-posted model must not be re-posted.
        """
        _no_window(monkeypatch)
        client = _RecordingClient()
        first = _sync(tmp_path)
        first.note_model("system.ai.kimi-k3")
        first.record(
            _usage_item(1, input_other=100, output=20, cache_read=50, cache_creation=5),
            wire="/w/a",
        )
        await first.sync(client)
        client.posts.clear()

        # Restart: a fresh sync built from the persisted usage state.
        sync = _sync(tmp_path, state=_read_usage_state(tmp_path)[0])
        await sync.sync(client)
        # Already-delivered model is not re-posted after restart.
        assert _model_posts(client) == []

        sync.record(_usage_item(9, input_other=10, output=1, model="other-alias"), wire="/w/a")
        await sync.sync(client)

        data = _usage_posts(client)[-1]["data"]
        assert data["cumulative_input_tokens"] == 100 + 50 + 5 + 10
        assert data["cumulative_cache_read_input_tokens"] == 50
        assert data["cumulative_output_tokens"] == 21
        # Attribution stays on the resolved llm.request model, not the alias.
        assert data["model"] == "system.ai.kimi-k3"

    @pytest.mark.asyncio
    async def test_replayed_rows_after_stale_cursor_are_not_rebilled(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Crash between the totals write and the cursor write must not
        double-bill: the billed high-water mark persists WITH the totals, so
        re-reading the same wire rows on restart is idempotent."""
        import logging

        caplog.set_level(logging.WARNING, logger="omnigent.kimi_native_forwarder")
        _no_window(monkeypatch)
        client = _RecordingClient()
        first = _sync(tmp_path)
        first.record(_usage_item(3, input_other=100, output=10), wire="/w/a")
        # Simulated hard kill: the wire cursor was NOT advanced, so a restart
        # replays line 3 into a sync rebuilt from the persisted usage state.
        sync = _sync(tmp_path, state=_read_usage_state(tmp_path)[0])
        sync.record(_usage_item(3, input_other=100, output=10), wire="/w/a")
        await sync.sync(client)

        data = _usage_posts(client)[-1]["data"]
        assert data["cumulative_input_tokens"] == 100
        assert data["cumulative_output_tokens"] == 10
        assert any("already-billed" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_same_millisecond_sibling_record_still_bills(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The high-water mark tiebreaks on the wire line, so a distinct
        record sharing the previous record's timestamp still counts."""
        _no_window(monkeypatch)
        client = _RecordingClient()
        sync = _sync(tmp_path)
        sync.record(_usage_item(3, input_other=100, output=10, time_ms=777), wire="/w/a")
        sync.record(_usage_item(4, input_other=50, output=5, time_ms=777), wire="/w/a")
        await sync.sync(client)

        assert _usage_posts(client)[-1]["data"]["cumulative_input_tokens"] == 150

    @pytest.mark.asyncio
    async def test_billing_floor_is_strictly_launch(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Records stamped even 1ms (or 9s) before launch are a prior
        session's history and never bill (the discovery mtime skew must not
        leak into billing) — and each floor skip leaves a once-per-wire
        diagnostic."""
        import logging

        caplog.set_level(logging.WARNING, logger="omnigent.kimi_native_forwarder")
        _no_window(monkeypatch)
        client = _RecordingClient()
        launch_ms = 1786275843173
        sync = _sync(tmp_path, billing_floor_ms=launch_ms)

        sync.record(
            _usage_item(1, input_other=90000, output=9000, time_ms=launch_ms - 9_000),
            wire="/w/a",
        )
        sync.record(
            _usage_item(2, input_other=99999, output=9999, time_ms=launch_ms - 1),
            wire="/w/a",
        )
        sync.record(_usage_item(3, input_other=11, output=2, time_ms=launch_ms), wire="/w/a")
        await sync.sync(client)

        data = _usage_posts(client)[-1]["data"]
        assert data["cumulative_input_tokens"] == 11
        assert data["cumulative_output_tokens"] == 2
        floor_warnings = [r for r in caplog.records if "pre-launch usage.record" in r.message]
        assert len(floor_warnings) == 1

    @pytest.mark.asyncio
    async def test_failed_post_retries_with_context_tokens_after_restart(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """context_tokens persists with the pending state: a post that failed
        right before a restart retries WITH the context occupancy."""
        _no_window(monkeypatch)
        client = _RecordingClient()
        client.fail_with = httpx.ConnectError("boom")
        first = _sync(tmp_path)
        first.record(_usage_item(1, input_other=10, output=1, cache_read=30), wire="/w/a")
        await first.sync(client)
        assert client.posts == []

        client.fail_with = None
        sync = _sync(tmp_path, state=_read_usage_state(tmp_path)[0])
        await sync.sync(client)

        data = _usage_posts(client)[0]["data"]
        assert data["context_tokens"] == 10 + 30
        assert data["cumulative_input_tokens"] == 40

    @pytest.mark.asyncio
    async def test_stale_mark_never_suppresses_a_different_wire(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The billed mark is scoped to its wire identity.

        Crash right after a wire switch persisted the new cursor: on restart
        the restored mark still names the OLD log, so the new log's rows —
        with smaller line numbers AND earlier timestamps than the old log's
        last-billed row — must still bill.
        """
        _no_window(monkeypatch)
        client = _RecordingClient()
        first = _sync(tmp_path)
        first.record(_usage_item(5, input_other=100, output=10, time_ms=2_000), wire="/w/a")

        sync = _sync(tmp_path, state=_read_usage_state(tmp_path)[0])
        sync.record(_usage_item(0, input_other=50, output=5, time_ms=1_000), wire="/w/b")
        await sync.sync(client)

        data = _usage_posts(client)[-1]["data"]
        assert data["cumulative_input_tokens"] == 150
        assert data["cumulative_output_tokens"] == 15

    @pytest.mark.asyncio
    async def test_future_stamped_row_does_not_suppress_later_rows(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Timestamps gate only the launch floor: one future-stamped row
        (misset clock, later corrected) must not suppress legitimate rows."""
        _no_window(monkeypatch)
        client = _RecordingClient()
        sync = _sync(tmp_path)

        far_future = int(time.time() * 1000) + 10_000_000_000
        sync.record(_usage_item(1, input_other=100, output=10, time_ms=far_future), wire="/w/a")
        sync.record(_usage_item(2, input_other=50, output=5, time_ms=1_000), wire="/w/a")
        await sync.sync(client)

        assert _usage_posts(client)[-1]["data"]["cumulative_input_tokens"] == 150

    @pytest.mark.asyncio
    async def test_persist_failure_never_blocks_delivery_and_state_catches_up(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A failed usage-state write never gates anything (design ruling).

        Delivery flows from the in-memory totals immediately; the per-poll
        sync retries the persist, and the next successful write captures the
        current state. Replay after recovery stays idempotent.
        """
        from omnigent import kimi_native_forwarder as fwd

        _no_window(monkeypatch)
        client = _RecordingClient()
        sync = _sync(tmp_path)
        real_write = fwd._write_usage_state
        monkeypatch.setattr(fwd, "_write_usage_state", lambda _b, _s: False)

        item = _usage_item(3, input_other=100, output=10)
        sync.record(item, wire="/w/a")
        await sync.sync(client)

        # Delivered from in-memory totals despite the failed persist...
        assert _usage_posts(client)[-1]["data"]["cumulative_input_tokens"] == 100
        # ...while nothing was written yet.
        assert _read_usage_state(tmp_path) == (None, True)

        # The write path recovers: the per-poll sync retries the persist
        # without any new wire record.
        monkeypatch.setattr(fwd, "_write_usage_state", real_write)
        await sync.sync(client)
        state, trusted = _read_usage_state(tmp_path)
        assert trusted is True
        assert state is not None
        assert state.totals["input_other"] == 100

        # A replay of the already-billed row stays idempotent.
        sync.record(item, wire="/w/a")
        await sync.sync(client)
        assert _usage_posts(client)[-1]["data"]["cumulative_input_tokens"] == 100

    def test_corrupt_usage_state_starts_fresh(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Confirmed corruption (file reads but definitively fails to parse)
        is a trusted fresh start — re-reading cannot fix it."""
        import logging

        caplog.set_level(logging.WARNING, logger="omnigent.kimi_native_forwarder")
        (tmp_path / "kimi_usage_state.json").write_text("{corrupt", encoding="utf-8")

        state, trusted = _read_usage_state(tmp_path)

        assert state is None
        assert trusted is True
        assert any("starting fresh" in r.message for r in caplog.records)

    def test_empty_usage_state_starts_fresh(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The only state writer is atomic (tmp + replace), so an empty file
        can never be a write in flight: it is confirmed corruption, and
        suspending on it would trap billing forever (nothing writes while
        suspended, so a re-read sees the same empty file)."""
        import logging

        caplog.set_level(logging.WARNING, logger="omnigent.kimi_native_forwarder")
        (tmp_path / "kimi_usage_state.json").write_text("", encoding="utf-8")

        state, trusted = _read_usage_state(tmp_path)

        assert state is None
        assert trusted is True
        assert any("starting fresh" in r.message for r in caplog.records)

    def test_invalid_utf8_usage_state_starts_fresh(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Undecodable bytes are confirmed corruption — a fresh start, not an
        exception that would crash the forwarder into a restart loop."""
        import logging

        caplog.set_level(logging.WARNING, logger="omnigent.kimi_native_forwarder")
        (tmp_path / "kimi_usage_state.json").write_bytes(b"\xff\xfe{}")

        state, trusted = _read_usage_state(tmp_path)

        assert state is None
        assert trusted is True
        assert any("starting fresh" in r.message for r in caplog.records)

    def _assert_starts_fresh(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, payload: dict
    ) -> None:
        import logging

        caplog.set_level(logging.WARNING, logger="omnigent.kimi_native_forwarder")
        (tmp_path / "kimi_usage_state.json").write_text(json.dumps(payload), encoding="utf-8")

        state, trusted = _read_usage_state(tmp_path)

        assert state is None
        assert trusted is True
        assert any("starting fresh" in r.message for r in caplog.records)

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(
                # A LIST of exactly the four counter names set()-compares equal
                # to the expected key set, so only the dict guard rejects it.
                lambda p: p.update(
                    totals=["input_other", "output", "cache_read", "cache_creation"]
                ),
                id="totals-wrong-type",
            ),
            pytest.param(lambda p: p["totals"].update(output=True), id="counter-boolean"),
            pytest.param(lambda p: p["totals"].update(output=-1), id="counter-negative"),
            pytest.param(lambda p: p["totals"].update(output="2"), id="counter-wrong-type"),
            pytest.param(lambda p: p.update(model=7), id="model-wrong-type"),
            pytest.param(lambda p: p.update(model=""), id="model-empty-string"),
            pytest.param(lambda p: p.update(posted_model=7), id="posted-model-wrong-type"),
            pytest.param(lambda p: p.update(posted_model=""), id="posted-model-empty-string"),
            pytest.param(lambda p: p.update(billed_wire=7), id="billed-wire-wrong-type"),
            pytest.param(lambda p: p.update(context_tokens=-1), id="context-tokens-negative"),
            pytest.param(lambda p: p.update(context_tokens="13"), id="context-tokens-wrong-type"),
            pytest.param(lambda p: p.update(context_tokens=True), id="context-tokens-boolean"),
            pytest.param(lambda p: p.update(billed_line="5"), id="billed-line-wrong-type"),
            pytest.param(lambda p: p.update(billed_line=True), id="billed-line-boolean"),
        ],
    )
    def test_schema_invalid_usage_state_starts_fresh(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, mutate: Callable[[dict], object]
    ) -> None:
        """One wrong-typed VALUE in an otherwise canonical payload is
        corruption. Every case keeps the full writer-emitted key set so the
        value-type rules themselves reject it, not the key-set guard —
        silently defaulting the value would trust a baseline that was never
        written (True is an instance of int, so booleans need their own
        rejection)."""
        payload = _valid_usage_payload()
        mutate(payload)
        self._assert_starts_fresh(tmp_path, caplog, payload)

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(lambda p: p.update(totals={"input_other": 10}), id="totals-single-key"),
            pytest.param(lambda p: p["totals"].pop("cache_creation"), id="totals-one-key-missing"),
            pytest.param(lambda p: p["totals"].update(extra=1), id="totals-extra-key"),
            pytest.param(lambda p: p.pop("totals"), id="totals-missing"),
            pytest.param(lambda p: p.pop("billed_line"), id="billed-line-missing"),
            pytest.param(lambda p: p.pop("posted_model"), id="nullable-field-missing"),
            pytest.param(lambda p: p.pop("context_tokens"), id="context-tokens-missing"),
            pytest.param(lambda p: p.update(extra=1), id="top-level-extra-key"),
            pytest.param(lambda p: p.update(billed_wire=None), id="line-without-wire"),
            pytest.param(lambda p: p.update(billed_line=-1), id="wire-without-line"),
            pytest.param(lambda p: p.update(billed_line=-3), id="line-below-sentinel"),
            pytest.param(lambda p: p.update(billed_wire=""), id="empty-wire-with-line"),
        ],
    )
    def test_partial_or_inconsistent_usage_state_starts_fresh(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture, mutate: Callable[[dict], object]
    ) -> None:
        """A missing/extra field, partial totals, or a torn watermark pair is
        corruption, not a trustable baseline: the writer always persists all
        four counters and the watermark as a pair, and trusting a subset
        would zero the missing counters while the watermark suppresses
        re-billing — a permanent undercount that never self-corrects (fresh
        start re-bills forward)."""
        payload = _valid_usage_payload()
        mutate(payload)
        self._assert_starts_fresh(tmp_path, caplog, payload)

    @pytest.mark.asyncio
    async def test_suspended_never_persists_and_adopts_recovered_state(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """While suspended, NOTHING persists — the intact on-disk state must
        not be clobbered by zeroed in-memory values even when writes would
        succeed — and recovery adopts the on-disk baseline.

        Rows seen while suspended are dropped (undercount, clamp-safe); an
        llm.request seen while suspended is newer than the disk model and
        wins on adopt.
        """
        _no_window(monkeypatch)
        client = _RecordingClient()
        prior = _UsageState(
            totals={"input_other": 100, "output": 20, "cache_read": 0, "cache_creation": 0},
            model="system.ai.kimi-k3",
            posted_model="system.ai.kimi-k3",
            context_tokens=42,
            billed_wire="/w/a",
            billed_line=5,
        )
        assert _write_usage_state(tmp_path, prior) is True
        on_disk = (tmp_path / "kimi_usage_state.json").read_text(encoding="utf-8")

        # A transient read failure hid the prior state at startup and persists
        # through the suspended row (recovery re-reads before dropping).
        from omnigent import kimi_native_forwarder as fwd

        real_read = fwd._read_usage_state
        monkeypatch.setattr(fwd, "_read_usage_state", lambda _b: (None, False))
        sync = _sync(tmp_path, state=None, trusted=False)
        sync.note_model("system.ai.kimi-k3-mini")
        sync.record(_usage_item(9, input_other=999, output=99), wire="/w/b")

        # Nothing persisted: the recoverable on-disk state is untouched.
        assert (tmp_path / "kimi_usage_state.json").read_text(encoding="utf-8") == on_disk

        # The per-poll sync re-reads, adopts, and unsuspends: the disk totals
        # are the baseline (the suspended row's 999 was dropped), the newer
        # in-memory model wins, and the already-posted model is not re-posted
        # under its old value.
        monkeypatch.setattr(fwd, "_read_usage_state", real_read)
        await sync.sync(client)
        assert [b["data"]["model"] for b in _model_posts(client)] == ["system.ai.kimi-k3-mini"]
        data = _usage_posts(client)[-1]["data"]
        assert data["cumulative_input_tokens"] == 100
        assert data["cumulative_output_tokens"] == 20
        assert data["context_tokens"] == 42

        # Billing resumed: a new row bills on top of the adopted baseline.
        sync.record(_usage_item(10, input_other=7, output=3), wire="/w/b")
        await sync.sync(client)
        assert _usage_posts(client)[-1]["data"]["cumulative_input_tokens"] == 107

    @pytest.mark.asyncio
    async def test_recovery_before_record_bills_same_poll_rows(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A row arriving after the state file became readable again bills in
        the same poll: recovery runs before the row is consumed, not after."""
        _no_window(monkeypatch)
        client = _RecordingClient()
        prior = _UsageState(
            totals={"input_other": 100, "output": 20, "cache_read": 0, "cache_creation": 0},
        )
        assert _write_usage_state(tmp_path, prior) is True

        # Suspended at startup, but the file is readable by the time the
        # poll's rows arrive.
        sync = _sync(tmp_path, state=None, trusted=False)
        sync.record(_usage_item(1, input_other=7, output=3), wire="/w/a")
        await sync.sync(client)

        data = _usage_posts(client)[-1]["data"]
        assert data["cumulative_input_tokens"] == 107
        assert data["cumulative_output_tokens"] == 23

    @pytest.mark.asyncio
    async def test_model_adopted_at_unsuspend_persists_despite_failed_post(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A model seen while suspended is persisted at unsuspend, before any
        POST: the wire cursor is already past its llm.request row, so a failed
        post followed by a crash must not lose it."""
        _no_window(monkeypatch)
        client = _RecordingClient()
        prior = _UsageState(
            totals={"input_other": 100, "output": 20, "cache_read": 0, "cache_creation": 0},
            model="system.ai.kimi-k3",
            posted_model="system.ai.kimi-k3",
        )
        assert _write_usage_state(tmp_path, prior) is True

        from omnigent import kimi_native_forwarder as fwd

        real_read = fwd._read_usage_state
        monkeypatch.setattr(fwd, "_read_usage_state", lambda _b: (None, False))
        sync = _sync(tmp_path, state=None, trusted=False)
        sync.note_model("system.ai.kimi-k3-mini")

        monkeypatch.setattr(fwd, "_read_usage_state", real_read)
        client.fail_with = httpx.ConnectError("server down")
        await sync.sync(client)

        # A crash here must find the adopted model on disk.
        state, trusted = _read_usage_state(tmp_path)
        assert trusted is True
        assert state is not None
        assert state.model == "system.ai.kimi-k3-mini"
        assert state.posted_model == "system.ai.kimi-k3"

    @pytest.mark.asyncio
    async def test_suspension_persists_until_state_reads_again(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A still-transient read failure keeps billing suspended each poll;
        once the file reads again the suspension lifts and new rows bill."""
        import logging

        caplog.set_level(logging.WARNING, logger="omnigent.kimi_native_forwarder")
        _no_window(monkeypatch)
        client = _RecordingClient()
        # A directory at the state path raises OSError on read: transient.
        state_path = tmp_path / "kimi_usage_state.json"
        state_path.mkdir()
        state, trusted = _read_usage_state(tmp_path)
        assert (state, trusted) == (None, False)

        sync = _sync(tmp_path, state=state, trusted=trusted)
        sync.record(_usage_item(1, input_other=100, output=10), wire="/w/a")
        await sync.sync(client)

        # Still suspended: nothing posted, nothing written (not even a tmp).
        assert _usage_posts(client) == []
        assert not (tmp_path / "kimi_usage_state.json.tmp").exists()
        assert any("dropping usage.record" in r.message for r in caplog.records)

        # The path reads again with a real prior state: recovery adopts it.
        state_path.rmdir()
        _write_usage_state(
            tmp_path,
            _UsageState(
                totals={"input_other": 50, "output": 5, "cache_read": 0, "cache_creation": 0}
            ),
        )
        await sync.sync(client)
        assert _usage_posts(client)[-1]["data"]["cumulative_input_tokens"] == 50

        sync.record(_usage_item(2, input_other=5, output=1), wire="/w/a")
        await sync.sync(client)
        assert _usage_posts(client)[-1]["data"]["cumulative_input_tokens"] == 55

    @pytest.mark.asyncio
    async def test_new_wire_keeps_cumulative_totals(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Adopting a new wire log resets the per-log view only.

        The cumulative totals carry forward: a zero-reset would make every
        later post a decrease the server ignores until the old peak is
        re-crossed.
        """
        _no_window(monkeypatch)
        client = _RecordingClient()
        sync = _sync(tmp_path)
        sync.record(_usage_item(1, input_other=100, output=20), wire="/w/a")
        await sync.sync(client)

        sync.note_new_wire()
        sync.record(_usage_item(1, input_other=7, output=3), wire="/w/b")
        await sync.sync(client)

        data = _usage_posts(client)[-1]["data"]
        assert data["cumulative_input_tokens"] == 107
        assert data["cumulative_output_tokens"] == 23
        # Context occupancy reflects only the new log's latest record.
        assert data["context_tokens"] == 7


class TestDiscoverWire:
    def _make_session(
        self, home: Path, session_dir_name: str, work_dir: str, *, mtime: float
    ) -> Path:
        wire = home / "sessions" / "wd_x" / session_dir_name / "agents" / "main" / "wire.jsonl"
        wire.parent.mkdir(parents=True, exist_ok=True)
        wire.write_text("{}\n", encoding="utf-8")
        import os

        os.utime(wire, (mtime, mtime))
        # session_index keys on the session dir (…/<wd_…>/<session_…>).
        idx = home / "session_index.jsonl"
        index_row = {"sessionDir": str(wire.parent.parent.parent), "workDir": work_dir}
        with idx.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(index_row) + "\n")
        return wire

    def test_picks_newest_matching_workspace(self, tmp_path: Path) -> None:
        home = tmp_path / "kimi-code-home"
        home.mkdir()
        self._make_session(home, "session_old", "/ws", mtime=1000.0)
        newest = self._make_session(home, "session_new", "/ws", mtime=2000.0)
        self._make_session(home, "session_other", "/different", mtime=3000.0)
        found = _discover_wire(home, "/ws", launch_epoch_ms=0)
        assert found == newest

    def test_none_before_any_session(self, tmp_path: Path) -> None:
        home = tmp_path / "kimi-code-home"
        home.mkdir()
        assert _discover_wire(home, "/ws", launch_epoch_ms=0) is None

    def test_ignores_sessions_before_launch(self, tmp_path: Path) -> None:
        home = tmp_path / "kimi-code-home"
        home.mkdir()
        self._make_session(home, "session_stale", "/ws", mtime=1000.0)
        # launch far in the future (ms) → the 1000s-mtime session is below the floor.
        assert _discover_wire(home, "/ws", launch_epoch_ms=9_000_000_000_000) is None


class _FakeAsyncClient:
    """Stands in for the loop's ``httpx.AsyncClient`` context manager.

    Records POST bodies; ``fail_usage_posts`` makes that many
    ``external_session_usage`` posts raise before succeeding, to exercise the
    per-poll retry path.
    """

    def __init__(self, *, fail_usage_posts: int = 0) -> None:
        self.posts: list[dict] = []
        self.fail_usage_posts = fail_usage_posts

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def post(self, url: str, *, headers: dict, json: dict) -> httpx.Response:
        del headers
        if json.get("type") == "external_session_usage" and self.fail_usage_posts > 0:
            self.fail_usage_posts -= 1
            raise httpx.ConnectError("boom")
        self.posts.append(json)
        return httpx.Response(200, request=httpx.Request("POST", url))


def _loop_wire(home: Path, rows: list[dict[str, object]]) -> Path:
    wire = home / "sessions" / "wd_x" / "session_loop" / "agents" / "main" / "wire.jsonl"
    wire.parent.mkdir(parents=True, exist_ok=True)
    wire.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return wire


def _loop_usage_row(*, input_other: int, output: int, time_ms: int) -> dict[str, object]:
    return {
        "type": "usage.record",
        "model": "kimi-k3-databricks",
        "usage": {
            "inputOther": input_other,
            "output": output,
            "inputCacheRead": 0,
            "inputCacheCreation": 0,
        },
        "usageScope": "turn",
        "time": time_ms,
    }


async def _drive_loop(
    monkeypatch: pytest.MonkeyPatch,
    *,
    bridge_dir: Path,
    home: Path,
    client: _FakeAsyncClient,
    launch_epoch_ms: int,
    until: Callable[[], bool],
    timeout_s: float = 10.0,
) -> None:
    """Run the real forwarder loop until *until* holds, then cancel it."""
    from omnigent import kimi_native_forwarder as fwd

    _no_window(monkeypatch)
    monkeypatch.setattr(fwd.httpx, "AsyncClient", lambda **_kw: client)
    task = asyncio.create_task(
        forward_kimi_wire_to_session(
            base_url="http://ap",
            headers={},
            session_id="conv_loop",
            bridge_dir=bridge_dir,
            kimi_home=home,
            workspace="/ws",
            launch_epoch_ms=launch_epoch_ms,
        )
    )
    try:
        async with asyncio.timeout(timeout_s):
            while not until():
                await asyncio.sleep(0.05)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


class TestForwardLoopUsage:
    @pytest.mark.asyncio
    async def test_failed_turn_final_usage_post_retries_without_new_records(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The live loop redelivers a failed usage post on later polls.

        The wire cursor advances past the record even while the post fails, so
        without the per-poll retry an idle session would undercount forever.
        """
        home = tmp_path / "home"
        bridge = tmp_path / "bridge"
        bridge.mkdir()
        now_ms = int(time.time() * 1000)
        _loop_wire(
            home,
            [
                {"type": "llm.request", "kind": "loop", "model": "system.ai.kimi-k3"},
                _loop_usage_row(input_other=42, output=7, time_ms=now_ms),
            ],
        )
        client = _FakeAsyncClient(fail_usage_posts=2)

        await _drive_loop(
            monkeypatch,
            bridge_dir=bridge,
            home=home,
            client=client,
            launch_epoch_ms=0,
            until=lambda: any(b["type"] == "external_session_usage" for b in client.posts),
        )

        usage = next(b for b in client.posts if b["type"] == "external_session_usage")
        assert usage["data"]["cumulative_input_tokens"] == 42
        assert usage["data"]["cumulative_output_tokens"] == 7
        assert usage["data"]["model"] == "system.ai.kimi-k3"
        # The cursor advanced past the record even while its post was failing.
        state = _read_state(bridge)
        assert state is not None
        assert state.last_line >= 2

    @pytest.mark.asyncio
    async def test_pre_launch_usage_history_is_not_billed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Resuming a pre-existing kimi session must not bill its history.

        Only usage stamped at/after the Omnigent launch epoch counts; the
        transcript is still mirrored in full.
        """
        home = tmp_path / "home"
        bridge = tmp_path / "bridge"
        bridge.mkdir()
        now_ms = int(time.time() * 1000)
        # History includes rows only 9s and 1ms before launch — inside the
        # discovery mtime skew, which must NOT leak into billing.
        _loop_wire(
            home,
            [
                _loop_usage_row(input_other=90000, output=9000, time_ms=now_ms - 9_000),
                _loop_usage_row(input_other=99999, output=9999, time_ms=now_ms - 1),
                {
                    "type": "turn.prompt",
                    "input": [{"type": "text", "text": "hi"}],
                    "origin": {"kind": "user"},
                },
                _loop_usage_row(input_other=11, output=2, time_ms=now_ms),
            ],
        )
        client = _FakeAsyncClient()

        await _drive_loop(
            monkeypatch,
            bridge_dir=bridge,
            home=home,
            client=client,
            launch_epoch_ms=now_ms,
            until=lambda: any(b["type"] == "external_session_usage" for b in client.posts),
        )

        usage = next(b for b in client.posts if b["type"] == "external_session_usage")
        assert usage["data"]["cumulative_input_tokens"] == 11
        assert usage["data"]["cumulative_output_tokens"] == 2
        # The pre-launch transcript is still mirrored.
        assert any(b["type"] == "external_conversation_item" for b in client.posts)

    @pytest.mark.asyncio
    async def test_wire_log_switch_carries_totals_forward(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A rediscovered wire log must not zero the cumulative totals.

        The server clamps cumulative fields, so a zero-reset would silently
        drop the new log's usage until it re-crossed the old peak.
        """
        home = tmp_path / "home"
        bridge = tmp_path / "bridge"
        bridge.mkdir()
        now_ms = int(time.time() * 1000)
        wire_a = _loop_wire(home, [_loop_usage_row(input_other=100, output=10, time_ms=now_ms)])
        client = _FakeAsyncClient()

        def _saw_first_post() -> bool:
            return any(b["type"] == "external_session_usage" for b in client.posts)

        def _switch_wire() -> None:
            shutil.rmtree(wire_a.parent.parent.parent)
            wire_b = home / "sessions" / "wd_x" / "session_next" / "agents" / "main" / "wire.jsonl"
            wire_b.parent.mkdir(parents=True, exist_ok=True)
            wire_b.write_text(
                json.dumps(_loop_usage_row(input_other=50, output=5, time_ms=now_ms)) + "\n",
                encoding="utf-8",
            )

        def _saw_combined_total() -> bool:
            if _saw_first_post() and not (home / "sessions" / "wd_x" / "session_next").exists():
                _switch_wire()
            return any(
                b["type"] == "external_session_usage"
                and b["data"]["cumulative_input_tokens"] == 150
                for b in client.posts
            )

        await _drive_loop(
            monkeypatch,
            bridge_dir=bridge,
            home=home,
            client=client,
            launch_epoch_ms=0,
            until=_saw_combined_total,
        )

        combined = [
            b
            for b in client.posts
            if b["type"] == "external_session_usage"
            and b["data"]["cumulative_input_tokens"] == 150
        ]
        assert combined
        assert combined[-1]["data"]["cumulative_output_tokens"] == 15

    @pytest.mark.asyncio
    async def test_transcript_flows_while_usage_state_unwritable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The wire cursor is never gated on usage-state durability.

        With the bridge dir unwritable for usage state, the live loop still
        mirrors the transcript, still delivers usage from in-memory totals,
        and still advances the cursor past every row (design ruling:
        transcript liveness wins over usage durability).
        """
        from omnigent import kimi_native_forwarder as fwd

        home = tmp_path / "home"
        bridge = tmp_path / "bridge"
        bridge.mkdir()
        monkeypatch.setattr(fwd, "_write_usage_state", lambda _b, _s: False)
        now_ms = int(time.time() * 1000)
        _loop_wire(
            home,
            [
                {"type": "llm.request", "kind": "loop", "model": "system.ai.kimi-k3"},
                _loop_usage_row(input_other=42, output=7, time_ms=now_ms),
                {
                    "type": "turn.prompt",
                    "input": [{"type": "text", "text": "hi"}],
                    "origin": {"kind": "user"},
                },
            ],
        )
        client = _FakeAsyncClient()

        def _done() -> bool:
            types = {b["type"] for b in client.posts}
            return {"external_conversation_item", "external_session_usage"} <= types

        await _drive_loop(
            monkeypatch,
            bridge_dir=bridge,
            home=home,
            client=client,
            launch_epoch_ms=0,
            until=_done,
        )

        usage = next(b for b in client.posts if b["type"] == "external_session_usage")
        assert usage["data"]["cumulative_input_tokens"] == 42
        # The cursor advanced past every row despite the failed persists.
        state = _read_state(bridge)
        assert state is not None
        assert state.last_line >= 3


class TestReadWireResilience:
    def test_invalid_utf8_is_replace_decoded_not_raised(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A torn/malformed write must not crash-loop the supervisor — and it
        must leave a (once-per-file) diagnostic rather than vanish silently."""
        import logging

        caplog.set_level(logging.WARNING, logger="omnigent.kimi_native_forwarder")
        wire = tmp_path / "wire.jsonl"
        good = json.dumps(
            {
                "type": "turn.prompt",
                "input": [{"type": "text", "text": "hi"}],
                "origin": {"kind": "user"},
            }
        ).encode()
        wire.write_bytes(b"\xff\xfe garbage \xff\n" + good + b"\n")

        items = read_kimi_wire_items(wire, 0)
        items_again = read_kimi_wire_items(wire, 0)

        assert [(i.role, i.text) for i in items] == [("user", "hi")]
        assert [(i.role, i.text) for i in items_again] == [("user", "hi")]
        warnings = [r for r in caplog.records if "unparseable wire row" in r.message]
        assert len(warnings) == 1

    def test_truncated_json_row_is_skipped_with_diagnostic(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        caplog.set_level(logging.WARNING, logger="omnigent.kimi_native_forwarder")
        wire = tmp_path / "wire.jsonl"
        wire.write_text('{"type": "turn.prom\n', encoding="utf-8")

        assert read_kimi_wire_items(wire, 0) == []
        warnings = [r for r in caplog.records if "unparseable wire row" in r.message]
        assert len(warnings) == 1

    def test_unreadable_file_logs_once_and_returns_empty(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        caplog.set_level(logging.WARNING, logger="omnigent.kimi_native_forwarder")
        missing = tmp_path / "gone" / "wire.jsonl"

        assert read_kimi_wire_items(missing, 0) == []
        assert read_kimi_wire_items(missing, 0) == []

        warnings = [r for r in caplog.records if "cannot read" in r.message]
        assert len(warnings) == 1
