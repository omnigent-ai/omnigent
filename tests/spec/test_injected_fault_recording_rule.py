"""Claiming "state unreachable" requires retrying the reproduction's fault injection."""

from pathlib import Path

_DEV = Path(__file__).resolve().parents[2] / "dev"


def _normalized(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_lane_rules_require_injecting_the_reproduction_fault_on_the_recorder() -> None:
    lanes = _normalized(_DEV / "recording-lanes.md")

    assert "An injected fault is not an unreachable state" in lanes
    assert "injected the same fault on the recorder's own spawned server + runner" in lanes
    assert "a PATH shim for the blocked binary" in lanes
    assert "name the injection you tried and how it failed" in lanes


def test_lane_rules_sanction_injected_fault_footage_for_sandbox_launch_stalls() -> None:
    lanes = _normalized(_DEV / "recording-lanes.md")

    assert "valid product footage" in lanes
    assert "the sanctioned lane for `linux_bwrap` launch stalls" in lanes
    assert "the clip's caption names it" in lanes


def test_repro_unreachable_state_blocker_requires_trying_the_injection() -> None:
    instructions = _normalized(_DEV / "repro-agent" / "AGENTS.md")

    assert "reached the failing state only through an injected fault" in instructions
    assert "recorder's spawned server + runner" in instructions
    assert "name the injection you tried and how it failed" in instructions


def test_resolve_recording_rules_reject_blockers_copied_from_the_repro_run() -> None:
    instructions = _normalized(_DEV / "resolve-agent" / "AGENTS.md")

    assert "Never carry the repro run's recording blocker forward unverified" in instructions
    assert "reached the failing state only through an injected fault" in instructions
    assert "A blocker copied from the repro run is never that blocker" in instructions
    assert "name the injection you tried and how it failed" in instructions
