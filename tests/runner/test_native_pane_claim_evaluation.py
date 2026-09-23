"""How the native-pane reaper judges a recorded ``running`` against evidence.

A claim is a ``running`` the runner recorded. It spares a pane only while
fresh; a stale one is confirmed or refuted by the harness's own state (turn
probe) or the server, and otherwise expires after an idle window.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import pytest

from omnigent.harnesses.codex_native import app_server as codex_app_server
from omnigent.harnesses.codex_native import bridge as codex_bridge
from omnigent.runner.native.orchestration import _AUTO_FORWARDER_TASKS
from omnigent.runner.session_status import StatusSource
from omnigent.terminals.pane_reaper import SpareReason
from tests.terminals.native_pane_rig import FakeServerClient, PaneRig, build_pane_rig


# These cases judge a claim as evidence; the shadow and veto cases set their own policy.
@pytest.fixture(autouse=True)
def _evidence_claim_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_NATIVE_PANE_CLAIM_POLICY", "evidence")


class _Clock:
    def __init__(self) -> None:
        self.now = 10_000.0

    def __call__(self) -> float:
        return self.now


class _ThreadReadClient:
    """Codex app-server double answering ``thread/read`` with a fixed status."""

    def __init__(self, status: dict[str, Any] | None) -> None:
        self.status = status
        self.requests: list[str] = []

    async def connect(self) -> None:
        return None

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        del params
        self.requests.append(method)
        if self.status is None:
            return {"result": {}}
        return {"result": {"thread": {"id": "thread_1", "status": self.status}}}

    async def close(self) -> None:
        return None


def _codex_bridge(rig: PaneRig, *, active_turn_id: str | None = "turn_1") -> None:
    codex_bridge.write_bridge_state(
        codex_bridge.bridge_dir_for_bridge_id(rig.conv_id),
        codex_bridge.CodexNativeBridgeState(
            session_id=rig.conv_id,
            socket_path="ws://127.0.0.1:1",
            thread_id="thread_1",
            codex_home="/tmp/codex-home",
            active_turn_id=active_turn_id,
        ),
    )


def _patch_codex(monkeypatch: pytest.MonkeyPatch, client: _ThreadReadClient) -> None:
    monkeypatch.setattr(
        codex_app_server, "client_for_transport", lambda *_a, **_k: client, raising=True
    )


async def _reap_candidate(rig: PaneRig) -> None:
    """One scan with the pane's idle clock already past the window."""
    rig.reaper._last_busy_at[rig.conv_id] = time.monotonic() - 10 * 3600
    await rig.reaper._scan_once()


async def test_codex_lost_relay_is_refuted_by_thread_read_idle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    clock = _Clock()
    rig = await build_pane_rig(tmp_path, monkeypatch, key="codex", status_clock=clock)
    _codex_bridge(rig)
    client = _ThreadReadClient({"type": "idle"})
    _patch_codex(monkeypatch, client)
    rig.book.record(rig.conv_id, "running", source=StatusSource.RUNNER)
    clock.now += 2 * 3600

    assessment = await rig.assess()
    assert assessment.reasons == frozenset()
    assert assessment.evidence_age_s == pytest.approx(2 * 3600)
    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        await _reap_candidate(rig)

    assert client.requests == ["thread/read"]
    record = rig.book.current(rig.conv_id)
    assert rig.closed == [rig.conv_id]
    assert record is None or record.status == "idle"
    assert any(
        rec.__dict__.get("event_name") == "native_pane_claim_refuted" for rec in caplog.records
    )


async def test_codex_active_thread_spares_a_silent_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = await build_pane_rig(tmp_path, monkeypatch, key="codex")
    _codex_bridge(rig)
    _patch_codex(monkeypatch, _ThreadReadClient({"type": "active", "activeFlags": []}))
    await _reap_candidate(rig)
    assert rig.alive()
    _patch_codex(
        monkeypatch, _ThreadReadClient({"type": "active", "activeFlags": ["waitingOnApproval"]})
    )
    await _reap_candidate(rig)
    assert rig.alive()


