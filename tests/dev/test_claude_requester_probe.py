"""Probe behavior is tested without supplying it with authenticated requester objects."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from dev.claude_requester_probe.analyze import summarize

ROOT = Path(__file__).resolve().parents[2]


def test_mod_observation_contract() -> None:
    """Execute the actual mod handlers, including interleaved streamed callbacks."""
    subprocess.run(
        ["node", "--test", str(Path(__file__).with_name("claude_requester_probe.test.mjs"))],
        cwd=ROOT,
        check=True,
        timeout=30,
    )


@pytest.mark.parametrize(
    "fault", [None, "duplicate", "late", "reload", "agent", "name", "sequence", "session"]
)
def test_delayed_call_requires_unique_original_emission(fault: str | None) -> None:
    """A new turn never relabels an old call, and ambiguous observations stay unknown."""
    emitted = {
        "incarnation": "one",
        "session_id": "session-one",
        "sequence": 1,
        "kind": "tool.emitted",
        "tool_use_id": "call",
        "turn_id": "old",
        "tool": "Read",
        "agent_id": None,
    }
    call = {**emitted, "sequence": 3, "kind": "tool.call"}
    call.pop("turn_id")
    events = [emitted, {"incarnation": "one", "sequence": 2, "kind": "turn.start"}, call]
    if fault == "duplicate":
        events.append({**emitted, "sequence": 4})
    elif fault == "late":
        emitted["sequence"] = 4
    elif fault == "reload":
        call["incarnation"] = "two"
    elif fault == "agent":
        call["agent_id"] = "child"
    elif fault == "name":
        call["tool"] = "Bash"
    elif fault == "sequence":
        events.append(dict(events[1]))
    elif fault == "session":
        call["session_id"] = "session-two"
    result = summarize(events)
    assert result["calls"][0]["turn_id"] == ("old" if fault is None else None)
    assert result["calls"][0]["requester"] == "unverified"
    assert result["authorization"] == "not_established"


def test_analyzer_refuses_missing_or_partial_capture(tmp_path: Path) -> None:
    """An unloaded mod and an interrupted event write cannot look like a successful probe."""
    command = [sys.executable, "-m", "dev.claude_requester_probe.analyze", str(tmp_path)]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert "no probe records" in result.stderr
    (tmp_path / "event.json").write_text('{"version":')
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert result.returncode == 2
    assert "malformed" in result.stderr
    (tmp_path / "event.json").write_text(
        json.dumps({"version": 1, "incarnation": "one", "sequence": 1, "kind": "session.start"})
    )
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert json.loads(result.stdout)["authorization"] == "not_established"
