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


def test_repro_agent_loads_the_screen_evidence_caption_rule_as_instructions() -> None:
    spec = load(_REPRO_AGENT, expand_env=False)
    assert "Caption the screen from screen evidence" in spec.instructions
    assert "Base screen claims on inspected frames" in _normalized(_DEV / "recording-lanes.md")


def test_lane_caption_rules_cover_the_whole_window_for_absence_claims() -> None:
    lanes = _normalized(_DEV / "recording-lanes.md")

    assert "covers the whole window from the trigger to the end state the caption names" in lanes
    assert "watches continuously until that end state" in lanes
    assert "support only a qualified claim that states the interval" in lanes
    assert "only shows what the final frame shows" in lanes
    assert "record how long it showed in `evidence`" in lanes
    assert "caption the end state and say the window was not checked" in lanes
    assert "whether a transient state is acceptable is a product question for review" in lanes
