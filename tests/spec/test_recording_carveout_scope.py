"""Recording rules distinguish CLI output from internal results and caption the screen."""

from pathlib import Path

from omnigent.spec import load

_DEV = Path(__file__).resolve().parents[2] / "dev"
_REPRO_AGENT = _DEV / "repro-agent"


def _normalized(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def _loaded_repro_instructions() -> str:
    spec = load(_REPRO_AGENT, expand_env=False)
    return " ".join(spec.instructions.split())


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
    assert "A blocker is the observed result of a recorder run" in lanes
    assert "A reason that names no attempted recorder run" in lanes
    assert "Text-only CLI output is not a reason to skip recording" in lanes
    assert "A missing recording from an earlier run is not a reason either" in lanes
    assert "Do not block the verdict, fix, or PR" in lanes


def test_repro_recording_rules_match_the_shared_guide() -> None:
    instructions = _normalized(_REPRO_AGENT / "AGENTS.md")
    lanes = _normalized(_DEV / "recording-lanes.md")

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
    assert "An absence is filmable" in instructions
    assert "An absence is filmable" in lanes
    assert "a reason that names no attempted recorder run is insufficient" in instructions


def test_repro_agent_films_likely_repro_facets_as_before_clips() -> None:
    instructions = _loaded_repro_instructions()
    lanes = _normalized(_DEV / "recording-lanes.md")

    assert (
        "a **`reproduced`** or **`likely_repro`** facet → **before-fix footage**" in instructions
    )
    assert '`"before"` for a `reproduced` or `likely_repro` facet\'s failing run' in instructions
    assert (
        "A `likely_repro` clip films the stand-in you drove; its caption names that stand-in"
        in instructions
    )
    assert (
        "`not_reproduced`, `needs_more_info`, and `needs_manual_review` facets have "
        "nothing to film" in instructions
    )
    assert "a `reproduced` or `likely_repro` facet yields a **`before`** clip" in lanes


def test_repro_agent_requires_a_recorder_attempt_once_the_preflight_check_passed() -> None:
    instructions = _loaded_repro_instructions()

    assert "expects a recorder run for every filmable facet when they are present" in instructions
    assert (
        "When the preflight recorder check passed, point the recorder at the facet's lane"
        in instructions
    )
    assert "name the command you ran and its observed error" in instructions
    assert '"The recorder was not stood up"' in instructions


def test_repro_agent_loads_the_screen_evidence_caption_rule_as_instructions() -> None:
    spec = load(_REPRO_AGENT, expand_env=False)
    assert "Caption the screen from screen evidence" in spec.instructions
    assert "Base screen claims on inspected frames" in _normalized(_DEV / "recording-lanes.md")


def test_caption_rules_reach_the_agent_without_treating_hidden_labels_as_visible() -> None:
    lanes = _normalized(_DEV / "recording-lanes.md")
    instructions = " ".join(load(_REPRO_AGENT, expand_env=False).instructions.split())
    assert "in the recorded viewport and unobstructed" in lanes
    assert "Quote only text the viewer can see" in lanes
    assert "quote only visibly rendered text" in instructions
    assert "hidden accessibility labels and backend logs are not screen evidence" in instructions


def test_caption_absence_claims_stay_scoped_to_the_checked_element() -> None:
    lanes = _normalized(_DEV / "recording-lanes.md")
    instructions = _loaded_repro_instructions()
    assert "A driver check covers only the element it targets" in lanes
    assert "a spinner on its sidebar row, and a sub-agent count badge" in lanes
    assert "An absence check on the chat pane's indicator supports" in lanes
    assert "Check every such place before claiming that none shows the state" in lanes
    assert "An absence check covers only the element it targets" in instructions
    assert "name the checked element" in instructions
    assert "after checking only the chat pane" in instructions
    assert "unless every place showing that state was checked" in instructions
