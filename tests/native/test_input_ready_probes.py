"""Per-harness ``input_ready_probe`` behaviour for the native terminal watcher.

Each probe must stay False while the TUI is still booting and turn True on the
same condition its harness's own message-delivery gate waits for; the watcher
logs ``native_input_ready`` on the first True.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.harnesses.antigravity_native import bridge as antigravity_bridge
from omnigent.harnesses.cursor_native import bridge as cursor_bridge
from omnigent.harnesses.devin_native import bridge as devin_bridge
from omnigent.harnesses.goose_native import bridge as goose_bridge
from omnigent.harnesses.hermes_native import bridge as hermes_bridge
from omnigent.harnesses.kimi_native import bridge as kimi_bridge
from omnigent.harnesses.kiro_native import bridge as kiro_bridge
from omnigent.harnesses.opencode_native import bridge as opencode_bridge
from omnigent.harnesses.qwen_native import bridge as qwen_bridge
from omnigent.inner.terminal import TerminalInstance
from omnigent.native.input_ready import PaneSettledProbe
from tests.runner.helpers import make_test_terminal_instance

_KIMI_FIXTURES = Path(__file__).parents[1] / "fixtures" / "kimi_native"


def _terminal(tmp_path: Path, pane: str | None = None) -> TerminalInstance:
    instance = make_test_terminal_instance("native", "main", tmp_path)
    if pane is not None:
        instance._remember_pane_snapshot(pane)
    return instance


@pytest.mark.parametrize(
    ("probe", "booting_pane", "ready_pane"),
    [
        pytest.param(
            cursor_bridge.native_input_ready,
            "Cursor Agent\nLoading…",
            "Cursor Agent\n→ Plan, search, build anything",
            id="cursor",
        ),
        pytest.param(
            kiro_bridge.native_input_ready,
            "Initializing",
            "history\n────────\n> ask a question or describe a task",
            id="kiro",
        ),
        pytest.param(
            devin_bridge.native_input_ready,
            "Starting Devin…",
            f"Devin\n> {devin_bridge._DEVIN_IDLE_PLACEHOLDER}",
            id="devin",
        ),
        pytest.param(
            antigravity_bridge.native_input_ready,
            "Antigravity\nverifying your account eligibility",
            "Antigravity\n>\n? for shortcuts",
            id="antigravity",
        ),
        pytest.param(
            kimi_bridge.native_input_ready,
            (_KIMI_FIXTURES / "banner_without_editor.txt").read_text(encoding="utf-8"),
            (_KIMI_FIXTURES / "first_boot_empty.txt").read_text(encoding="utf-8"),
            id="kimi",
        ),
    ],
)
def test_pane_marker_probes(
    tmp_path: Path, probe: object, booting_pane: str, ready_pane: str
) -> None:
    assert callable(probe)
    assert probe("conv", _terminal(tmp_path)) is False
    assert probe("conv", _terminal(tmp_path, booting_pane)) is False
    assert probe("conv", _terminal(tmp_path, ready_pane)) is True


def test_qwen_probe_waits_for_boot_system_event(tmp_path: Path) -> None:
    qwen_bridge.prepare_bridge_files(tmp_path)
    events = qwen_bridge.events_file_path(tmp_path)
    instance = _terminal(tmp_path)
    instance.args = ["--input-file", "in.jsonl", "--json-file", str(events)]

    assert qwen_bridge.native_input_ready("conv", instance) is False
    events.write_text(json.dumps({"type": "system", "subtype": "session_start"}) + "\n")
    assert qwen_bridge.native_input_ready("conv", instance) is True
    assert qwen_bridge.native_input_ready("conv", _terminal(tmp_path)) is False


def test_opencode_probe_requires_server_bound_to_this_session(tmp_path: Path) -> None:
    instance = _terminal(tmp_path)
    instance.env = {"XDG_DATA_HOME": str(opencode_bridge.xdg_data_home_for_bridge_dir(tmp_path))}
    assert opencode_bridge.native_input_ready("conv_new", instance) is False

    def _bind(session_id: str) -> None:
        opencode_bridge.write_bridge_state(
            tmp_path,
            opencode_bridge.OpenCodeNativeBridgeState(
                session_id=session_id,
                server_base_url="http://127.0.0.1:1",
                opencode_session_id="ses_1",
            ),
        )

    _bind("conv_old")
    assert opencode_bridge.native_input_ready("conv_new", instance) is False
    _bind("conv_new")
    assert opencode_bridge.native_input_ready("conv_new", instance) is True


@pytest.mark.parametrize(
    "probe",
    [goose_bridge.native_input_ready, hermes_bridge.native_input_ready],
    ids=["goose", "hermes"],
)
def test_pane_settled_probes_match_harness_settle_gate(tmp_path: Path, probe: object) -> None:
    assert isinstance(probe, PaneSettledProbe)
    instance = _terminal(tmp_path)
    assert probe("conv", instance) is False  # no output yet

    instance._remember_pane_snapshot("booting ⠋")
    assert probe("conv", instance) is False
    instance._remember_pane_snapshot("booting ⠙")  # spinner churn resets the count
    # Like ``_settle_pane``: the change, then three unchanged polls in a row.
    results = [probe("conv", instance) for _ in range(4)]
    assert results == [False, False, False, True]
