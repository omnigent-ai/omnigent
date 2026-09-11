from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_REPRO_AGENT_INSTRUCTIONS = _REPO_ROOT / "dev" / "repro-agent" / "AGENTS.md"
_RECORDING_LANES = _REPO_ROOT / "dev" / "recording-lanes.md"


def _normalized(path: Path) -> str:
    """Whitespace-collapsed, lower-cased text so checks survive rewrapping/rewording."""
    return " ".join(path.read_text(encoding="utf-8").split()).lower()


def test_recording_mandate_treats_a_reproduction_owned_server_as_filmable() -> None:
    """A reproduction's own running server must be attempted as a recording target.

    We assert the *behavioural contract*, not any particular sentence: the agent
    instructions must (a) tell the agent to point the recorder at the server the
    reproduction already runs, via the real ``--ui-base-url`` mechanism, and
    (b) forbid skipping the lane on the stock fixture's limits alone — a skip is
    only justified by an attach that was attempted and failed.
    """
    instructions = _normalized(_REPRO_AGENT_INSTRUCTIONS)

    # (a) the reproduction-owned server is a recording target, driven by --ui-base-url
    assert "--ui-base-url" in instructions
    assert "recording target" in instructions
    # (b) the stock fixture's limit is not, by itself, a valid skip reason
    assert "stock fixture" in instructions
    assert "recording_unavailable_reason" in instructions
    assert "attempted" in instructions and "attach" in instructions


def test_recording_lanes_document_how_to_film_against_a_running_server() -> None:
    """The lane doc must carry the durable mechanics for a caller-owned server.

    These are stable identifiers (flags / env vars / field names), not prose, so
    the doc can be reworded freely without breaking the contract this guards.
    """
    lanes = _normalized(_RECORDING_LANES)

    assert "--ui-base-url" in lanes
    # the real safety-flag that lets a stand-in stack on a dev port through
    assert "omnigent_e2e_allow_dev_base_url" in lanes
    # the record dir that films every context the test opens
    assert "omnigent_e2e_record_dir" in lanes
    # a skip must be justified in the field, not by citing the fixture's limits
    assert "recording_unavailable_reason" in lanes
    assert "stock fixture" in lanes


def test_static_text_escape_excludes_transient_error_moments() -> None:
    """An error appearing mid-journey is watchable, not static text.

    The static-text escape covers outcomes where nothing on the surface moves; a
    mid-turn error surfacing in a live session is a temporal moment both docs must
    classify as filmable. We check the concept (a mid-journey/mid-turn error that
    the escape does not cover) rather than a fixed sentence.
    """
    for path in (_REPRO_AGENT_INSTRUCTIONS, _RECORDING_LANES):
        text = _normalized(path)
        assert "static text" in text, path
        # the escape is explicitly scoped away from a transient mid-journey error
        assert "mid-journey" in text or "mid-turn" in text, path