async def test_codex_probe_active_past_max_turn_is_reaped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIGENT_NATIVE_PANE_MAX_TURN_S", "0.005")
    rig = await build_pane_rig(tmp_path, monkeypatch, key="codex")
    _codex_bridge(rig)
    _patch_codex(monkeypatch, _ThreadReadClient({"type": "active", "activeFlags": []}))
    await _reap_candidate(rig)
    assert rig.alive()
    time.sleep(0.01)
    await _reap_candidate(rig)
    assert not rig.alive()


async def test_antigravity_lost_relay_is_refuted_by_cascade_idle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.harnesses.antigravity_native import reader, rpc

    clock = _Clock()
    rig = await build_pane_rig(tmp_path, monkeypatch, key="antigravity", status_clock=clock)
    monkeypatch.setattr(reader, "_resolve_cascade_id", lambda _dir: "cascade_1")
    monkeypatch.setattr(reader, "_resolve_rpc_port", lambda _cascade: 4242)
    summaries = {"cascade_1": {"status": "CASCADE_RUN_STATUS_RUNNING"}}
    monkeypatch.setattr(
        rpc, "get_all_cascade_trajectories", lambda _port: {"trajectorySummaries": summaries}
    )
    rig.book.record(rig.conv_id, "running", source=StatusSource.RUNNER)
    clock.now += 2 * 3600

    await _reap_candidate(rig)
    assert rig.alive()
    assert rig.book.claim(rig.conv_id) is not None

    summaries["cascade_1"] = {"status": "CASCADE_RUN_STATUS_IDLE"}
    await _reap_candidate(rig)
    assert not rig.alive()


async def test_claude_status_file_idle_refutes_a_stale_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    rig = await build_pane_rig(tmp_path, monkeypatch, key="claude", status_clock=clock)
    status_file = tmp_path / "status.json"
    status_file.write_text(json.dumps({"status": "idle"}), encoding="utf-8")
    monkeypatch.setattr(rig.resources, "status_poller_path", lambda _sid: status_file)
    rig.book.record(rig.conv_id, "running", source=StatusSource.STATUS_FILE)
    clock.now += 600

    assessment = await rig.assess()

    assert assessment.reasons == frozenset()
    assert assessment.facts["claim_refuted"] is True
    assert assessment.evidence_age_s == pytest.approx(7200, abs=5)
    confirm = rig.reaper._confirm_reap
    assert confirm is not None
    assert (await confirm(rig.pane)).proceed is True
    record = rig.book.current(rig.conv_id)
    assert record is not None
    assert record.status == "idle"
    assert record.origin is StatusSource.RECONCILE


@pytest.mark.parametrize(
    ("raw", "reason"),
    [("busy", SpareReason.TURN_PROBE), ("waiting", SpareReason.AWAITING_HUMAN)],
)
async def test_claude_status_file_confirms_a_silent_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raw: str, reason: SpareReason
) -> None:
    clock = _Clock()
    rig = await build_pane_rig(tmp_path, monkeypatch, key="claude", status_clock=clock)
    status_file = tmp_path / "status.json"
    status_file.write_text(
        json.dumps({"status": raw, "waitingFor": "permission prompt"}), encoding="utf-8"
    )
    monkeypatch.setattr(rig.resources, "status_poller_path", lambda _sid: status_file)
    rig.book.record(rig.conv_id, "running", source=StatusSource.STATUS_FILE)
    clock.now += 600

    assessment = await rig.assess()

    assert assessment.reasons == {reason}
    await _reap_candidate(rig)
    assert rig.alive()


@pytest.mark.parametrize("server_status", ["idle", "failed"])
async def test_server_idle_refutes_a_claim_no_probe_can_answer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    server_status: str,
) -> None:
    clock = _Clock()
    server = FakeServerClient(status=server_status)
    rig = await build_pane_rig(tmp_path, monkeypatch, key="pi", server=server, status_clock=clock)
    rig.book.record(rig.conv_id, "running", source=StatusSource.PTY)
    clock.now += 2 * 3600

    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        await _reap_candidate(rig)

    assert not rig.alive()
    assert server.snapshot_gets == 1
    events = {rec.__dict__.get("event_name"): rec for rec in caplog.records}
    refuted = events["native_pane_claim_refuted"]
    assert refuted.attributes["refuted_by"] == f"server status {server_status}"
    assert refuted.attributes["claim_source"] == "pty"
    assert "native_pane_reap_unverified_claim" not in events


