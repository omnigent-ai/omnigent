"""Recording rules distinguish CLI output from internal results and caption the screen."""

from pathlib import Path

from omnigent.spec import load

_DEV = Path(__file__).resolve().parents[2] / "dev"
_REPRO_AGENT = _DEV / "repro-agent"


def _normalized(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_lane_rules_distinguish_cli_output_from_internal_results() -> None:
    lanes = _normalized(_DEV / "recording-lanes.md")

    assert "record the real command and its output" in lanes
    assert "output is only an error message, hint, or status line" in lanes
    assert "`omnigent host` prints the wrong error after login expires" in lanes
    assert "written evidence is enough when no user interface shows the result" in lanes
    assert "an error string, a value, a log line" not in lanes


def test_lane_recording_blockers_are_explicit_and_do_not_block_delivery() -> None:
    lanes = _normalized(_DEV / "recording-lanes.md")

    assert "required tool is missing" in lanes
    assert "Name the specific blocker in `recording_unavailable_reason`" in lanes
    assert "Text-only CLI output is not a reason to skip recording" in lanes
    assert "A missing recording from an earlier run is not a reason either" in lanes
    assert "Do not block the verdict, fix, or PR" in lanes


def test_repro_recording_rules_match_the_shared_guide() -> None:
    instructions = _normalized(_REPRO_AGENT / "AGENTS.md")

    assert (
        "For internal/API-only results with no visible user interaction, "
        "written evidence is enough" in instructions
    )
    assert (
        "record the real command and its output, even if only an error message changes"
        in instructions
    )
    assert "Text-only CLI output is not a reason to skip recording" in instructions
    assert "name the specific blocker in `recording_unavailable_reason`" in instructions
    assert "Do not block the verdict because footage is missing or rejected" in instructions


def test_finishing_a_clip_captions_the_screen_from_screen_evidence() -> None:
    lanes = _normalized(_DEV / "recording-lanes.md")

    assert "every claim it makes about the screen needs screen evidence" in lanes
    assert "Backend state is not screen evidence" in lanes
    assert "says what the server stored, not what the viewer sees" in lanes
    assert (
        "streams each `MessageDisplay` chunk to the Chat view as a "
        "`response.output_text.delta` event" in lanes
    )
    assert "has already been painted on screen when the error pill appears" in lanes
    assert 'never "no reply bubble appears"' in lanes
    assert "Read every frame you extract against each screen claim in the caption" in lanes
    assert "Frames you extracted but never compared with the caption verify nothing" in lanes


def test_repro_agent_loads_the_screen_evidence_caption_rule_as_instructions() -> None:
    spec = load(_REPRO_AGENT, expand_env=False)

    assert spec.instructions is not None
    instructions = " ".join(spec.instructions.split())
    assert "Caption the screen from screen evidence" in instructions
    assert "proves what the server stored, not what the frames show" in instructions
    assert (
        "Claude-native streams the reply text to the Chat view before the final "
        "transcript item is posted" in instructions
    )
    assert (
        "Compare every frame you extract with each screen claim in the caption and in "
        "the Observed lines of `journey`" in instructions
    )
    assert "keep the backend check in `evidence`" in instructions
    assert (
        "Each screen claim comes from screen evidence, not from a transcript or API check"
        in instructions
    )
