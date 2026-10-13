"""Recording rules end clips on the assistant's reply, not the prompt's echo."""

from pathlib import Path

_DEV = Path(__file__).resolve().parents[2] / "dev"


def _normalized(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_web_lane_scopes_reply_waits_to_the_assistant_message() -> None:
    lanes = _normalized(_DEV / "recording-lanes.md")

    assert "End the clip on the assistant's reply, not on an echo of the prompt" in lanes
    assert "the user's own message bubble already contains the expected text" in lanes
    assert "passes the moment the prompt renders and proves nothing about the reply" in lanes
    assert "Scope the wait to the assistant's message, excluding the user's bubble" in lanes
    assert "wait for the working indicator to clear before stopping the recording" in lanes
    assert "treat that recording attempt as failed" in lanes


def test_lane_captions_are_checked_against_the_footage() -> None:
    lanes = _normalized(_DEV / "recording-lanes.md")

    assert "The caption is a claim about the footage" in lanes
    assert "confirm the final frames actually show the end state" in lanes
    assert "A driver's printed probe result is not that confirmation" in lanes
    assert "a check the prompt text itself satisfies is vacuous" in lanes
    assert "Caption only what is visible on screen" in lanes


def test_repro_clips_end_on_the_outcome_and_exclude_the_prompt_echo() -> None:
    instructions = _normalized(_DEV / "repro-agent" / "AGENTS.md")

    assert "caption only what the footage shows" in instructions
    assert "the user's own bubble echoes the marker" in instructions
    assert "a page-wide text check passes before any reply renders" in instructions
    assert "scope the wait to the assistant's message" in instructions
    assert "Never cite such a vacuous check as proof the reply arrived" in instructions


def test_resolve_after_clips_require_the_visible_reply() -> None:
    instructions = _normalized(_DEV / "resolve-agent" / "AGENTS.md")

    assert "End the clip on the corrected outcome actually rendering" in instructions
    assert "check the caption against the footage before declaring it" in instructions
    assert "scope the reply check to the assistant's message" in instructions
    assert "wait for the working indicator to clear before stopping the recording" in instructions
    assert "never cite the vacuous check as proof the answer arrived" in instructions