async def test_server_running_never_keeps_a_stale_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    clock = _Clock()
    server = FakeServerClient(status="running")
    rig = await build_pane_rig(tmp_path, monkeypatch, key="pi", server=server, status_clock=clock)
    rig.book.record(rig.conv_id, "running", source=StatusSource.PTY)
    clock.now += 2 * 3600

    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        await _reap_candidate(rig)

    assert not rig.alive()
    assert any(
        rec.__dict__.get("event_name") == "native_pane_reap_unverified_claim"
        for rec in caplog.records
    )


async def test_fresh_claim_keeps_a_quiet_pane_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    rig = await build_pane_rig(tmp_path, monkeypatch, key="pi", status_clock=clock)
    rig.book.record(rig.conv_id, "running", source=StatusSource.PTY)
    clock.now += 30

    assessment = await rig.assess()

    assert assessment.reasons == frozenset()
    assert assessment.evidence_age_s == pytest.approx(30)
    assert assessment.busy is True


async def test_shadow_policy_spares_and_logs_would_expire_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("OMNIGENT_NATIVE_PANE_CLAIM_POLICY", "shadow")
    clock = _Clock()
    rig = await build_pane_rig(tmp_path, monkeypatch, key="pi", status_clock=clock)
    rig.book.record(rig.conv_id, "running", source=StatusSource.PTY)
    clock.now += 2 * 3600

    with caplog.at_level(logging.INFO, logger="omnigent.runner.app"):
        for _ in range(3):
            assessment = await rig.assess()
            assert assessment.reasons == {SpareReason.STATUS_CLAIM}
    events = [rec.__dict__.get("event_name") for rec in caplog.records]
    assert events.count("native_pane_claim_would_expire") == 1
    assert events.count("native_pane_status_contradiction") == 1
    await _reap_candidate(rig)
    assert rig.alive()


async def test_veto_policy_reconciles_a_claim_held_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIGENT_NATIVE_PANE_CLAIM_POLICY", "veto")
    clock = _Clock()
    server = FakeServerClient(status="idle")
    rig = await build_pane_rig(tmp_path, monkeypatch, key="pi", server=server, status_clock=clock)
    rig.book.record(rig.conv_id, "running", source=StatusSource.PTY)
    clock.now += 2 * 3600

    await rig.reaper._scan_once()

    assert rig.book.claim(rig.conv_id) is None
    assert rig.alive()


async def test_relay_reassert_never_refreshes_the_episode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    rig = await build_pane_rig(tmp_path, monkeypatch, key="hermes", status_clock=clock)
    for _ in range(10):
        rig.resources.note_external_session_status(rig.conv_id, "running")
        clock.now += 400

    assessment = await rig.assess()

    assert assessment.evidence_age_s == pytest.approx(4000)
    assert not assessment.busy
    await _reap_candidate(rig)
    assert not rig.alive()


async def test_devin_inferred_active_is_ignored_after_an_accepted_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.harnesses.devin_native import bridge as devin_bridge

    rig = await build_pane_rig(tmp_path, monkeypatch, key="devin")
    bridge_dir = devin_bridge.prepare_bridge_dir(rig.conv_id)
    devin_bridge.record_hook_event(bridge_dir, {"hook_event_name": "UserPromptSubmit"})
    confirm = rig.reaper._confirm_reap
    assert confirm is not None

    rig.book.record(rig.conv_id, "running", source=StatusSource.RUNNER)
    spared = await confirm(rig.pane)
    assert spared.proceed is False
    assert spared.reason == SpareReason.TURN_PROBE

    rig.book.record(rig.conv_id, "idle", source=StatusSource.CONTROL)
    proceed = await confirm(rig.pane)
    assert proceed.proceed is True
    assert proceed.facts["probe_discounted"]


