from pathlib import Path

from omnigent.spec import load

_RESOLVE_AGENT = Path(__file__).resolve().parents[2] / "dev" / "resolve-agent"


def test_resolve_agent_delegates_independent_review_to_polly() -> None:
    spec = load(_RESOLVE_AGENT)
    instructions = (_RESOLVE_AGENT / "AGENTS.md").read_text(encoding="utf-8")

    assert spec.spawn is False
    assert "cross_review" not in instructions
    assert "independent cross-vendor review" not in instructions


def test_resolve_agent_bounds_local_validation() -> None:
    instructions = (_RESOLVE_AGENT / "AGENTS.md").read_text(encoding="utf-8")
    normalized = " ".join(instructions.split())

    assert "Run only the directly affected test file/module" in normalized
    assert "Do not run the full repository suite" in normalized
    assert "GitHub CI owns that exhaustive coverage after publication" in normalized


def test_resolve_agent_stages_the_ci_bundle_inside_the_worktree() -> None:
    """The ci_link recovery must name an in-worktree bundle destination.

    The runner's file tools are worktree-scoped, so a bundle staged under
    /tmp or $RUNNER_TEMP errors every file-tool read and forces shell
    fallbacks; the instructions must point the download inside the worktree.
    """
    instructions = (_RESOLVE_AGENT / "AGENTS.md").read_text(encoding="utf-8")
    normalized = " ".join(instructions.split())

    assert ".omnigent/repro-bundle" in normalized
    assert "file tools are worktree-scoped" in normalized
    assert "`/tmp` or `$RUNNER_TEMP`" in normalized


def _normalized_resolve_instructions() -> str:
    text = (_RESOLVE_AGENT / "AGENTS.md").read_text(encoding="utf-8")
    return " ".join(text.split())


def test_textual_carveout_scoped_to_api_and_test_asserted_values() -> None:
    normalized = _normalized_resolve_instructions()

    assert (
        "The no-footage carve-out applies **only** to `api` facets and to "
        "values only a test asserts" in normalized
    )
    assert "just a static line, value, or the absence of an error" not in normalized
    assert "For purely textual evidence" not in normalized


def test_cli_console_output_is_always_filmed() -> None:
    normalized = _normalized_resolve_instructions()

    assert (
        "A `cli`/`terminal` facet whose fixed outcome is what a command prints "
        "on its console is **always filmed**" in normalized
    )
    assert "expired-login `omnigent host` example" in normalized


def test_unavailable_reason_rejects_purely_textual_on_cli_facets() -> None:
    normalized = _normalized_resolve_instructions()

    assert (
        "On a `cli`/`terminal` facet the reason must name a concrete tooling "
        "or reachability blocker (`vhs`/`ttyd` missing, the host/server won't "
        "boot)" in normalized
    )
    assert (
        '"the outcome is purely textual" and "the repro handoff carried no '
        'recordings" are not accepted reasons on those facets' in normalized
    )
