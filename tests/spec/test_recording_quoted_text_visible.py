"""Recording captions quote only text the clip's final frame shows."""

from pathlib import Path

from omnigent.spec import load

_DEV = Path(__file__).resolve().parents[2] / "dev"
_REPRO_AGENT = _DEV / "repro-agent"


def _normalized(text: str) -> str:
    return " ".join(text.split())


def _read(path: Path) -> str:
    return _normalized(path.read_text(encoding="utf-8"))


def test_clip_rules_require_quoted_text_to_be_legible_in_the_final_frame() -> None:
    lanes = _read(_DEV / "recording-lanes.md")

    assert "Any text the caption quotes must be legible in the final frame" in lanes
    assert "an error pill's message body, a folded tool card" in lanes
    assert "is in the DOM but not on screen until it is expanded" in lanes
    assert "or leave the quote out of the caption" in lanes


def test_web_lane_expands_collapsed_detail_before_the_clip_ends() -> None:
    lanes = _read(_DEV / "recording-lanes.md")

    assert "Expand collapsed detail before the clip ends" in lanes
    assert "shows only its headline" in lanes
    assert "sits inside the collapsed body" in lanes
    assert "is satisfied by the hidden body and says nothing about what the viewer sees" in lanes
    assert 'expect(pill.get_by_test_id("error-message-content")).to_have_text(...)' in lanes
    assert "caption only the collapsed cards" in lanes
    assert "never the quoted message" in lanes


def test_finishing_a_clip_forbids_quoting_text_the_final_frame_does_not_show() -> None:
    lanes = _read(_DEV / "recording-lanes.md")

    assert "Every phrase the caption quotes must be readable in the final frame" in lanes
    assert (
        "Text a DOM or transcript assertion found inside a collapsed element is not visible"
        in lanes
    )
    assert (
        "when you cannot confirm the final frame shows the quoted text, do not quote it" in lanes
    )
    assert "say the detail was established from the DOM" in lanes


def test_repro_clip_rules_quote_only_text_the_final_frame_shows() -> None:
    instructions = _read(_REPRO_AGENT / "AGENTS.md")

    assert "Quote only text the final frame shows" in instructions
    assert "expand a collapsed error pill or folded card in the driver" in instructions
    assert "assert the expanded body is visible before stopping" in instructions
    assert "is satisfied by the collapsed body and proves nothing about the screen" in instructions
    assert (
        'caption only what is visible ("generic error cards; detail text asserted from the DOM")'
        in instructions
    )
    assert "a message still folded inside a collapsed error pill is not" in instructions


def test_repro_agent_bundle_loads_the_quoted_text_rule_as_instructions() -> None:
    spec = load(_REPRO_AGENT, expand_env=False)

    assert spec.instructions is not None
    assert "Quote only text the final frame shows" in _normalized(spec.instructions)


def test_resolve_after_clips_quote_only_text_the_final_frame_shows() -> None:
    rules = _read(_DEV / "resolve-agent" / "skills" / "resolve-author-fix" / "SKILL.md")

    assert "Quote in an after-clip caption only text the final frame shows" in rules
    assert "is in the DOM, not on screen" in rules
    assert "assert the expanded body is visible before stopping" in rules
    assert "caption only the visible cards" in rules
