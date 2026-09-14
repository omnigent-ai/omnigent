"""The recording docs' textual carve-out must not cover CLI console output."""

from pathlib import Path

_DEV = Path(__file__).resolve().parents[2] / "dev"


def _normalized(path: Path) -> str:
    return " ".join(path.read_text(encoding="utf-8").split())


def test_lane_carveout_excludes_command_console_output() -> None:
    lanes = _normalized(_DEV / "recording-lanes.md")

    assert "Text a command prints to its console is never that carve-out" in lanes
    assert "its expired-login `omnigent host` example is exactly this shape" in lanes
    assert (
        '"The outcome is just a log line" never exempts a runnable command from footage.' in lanes
    )
    assert "an error string, a value, a log line" not in lanes


def test_lane_unavailable_reason_requires_concrete_blocker_on_cli() -> None:
    lanes = _normalized(_DEV / "recording-lanes.md")

    assert (
        "On a `cli`/`terminal` facet the reason must name a concrete tooling "
        "or reachability blocker (`vhs`/`ttyd` missing, the host/server won't "
        "boot)" in lanes
    )
    assert (
        '"the outcome is purely textual" and "the upstream handoff carried no '
        'recordings" are not accepted reasons there' in lanes
    )


def test_repro_carveout_scoped_like_the_lane_doc() -> None:
    instructions = _normalized(_DEV / "repro-agent" / "AGENTS.md")

    assert (
        "The no-footage carve-out applies only to `api` facets and to values "
        "only a test asserts" in instructions
    )
    assert (
        "A `cli`/`terminal` facet whose outcome is what a command prints is "
        "always filmed" in instructions
    )
    assert '"purely textual" is not an accepted reason on those facets' in instructions