async def test_opencode_mid_turn_without_a_claim_is_spared_by_the_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio

    from omnigent.harnesses.opencode_native import bridge as opencode_bridge
    from omnigent.harnesses.opencode_native.client import OpenCodeClient

    rig = await build_pane_rig(tmp_path, monkeypatch, key="opencode")
    bridge_dir = opencode_bridge.bridge_dir_for_bridge_id(rig.conv_id)
    opencode_bridge.write_bridge_state(
        bridge_dir,
        opencode_bridge.OpenCodeNativeBridgeState(
            session_id=rig.conv_id,
            server_base_url="http://127.0.0.1:1",
            opencode_session_id="ses_1",
            active_message_id="msg_1",
            status="busy",
        ),
    )

    async def _no_permissions(self: OpenCodeClient) -> list[dict[str, object]]:
        return []

    monkeypatch.setattr(OpenCodeClient, "list_permissions", _no_permissions)
    rig.book.record(rig.conv_id, "idle", source=StatusSource.RUNNER)
    forwarder = asyncio.get_running_loop().create_future()
    _AUTO_FORWARDER_TASKS[rig.conv_id] = forwarder  # type: ignore[assignment]
    try:
        await _reap_candidate(rig)
        assert rig.alive()
    finally:
        _AUTO_FORWARDER_TASKS.pop(rig.conv_id, None)
        forwarder.cancel()


async def test_a_new_dispatch_restarts_a_stale_claims_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _Clock()
    rig = await build_pane_rig(tmp_path, monkeypatch, key="codex", status_clock=clock)
    rig.book.record(rig.conv_id, "running", source=StatusSource.RUNNER)
    clock.now += 5 * 3600
    rig.book.record(rig.conv_id, "running", source=StatusSource.RUNNER)
    clock.now += 10

    assessment = await rig.assess()

    assert assessment.evidence_age_s == pytest.approx(10)
    assert assessment.busy is True


@pytest.mark.parametrize("new_edge", ["runner_dispatch", "pane_running", "status_change"])
async def test_a_refutation_lands_only_on_the_claim_it_judged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, new_edge: str
) -> None:
    import asyncio

    from omnigent.harnesses.codex_native import pane_probe as codex_pane_probe
    from omnigent.runner.native.pane_probe_types import TurnProbe, TurnState

    rig = await build_pane_rig(tmp_path, monkeypatch, key="codex")
    # A stale running claim from a turn whose relayed idle was lost.
    rig.book.record(rig.conv_id, "running", source=StatusSource.RUNNER)
    gate = asyncio.Event()
    started = asyncio.Event()

    async def _slow_probe(_ctx: object) -> TurnProbe:
        started.set()
        await gate.wait()
        return TurnProbe(TurnState.INACTIVE, "vendor", detail="thread/read: idle")

    monkeypatch.setattr(codex_pane_probe, "probe_pane_turn", _slow_probe)
    confirm = asyncio.create_task(rig.reaper._confirm_reap(rig.pane))
    await asyncio.wait_for(started.wait(), 5)
    # While codex answers for the old turn, a new one starts.
    if new_edge == "runner_dispatch":
        rig.book.record(rig.conv_id, "running", source=StatusSource.RUNNER)
    elif new_edge == "pane_running":
        rig.book.record(rig.conv_id, "running", source=StatusSource.PTY)
    else:
        rig.book.record(rig.conv_id, "idle", source=StatusSource.RELAY)
        rig.book.record(rig.conv_id, "running", source=StatusSource.RELAY)
    gate.set()
    verdict = await confirm

    current = rig.book.current(rig.conv_id)
    assert current is not None and current.status == "running", current
    assert StatusSource.RECONCILE not in current.sources
    assert not verdict.proceed
    assert verdict.reason == SpareReason.STATUS_CLAIM


async def test_a_relayed_rerun_does_not_block_a_refutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A relay re-asserting a finished turn's running is exactly what a
    # refutation is for; only a local edge (new work) fences it.
    rig = await build_pane_rig(tmp_path, monkeypatch, key="codex")
    rig.book.record(rig.conv_id, "running", source=StatusSource.RUNNER)
    claim = rig.book.claim(rig.conv_id)
    assert claim is not None
    rig.book.record(rig.conv_id, "running", source=StatusSource.RELAY)
    assert rig.book.refute(rig.conv_id, claim) is True
    current = rig.book.current(rig.conv_id)
    assert current is not None and current.status == "idle"
    assert current.origin is StatusSource.RECONCILE


