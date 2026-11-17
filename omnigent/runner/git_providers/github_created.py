"""Verify PR URLs a shell create printed that stdout alone cannot attribute to it."""

from __future__ import annotations

import functools
import tempfile
import time
from datetime import UTC, datetime

from omnigent.runner.git_providers import ShellPrOp
from omnigent.runner.git_providers.tool_output import output_text, pr_reference
from omnigent.runner.session_prs import PullRequestRef

# A PR this call created was opened during it; older PRs in the output came from reads.
_CREATED_WINDOW_S = 600.0
# GitHub stamps ``created_at``; the runner's clock may lag it.
_CLOCK_SKEW_S = 120.0
_MAX_CANDIDATES = 20


def created_lookup(
    *,
    mixed: bool,
    excluded: frozenset[str] = frozenset(),
    excluded_numbers: frozenset[int] = frozenset(),
) -> ShellPrOp:
    """Describe a deferred check of stdout PR URLs without changing stdout attribution.

    :param mixed: Reads share the call's stdout, so the immediate scan skipped it.
    :param excluded: URLs of PRs the call's commands name, which existed before it.
    :param excluded_numbers: Numbers of named PRs whose repository is implicit; any
        candidate with one is skipped.
    """
    return ShellPrOp(
        tracks=True,
        creates=True,
        target=None,
        content_only=True,
        resolve_result=functools.partial(
            created_prs, mixed=mixed, excluded=excluded, excluded_numbers=excluded_numbers
        ),
    )


def created_prs(
    result: object,
    *,
    mixed: bool = True,
    excluded: frozenset[str] = frozenset(),
    excluded_numbers: frozenset[int] = frozenset(),
) -> list[PullRequestRef]:
    """Return the bare-line GitHub PR URLs in *result* that a local ``gh`` account just opened.

    Another author's PR, or one opened outside the window, came from other output.
    Without reads, stdout needs this check only when Claude's creation metadata, which
    names just the last PR created, replaced the immediate scan.
    """
    from omnigent.runner import github_resource
    from omnigent.runner.pr_observer import _created_pr_metadata, _failed

    if _failed(result):
        return []
    metadata = _created_pr_metadata(result)
    if not mixed and metadata is None:
        return []
    # A separate stderr holds diagnostics, which may mention unrelated PRs.
    if isinstance(result, dict):
        text = output_text({key: value for key, value in result.items() if key != "stderr"})
    else:
        text = output_text(result)
    candidates: dict[str, PullRequestRef] = {}
    for line in text.splitlines():
        if len(line.split()) == 1 and (reference := pr_reference(line.strip())):
            if reference.provider == "github" and reference.number not in excluded_numbers:
                candidates[reference.url] = reference
    for url in {*excluded, *([metadata.url] if metadata else [])}:
        candidates.pop(url, None)
    if not candidates or len(candidates) > _MAX_CANDIDATES:
        return []
    references: list[PullRequestRef] = []
    now = time.time()
    deadline = time.monotonic() + 5.0
    root = tempfile.gettempdir()
    with github_resource._gh_deadline_scope(deadline):
        _, accounts = github_resource._list_accounts(root)
        signed_in = [account for account in accounts if account.get("state") == "success"]
        logins = {
            (str(account["host"]).lower(), str(account["login"]).lower()) for account in signed_in
        }
        tokens: dict[tuple[str, str], str | None] = {}

        def lookup(reference: PullRequestRef) -> str | None:
            """Read the PR as the default account, then as each other signed-in account."""
            # A session may reach a private repo through GH_TOKEN for a non-default account.
            alternates = (
                []
                if github_resource._in_sandbox()
                else [
                    str(account["login"])
                    for account in signed_in
                    if not account.get("active")
                    and str(account["host"]).lower() == reference.host.lower()
                ]
            )
            for login in [None, *alternates]:
                token = None
                if login is not None:
                    key = (reference.host, login)
                    if key not in tokens:
                        tokens[key] = github_resource._gh_auth_token(root, login, reference.host)
                    if (token := tokens[key]) is None:
                        continue
                code, output, _ = github_resource._gh(
                    [
                        "api",
                        "--hostname",
                        reference.host,
                        f"repos/{reference.repository}/pulls/{reference.number}",
                        "--jq",
                        '[.user.login, .created_at] | join(" ")',
                    ],
                    cwd=root,
                    token=token,
                    timeout=5.0,
                )
                if code == 0:
                    return output
            return None

        for reference in candidates.values():
            if time.monotonic() >= deadline:
                break
            # Stdout names any host; query only hosts a local account signs in to.
            if reference.host.lower() not in {host for host, _ in logins}:
                continue
            if (output := lookup(reference)) is None:
                continue
            author, _, created_at = output.strip().partition(" ")
            if (reference.host.lower(), author.lower()) not in logins:
                continue
            try:
                opened = datetime.fromisoformat(created_at)
            except ValueError:
                continue
            if opened.tzinfo is None:
                opened = opened.replace(tzinfo=UTC)
            if -_CLOCK_SKEW_S <= now - opened.timestamp() <= _CREATED_WINDOW_S:
                references.append(reference)
    return references
