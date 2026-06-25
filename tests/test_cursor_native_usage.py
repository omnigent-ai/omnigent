"""Unit tests for cursor-native token-usage capture.

Covers the pure pieces a live cursor-agent isn't needed for: normalizing the
``stop``-hook payload, the append-only usage log + recorder CLI, the cumulative
accumulator (per-turn sum + generation-id dedup), state round-trip, the
``external_session_usage`` POST shape, and the poll loop (POST-on-change,
no-repost-when-unchanged, persist-only-after-success). The live tmux +
cursor-agent hook path is exercised by the e2e gate, not here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from omnigent import cursor_native_usage as usage

# A representative cursor ``stop``-hook payload (live-captured field set).
_TURN1 = {
    "generation_id": "g1",
    "model": "claude-4-sonnet",
    "status": "completed",
    "input_tokens": 23666,
    "output_tokens": 5,
    "cache_read_tokens": 23617,
    "cache_write_tokens": 47,
}
_TURN2 = {
    "generation_id": "g2",
    "model": "claude-4-sonnet",
    "status": "completed",
    "input_tokens": 24010,
    "output_tokens": 120,
    "cache_read_tokens": 23700,
    "cache_write_tokens": 60,
}


class TestNormalizeHookPayload:
    def test_extracts_usage_fields(self) -> None:
        line = usage.normalize_hook_payload(_TURN1)
        assert line == {
            "generation_id": "g1",
            "model": "claude-4-sonnet",
            "input_tokens": 23666,
            "output_tokens": 5,
            "cache_read_tokens": 23617,
            "cache_write_tokens": 47,
        }

    def test_falls_back_to_conversation_id(self) -> None:
        line = usage.normalize_hook_payload({"conversation_id": "c1", "output_tokens": 3})
        assert line is not None and line["generation_id"] == "c1"

    def test_skips_when_no_generation_id(self) -> None:
        assert usage.normalize_hook_payload({"output_tokens": 3}) is None

    def test_skips_when_all_tokens_zero(self) -> None:
        payload = {"generation_id": "g", "input_tokens": 0, "output_tokens": 0}
        assert usage.normalize_hook_payload(payload) is None

    def test_omits_model_when_absent(self) -> None:
        line = usage.normalize_hook_payload({"generation_id": "g", "output_tokens": 4})
        assert line is not None and "model" not in line

    def test_coerces_and_floors_negative_tokens(self) -> None:
        line = usage.normalize_hook_payload(
            {"generation_id": "g", "input_tokens": "7", "output_tokens": -3}
        )
        assert line is not None
        assert line["input_tokens"] == 7  # coerced from str
        assert line["output_tokens"] == 0  # negative floored

    def test_non_dict_is_skipped(self) -> None:
        assert usage.normalize_hook_payload("nope") is None


class TestRecordUsagePayload:
    def test_appends_one_line_per_turn(self, tmp_path: Path) -> None:
        assert usage.record_usage_payload(tmp_path, _TURN1) is True
        assert usage.record_usage_payload(tmp_path, _TURN2) is True
        lines = (tmp_path / usage.USAGE_FILE).read_text().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["generation_id"] == "g1"

    def test_non_billable_payload_is_skipped(self, tmp_path: Path) -> None:
        assert usage.record_usage_payload(tmp_path, {"text": "hi"}) is False
        assert not (tmp_path / usage.USAGE_FILE).exists()


class TestRecordUsageCli:
    def test_cli_reads_stdin_appends_and_emits_continue(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_TURN1)))
        rc = usage._cli_record_usage(tmp_path)
        assert rc == 0
        # cursor reads stdout as the hook response; "{}" means "continue".
        assert capsys.readouterr().out == "{}"
        assert (tmp_path / usage.USAGE_FILE).read_text().strip()

    def test_cli_never_fails_on_garbage_stdin(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        import io

        monkeypatch.setattr("sys.stdin", io.StringIO("not json{{{"))
        assert usage._cli_record_usage(tmp_path) == 0
        assert capsys.readouterr().out == "{}"
        assert not (tmp_path / usage.USAGE_FILE).exists()

    def test_module_main_entrypoint(self, tmp_path: Path) -> None:
        # End-to-end through the real CLI the hooks.json command invokes. ``-I``
        # is dropped here (the worktree isn't pip-installed); PYTHONPATH makes it
        # importable, mirroring the installed-package resolution in production.
        env = {"PYTHONPATH": str(Path(__file__).resolve().parents[1])}
        proc = subprocess.run(
            [sys.executable, "-m", "omnigent.cursor_native_usage", "record-usage",
             "--bridge-dir", str(tmp_path)],
            input=json.dumps(_TURN1),
            capture_output=True,
            text=True,
            env=env,
        )
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == "{}"
        assert (tmp_path / usage.USAGE_FILE).read_text().strip()


class TestUsageAccumulator:
    def test_sums_per_turn_counts(self) -> None:
        acc = usage._UsageAccumulator()
        assert acc.add_line(usage.normalize_hook_payload(_TURN1)) is True  # type: ignore[arg-type]
        assert acc.add_line(usage.normalize_hook_payload(_TURN2)) is True  # type: ignore[arg-type]
        assert acc.input_tokens == 23666 + 24010
        assert acc.output_tokens == 5 + 120
        assert acc.cache_read_tokens == 23617 + 23700
        assert acc.model == "claude-4-sonnet"

    def test_dedups_by_generation_id(self) -> None:
        acc = usage._UsageAccumulator()
        line = usage.normalize_hook_payload(_TURN1)
        assert acc.add_line(line) is True  # type: ignore[arg-type]
        assert acc.add_line(line) is False  # type: ignore[arg-type] — same gen id, ignored
        assert acc.output_tokens == 5  # not doubled

    def test_latest_model_wins(self) -> None:
        acc = usage._UsageAccumulator()
        acc.add_line({"generation_id": "a", "model": "claude-4-sonnet", "output_tokens": 1})
        acc.add_line({"generation_id": "b", "model": "gpt-5", "output_tokens": 1})
        assert acc.model == "gpt-5"

    def test_line_without_gen_id_is_ignored(self) -> None:
        acc = usage._UsageAccumulator()
        assert acc.add_line({"output_tokens": 9}) is False
        assert acc.output_tokens == 0


class TestStateRoundTrip:
    def test_write_then_read_preserves_totals_and_seen(self, tmp_path: Path) -> None:
        acc = usage._UsageAccumulator(
            input_tokens=10, output_tokens=2, cache_read_tokens=4, model="gpt-5", seen={"g1"}
        )
        usage._write_usage_state(tmp_path, acc)
        loaded = usage._read_usage_state(tmp_path)
        assert loaded.input_tokens == 10
        assert loaded.output_tokens == 2
        assert loaded.cache_read_tokens == 4
        assert loaded.model == "gpt-5"
        assert loaded.seen == {"g1"}

    def test_missing_state_is_cold_default(self, tmp_path: Path) -> None:
        loaded = usage._read_usage_state(tmp_path)
        assert (loaded.input_tokens, loaded.output_tokens, loaded.cache_read_tokens) == (0, 0, 0)
        assert loaded.model is None and loaded.seen == set()


class TestReadUsageLines:
    def test_reads_valid_lines_and_skips_garbage(self, tmp_path: Path) -> None:
        (tmp_path / usage.USAGE_FILE).write_text(
            json.dumps({"generation_id": "g1", "output_tokens": 1})
            + "\n\n"  # blank line tolerated
            + "not json\n"  # garbage tolerated
            + json.dumps({"generation_id": "g2", "output_tokens": 2})
            + "\n"
        )
        lines = usage._read_usage_lines(tmp_path)
        assert [line["generation_id"] for line in lines] == ["g1", "g2"]

    def test_missing_file_is_empty(self, tmp_path: Path) -> None:
        assert usage._read_usage_lines(tmp_path) == []


class TestUsagePostBody:
    def test_shape_with_model(self) -> None:
        acc = usage._UsageAccumulator(
            input_tokens=100, output_tokens=20, cache_read_tokens=80, model="gpt-5"
        )
        assert usage._usage_post_body(acc) == {
            "cumulative_input_tokens": 100,
            "cumulative_output_tokens": 20,
            "cumulative_cache_read_input_tokens": 80,
            "model": "gpt-5",
        }

    def test_omits_model_when_none(self) -> None:
        body = usage._usage_post_body(usage._UsageAccumulator(output_tokens=5))
        assert "model" not in body
        assert body["cumulative_output_tokens"] == 5


class TestClearUsageState:
    def test_removes_log_and_state(self, tmp_path: Path) -> None:
        usage.record_usage_payload(tmp_path, _TURN1)
        usage._write_usage_state(tmp_path, usage._UsageAccumulator(output_tokens=1))
        usage.clear_cursor_usage_state(tmp_path)
        assert not (tmp_path / usage.USAGE_FILE).exists()
        assert not (tmp_path / usage._USAGE_STATE_FILE).exists()

    def test_noop_when_absent(self, tmp_path: Path) -> None:
        usage.clear_cursor_usage_state(tmp_path)  # must not raise


class _CtxRecordingClient:
    """Async httpx-client stub (records POSTs, 200) usable as ``async with``."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []

    async def __aenter__(self) -> _CtxRecordingClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def post(self, url: str, *, json: dict) -> httpx.Response:
        self.posts.append((url, json))
        return httpx.Response(200, request=httpx.Request("POST", url))


