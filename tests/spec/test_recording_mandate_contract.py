"""Contract tests for the agents' recording mandate.

A view-visibility bug (content missing from chat while the terminal pane shows
it) can be verified structurally — a settings dict, a unit-level relay drive —
and then shipped with ``recordings: []`` by calling the facets "structural /
textual". These pin the instruction contract that forbids that: the textual
exemption is judged by the ticket's symptom, not by how the facet was verified.
"""

from pathlib import Path

_DEV = Path(__file__).resolve().parents[2] / "dev"


def _normalized(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_recording_lanes_bind_textual_exemption_to_ticket_symptom() -> None:
    lanes = _normalized(_DEV / "recording-lanes.md")

    assert (
        "Judge the textual exemption by the ticket's symptom, never by how you "
        "verified the facet." in lanes
    )
    assert "content missing from (or wrong in) a rendered view" in lanes
    assert "never reclassifies the facet as textual" in lanes
    assert "quotes the concrete error each documented lane produced" in lanes


def test_recording_lanes_allow_captioned_fixture_stand_ins_for_visibility_bugs() -> None:
    lanes = _normalized(_DEV / "recording-lanes.md")

    assert "fixture-driven stand-in" in lanes
    assert "the caption must say it is one" in lanes


def test_repro_instructions_restate_the_visibility_carve_out() -> None:
    instructions = _normalized(_DEV / "repro-agent" / "AGENTS.md")

    assert "Judge that exemption by the **ticket's symptom**" in instructions
    assert "content missing from (or wrong in) a rendered view" in instructions
    # The handoff-field description must not offer "purely textual" as an
    # escape hatch for a missing-content-in-a-view symptom.
    assert (
        "A symptom of content missing from (or wrong in) a rendered view is "
        'never "purely textual"' in instructions
    )
