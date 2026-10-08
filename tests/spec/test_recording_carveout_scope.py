"""Recording rules distinguish CLI output from internal results."""

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


def test_cli_lane_backs_every_captioned_step_with_a_wait() -> None:
    lanes = _normalized(_DEV / "recording-lanes.md")

    assert "Every captioned step needs its own `Wait`, not a `Sleep`" in lanes
    assert "claims that output rendered on screen" in lanes
    assert "A `Sleep` only passes time" in lanes
    assert "If a captioned `Wait` times out, that step did not happen as described" in lanes
    assert "never caption the intended output" in lanes


def test_cli_lane_pre_answers_harness_first_run_prompts_before_filming() -> None:
    lanes = _normalized(_DEV / "recording-lanes.md")

    assert "Pre-answer first-run prompts before filming a harness CLI" in lanes
    assert "Do you trust this folder?" in lanes
    assert "ensure_claude_workspace_trusted(Path(cwd))" in lanes
    assert 'projects["<abs cwd>"].hasTrustDialogAccepted' in lanes
    assert "`claude agents` then opens an interactive view that stays open" in lanes


def test_finishing_a_clip_checks_every_captioned_step() -> None:
    lanes = _normalized(_DEV / "recording-lanes.md")

    assert "Check every step the caption names, not only the last one" in lanes
    assert "ffmpeg -ss <seconds> -i <clip> -frames:v 1 step.png" in lanes
    assert "A command the tape typed and slept past has no evidence of its output" in lanes
    assert "an unanswered folder-trust dialog" in lanes


def test_repro_clip_rules_caption_each_step_from_its_own_evidence() -> None:
    instructions = _normalized(_REPRO_AGENT / "AGENTS.md")

    assert "Caption each step from its own evidence" in instructions
    assert "a `Sleep` after the command verifies nothing" in instructions
    assert "Claude Code's folder-trust dialog under a fresh `CLAUDE_CONFIG_DIR`" in instructions
    assert "caption the prompt or re-record — never the intended listing" in instructions
    assert 'is "run `claude agents`", not "`claude agents` lists the session"' in instructions


def test_repro_agent_loads_the_per_step_caption_rule_as_instructions() -> None:
    spec = load(_REPRO_AGENT, expand_env=False)

    assert spec.instructions is not None
    assert "Caption each step from its own evidence" in " ".join(spec.instructions.split())


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
