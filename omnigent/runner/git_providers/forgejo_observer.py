"""Network-free attribution of successful fj, tea, Forgejo MCP, and AGit pull request writes."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from omnigent.git_providers import EnvInstances
from omnigent.git_providers.forgejo import ForgejoProvider
from omnigent.runner.git_providers import ShellPrOp, ShellSegment
from omnigent.runner.session_prs import PullRequestRef


def reference(value: object) -> PullRequestRef | None:
    """Read only a Forgejo pull request URL on an owned authority."""
    if not isinstance(value, str):
        return None
    parsed = ForgejoProvider().parse_pr_url(value, EnvInstances())
    return PullRequestRef(**vars(parsed)) if parsed else None


def shell_pr_operations(segments: Sequence[ShellSegment]) -> list[ShellPrOp]:
    """No shell command is recognized yet."""
    del segments
    return []


def pr_from_object(obj: Mapping[str, object]) -> PullRequestRef | None:
    """Forgejo objects name their pull request by ``html_url``, which the observer reads."""
    del obj
    return None


def mcp_prs(
    tool_name: str, arguments: dict[str, object], result: object
) -> tuple[list[PullRequestRef], bool] | None:
    """No MCP tool is recognized yet."""
    del tool_name, arguments, result
    return None
