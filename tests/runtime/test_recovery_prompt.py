"""Recovery instructions distinguish retained state from partial checkpoints."""

from omnigent.onboarding.sandboxes.types import RestoredState, ResumeOutcome
from omnigent.runtime.recovery import DEFAULT_AUTO_RESUME_RECOVERY_PROMPT, compose_recovery_prompt


def test_recovery_prompt_keeps_partial_and_unknown_state_explicit() -> None:
    outcome = ResumeOutcome(
        workspace=RestoredState(
            status="partial", scope="tracked repository files", checkpoint_at=42
        )
    )
    prompt = compose_recovery_prompt(outcome, "Continue the pending task.")
    assert prompt.startswith("Continue the pending task.\n\n")
    assert '"checkpoint_at": 42' in prompt
    assert '"scope": "tracked repository files"' in prompt
    assert '"status": "partial"' in prompt
    assert '"status": "unknown"' in prompt
    assert "does not establish that other files survived" in prompt
    assert "same workspace" not in DEFAULT_AUTO_RESUME_RECOVERY_PROMPT
