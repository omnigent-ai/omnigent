"""Unit tests for the kimi-native transcript forwarder.

Covers the pure parsing/discovery helpers against kimi's real ``wire.jsonl``
event schema (turn.prompt + content.part + usage.record + llm.request,
including the sanitized real-session fixtures under
``tests/fixtures/kimi_wire``), the byte-offset state round-trip, strict
launch-epoch session discovery, the usage/model mirroring sync, and the
forward loop's lifecycle edges (``turn.ended`` records, pane death,
quiescence with in-flight tool suppression, transient-vs-poison POST
rejections, edge dedupe across restarts) with the POST helpers stubbed out.
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

import omnigent._native_forwarder_health as forwarder_health
import omnigent.kimi_native_forwarder as fwd
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
    read_new_kimi_wire_items,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "kimi_wire"

#: Sentinel marking "remove this key" in test-row builders.
_ABSENT = object()

_SEG_TOKEN_KEYS = ("input_other", "output", "cache_read", "cache_creation")


def _segment(
    *,
    input_other: int = 0,
    output: int = 0,
    cache_read: int = 0,
    cache_creation: int = 0,
    cost_usd: float = 0.0,
    priced: dict[str, int] | None = None,
) -> dict:
    """A writer-shaped per-model segment (tokens + accrued cost + snapshot)."""
    seg: dict = {
        "input_other": input_other,
        "output": output,
        "cache_read": cache_read,
        "cache_creation": cache_creation,
        "cost_usd": cost_usd,
        "priced": dict(priced) if priced is not None else dict.fromkeys(_SEG_TOKEN_KEYS, 0),
    }
    return seg


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

    def test_turn_prompt_carries_lifecycle_timestamp(self) -> None:
        row = {
            "type": "turn.prompt",
            "input": [{"type": "text", "text": "resume"}],
            "origin": {"kind": "user"},
            "time": 1_786_275_843_173,
        }

        item = _row_to_item(3, row)

        assert item is not None
        assert item.time_ms == 1_786_275_843_173

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

    def test_metadata_rows_skipped(self) -> None:
        for row in (
            {"type": "metadata", "protocol_version": 1},
            {"type": "usage.record", "usage": {}},
            {"type": "context.append_message", "message": {"role": "user", "content": []}},
            {"type": "turn.cancel", "turnId": 0, "reason": "user_cancelled"},
        ):
            assert _row_to_item(0, row) is None

    def test_tool_call_and_result_are_bookkeeping_items(self) -> None:
        """Tool events are never posted but tracked for the quiescence gate."""
        call = {
            "type": "context.append_loop_event",
            "event": {"type": "tool.call", "uuid": "t1", "name": "Read"},
        }
        result = {
            "type": "context.append_loop_event",
            "event": {"type": "tool.result", "parentUuid": "t1"},
        }
        call_item = _row_to_item(7, call)
        result_item = _row_to_item(8, result)
        assert call_item is not None and call_item.kind == "tool_call"
        assert result_item is not None and result_item.kind == "tool_result"

    def test_step_end_never_drives_a_turn_edge(self) -> None:
        """Edges come from turn.ended: failed/cancelled turns write no step.end."""
        for reason in ("end_turn", "length", "tool_use", "error", "abort"):
            row = {
                "type": "context.append_loop_event",
                "event": {"type": "step.end", "turnId": "0", "step": 1, "finishReason": reason},
            }
            assert _row_to_item(16, row) is None, reason

    def test_turn_ended_completed_is_turn_end(self) -> None:
        row = {"type": "turn.ended", "turnId": 0, "reason": "completed", "durationMs": 174932}
        item = _row_to_item(31, row)
        assert item is not None
        assert item.kind == "turn_end"
        assert item.response_id == "kimi:turn_end:31"

    def test_turn_ended_cancelled_is_turn_end(self) -> None:
        row = {"type": "turn.ended", "turnId": 0, "reason": "cancelled", "durationMs": 1112}
        item = _row_to_item(10, row)
        assert item is not None
        assert item.kind == "turn_end"

    def test_turn_ended_failed_is_turn_failed_with_error_message(self) -> None:
        row = {
            "type": "turn.ended",
            "turnId": 0,
            "reason": "failed",
            "error": {"code": "provider.auth_error", "message": "401 Invalid Token"},
        }
        item = _row_to_item(9, row)
        assert item is not None
        assert item.kind == "turn_failed"
        assert item.text == "401 Invalid Token"
        assert item.response_id == "kimi:turn_failed:9"

    def test_turn_ended_unknown_reason_keeps_turn_open(self) -> None:
        """Unknown vocabulary never fails open to 'failed'; the fallbacks close it."""
        row = {"type": "turn.ended", "turnId": 0, "reason": "paused"}
        assert _row_to_item(9, row) is None

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


class TestRealWireFixtures:
    """Sanitized real kimi 0.34 wire logs are the ground truth for turn edges."""

    def test_completed_turn_ends_with_single_idle_edge(self) -> None:
        items = read_kimi_wire_items(_FIXTURES / "completed.jsonl", 0)
        kinds = [i.kind for i in items]
        assert kinds[-1] == "turn_end"
        assert kinds.count("turn_end") == 1
        assert "turn_failed" not in kinds
        assert kinds.count("tool_call") == kinds.count("tool_result") == 3

    def test_failed_turn_emits_failed_edge_without_any_step_end(self) -> None:
        items = read_kimi_wire_items(_FIXTURES / "failed.jsonl", 0)
        kinds = [i.kind for i in items]
        # The llm.request row also yields a "model" item (usage/cost mirror).
        assert kinds == ["message", "model", "turn_failed"]
        assert items[0].role == "user"
        assert items[-1].text == "401 Invalid Token"

    def test_cancelled_turns_each_emit_an_idle_edge(self) -> None:
        items = read_kimi_wire_items(_FIXTURES / "cancelled.jsonl", 0)
        kinds = [i.kind for i in items]
        assert kinds.count("turn_end") == 2
        assert "turn_failed" not in kinds
        assert sum(1 for i in items if i.kind == "message" and i.role == "user") == 2


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
        _write_state(
            tmp_path,
            _ForwardState(
                wire_path="/x/wire.jsonl",
                last_line=7,
                offset=345,
                turn_open=True,
                tools_in_flight=2,
                last_edge_id="kimi:turn_end:5",
                dropped_edge_status="failed",
                last_activity_ts=1_700_000_000.5,
                last_seen_offset=400,
            ),
        )
        loaded = _read_state(tmp_path)
        assert loaded is not None
        assert loaded.wire_path == "/x/wire.jsonl"
        assert loaded.last_line == 7
        assert loaded.offset == 345
        assert loaded.turn_open is True
        assert loaded.tools_in_flight == 2
        assert loaded.last_edge_id == "kimi:turn_end:5"
        assert loaded.dropped_edge_status == "failed"
        assert loaded.last_activity_ts == 1_700_000_000.5
        assert loaded.last_seen_offset == 400
        clear_kimi_bridge_state(tmp_path)
        assert _read_state(tmp_path) is None

    def test_state_without_offset_is_discarded(self, tmp_path: Path) -> None:
        # No shipped build ever wrote line-only state; treat it as no state at
        # all so the tail restarts cleanly instead of migrating.
        (tmp_path / "kimi_forwarder.json").write_text(
            json.dumps({"wire_path": "/x/wire.jsonl", "last_line": 3}), encoding="utf-8"
        )
        assert _read_state(tmp_path) is None

    def test_lifecycle_fields_default_when_absent(self, tmp_path: Path) -> None:
        (tmp_path / "kimi_forwarder.json").write_text(
            json.dumps({"wire_path": "/x/wire.jsonl", "last_line": 3, "offset": 10}),
            encoding="utf-8",
        )
        loaded = _read_state(tmp_path)
        assert loaded is not None
        assert loaded.turn_open is False
        assert loaded.tools_in_flight == 0
        assert loaded.last_edge_id is None
        assert loaded.last_assistant_text == ""

    def test_last_assistant_text_round_trips(self, tmp_path: Path) -> None:
        _write_state(
            tmp_path,
            _ForwardState(
                wire_path="/x/wire.jsonl",
                last_line=2,
                offset=64,
                turn_open=True,
                last_assistant_text="partial result",
            ),
        )
        loaded = _read_state(tmp_path)
        assert loaded is not None
        assert loaded.last_assistant_text == "partial result"


class TestReadNewIncremental:
    def _write_rows(self, path: Path, rows: list[dict[str, object]], *, partial: str = "") -> None:
        text = "".join(json.dumps(r) + "\n" for r in rows) + partial
        path.write_text(text, encoding="utf-8")

    def _prompt(self, text: str) -> dict[str, object]:
        return {
            "type": "turn.prompt",
            "input": [{"type": "text", "text": text}],
            "origin": {"kind": "user"},
        }

    def test_reads_only_past_offset_and_resumes(self, tmp_path: Path) -> None:
        wire = tmp_path / "wire.jsonl"
        self._write_rows(wire, [{"type": "metadata"}, self._prompt("hi")])
        items, offset, line = read_new_kimi_wire_items(wire, 0, 0)
        assert [(i.role, i.text) for i in items] == [("user", "hi")]
        assert offset == wire.stat().st_size
        assert line == 2
        assert items[0].offset_after == offset
        # Append one more row; the next read starts at the persisted cursor.
        with wire.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(self._prompt("again")) + "\n")
        items, offset2, line2 = read_new_kimi_wire_items(wire, offset, line)
        assert [(i.text, i.line_no) for i in items] == [("again", 2)]
        assert offset2 == wire.stat().st_size
        assert line2 == 3

    def test_partial_trailing_line_left_for_next_poll(self, tmp_path: Path) -> None:
        wire = tmp_path / "wire.jsonl"
        self._write_rows(wire, [self._prompt("hi")], partial='{"type": "turn.pro')
        items, offset, line = read_new_kimi_wire_items(wire, 0, 0)
        assert len(items) == 1
        assert offset < wire.stat().st_size
        assert line == 1

    def test_dead_writer_final_line_is_consumed_without_newline(self, tmp_path: Path) -> None:
        wire = tmp_path / "wire.jsonl"
        wire.write_text(json.dumps(self._prompt("final")), encoding="utf-8")

        items, offset, line = read_new_kimi_wire_items(wire, 0, 0, include_unterminated=True)

        assert [(item.text, item.line_no) for item in items] == [("final", 0)]
        assert offset == wire.stat().st_size
        assert line == 1

    def test_truncated_file_restarts_tail(self, tmp_path: Path) -> None:
        wire = tmp_path / "wire.jsonl"
        self._write_rows(wire, [self._prompt("fresh")])
        items, offset, line = read_new_kimi_wire_items(wire, 10_000, 42)
        assert [(i.text, i.line_no) for i in items] == [("fresh", 0)]
        assert offset == wire.stat().st_size
        assert line == 1

    def test_missing_file_is_noop(self, tmp_path: Path) -> None:
        assert read_new_kimi_wire_items(tmp_path / "nope.jsonl", 5, 1) == ([], 5, 1)

    def test_usage_state_round_trip(self, tmp_path: Path) -> None:
        state = _UsageState(
            totals={"input_other": 100, "output": 20, "cache_read": 50, "cache_creation": 5},
            model="system.ai.kimi-k3",
            posted_model="system.ai.kimi-k3",
            context_tokens=150,
            billed={"/x/wire.jsonl": 42},
            by_model={
                "system.ai.kimi-k3": _segment(
                    input_other=100, output=20, cache_read=50, cache_creation=5
                )
            },
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
        _write_state(tmp_path, _ForwardState(wire_path="/x/wire.jsonl", last_line=7, offset=0))
        totals = {"input_other": 100, "output": 20, "cache_read": 0, "cache_creation": 0}
        _write_usage_state(
            tmp_path,
            _UsageState(totals=dict(totals), by_model={"system.ai.kimi-k3": _segment(**totals)}),
        )

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


def _usage_row(
    *,
    input_other: int = 0,
    output: int = 0,
    cache_read: int = 0,
    cache_creation: int = 0,
    model: str = "kimi-k3-databricks",
    time_ms: int = 1786275843173,
) -> dict[str, object]:
    """A pinned-schema ``usage.record`` wire row."""
    return {
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
    item = _row_to_item(
        line_no,
        _usage_row(
            input_other=input_other,
            output=output,
            cache_read=cache_read,
            cache_creation=cache_creation,
            model=model,
            time_ms=time_ms,
        ),
    )
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
        "billed": {"/w/a": 5},
        "by_model": {"system.ai.kimi-k3": _segment(input_other=10, output=2, cache_read=3)},
    }


def _no_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the usage sync off the real model-catalog/litellm lookups.

    Also stubs pricing to "unresolvable" so payload assertions stay
    cost-free; cost tests override ``fetch_model_pricing`` afterwards.
    """
    from omnigent.llms import context_window

    monkeypatch.setattr(context_window, "find_model_context_window", lambda _m, **_kw: None)
    monkeypatch.setattr(context_window, "fetch_model_pricing", lambda _m: None)


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
    async def test_context_window_lookup_retries_after_transient_failure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from omnigent.llms import context_window

        attempts = 0

        def _lookup(_model: str) -> int:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("catalog temporarily unavailable")
            return 262_144

        monkeypatch.setattr(context_window, "find_model_context_window", _lookup)
        sync = _sync(tmp_path)
        sync.note_model("system.ai.kimi-k3")

        assert await sync._context_window() is None
        assert await sync._context_window() == 262_144
        assert attempts == 2

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
            pytest.param(
                # JSON stringifies non-string keys, so a wrong-typed KEY is
                # unrepresentable on disk; a float line is the analogous rot.
                lambda p: p.update(billed={"/w/a": 5.5}),
                id="billed-line-float",
            ),
            pytest.param(lambda p: p.update(context_tokens=-1), id="context-tokens-negative"),
            pytest.param(lambda p: p.update(context_tokens="13"), id="context-tokens-wrong-type"),
            pytest.param(lambda p: p.update(context_tokens=True), id="context-tokens-boolean"),
            pytest.param(lambda p: p.update(billed={"/w/a": "5"}), id="billed-line-wrong-type"),
            pytest.param(lambda p: p.update(billed={"/w/a": True}), id="billed-line-boolean"),
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
            pytest.param(lambda p: p.pop("billed"), id="billed-missing"),
            pytest.param(lambda p: p.pop("posted_model"), id="nullable-field-missing"),
            pytest.param(lambda p: p.pop("context_tokens"), id="context-tokens-missing"),
            pytest.param(lambda p: p.update(extra=1), id="top-level-extra-key"),
            pytest.param(lambda p: p.update(billed="not-a-dict"), id="billed-not-a-dict"),
            pytest.param(lambda p: p.update(billed={"/w/a": -1}), id="billed-negative-line"),
            pytest.param(lambda p: p.update(billed={"": 5}), id="billed-empty-wire-key"),
            pytest.param(lambda p: p.pop("by_model"), id="by-model-missing"),
            pytest.param(lambda p: p.update(by_model={}), id="by-model-sum-mismatch"),
            pytest.param(
                lambda p: p["by_model"].update({"": p["by_model"].pop("system.ai.kimi-k3")}),
                id="by-model-empty-key",
            ),
            pytest.param(
                lambda p: p["by_model"]["system.ai.kimi-k3"].pop("cache_read"),
                id="by-model-partial-segment",
            ),
            pytest.param(
                lambda p: p["by_model"]["system.ai.kimi-k3"].pop("cost_usd"),
                id="segment-cost-missing",
            ),
            pytest.param(
                lambda p: p["by_model"]["system.ai.kimi-k3"].update(cost_usd=-0.5),
                id="segment-cost-negative",
            ),
            pytest.param(
                lambda p: p["by_model"]["system.ai.kimi-k3"].update(cost_usd=float("nan")),
                id="segment-cost-nan",
            ),
            pytest.param(
                lambda p: p["by_model"]["system.ai.kimi-k3"]["priced"].pop("output"),
                id="segment-priced-partial",
            ),
            pytest.param(
                # priced ahead of the tokens would under-price later deltas.
                lambda p: p["by_model"]["system.ai.kimi-k3"]["priced"].update(input_other=999),
                id="segment-priced-ahead",
            ),
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
            billed={"/w/a": 5},
            by_model={"system.ai.kimi-k3": _segment(input_other=100, output=20)},
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
            by_model={"system.ai.kimi-k3": _segment(input_other=100, output=20)},
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
            by_model={"system.ai.kimi-k3": _segment(input_other=100, output=20)},
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
                totals={"input_other": 50, "output": 5, "cache_read": 0, "cache_creation": 0},
                by_model={"system.ai.kimi-k3": _segment(input_other=50, output=5)},
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

    @pytest.mark.asyncio
    async def test_mid_session_model_switch_prices_each_segment(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Cost is the sum over model segments, priced at each segment's model.

        Repricing the WHOLE cumulative total at the current model would make
        an expensive→cheap switch report a LOWER total (the server's
        monotonic clamp then bills the later turns as $0) and overcharge a
        cheap→expensive switch.
        """
        from omnigent.llms import context_window
        from omnigent.llms.context_window import ModelPricing

        _no_window(monkeypatch)
        rates = {
            "system.ai.kimi-k3": ModelPricing(input_per_token=3e-6, output_per_token=15e-6),
            "system.ai.kimi-k2.7": ModelPricing(input_per_token=0.95e-6, output_per_token=4e-6),
        }
        monkeypatch.setattr(context_window, "fetch_model_pricing", rates.get)
        client = _RecordingClient()
        sync = _sync(tmp_path)

        sync.note_model("system.ai.kimi-k3")
        sync.record(_usage_item(1, input_other=1_000_000), wire="/w/a")
        await sync.sync(client)
        first = _usage_posts(client)[-1]["data"]
        assert first["cumulative_cost_usd"] == pytest.approx(3.00)

        sync.note_model("system.ai.kimi-k2.7")
        sync.record(_usage_item(2, input_other=1_000_000), wire="/w/a")
        await sync.sync(client)
        second = _usage_posts(client)[-1]["data"]
        # Segment-priced: $3.00 (K3 meg) + $0.95 (K2.7 meg) — NOT 2M @ K2.7
        # ($1.90, a decrease the server would clamp away).
        assert second["cumulative_cost_usd"] == pytest.approx(3.95)
        assert second["cumulative_cost_usd"] > first["cumulative_cost_usd"]

    def test_interleaved_requests_attribute_each_usage_to_its_request(
        self, tmp_path: Path
    ) -> None:
        sync = _sync(tmp_path)
        sync.note_model("system.ai.kimi-k3")
        sync.note_model("system.ai.kimi-k2.7")

        sync.record(_usage_item(2, input_other=30, model="kimi-k3-databricks"), wire="/w/a")
        sync.record(_usage_item(3, input_other=9, model="kimi-k2.7-databricks"), wire="/w/a")

        state, trusted = _read_usage_state(tmp_path)
        assert trusted is True and state is not None
        assert state.by_model["system.ai.kimi-k3"]["input_other"] == 30
        assert state.by_model["system.ai.kimi-k2.7"]["input_other"] == 9

    def test_wire_restart_discards_pending_model_attribution(self, tmp_path: Path) -> None:
        sync = _sync(tmp_path)
        sync.note_model("system.ai.kimi-k3")

        sync.note_wire_restarted("/w/a")
        sync.note_model("system.ai.kimi-k2.7")
        sync.record(_usage_item(1, input_other=9), wire="/w/a")

        state, trusted = _read_usage_state(tmp_path)
        assert trusted is True and state is not None
        assert state.by_model == {"system.ai.kimi-k2.7": _segment(input_other=9)}

    @pytest.mark.asyncio
    async def test_cost_omitted_when_any_segment_unpriceable(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """One unpriceable segment omits the client cost entirely.

        A partial sum would under-report; omitting defers to the server's
        current-model token pricing (the pre-segment behavior)."""
        from omnigent.llms import context_window
        from omnigent.llms.context_window import ModelPricing

        _no_window(monkeypatch)
        rates = {"system.ai.kimi-k3": ModelPricing(input_per_token=3e-6, output_per_token=15e-6)}
        monkeypatch.setattr(context_window, "fetch_model_pricing", rates.get)
        client = _RecordingClient()
        sync = _sync(tmp_path)

        sync.note_model("system.ai.kimi-k3")
        sync.record(_usage_item(1, input_other=100), wire="/w/a")
        sync.note_model("totally-uncatalogued")
        sync.record(_usage_item(2, input_other=50), wire="/w/a")
        await sync.sync(client)

        data = _usage_posts(client)[-1]["data"]
        assert "cumulative_cost_usd" not in data
        assert data["cumulative_input_tokens"] == 150

    @pytest.mark.asyncio
    async def test_segments_survive_restart(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Per-model segments persist, so a restarted forwarder keeps pricing
        pre-restart tokens at their own model after a switch."""
        from omnigent.llms import context_window
        from omnigent.llms.context_window import ModelPricing

        _no_window(monkeypatch)
        rates = {
            "system.ai.kimi-k3": ModelPricing(input_per_token=3e-6, output_per_token=15e-6),
            "system.ai.kimi-k2.7": ModelPricing(input_per_token=0.95e-6, output_per_token=4e-6),
        }
        monkeypatch.setattr(context_window, "fetch_model_pricing", rates.get)
        client = _RecordingClient()
        sync = _sync(tmp_path)
        sync.note_model("system.ai.kimi-k3")
        sync.record(_usage_item(1, input_other=1_000_000), wire="/w/a")

        state, trusted = _read_usage_state(tmp_path)
        assert trusted is True and state is not None
        assert state.by_model == {"system.ai.kimi-k3": _segment(input_other=1_000_000)}
        resumed = _sync(tmp_path, state=state)
        resumed.note_model("system.ai.kimi-k2.7")
        resumed.record(_usage_item(2, input_other=1_000_000), wire="/w/a")
        await resumed.sync(client)
        assert _usage_posts(client)[-1]["data"]["cumulative_cost_usd"] == pytest.approx(3.95)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("old_rate", "new_rate", "expected"),
        [
            # Decrease: whole-total repricing would report 2.00, which the
            # server's clamp freezes at the stale 3.00 high-water (probed);
            # the running sum keeps growing: 3.00 accrued + 1M @ $1/M.
            pytest.param(3e-6, 1e-6, 4.00, id="rate-decrease"),
            # Increase: whole-total repricing would retroactively charge the
            # first meg at $3/M (6.00); accrued history stays at its rate.
            pytest.param(1e-6, 3e-6, 4.00, id="rate-increase"),
        ],
    )
    async def test_rate_change_prices_only_the_new_delta(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        old_rate: float,
        new_rate: float,
        expected: float,
    ) -> None:
        """Cost is a running sum: a rate change (fallback→catalog, catalog
        bump) between runs prices only tokens accrued AFTER it."""
        from omnigent.llms import context_window
        from omnigent.llms.context_window import ModelPricing

        _no_window(monkeypatch)
        client = _RecordingClient()
        monkeypatch.setattr(
            context_window,
            "fetch_model_pricing",
            {
                "system.ai.kimi-k3": ModelPricing(input_per_token=old_rate, output_per_token=0.0)
            }.get,
        )
        sync = _sync(tmp_path)
        sync.note_model("system.ai.kimi-k3")
        sync.record(_usage_item(1, input_other=1_000_000), wire="/w/a")
        await sync.sync(client)
        assert _usage_posts(client)[-1]["data"]["cumulative_cost_usd"] == pytest.approx(
            old_rate * 1e6
        )

        # Restart under the new rate: the persisted accrual must not reprice.
        monkeypatch.setattr(
            context_window,
            "fetch_model_pricing",
            {
                "system.ai.kimi-k3": ModelPricing(input_per_token=new_rate, output_per_token=0.0)
            }.get,
        )
        state, trusted = _read_usage_state(tmp_path)
        assert trusted is True and state is not None
        resumed = _sync(tmp_path, state=state)
        resumed.record(_usage_item(2, input_other=1_000_000), wire="/w/a")
        await resumed.sync(client)
        posted = _usage_posts(client)[-1]["data"]["cumulative_cost_usd"]
        assert posted == pytest.approx(expected)

    @pytest.mark.asyncio
    async def test_unpriced_delta_defers_cost_until_pricing_returns(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A pricing outage omits the cost only while a delta is unpriced;
        the accrued history is never repriced when pricing returns."""
        from omnigent.llms import context_window
        from omnigent.llms.context_window import ModelPricing

        _no_window(monkeypatch)
        client = _RecordingClient()
        rates = {"system.ai.kimi-k3": ModelPricing(input_per_token=3e-6, output_per_token=0.0)}
        monkeypatch.setattr(context_window, "fetch_model_pricing", rates.get)
        sync = _sync(tmp_path)
        sync.note_model("system.ai.kimi-k3")
        sync.record(_usage_item(1, input_other=1_000_000), wire="/w/a")
        await sync.sync(client)
        assert _usage_posts(client)[-1]["data"]["cumulative_cost_usd"] == pytest.approx(3.00)

        # Restart into a pricing outage with a fresh delta outstanding
        # (in-process, a successful lookup stays cached — the outage is only
        # observable across a restart): cost is omitted this post (server
        # token-fallback), tokens still post.
        monkeypatch.setattr(context_window, "fetch_model_pricing", lambda _m: None)
        state, _trusted = _read_usage_state(tmp_path)
        assert state is not None
        outage = _sync(tmp_path, state=state)
        outage.record(_usage_item(2, input_other=1_000_000), wire="/w/a")
        await outage.sync(client)
        assert "cumulative_cost_usd" not in _usage_posts(client)[-1]["data"]

        # Restart again with pricing back at a HIGHER rate: only the
        # outstanding delta prices at it — the accrued first meg stays at
        # $3.00, not repriced to $9.00.
        monkeypatch.setattr(
            context_window,
            "fetch_model_pricing",
            {"system.ai.kimi-k3": ModelPricing(input_per_token=9e-6, output_per_token=0.0)}.get,
        )
        state2, _trusted2 = _read_usage_state(tmp_path)
        assert state2 is not None
        recovered = _sync(tmp_path, state=state2)
        recovered.record(_usage_item(3, input_other=1), wire="/w/a")
        await recovered.sync(client)
        assert _usage_posts(client)[-1]["data"]["cumulative_cost_usd"] == pytest.approx(
            3.00 + 9.000009
        )

    def test_record_without_llm_request_attributes_to_alias(self, tmp_path: Path) -> None:
        """Before any llm.request, the record's own alias keys the segment."""
        sync = _sync(tmp_path)
        sync.record(_usage_item(1, input_other=10, model="kimi-k3-databricks"), wire="/w/a")
        state, _trusted = _read_usage_state(tmp_path)
        assert state is not None
        assert set(state.by_model) == {"kimi-k3-databricks"}

    @pytest.mark.asyncio
    async def test_wire_restart_resets_billed_mark_for_that_wire(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A recreated wire's rows bill even at/below the old watermark line.

        Line numbers restart in the recreated log; without the reset every
        row up to the stale mark is rejected as a replay — a permanent
        undercount."""
        _no_window(monkeypatch)
        client = _RecordingClient()
        sync = _sync(tmp_path)
        sync.record(_usage_item(5, input_other=100), wire="/w/a")

        sync.note_wire_restarted("/w/a")
        sync.record(_usage_item(1, input_other=7), wire="/w/a")
        await sync.sync(client)
        assert _usage_posts(client)[-1]["data"]["cumulative_input_tokens"] == 107

    @pytest.mark.asyncio
    async def test_wire_bounce_keeps_each_wires_watermark(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A→B→A discovery bounce must not re-bill A's rows.

        A single (wire, line) watermark was overwritten by B's mark, so a
        replay of A's already-billed row after returning to A re-billed it
        (probed: 207 tokens from 100 + 7 + a 100-token replay)."""
        _no_window(monkeypatch)
        client = _RecordingClient()
        sync = _sync(tmp_path)
        sync.record(_usage_item(5, input_other=100), wire="/w/a")
        sync.record(_usage_item(1, input_other=7), wire="/w/b")
        # Back on wire A: its billed row replays (crash between the totals
        # write and the wire-cursor write) — must be skipped, not re-billed.
        sync.record(_usage_item(5, input_other=100), wire="/w/a")
        await sync.sync(client)
        assert _usage_posts(client)[-1]["data"]["cumulative_input_tokens"] == 107

        # A genuinely new row on A still bills past its retained mark.
        sync.record(_usage_item(6, input_other=5), wire="/w/a")
        await sync.sync(client)
        assert _usage_posts(client)[-1]["data"]["cumulative_input_tokens"] == 112

    def test_nine_wire_bounce_never_rebills_evicted_history(self, tmp_path: Path) -> None:
        """Every adopted wire retains its idempotency mark for the session."""
        sync = _sync(tmp_path)
        for i in range(9):
            sync.record(_usage_item(1, input_other=1), wire=f"/w/{i}")
        sync.record(_usage_item(1, input_other=1), wire="/w/0")

        state, _trusted = _read_usage_state(tmp_path)
        assert state is not None
        assert len(state.billed) == 9
        assert state.totals["input_other"] == 9

    def test_wire_restart_keeps_other_wires_marks(self, tmp_path: Path) -> None:
        """Restarting one wire must not unmark another wire's billed rows."""
        sync = _sync(tmp_path)
        sync.record(_usage_item(5, input_other=100), wire="/w/a")
        sync.note_wire_restarted("/w/b")
        state, _trusted = _read_usage_state(tmp_path)
        assert state is not None
        assert state.billed == {"/w/a": 5}


class TestDiscoverWire:
    def _make_session(
        self,
        home: Path,
        session_dir_name: str,
        work_dir: str,
        *,
        mtime: float,
        first_row: str = "{}",
    ) -> Path:
        wire = home / "sessions" / "wd_x" / session_dir_name / "agents" / "main" / "wire.jsonl"
        wire.parent.mkdir(parents=True, exist_ok=True)
        wire.write_text(first_row + "\n", encoding="utf-8")
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

    def test_wire_just_before_launch_is_not_adopted(self, tmp_path: Path) -> None:
        """Adoption pins strictly to the launch epoch — no skew window."""
        home = tmp_path / "kimi-code-home"
        home.mkdir()
        self._make_session(home, "session_prior", "/ws", mtime=9_995.0)
        assert _discover_wire(home, "/ws", launch_epoch_ms=10_000_000) is None

    def test_subsecond_wire_before_launch_ms_is_not_adopted(self, tmp_path: Path) -> None:
        """A precise mtime 400ms before launch is a prior launch's wire, not ours."""
        home = tmp_path / "kimi-code-home"
        home.mkdir()
        self._make_session(home, "session_prior", "/ws", mtime=10_000.2)
        assert _discover_wire(home, "/ws", launch_epoch_ms=10_000_600) is None

    def test_precise_wire_after_launch_ms_is_adopted(self, tmp_path: Path) -> None:
        home = tmp_path / "kimi-code-home"
        home.mkdir()
        wire = self._make_session(home, "session_fresh", "/ws", mtime=10_000.75)
        assert _discover_wire(home, "/ws", launch_epoch_ms=10_000_600) == wire

    def test_whole_second_mtime_within_launch_second_is_adopted(self, tmp_path: Path) -> None:
        """A second-truncating filesystem only needs to reach the launch second
        when the wire content offers no timestamp to break the tie."""
        home = tmp_path / "kimi-code-home"
        home.mkdir()
        wire = self._make_session(home, "session_fresh", "/ws", mtime=10_000.0)
        assert _discover_wire(home, "/ws", launch_epoch_ms=10_000_600) == wire

    def test_coarse_tie_rejected_when_header_predates_launch(self, tmp_path: Path) -> None:
        """A same-second truncated mtime defers to the wire's metadata header."""
        home = tmp_path / "kimi-code-home"
        home.mkdir()
        header = json.dumps({"type": "metadata", "created_at": 10_000_200})
        self._make_session(home, "session_prior", "/ws", mtime=10_000.0, first_row=header)
        assert _discover_wire(home, "/ws", launch_epoch_ms=10_000_600) is None

    def test_coarse_tie_adopted_when_header_postdates_launch(self, tmp_path: Path) -> None:
        home = tmp_path / "kimi-code-home"
        home.mkdir()
        header = json.dumps({"type": "metadata", "created_at": 10_000_650})
        wire = self._make_session(home, "session_fresh", "/ws", mtime=10_000.0, first_row=header)
        assert _discover_wire(home, "/ws", launch_epoch_ms=10_000_600) == wire

    def test_coarse_tie_deferred_while_header_mid_write(self, tmp_path: Path) -> None:
        """A partial/unparseable first line defers adoption to the next poll —
        it must not fall through to the coarse mtime floor."""
        import os

        home = tmp_path / "kimi-code-home"
        home.mkdir()
        wire = self._make_session(home, "session_fresh", "/ws", mtime=10_000.0)
        # Header truncated mid-write (no trailing newline yet).
        wire.write_text('{"type": "metadata", "created_', encoding="utf-8")
        os.utime(wire, (10_000.0, 10_000.0))
        assert _discover_wire(home, "/ws", launch_epoch_ms=10_000_600) is None
        # A complete-but-broken line is equally untrustworthy.
        wire.write_text('{"broken json}\n', encoding="utf-8")
        os.utime(wire, (10_000.0, 10_000.0))
        assert _discover_wire(home, "/ws", launch_epoch_ms=10_000_600) is None
        # Next poll the header is complete: the timestamp decides.
        header = json.dumps({"type": "metadata", "created_at": 10_000_650})
        wire.write_text(header + "\n", encoding="utf-8")
        os.utime(wire, (10_000.0, 10_000.0))
        assert _discover_wire(home, "/ws", launch_epoch_ms=10_000_600) == wire


def _wire_home(tmp_path: Path, rows: list[dict[str, object]]) -> tuple[Path, Path]:
    """Build a kimi home holding one discoverable session wire with *rows*."""
    home = tmp_path / "kimi-code-home"
    wire = home / "sessions" / "wd_x" / "session_a" / "agents" / "main" / "wire.jsonl"
    wire.parent.mkdir(parents=True)
    wire.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return home, wire


def _prompt_row(text: str = "go", *, time_ms: int | None = None) -> dict[str, object]:
    row: dict[str, object] = {
        "type": "turn.prompt",
        "input": [{"type": "text", "text": text}],
        "origin": {"kind": "user"},
    }
    if time_ms is not None:
        row["time"] = time_ms
    return row


def _assistant_row(uuid: str, text: str) -> dict[str, object]:
    return {
        "type": "context.append_loop_event",
        "event": {"type": "content.part", "uuid": uuid, "part": {"type": "text", "text": text}},
    }


def _tool_call_row(uuid: str = "t1") -> dict[str, object]:
    return {
        "type": "context.append_loop_event",
        "event": {"type": "tool.call", "uuid": uuid, "turnId": "0", "name": "Bash"},
    }


def _turn_ended_row(
    reason: str, *, error_message: str | None = None, time_ms: int | None = None
) -> dict[str, object]:
    row: dict[str, object] = {"type": "turn.ended", "turnId": 0, "reason": reason}
    if error_message is not None:
        row["error"] = {"code": "provider.auth_error", "message": error_message}
    if time_ms is not None:
        row["time"] = time_ms
    return row


async def _drive_loop_until(
    tmp_path: Path,
    rows: list[dict[str, object]],
    done: Callable[[], bool],
    *,
    pane_alive: Callable[[], bool] | None = None,
    quiescence_s: float = 60.0,
    tool_quiescence_s: float = 60.0,
    prepare: Callable[[Path, Path], None] | None = None,
    launch_epoch_ms: int = 0,
) -> None:
    """Run the forward loop against a canned wire until *done* (then cancel).

    *prepare* runs with ``(bridge_dir, wire_path)`` before the loop starts, for
    tests that seed persisted forwarder state.
    """
    bridge = tmp_path / "bridge"
    bridge.mkdir(exist_ok=True)
    home, wire = _wire_home(tmp_path, rows)
    if prepare is not None:
        prepare(bridge, wire)
    task = asyncio.create_task(
        forward_kimi_wire_to_session(
            base_url="http://test",
            headers={},
            session_id="conv_k",
            bridge_dir=bridge,
            kimi_home=home,
            workspace="/ws",
            launch_epoch_ms=launch_epoch_ms,
            pane_alive=pane_alive,
            quiescence_s=quiescence_s,
            tool_quiescence_s=tool_quiescence_s,
            poll_interval_s=0.01,
            post_backoff_initial_s=0.01,
        )
    )
    try:
        for _ in range(300):
            if done():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("forward loop never reached the expected state")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestForwardLoopEdges:
    """Lifecycle edges of the forward loop, with the POST helpers stubbed."""

    @pytest.fixture(autouse=True)
    def _stub_posts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.items: list[str] = []
        self.statuses: list[tuple[str, str]] = []
        forwarder_health.clear()

        async def _fake_item(_client: object, **kwargs: object) -> None:
            self.items.append(kwargs["item"].response_id)  # type: ignore[union-attr]

        async def _fake_status(_client: object, **kwargs: object) -> None:
            self.statuses.append((str(kwargs["status"]), str(kwargs["output"])))

        monkeypatch.setattr(fwd, "_post_conversation_item", _fake_item)
        monkeypatch.setattr(fwd, "_post_reasoning_item", _fake_item)
        monkeypatch.setattr(fwd, "_post_external_session_status", _fake_status)

    async def test_turn_ended_failed_posts_failed_edge(self, tmp_path: Path) -> None:
        rows = [_prompt_row(), _assistant_row("u1", "partial"), _turn_ended_row("failed")]
        await _drive_loop_until(tmp_path, rows, lambda: bool(self.statuses))
        assert self.statuses == [("failed", "partial")]

    async def test_failed_turn_without_output_surfaces_provider_error(
        self, tmp_path: Path
    ) -> None:
        # The failed fixture shape: prompt → turn.ended(failed), no assistant text.
        rows = [_prompt_row(), _turn_ended_row("failed", error_message="401 Invalid Token")]
        await _drive_loop_until(tmp_path, rows, lambda: bool(self.statuses))
        assert self.statuses == [("failed", "401 Invalid Token")]

    async def test_historical_failed_edge_is_not_replayed_on_resume(self, tmp_path: Path) -> None:
        """A resumed wire ending in yesterday's turn.ended(failed) must not
        re-post that dead turn's failure over the live session.

        The failed.jsonl shape (prompt → turn.ended failed, timestamped)
        resumed after the launch-epoch floor: only the live turn's edge
        posts."""
        now_ms = int(time.time() * 1000)
        rows = [
            # Yesterday's failed turn (the failed.jsonl shape).
            _prompt_row("old prompt"),
            _turn_ended_row(
                "failed", error_message="401 Invalid Token", time_ms=now_ms - 86_400_000
            ),
            # The live session's turn.
            _prompt_row("new prompt"),
            _assistant_row("u1", "fresh answer"),
            _turn_ended_row("completed", time_ms=now_ms),
        ]
        await _drive_loop_until(
            tmp_path,
            rows,
            lambda: bool(self.statuses),
            launch_epoch_ms=now_ms - 60_000,
        )
        # The dead turn's failure never surfaces; only the live idle edge.
        assert self.statuses == [("idle", "fresh answer")]

    @pytest.mark.parametrize("edge_time_ms", [None, 1_000])
    async def test_historical_or_timeless_edge_cannot_close_dead_resumed_turn(
        self, tmp_path: Path, edge_time_ms: int | None
    ) -> None:
        launch_ms = 2_000
        rows = [
            _prompt_row("old prompt", time_ms=1_000),
            _turn_ended_row("failed", error_message="401 Invalid Token", time_ms=edge_time_ms),
        ]
        bridge = tmp_path / "bridge"

        def _consumed() -> bool:
            state = _read_state(bridge)
            return state is not None and state.last_line == 2

        await _drive_loop_until(
            tmp_path,
            rows,
            _consumed,
            launch_epoch_ms=launch_ms,
            pane_alive=lambda: False,
        )

        assert self.statuses == []

    async def test_larger_same_path_recreation_resets_generation_state(
        self, tmp_path: Path
    ) -> None:
        rows = [_prompt_row(), _turn_ended_row("completed")]

        def _seed(bridge: Path, wire: Path) -> None:
            old_text = "".join(
                json.dumps({"type": "metadata", "index": index}) + "\n" for index in range(7)
            )
            wire.write_text(old_text, encoding="utf-8")
            old_stat = wire.stat()
            (bridge / "kimi_forwarder.json").write_text(
                json.dumps(
                    {
                        "wire_path": str(wire),
                        "last_line": 7,
                        "offset": len(old_text.encode()),
                        "last_edge_id": "kimi:turn_end:8",
                        "last_seen_offset": len(old_text.encode()),
                        "wire_dev": old_stat.st_dev,
                        "wire_ino": old_stat.st_ino,
                    }
                ),
                encoding="utf-8",
            )
            replacement = wire.with_suffix(".replacement")
            replacement.write_bytes(
                b" " * (len(old_text.encode()) - 1)
                + b"\n"
                + b"".join(json.dumps(row).encode() + b"\n" for row in rows)
            )
            replacement.replace(wire)

        bridge = tmp_path / "bridge"

        def _done() -> bool:
            state = _read_state(bridge)
            return bool(self.statuses) or (
                state is not None
                and Path(state.wire_path).exists()
                and state.offset == Path(state.wire_path).stat().st_size
            )

        await _drive_loop_until(tmp_path, rows, _done, prepare=_seed)

        assert self.statuses == [("idle", "")]

    async def test_inode_replacement_discards_prior_assistant_text(self, tmp_path: Path) -> None:
        rows = [_prompt_row("new prompt"), _turn_ended_row("completed")]

        def _seed(bridge: Path, wire: Path) -> None:
            old_rows = [_prompt_row("old prompt"), _assistant_row("old", "OLD ANSWER")]
            old_text = "".join(json.dumps(row) + "\n" for row in old_rows)
            wire.write_text(old_text, encoding="utf-8")
            old_stat = wire.stat()
            _write_state(
                bridge,
                _ForwardState(
                    wire_path=str(wire),
                    last_line=2,
                    offset=len(old_text.encode()),
                    turn_open=True,
                    last_assistant_text="OLD ANSWER",
                    last_seen_offset=len(old_text.encode()),
                    wire_dev=old_stat.st_dev,
                    wire_ino=old_stat.st_ino,
                ),
            )
            replacement = wire.with_suffix(".replacement")
            replacement.write_text(
                "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
            )
            replacement.replace(wire)

        await _drive_loop_until(tmp_path, rows, lambda: bool(self.statuses), prepare=_seed)

        assert self.statuses == [("idle", "")]

    async def test_turn_ended_completed_posts_idle_edge_with_output(self, tmp_path: Path) -> None:
        rows = [_prompt_row(), _assistant_row("u1", "done!"), _turn_ended_row("completed")]
        await _drive_loop_until(tmp_path, rows, lambda: bool(self.statuses))
        assert self.statuses == [("idle", "done!")]

    async def test_turn_ended_cancelled_posts_idle_edge(self, tmp_path: Path) -> None:
        rows = [_prompt_row(), _turn_ended_row("cancelled")]
        await _drive_loop_until(tmp_path, rows, lambda: bool(self.statuses))
        assert self.statuses == [("idle", "")]

    async def test_pane_death_mid_turn_posts_failed_edge(self, tmp_path: Path) -> None:
        # A prompt with no terminal edge and a dead pane: fail instead of strand.
        rows = [_prompt_row(), _assistant_row("u1", "so far")]
        await _drive_loop_until(
            tmp_path, rows, lambda: bool(self.statuses), pane_alive=lambda: False
        )
        assert self.statuses == [("failed", "so far")]

    async def test_dead_pane_waits_for_unterminated_terminal_row(self, tmp_path: Path) -> None:
        rows = [_prompt_row(), _turn_ended_row("completed")]

        def _remove_final_newline(_bridge: Path, wire: Path) -> None:
            wire.write_bytes(wire.read_bytes()[:-1])

        await _drive_loop_until(
            tmp_path,
            rows,
            lambda: bool(self.statuses),
            pane_alive=lambda: False,
            prepare=_remove_final_newline,
        )

        assert self.statuses == [("idle", "")]

    async def test_quiescence_closes_turn_without_edge_as_idle(self, tmp_path: Path) -> None:
        # A wedged turn writes no wire edge; the quiet-wire fallback closes it.
        rows = [_prompt_row(), _assistant_row("u1", "so far")]
        await _drive_loop_until(tmp_path, rows, lambda: bool(self.statuses), quiescence_s=0.05)
        assert self.statuses == [("idle", "so far")]

    async def test_quiescence_suppressed_while_tool_in_flight(self, tmp_path: Path) -> None:
        """A silent wire mid-tool is a long tool call; only pane death closes it."""
        pane_up = True
        rows = [_prompt_row(), _tool_call_row()]
        checks = 0

        def _done() -> bool:
            nonlocal pane_up, checks
            if self.statuses:
                return True
            checks += 1
            # 20 checks ≈ 200ms with a 20ms quiescence window: staying edge-free
            # this long proves suppression; then a pane death must close it.
            if checks == 20:
                pane_up = False
            return False

        await _drive_loop_until(
            tmp_path,
            rows,
            _done,
            pane_alive=lambda: pane_up,
            quiescence_s=0.02,
        )
        assert self.statuses == [("failed", "")]

    async def test_hung_tool_watchdog_fails_turn_after_ceiling(self, tmp_path: Path) -> None:
        """Crash-mid-tool replay: the real completed-turn wire cut right after its
        first tool.call, then eternal silence in an alive pane. Suppression must
        not last forever — the tool-quiescence ceiling fails the turn."""
        fixture_rows = [
            json.loads(line)
            for line in (_FIXTURES / "completed.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        cut = next(
            i
            for i, row in enumerate(fixture_rows)
            if row.get("type") == "context.append_loop_event"
            and row.get("event", {}).get("type") == "tool.call"
        )
        rows = fixture_rows[: cut + 1]
        await _drive_loop_until(
            tmp_path,
            rows,
            lambda: bool(self.statuses),
            pane_alive=lambda: True,
            quiescence_s=0.02,
            tool_quiescence_s=0.1,
        )
        assert self.statuses == [("failed", "sanitized text part")]

    async def test_readopted_wire_ignores_stale_edge_dedupe(self, tmp_path: Path) -> None:
        """A newly discovered wire restarts line numbering; a stale edge id from
        the prior wire must not swallow the new wire's edge at the same line."""
        rows = [_prompt_row(), _turn_ended_row("completed")]

        def _seed(bridge: Path, wire: Path) -> None:
            _write_state(
                bridge,
                _ForwardState(
                    wire_path=str(wire.parent / "gone.jsonl"),
                    last_line=5,
                    offset=999,
                    last_edge_id="kimi:turn_end:1",
                ),
            )

        await _drive_loop_until(tmp_path, rows, lambda: bool(self.statuses), prepare=_seed)
        assert self.statuses == [("idle", "")]

    async def test_recreated_same_path_wire_ignores_stale_edge_dedupe(
        self, tmp_path: Path
    ) -> None:
        """A truncated/recreated wire at the SAME path restarts line numbering;
        the stale edge id must reset exactly like the discovery branch or the
        new log's edge at the same line never posts (and with a stale
        quiescence id the turn is even left closed with no watchdog armed)."""
        rows = [_prompt_row(), _turn_ended_row("completed")]

        def _seed(bridge: Path, wire: Path) -> None:
            # Prior generation: cursor far past the recreated file's size, and
            # an edge id colliding with the new log's turn.ended line.
            _write_state(
                bridge,
                _ForwardState(
                    wire_path=str(wire),
                    last_line=50,
                    offset=wire.stat().st_size + 10_000,
                    last_edge_id="kimi:turn_end:1",
                    last_seen_offset=wire.stat().st_size + 10_000,
                ),
            )

        await _drive_loop_until(tmp_path, rows, lambda: bool(self.statuses), prepare=_seed)
        assert self.statuses == [("idle", "")]

    async def test_stalled_cursor_quiescence_ids_do_not_collide_across_turns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Turn B's fallback close must post even when failed POSTs stalled
        the delivery cursor at turn A's line.

        A line-only fallback id collided with turn A's persisted id, took
        the dedupe branch, set turn_open=False WITHOUT posting, and disarmed
        both watchdogs — the session stranded running. The per-turn
        discriminator keeps the ids distinct."""

        async def _failing_item(_client: object, **kwargs: object) -> None:
            item = kwargs["item"]
            if "B" in item.text:  # type: ignore[union-attr]
                raise httpx.ConnectError("stalled")
            self.items.append(item.response_id)  # type: ignore[union-attr]

        monkeypatch.setattr(fwd, "_post_conversation_item", _failing_item)

        wire_ref: list[Path] = []

        def _capture(_bridge: Path, wire: Path) -> None:
            wire_ref.append(wire)

        phase = {"n": 0}

        def _done() -> bool:
            if phase["n"] == 0 and len(self.statuses) == 1:
                # Turn A closed by quiescence; prompt B arrives but its POST
                # fails forever, so the cursor stalls at A's line.
                with wire_ref[0].open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(_prompt_row("prompt B")) + "\n")
                phase["n"] = 1
                return False
            return len(self.statuses) == 2

        await _drive_loop_until(
            tmp_path,
            [_prompt_row("prompt A")],
            _done,
            quiescence_s=0.2,
            prepare=_capture,
        )
        # Both turns' quiescence closes posted — turn B was not swallowed by
        # a colliding dedupe id.
        assert self.statuses == [("idle", ""), ("idle", "")]

    async def test_shrunk_same_path_wire_ignores_stale_edge_dedupe(self, tmp_path: Path) -> None:
        """Same-path recreation detected by the high-water gate (delivery
        cursor still under the new size, so the reader did NOT restart) must
        also reset the stale edge id before re-reading."""
        rows = [_prompt_row(), _turn_ended_row("completed")]

        def _seed(bridge: Path, wire: Path) -> None:
            _write_state(
                bridge,
                _ForwardState(
                    wire_path=str(wire),
                    last_line=0,
                    offset=0,
                    last_edge_id="kimi:turn_end:1",
                    last_seen_offset=wire.stat().st_size + 10_000,
                ),
            )

        await _drive_loop_until(tmp_path, rows, lambda: bool(self.statuses), prepare=_seed)
        assert self.statuses == [("idle", "")]

    async def test_restart_forwards_persisted_assistant_text_on_edge(self, tmp_path: Path) -> None:
        """A restart between the assistant message and the turn edge still
        forwards the real result on the edge, not an empty output."""
        rows = [_prompt_row(), _assistant_row("u1", "so far"), _turn_ended_row("completed")]

        def _seed(bridge: Path, wire: Path) -> None:
            # A prior forwarder posted the prompt + assistant message (cursor
            # just before turn.ended) and persisted the assistant text.
            consumed = len("".join(json.dumps(r) + "\n" for r in rows[:2]).encode("utf-8"))
            _write_state(
                bridge,
                _ForwardState(
                    wire_path=str(wire),
                    last_line=2,
                    offset=consumed,
                    turn_open=True,
                    last_assistant_text="from before restart",
                ),
            )

        await _drive_loop_until(tmp_path, rows, lambda: bool(self.statuses), prepare=_seed)
        assert self.statuses == [("idle", "from before restart")]

    async def test_assistant_text_is_persisted_with_the_cursor(self, tmp_path: Path) -> None:
        """The forward loop persists the in-flight assistant text so a crash
        before the edge can restore it."""
        rows = [_prompt_row(), _assistant_row("u1", "hello world")]
        bridges: list[Path] = []

        def _capture(bridge: Path, _wire: Path) -> None:
            bridges.append(bridge)

        await _drive_loop_until(tmp_path, rows, lambda: "kimi:u1" in self.items, prepare=_capture)
        state = _read_state(bridges[0])
        assert state is not None
        assert state.last_assistant_text == "hello world"

    async def test_turn_open_persists_across_restart(self, tmp_path: Path) -> None:
        """A restarted forwarder still fails a stranded turn it never observed."""
        rows = [_prompt_row(), _assistant_row("u1", "so far")]

        def _seed(bridge: Path, wire: Path) -> None:
            # State a prior forwarder persisted mid-turn: wire fully consumed,
            # turn still open.
            _write_state(
                bridge,
                _ForwardState(
                    wire_path=str(wire),
                    last_line=2,
                    offset=wire.stat().st_size,
                    turn_open=True,
                ),
            )

        await _drive_loop_until(
            tmp_path,
            rows,
            lambda: bool(self.statuses),
            pane_alive=lambda: False,
            prepare=_seed,
        )
        assert self.statuses == [("failed", "")]
        assert self.items == []

    async def test_dropped_edge_status_persists_across_restart(self, tmp_path: Path) -> None:
        """A restart must not soften a poison-dropped failure back to idle."""
        rows = [_prompt_row()]

        def _seed(bridge: Path, wire: Path) -> None:
            _write_state(
                bridge,
                _ForwardState(
                    wire_path=str(wire),
                    last_line=1,
                    offset=wire.stat().st_size,
                    turn_open=True,
                    dropped_edge_status="failed",
                ),
            )

        await _drive_loop_until(
            tmp_path, rows, lambda: bool(self.statuses), quiescence_s=0.05, prepare=_seed
        )
        assert self.statuses == [("failed", "")]

    async def test_tool_silence_clock_survives_restart(self, tmp_path: Path) -> None:
        """A restart resumes the tool-quiescence window instead of re-arming it,
        so a crash-looping forwarder still fires the hung-tool watchdog."""
        rows = [_prompt_row(), _tool_call_row()]

        def _seed(bridge: Path, wire: Path) -> None:
            import time as _time

            _write_state(
                bridge,
                _ForwardState(
                    wire_path=str(wire),
                    last_line=2,
                    offset=wire.stat().st_size,
                    turn_open=True,
                    tools_in_flight=1,
                    last_activity_ts=_time.time() - 100.0,
                ),
            )

        await _drive_loop_until(
            tmp_path,
            rows,
            lambda: bool(self.statuses),
            pane_alive=lambda: True,
            quiescence_s=200.0,
            tool_quiescence_s=50.0,
            prepare=_seed,
        )
        assert self.statuses == [("failed", "")]

    async def test_wall_clock_jump_gets_restart_grace(self, tmp_path: Path) -> None:
        """A huge apparent elapsed silence (sleep/wake, NTP step) must not fire
        a watchdog on the spot — the restart grace gives the wire a moment."""
        rows = [_prompt_row()]

        def _seed(bridge: Path, wire: Path) -> None:
            size = wire.stat().st_size
            _write_state(
                bridge,
                _ForwardState(
                    wire_path=str(wire),
                    last_line=1,
                    offset=size,
                    turn_open=True,
                    last_activity_ts=time.time() - 10_000.0,
                    last_seen_offset=size,
                ),
            )

        started = time.monotonic()
        await _drive_loop_until(
            tmp_path, rows, lambda: bool(self.statuses), quiescence_s=5.0, prepare=_seed
        )
        assert self.statuses == [("idle", "")]
        assert time.monotonic() - started >= 0.8

    async def test_near_expired_window_still_fires_promptly(self, tmp_path: Path) -> None:
        """The grace floor must not re-arm the whole window for a crash loop."""
        rows = [_prompt_row()]

        def _seed(bridge: Path, wire: Path) -> None:
            size = wire.stat().st_size
            _write_state(
                bridge,
                _ForwardState(
                    wire_path=str(wire),
                    last_line=1,
                    offset=size,
                    turn_open=True,
                    last_activity_ts=time.time() - 4.5,
                    last_seen_offset=size,
                ),
            )

        started = time.monotonic()
        await _drive_loop_until(
            tmp_path, rows, lambda: bool(self.statuses), quiescence_s=5.0, prepare=_seed
        )
        assert self.statuses == [("idle", "")]
        assert time.monotonic() - started <= 2.5

    async def test_same_path_wire_shrink_reseeds_high_water(self, tmp_path: Path) -> None:
        """A wire recreated at the same path below the observed high-water must
        refresh the activity clock and reseed the gate — not leave the stale
        high-water blinding it while quiescence falsely closes the live turn."""
        rows = [_prompt_row()]
        bridge = tmp_path / "bridge"

        def _seed(bridge_dir: Path, wire: Path) -> None:
            # A prior run observed a 100kB wire (delivery stuck at 0) that kimi
            # has since recreated as this much smaller one, 100s ago.
            _write_state(
                bridge_dir,
                _ForwardState(
                    wire_path=str(wire),
                    last_line=0,
                    offset=0,
                    turn_open=True,
                    last_activity_ts=time.time() - 100.0,
                    last_seen_offset=100_000,
                ),
            )

        def _reseeded() -> bool:
            state = _read_state(bridge)
            return (
                state is not None
                and state.last_seen_offset is not None
                and 0 < state.last_seen_offset < 100_000
                and state.offset == state.last_seen_offset
            )

        await _drive_loop_until(tmp_path, rows, _reseeded, quiescence_s=50.0, prepare=_seed)
        assert self.statuses == []

    async def test_replayed_turn_edge_is_deduped(self, tmp_path: Path) -> None:
        """A crash between an edge POST and the cursor persist must not double-post."""
        rows = [_prompt_row(), _turn_ended_row("completed")]
        bridge = tmp_path / "bridge"

        def _seed(bridge_dir: Path, wire: Path) -> None:
            # A prior forwarder posted the edge (line 1) but crashed before
            # advancing the cursor past it.
            _write_state(
                bridge_dir,
                _ForwardState(
                    wire_path=str(wire),
                    last_line=1,
                    offset=len(json.dumps(rows[0])) + 1,
                    turn_open=True,
                    last_edge_id="kimi:turn_end:1",
                ),
            )

        def _consumed() -> bool:
            state = _read_state(bridge)
            return state is not None and state.last_line >= 2 and not state.turn_open

        await _drive_loop_until(tmp_path, rows, _consumed, prepare=_seed)
        assert self.statuses == []


class TestForwardLoopPostFailures:
    async def test_poison_item_dropped_after_bounded_retries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A permanent-4xx item is skipped after bounded retries; the tail moves on."""
        posted: list[str] = []
        statuses: list[str] = []
        rejections = 0

        async def _fake_item(_client: object, **kwargs: object) -> None:
            response_id = kwargs["item"].response_id  # type: ignore[union-attr]
            if response_id == "kimi:turn:0":
                nonlocal rejections
                rejections += 1
                request = httpx.Request("POST", "http://test")
                raise httpx.HTTPStatusError(
                    "422", request=request, response=httpx.Response(422, request=request)
                )
            posted.append(str(response_id))

        async def _fake_status(_client: object, **kwargs: object) -> None:
            statuses.append(str(kwargs["status"]))

        monkeypatch.setattr(fwd, "_post_conversation_item", _fake_item)
        monkeypatch.setattr(fwd, "_post_external_session_status", _fake_status)

        rows = [_prompt_row("poison"), _assistant_row("u1", "ok"), _turn_ended_row("completed")]
        await _drive_loop_until(tmp_path, rows, lambda: bool(statuses))
        assert rejections == 3
        assert posted == ["kimi:u1"]
        assert statuses == ["idle"]

    @pytest.mark.parametrize("status_code", [401, 403, 429])
    async def test_auth_and_rate_limit_rejections_retry_and_never_drop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status_code: int
    ) -> None:
        """401/403/429 are transient outages, not poison: the item must survive."""
        posted: list[str] = []
        rejections = 0

        async def _fake_item(_client: object, **kwargs: object) -> None:
            nonlocal rejections
            if rejections < 4:
                rejections += 1
                request = httpx.Request("POST", "http://test")
                raise httpx.HTTPStatusError(
                    str(status_code),
                    request=request,
                    response=httpx.Response(status_code, request=request),
                )
            posted.append(str(kwargs["item"].response_id))  # type: ignore[union-attr]

        monkeypatch.setattr(fwd, "_post_conversation_item", _fake_item)

        await _drive_loop_until(tmp_path, [_prompt_row("edge")], lambda: bool(posted))
        assert rejections == 4
        assert posted == ["kimi:turn:0"]

    async def test_endless_transient_rejection_degrades_health(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A revoked token's endless 401s must be visible to the idle-turn watchdog."""
        forwarder_health.clear()
        attempts = 0

        async def _fake_item(_client: object, **kwargs: object) -> None:
            nonlocal attempts
            attempts += 1
            request = httpx.Request("POST", "http://test")
            raise httpx.HTTPStatusError(
                "401", request=request, response=httpx.Response(401, request=request)
            )

        monkeypatch.setattr(fwd, "_post_conversation_item", _fake_item)

        await _drive_loop_until(
            tmp_path,
            [_prompt_row()],
            lambda: attempts >= 2 and forwarder_health.recent_post_failure(60.0) is not None,
        )
        forwarder_health.clear()

    async def test_poison_dropped_failed_edge_routes_failed_via_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A permanently rejected turn_failed edge must not be closed as idle later."""
        posted: list[tuple[str, str]] = []
        edge_attempts = 0

        async def _fake_item(_client: object, **kwargs: object) -> None:
            return None

        async def _fake_status(_client: object, **kwargs: object) -> None:
            nonlocal edge_attempts
            edge_attempts += 1
            if edge_attempts <= 3:
                request = httpx.Request("POST", "http://test")
                raise httpx.HTTPStatusError(
                    "404", request=request, response=httpx.Response(404, request=request)
                )
            posted.append((str(kwargs["status"]), str(kwargs["output"])))

        monkeypatch.setattr(fwd, "_post_conversation_item", _fake_item)
        monkeypatch.setattr(fwd, "_post_external_session_status", _fake_status)

        rows = [_prompt_row(), _turn_ended_row("failed", error_message="boom")]
        await _drive_loop_until(tmp_path, rows, lambda: bool(posted), quiescence_s=0.2)
        assert edge_attempts == 4
        assert posted == [("failed", "boom")]

    async def test_redelivered_prompt_keeps_one_fallback_sequence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        attempts = 0
        statuses: list[tuple[str, str]] = []
        bridge = tmp_path / "bridge"

        async def _failing_item(_client: object, **kwargs: object) -> None:
            nonlocal attempts
            attempts += 1
            raise httpx.ConnectError("no route")

        async def _status(_client: object, **kwargs: object) -> None:
            statuses.append((str(kwargs["status"]), str(kwargs["output"])))

        monkeypatch.setattr(fwd, "_post_conversation_item", _failing_item)
        monkeypatch.setattr(fwd, "_post_external_session_status", _status)

        await _drive_loop_until(
            tmp_path,
            [_prompt_row()],
            lambda: attempts >= 8 and bool(statuses),
            quiescence_s=0.05,
        )

        state = _read_state(bridge)
        assert state is not None
        assert state.turn_seq == 1
        assert statuses == [("idle", "")]

    async def test_high_water_persisted_without_cursor_advance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Observed-tail state persists on advancement even while delivery fails,
        so a crash loop can't replay the same rows into a fresh clock."""
        bridge = tmp_path / "bridge"

        async def _failing_item(_client: object, **kwargs: object) -> None:
            raise httpx.ConnectError("no route to host")

        monkeypatch.setattr(fwd, "_post_conversation_item", _failing_item)

        def _persisted() -> bool:
            state = _read_state(bridge)
            return state is not None and (state.last_seen_offset or 0) > 0 and state.offset == 0

        await _drive_loop_until(tmp_path, [_prompt_row()], _persisted)
        forwarder_health.clear()

    async def test_crash_loop_replay_does_not_rearm_silence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A restart re-reading rows already observed (persisted high-water)
        must resume the silence clock, not restart the full window."""
        statuses: list[tuple[str, str]] = []

        async def _failing_item(_client: object, **kwargs: object) -> None:
            raise httpx.ConnectError("no route to host")

        async def _fake_status(_client: object, **kwargs: object) -> None:
            statuses.append((str(kwargs["status"]), str(kwargs["output"])))

        monkeypatch.setattr(fwd, "_post_conversation_item", _failing_item)
        monkeypatch.setattr(fwd, "_post_external_session_status", _fake_status)

        def _seed(bridge: Path, wire: Path) -> None:
            # A prior crashed run already observed the whole tail 100s ago but
            # never delivered it (cursor still 0).
            _write_state(
                bridge,
                _ForwardState(
                    wire_path=str(wire),
                    last_line=0,
                    offset=0,
                    last_activity_ts=time.time() - 100.0,
                    last_seen_offset=wire.stat().st_size,
                ),
            )

        await _drive_loop_until(
            tmp_path, [_prompt_row()], lambda: bool(statuses), quiescence_s=50.0, prepare=_seed
        )
        assert statuses == [("idle", "")]
        forwarder_health.clear()

    async def test_failed_redelivery_does_not_defer_quiescence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Retries re-reading the same unposted tail must not refresh the
        silence timers — under a permanent outage the fallback still fires."""
        statuses: list[tuple[str, str]] = []

        async def _failing_item(_client: object, **kwargs: object) -> None:
            raise httpx.ConnectError("no route to host")

        async def _fake_status(_client: object, **kwargs: object) -> None:
            statuses.append((str(kwargs["status"]), str(kwargs["output"])))

        monkeypatch.setattr(fwd, "_post_conversation_item", _failing_item)
        monkeypatch.setattr(fwd, "_post_external_session_status", _fake_status)

        await _drive_loop_until(
            tmp_path, [_prompt_row()], lambda: bool(statuses), quiescence_s=0.05
        )
        assert statuses == [("idle", "")]
        forwarder_health.clear()

    async def test_persistent_edge_failure_alert_is_rate_limited(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Edge delivery failing continuously past the threshold logs an error."""
        import logging

        monkeypatch.setattr(fwd, "_EDGE_FAILURE_ALERT_S", 0.05)

        async def _fake_item(_client: object, **kwargs: object) -> None:
            return None

        async def _failing_status(_client: object, **kwargs: object) -> None:
            raise httpx.ConnectError("no route to host")

        monkeypatch.setattr(fwd, "_post_conversation_item", _fake_item)
        monkeypatch.setattr(fwd, "_post_external_session_status", _failing_status)

        def _alerted() -> bool:
            return any("undelivered" in rec.message for rec in caplog.records)

        with caplog.at_level(logging.ERROR, logger="omnigent.kimi_native_forwarder"):
            await _drive_loop_until(tmp_path, [_prompt_row()], _alerted, pane_alive=lambda: False)
        alerts = [rec for rec in caplog.records if "undelivered" in rec.message]
        assert alerts and alerts[0].levelno == logging.ERROR
        forwarder_health.clear()

    async def test_supersede_over_dropped_failure_is_logged(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """One poll batch: turn A's failed edge poison-drops, prompt B supersedes.
        Session status is single-valued, so B's outcome wins — but the swallowed
        failure must leave a loud trace."""
        import logging

        posted: list[tuple[str, str]] = []
        edge_attempts = 0

        async def _fake_item(_client: object, **kwargs: object) -> None:
            return None

        async def _fake_status(_client: object, **kwargs: object) -> None:
            nonlocal edge_attempts
            edge_attempts += 1
            if edge_attempts <= 3:
                request = httpx.Request("POST", "http://test")
                raise httpx.HTTPStatusError(
                    "404", request=request, response=httpx.Response(404, request=request)
                )
            posted.append((str(kwargs["status"]), str(kwargs["output"])))

        monkeypatch.setattr(fwd, "_post_conversation_item", _fake_item)
        monkeypatch.setattr(fwd, "_post_external_session_status", _fake_status)

        rows = [
            _prompt_row("A"),
            _turn_ended_row("failed", error_message="boom"),
            _prompt_row("B"),
            _assistant_row("u1", "reply"),
            _turn_ended_row("completed"),
        ]
        with caplog.at_level(logging.ERROR, logger="omnigent.kimi_native_forwarder"):
            await _drive_loop_until(tmp_path, rows, lambda: bool(posted))
        assert posted == [("idle", "reply")]
        assert any("superseded" in rec.message for rec in caplog.records)

    async def test_transport_failure_is_recorded_and_retried(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A connect failure attributes itself to forwarder health and holds the cursor."""
        forwarder_health.clear()
        attempts = 0

        async def _fake_item(_client: object, **kwargs: object) -> None:
            nonlocal attempts
            attempts += 1
            raise httpx.ConnectError("no route to host")

        monkeypatch.setattr(fwd, "_post_conversation_item", _fake_item)

        await _drive_loop_until(tmp_path, [_prompt_row()], lambda: attempts >= 3)
        assert forwarder_health.recent_post_failure(60.0) is not None
        forwarder_health.clear()

    async def test_fallback_edge_failure_is_attributed_to_health(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failing pane-death edge POST must show up in forwarder health."""
        forwarder_health.clear()

        async def _fake_item(_client: object, **kwargs: object) -> None:
            return None

        async def _failing_status(_client: object, **kwargs: object) -> None:
            raise httpx.ConnectError("no route to host")

        monkeypatch.setattr(fwd, "_post_conversation_item", _fake_item)
        monkeypatch.setattr(fwd, "_post_external_session_status", _failing_status)

        await _drive_loop_until(
            tmp_path,
            [_prompt_row()],
            lambda: forwarder_health.recent_post_failure(60.0) is not None,
            pane_alive=lambda: False,
        )
        forwarder_health.clear()


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


async def _drive_loop(
    monkeypatch: pytest.MonkeyPatch,
    *,
    bridge_dir: Path,
    home: Path,
    client: _FakeAsyncClient,
    launch_epoch_ms: int,
    until: Callable[[], bool],
    timeout_s: float = 10.0,
    pane_alive: Callable[[], bool] | None = None,
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
            pane_alive=pane_alive,
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
    async def test_dead_writer_stable_final_usage_without_newline_is_billed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        home = tmp_path / "home"
        bridge = tmp_path / "bridge"
        bridge.mkdir()
        now_ms = int(time.time() * 1000)
        wire = _loop_wire(
            home,
            [
                _prompt_row(),
                {"type": "llm.request", "kind": "loop", "model": "system.ai.kimi-k3"},
                _usage_row(input_other=42, output=7, time_ms=now_ms),
            ],
        )
        wire.write_bytes(wire.read_bytes().removesuffix(b"\n"))
        client = _FakeAsyncClient()

        await _drive_loop(
            monkeypatch,
            bridge_dir=bridge,
            home=home,
            client=client,
            launch_epoch_ms=0,
            until=lambda: any(post["type"] == "external_session_usage" for post in client.posts),
            timeout_s=1.5,
            pane_alive=lambda: False,
        )

        usage = next(post for post in client.posts if post["type"] == "external_session_usage")
        assert usage["data"]["cumulative_input_tokens"] == 42

    @pytest.mark.asyncio
    async def test_pending_usage_delivers_without_any_wire(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Persisted-but-undelivered totals post even when no wire log exists.

        The per-poll retry must not be gated on wire availability: a usage
        post that failed right before the wire vanished (or before a restart
        that never rediscovers one) would otherwise stay undelivered forever
        despite being safely persisted."""
        home = tmp_path / "home"
        home.mkdir()
        bridge = tmp_path / "bridge"
        bridge.mkdir()
        totals = {"input_other": 100, "output": 20, "cache_read": 0, "cache_creation": 0}
        assert _write_usage_state(
            bridge,
            _UsageState(totals=dict(totals), by_model={"system.ai.kimi-k3": _segment(**totals)}),
        )
        client = _FakeAsyncClient()
        await _drive_loop(
            monkeypatch,
            bridge_dir=bridge,
            home=home,
            client=client,
            launch_epoch_ms=0,
            until=lambda: any(p.get("type") == "external_session_usage" for p in client.posts),
        )
        usage = [p for p in client.posts if p.get("type") == "external_session_usage"]
        assert usage[0]["data"]["cumulative_input_tokens"] == 100

    @pytest.mark.asyncio
    async def test_fallback_close_delivers_final_usage(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A pane-death close still delivers the turn's usage totals."""
        home = tmp_path / "home"
        bridge = tmp_path / "bridge"
        bridge.mkdir()
        now_ms = int(time.time() * 1000)
        _loop_wire(
            home,
            [
                _prompt_row(),
                {"type": "llm.request", "kind": "loop", "model": "system.ai.kimi-k3"},
                _usage_row(input_other=42, output=7, time_ms=now_ms),
            ],
        )
        client = _FakeAsyncClient()

        def _done() -> bool:
            kinds = [p.get("type") for p in client.posts]
            return "external_session_status" in kinds and "external_session_usage" in kinds

        await _drive_loop(
            monkeypatch,
            bridge_dir=bridge,
            home=home,
            client=client,
            launch_epoch_ms=0,
            until=_done,
            pane_alive=lambda: False,
        )
        status = [p for p in client.posts if p.get("type") == "external_session_status"]
        assert status[-1]["data"]["status"] == "failed"
        usage = [p for p in client.posts if p.get("type") == "external_session_usage"]
        assert usage[-1]["data"]["cumulative_input_tokens"] == 42

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
                _usage_row(input_other=42, output=7, time_ms=now_ms),
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
                _usage_row(input_other=90000, output=9000, time_ms=now_ms - 9_000),
                _usage_row(input_other=99999, output=9999, time_ms=now_ms - 1),
                {
                    "type": "turn.prompt",
                    "input": [{"type": "text", "text": "hi"}],
                    "origin": {"kind": "user"},
                },
                _usage_row(input_other=11, output=2, time_ms=now_ms),
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
        wire_a = _loop_wire(home, [_usage_row(input_other=100, output=10, time_ms=now_ms)])
        client = _FakeAsyncClient()

        def _saw_first_post() -> bool:
            return any(b["type"] == "external_session_usage" for b in client.posts)

        def _switch_wire() -> None:
            shutil.rmtree(wire_a.parent.parent.parent)
            wire_b = home / "sessions" / "wd_x" / "session_next" / "agents" / "main" / "wire.jsonl"
            wire_b.parent.mkdir(parents=True, exist_ok=True)
            wire_b.write_text(
                json.dumps(_usage_row(input_other=50, output=5, time_ms=now_ms)) + "\n",
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
                _usage_row(input_other=42, output=7, time_ms=now_ms),
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
