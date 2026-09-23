"""The native-pane reaper's server check right before teardown.

The server's pending-prompt index is the one harness-agnostic view of an open
approval card, so a candidate pane is spared while one targets its session.
The check fails closed while the server is unreachable, but only for a bounded
grace window.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import httpx
import pytest

from omnigent.runner.session_status import StatusSource
from omnigent.terminals.pane_reaper import ConfirmVerdict, SpareReason
from tests.terminals.native_pane_rig import FakeServerClient, PaneRig, build_pane_rig


async def _reap_candidate(rig: PaneRig) -> None:
    rig.reaper._last_busy_at[rig.conv_id] = time.monotonic() - 10 * 3600
    await rig.reaper._scan_once()


def _elicitation(target: str | None) -> dict[str, object]:
    params: dict[str, object] = {"message": "Allow?", "phase": "tool_call"}
    if target is not None:
        params["target_session_id"] = target
    return {"type": "response.elicitation_request", "elicitation_id": "e1", "params": params}


async def test_pending_prompt_for_the_session_spares_its_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = FakeServerClient(pending=[_elicitation(None)])
    rig = await build_pane_rig(tmp_path, monkeypatch, key="goose", server=server)
    confirm = rig.reaper._confirm_reap
    assert confirm is not None

    verdict = await confirm(rig.pane)

    assert verdict.proceed is False
    assert verdict.reason == SpareReason.SERVER_CHECK
    await _reap_candidate(rig)
    assert rig.alive()


async def test_prompt_retargeted_to_the_session_spares_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = FakeServerClient()
    rig = await build_pane_rig(tmp_path, monkeypatch, key="cursor", server=server)
    server.pending = [_elicitation(rig.conv_id)]
    await _reap_candidate(rig)
    assert rig.alive()


async def test_prompt_targeting_another_session_does_not_spare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = FakeServerClient(pending=[_elicitation("conv_someone_else")])
    rig = await build_pane_rig(tmp_path, monkeypatch, key="cursor", server=server)
    await _reap_candidate(rig)
    assert not rig.alive()


async def test_session_gone_on_the_server_reaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = FakeServerClient(status_code=404)
    rig = await build_pane_rig(tmp_path, monkeypatch, key="kiro", server=server)
    await _reap_candidate(rig)
    assert not rig.alive()


@pytest.mark.parametrize("failure", ["transport", "5xx"])
async def test_unreachable_server_spares_until_the_grace_then_reaps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: str,
) -> None:
    monkeypatch.setenv("OMNIGENT_NATIVE_PANE_SERVER_UNREACHABLE_GRACE_S", "0.05")
    server = FakeServerClient()
    if failure == "transport":
        server.error = httpx.ConnectError("server down")
    else:
        server.status_code = 503
    rig = await build_pane_rig(tmp_path, monkeypatch, key="qwen", server=server)

    await _reap_candidate(rig)
    assert rig.alive()
    time.sleep(0.06)
    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        await _reap_candidate(rig)

    assert not rig.alive()
    assert any("server unreachable" in rec.getMessage() for rec in caplog.records)


async def test_kill_switch_skips_the_server_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIGENT_NATIVE_PANE_REAP_SERVER_CHECK", "0")
    server = FakeServerClient(pending=[_elicitation(None)])
    rig = await build_pane_rig(tmp_path, monkeypatch, key="goose", server=server)
    await _reap_candidate(rig)
    assert not rig.alive()
    assert server.snapshot_gets == 0


async def test_an_unreachable_server_is_warned_about_once_per_outage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("OMNIGENT_NATIVE_PANE_SERVER_UNREACHABLE_GRACE_S", "0")
    server = FakeServerClient()
    server.status_code = 503
    rig = await build_pane_rig(tmp_path, monkeypatch, key="qwen", server=server)
    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        for _ in range(3):
            verdict = await rig.reaper._confirm_reap(rig.pane)
            assert verdict.proceed
        server.status_code = 200  # the server is back
        await rig.reaper._confirm_reap(rig.pane)
        server.status_code = 503  # a new outage is warned about again
        await rig.reaper._confirm_reap(rig.pane)
    warned = [r for r in caplog.records if "server unreachable" in r.getMessage()]
    assert len(warned) == 2


async def test_a_turn_dispatched_while_the_server_is_asked_spares_the_pane(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = await build_pane_rig(tmp_path, monkeypatch, key="pi")
    base_get = rig.server.get

    async def _get(url: str, **kwargs: object) -> object:
        # The next turn is dispatched while the check awaits the server.
        rig.book.record(rig.conv_id, "running", source=StatusSource.RUNNER)
        return await base_get(url, **kwargs)

    monkeypatch.setattr(rig.server, "get", _get)
    confirm = rig.reaper._confirm_reap
    assert confirm is not None

    verdict = await confirm(rig.pane)

    assert verdict.proceed is False
    assert verdict.reason == SpareReason.RUNNER_TURN
    assert verdict.facts["turn_dispatched"] is True
    await _reap_candidate(rig)
    assert rig.alive()


async def test_a_server_check_hold_ends_at_the_approval_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIGENT_NATIVE_PANE_APPROVAL_MAX_S", "10")
    rig = await build_pane_rig(tmp_path, monkeypatch, key="goose")
    spare = ConfirmVerdict(False, SpareReason.SERVER_CHECK)

    assert rig.reaper._override_spare(rig.pane, spare, 100.0) is False
    assert rig.reaper._override_spare(rig.pane, spare, 109.9) is False
    assert rig.reaper._override_spare(rig.pane, spare, 110.0) is True
