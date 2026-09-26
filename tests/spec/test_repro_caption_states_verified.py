"""Repro captions and journeys name only UI states the run verified."""

from pathlib import Path

from omnigent.spec import load

_REPRO_AGENT = Path(__file__).resolve().parents[2] / "dev" / "repro-agent"


def _normalized(text: str) -> str:
    return " ".join(text.split())


def _instructions() -> str:
    return _normalized((_REPRO_AGENT / "AGENTS.md").read_text(encoding="utf-8"))


def test_repro_clip_rules_require_each_named_ui_state_to_be_verified() -> None:
    instructions = _instructions()

    assert "Caption only verified states" in instructions
    assert "Every UI state the caption or the steps to reproduce names" in instructions
    assert (
        "must be asserted by the recorded test on that element or confirmed in the "
        "final frame viewed as an image" in instructions
    )


def test_repro_clip_rules_forbid_unverified_negatives() -> None:
    instructions = _instructions()

    assert (
        'Never write an unverified negative such as "the switcher never appears" '
        "from the expected failure model" in instructions
    )
    assert "a locator that accepts several outcomes" in instructions
    assert "establishes none of them" in instructions
    assert "a screenshot you could not view confirms nothing" in instructions
    assert "Leave an unverified state out of the caption and the journey" in instructions


def test_repro_caption_and_journey_fields_carry_the_verification_rule() -> None:
    instructions = _instructions()

    assert "Name only states the test asserted or the final frame confirmed" in instructions
    assert 'an unverified negative such as "X never appears" does not belong here' in instructions
    assert "Write each `Observed:` line from what the page or output showed" in instructions
    assert "do not restate the expected failure model as an observation" in instructions


def test_unavailable_visual_inspection_limits_claims_to_assertions() -> None:
    instructions = _instructions()

    assert (
        "claim in the caption and journey only what those assertions established" in instructions
    )


def test_repro_agent_bundle_loads_the_verification_rule_as_instructions() -> None:
    spec = load(_REPRO_AGENT, expand_env=False)

    assert spec.instructions is not None
    assert "Caption only verified states" in _normalized(spec.instructions)
