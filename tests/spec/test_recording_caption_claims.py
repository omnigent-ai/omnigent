"""Recording captions claim only driver-asserted states the footage shows."""

from pathlib import Path

_DEV = Path(__file__).resolve().parents[2] / "dev"


def _normalized(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_web_lane_backs_each_caption_claim_with_a_driver_assertion() -> None:
    lanes = _normalized(_DEV / "recording-lanes.md")

    assert "Assert every visible state the caption will claim" in lanes
    assert "backed by a driver assertion on that same rendered element" in lanes
    assert (
        "A green check on a backend value (a readiness map entry, an API response) "
        "says nothing about what the page displayed" in lanes
    )
    assert "a label read only to enrich a failure message asserts nothing" in lanes
    assert "assert both its absence and that no replacement notice announced a swap" in lanes


def test_finishing_a_clip_checks_captions_against_the_final_frames() -> None:
    lanes = _normalized(_DEV / "recording-lanes.md")

    assert "check its final frames" in lanes
    assert "confirm they show every state the caption claims" in lanes
    assert "caption what they actually show" in lanes
    assert "a finding about the fix, not a wording problem to smooth over" in lanes


def test_resolve_after_clip_captions_need_asserted_and_shown_states() -> None:
    rules = _normalized(_DEV / "resolve-agent" / "skills" / "resolve-author-fix" / "SKILL.md")

    assert "Caption an after clip only with visible states the driver asserted" in rules
    assert "the final frames actually show" in rules
    assert (
        "A passing internal check (a readiness map entry, an API response) is not "
        "evidence of what the screen showed" in rules
    )
    assert "the behavior is not proven fixed" in rules


def test_handoff_after_clip_captions_match_the_final_frames() -> None:
    handoff = _normalized(_DEV / "resolve-agent" / "skills" / "resolve-handoff" / "SKILL.md")

    assert "ending with the corrected behavior as the final frames actually show it" in handoff
    assert "never a selected item or absent notice the clip does not display" in handoff
