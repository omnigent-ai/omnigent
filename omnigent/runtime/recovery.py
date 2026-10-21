"""Shared lifecycle instructions for continuing an interrupted task."""

from __future__ import annotations

import json
from dataclasses import asdict

from omnigent.onboarding.sandboxes.types import ResumeOutcome

DEFAULT_AUTO_RESUME_RECOVERY_PROMPT = (
    "This session was interrupted. Inspect the current workspace and task history, "
    "check the outcome of interrupted operations before repeating them, and continue "
    "the existing task from the state that is actually available. Verify which "
    "background processes are still running before restarting them."
)


def compose_recovery_prompt(outcome: ResumeOutcome, instructions: str) -> str:
    """Append provider-confirmed facts after the deployment's continuation instructions."""
    return (
        f"{instructions.strip()}\n\n"
        "The following JSON describes the provider's restoration result. "
        "Unknown means preservation has not been verified; partial or scoped restoration "
        "does not establish that other files survived. Checkpoint times describe the "
        "saved state, not the time of this restart.\n"
        f"{json.dumps(asdict(outcome), sort_keys=True)}"
    )
