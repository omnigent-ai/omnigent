"""Recording rules stop web clips on a visibly asserted end state, then hold it."""

from pathlib import Path

_DEV = Path(__file__).resolve().parents[2] / "dev"


def _normalized(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_web_lane_stops_on_a_visible_assertion_and_holds_it() -> None:
    lanes = _normalized(_DEV / "recording-lanes.md")

    assert "Stop on a visible assertion of the end state, then hold it" in lanes
    assert "locate the exact element whose rendered text or state changes" in lanes
    assert "require it to be visible" in lanes
    assert (
        "A DOM text-content probe such as `to_contain_text` on a container is "
        "satisfied by hidden or pending-state markup before the label paints" in lanes
    )
    assert "hold the settled state for at least 3 seconds" in lanes
    assert "a fixed sleep is that hold, never the stop condition itself" in lanes


def test_captions_claim_only_visibly_asserted_end_states() -> None:
    lanes = _normalized(_DEV / "recording-lanes.md")

    assert (
        "A caption must not claim a visible end state the driver never visibly asserted" in lanes
    )
    assert "caption only what the footage demonstrably shows" in lanes
    assert "re-record the journey with a visible stop assertion" in lanes


def test_resolve_after_clips_require_a_visible_end_state_and_hold() -> None:
    rules = _normalized(_DEV / "resolve-agent" / "skills" / "resolve-author-fix" / "SKILL.md")

    assert "End the after clip on the corrected state visibly on screen" in rules
    assert "the driver's final wait must assert the rendered element itself" in rules
    assert (
        "never a DOM text-content probe like `to_contain_text` that hidden or "
        "pending-state markup satisfies before the label paints" in rules
    )
    assert "Hold the settled state for at least 3 seconds before stopping" in rules
    assert "caption only an end state the driver visibly asserted" in rules
