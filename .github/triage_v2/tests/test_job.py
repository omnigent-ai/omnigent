from __future__ import annotations

import pytest

from issue_prioritization.job import validate_github_write_gate
from issue_prioritization.pipeline import PipelineMode


def test_dry_run_does_not_require_github_credentials() -> None:
    validate_github_write_gate(PipelineMode.DRY_RUN, "false", "")


def test_apply_requires_both_write_gate_and_secret_scope() -> None:
    with pytest.raises(RuntimeError, match="allow_github_writes is false"):
        validate_github_write_gate(PipelineMode.APPLY, "false", "scope")

    with pytest.raises(RuntimeError, match="github_secret_scope is required"):
        validate_github_write_gate(PipelineMode.APPLY, "true", "")

    validate_github_write_gate(PipelineMode.APPLY, "true", "scope")


def test_legacy_adoption_requires_read_credentials_but_not_write_gate() -> None:
    with pytest.raises(RuntimeError, match="github_secret_scope is required"):
        validate_github_write_gate(
            PipelineMode.DRY_RUN,
            "false",
            "",
            adopt_legacy_bot_priorities=True,
        )

    validate_github_write_gate(
        PipelineMode.DRY_RUN,
        "false",
        "scope",
        adopt_legacy_bot_priorities=True,
    )