async def _run_loop_until(
    monkeypatch: pytest.MonkeyPatch,
    bridge_dir: Path,
    until,
    *,
    client: _CtxRecordingClient | None = None,
    max_wait_s: float = 3.0,
) -> _CtxRecordingClient:
    """Run the real poll loop with a recording client until *until(client)* holds."""
    client = client or _CtxRecordingClient()
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client)
    task = asyncio.create_task(
        usage.forward_cursor_usage_to_session(
            base_url="http://test",
            headers={},
            session_id="conv_1",
            bridge_dir=bridge_dir,
            poll_interval_s=0.01,
        )
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_wait_s
    try:
        while loop.time() < deadline:
            if until(client):
                return client
            await asyncio.sleep(0.01)
        raise AssertionError("loop condition was not met before timeout")
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
class TestForwardLoop:
    async def test_posts_cumulative_usage_and_persists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        usage.record_usage_payload(tmp_path, _TURN1)
        usage.record_usage_payload(tmp_path, _TURN2)
        client = await _run_loop_until(monkeypatch, tmp_path, lambda c: len(c.posts) >= 1)
        url, body = client.posts[0]
        assert url == "/v1/sessions/conv_1/events"
        assert body["type"] == "external_session_usage"
        assert body["data"] == {
            "cumulative_input_tokens": 23666 + 24010,
            "cumulative_output_tokens": 5 + 120,
            "cumulative_cache_read_input_tokens": 23617 + 23700,
            "model": "claude-4-sonnet",
        }
        # State persisted after the successful POST so a restart resumes.
        persisted = usage._read_usage_state(tmp_path)
        assert persisted.seen == {"g1", "g2"}

    async def test_no_repost_when_unchanged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        usage.record_usage_payload(tmp_path, _TURN1)
        client = await _run_loop_until(monkeypatch, tmp_path, lambda c: len(c.posts) >= 1)
        # Let several more polls run; with no new turns there must be no 2nd POST.
        await asyncio.sleep(0.1)
        assert len(client.posts) == 1

    async def test_new_turn_triggers_followup_post(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        usage.record_usage_payload(tmp_path, _TURN1)
        client = _CtxRecordingClient()
        # First POST covers turn 1.
        await _run_loop_until(monkeypatch, tmp_path, lambda c: len(c.posts) >= 1, client=client)
        # Append a second turn and run again: a fresh cumulative POST must land.
        usage.record_usage_payload(tmp_path, _TURN2)
        await _run_loop_until(monkeypatch, tmp_path, lambda c: len(c.posts) >= 2, client=client)
        _, body = client.posts[-1]
        assert body["data"]["cumulative_output_tokens"] == 5 + 120

    async def test_failed_post_is_not_persisted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class _FailingClient(_CtxRecordingClient):
            async def post(self, url: str, *, json: dict) -> httpx.Response:
                self.posts.append((url, json))
                raise httpx.ConnectError("boom")

        usage.record_usage_payload(tmp_path, _TURN1)
        client = await _run_loop_until(
            monkeypatch, tmp_path, lambda c: len(c.posts) >= 1, client=_FailingClient()
        )
        assert len(client.posts) >= 1  # it tried
        # A failed flush must NOT advance persisted state — the turn stays unseen
        # so the next poll retries it (no silent loss).
        assert usage._read_usage_state(tmp_path).seen == set()
