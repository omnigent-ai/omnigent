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