@pytest.mark.parametrize("claimed", [True, False], ids=["stale_claim", "no_claim"])
async def test_a_probe_that_cannot_answer_spares_a_stale_claim_one_window_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, claimed: bool
) -> None:
    # codex with no bridge state: its probe answers UNKNOWN.
    monkeypatch.setenv("OMNIGENT_NATIVE_PANE_CLAIM_POLICY", "evidence")
    clock = _Clock()
    rig = await build_pane_rig(tmp_path, monkeypatch, key="codex", status_clock=clock)
    if claimed:
        rig.book.record(rig.conv_id, "running", source=StatusSource.RUNNER)
    clock.now += 2 * 3600
    confirm = rig.reaper._confirm_reap
    assert confirm is not None

    verdict = await confirm(rig.pane)

    assert verdict.facts["probe"].startswith("unknown")
    if not claimed:
        # Nothing to confirm: an unanswerable probe never delays the reap.
        assert verdict.proceed is True
        await _reap_candidate(rig)
        assert not rig.alive()
        return
    assert verdict.proceed is False
    assert verdict.unknown is True
    await _reap_candidate(rig)
    assert rig.alive()
    # One idle window later the reaper decides on local evidence.
    rig.reaper._unknown_since[rig.conv_id] -= 3600
    await _reap_candidate(rig)
    assert not rig.alive()


@pytest.mark.parametrize("source", ["status_file", "relay"])
async def test_a_dialog_is_re_read_only_from_the_file_that_reported_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    # Claude's status poller recorded a dialog, then stopped; the user answered
    # it in the pane and Claude finished, so the file says idle. The probe
    # re-reads that file and the record no longer holds the pane. A relayed
    # dialog has no such re-read: it holds until its bound although codex's
    # app-server reads idle.
    from tests.terminals.native_pane_rig import report_harness_state

    clock = _Clock()
    key = "claude" if source == "status_file" else "codex"
    rig = await build_pane_rig(tmp_path, monkeypatch, key=key, status_clock=clock)
    if source == "status_file":
        rig.write_claude_status("waiting", waiting_for="permission prompt")
        await rig.fire("on_tick")
        rig.write_claude_status("idle")
    else:
        rig.resources.note_external_session_status(
            rig.conv_id, "running", blocked_on="permission prompt"
        )
        report_harness_state(rig, monkeypatch, tmp_path, "idle")
    assert rig.book.blocked(rig.conv_id) is not None
    clock.now += 2 * 3600

    assessment = await rig.assess()
    await _reap_candidate(rig)

    if source == "status_file":
        assert SpareReason.AWAITING_HUMAN not in assessment.reasons
        assert assessment.facts["dialog_refuted"] is True
        assert not rig.alive()
    else:
        assert SpareReason.AWAITING_HUMAN in assessment.reasons
        assert rig.alive()


@pytest.mark.parametrize("policy", ["veto", "shadow"])
async def test_a_claim_held_pane_is_reconciled_once_per_idle_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: str
) -> None:
    monkeypatch.setenv("OMNIGENT_NATIVE_PANE_CLAIM_POLICY", policy)
    clock = _Clock()
    # The server says running, which never refutes (or keeps) a claim.
    server = FakeServerClient(status="running")
    rig = await build_pane_rig(tmp_path, monkeypatch, key="pi", server=server, status_clock=clock)
    rig.book.record(rig.conv_id, "running", source=StatusSource.PTY)
    clock.now += 2 * 3600

    for _ in range(3):
        await rig.reaper._scan_once()
    assert server.snapshot_gets == 1
    rig.reaper._reconciled_at[rig.conv_id] -= 3600
    await rig.reaper._scan_once()
    assert server.snapshot_gets == 2
    assert rig.alive()
    assert rig.book.claim(rig.conv_id) is not None
