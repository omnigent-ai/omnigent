"""Resolve existing GitHub PRs for branches a shell call pushed."""

from __future__ import annotations

import functools
import json
import re
import tempfile
import time
from collections.abc import Iterable, Sequence
from pathlib import PurePath
from urllib.parse import quote

from omnigent.git_providers import resolve_remote
from omnigent.policies.builtins.github import _git_subcommand_index
from omnigent.runner.git_providers import ShellPrOp, ShellSegment
from omnigent.runner.git_providers.push_reflog import mark_reported, recent_pushes
from omnigent.runner.git_providers.tool_output import output_text, pr_reference
from omnigent.runner.session_prs import PullRequestRef

_NORMAL_UPDATE = re.compile(
    r"^[ \t]*[+*]?[ \t]*(?:[0-9a-f]{4,64}\.\.\.?[0-9a-f]{4,64}|\[new branch\])"
    r"\s+\S+\s+->\s+(\S+)(?:\s+\(forced update\))?\s*$"
)
_PORCELAIN_UPDATE = re.compile(
    r"^[ \t]*[+*]?[ \t]*[^\s:]+:refs/heads/([^\s]+)\t"
    r"(?:[0-9a-f]{4,64}\.\.\.?[0-9a-f]{4,64}|\[new branch\])"
    r"(?: \(forced update\))?\s*$"
)
# ponytail: quiet pushes need native remote/ref metadata before they can be tracked.
_PUSH_FLAGS = frozenset(
    {
        "--all",
        "--atomic",
        "--force",
        "--force-with-lease",
        "--force-if-includes",
        "--set-upstream",
        "--no-verify",
        "--verify",
        "--thin",
        "--no-thin",
        "--porcelain",
        "--verbose",
        "--progress",
        "--ipv4",
        "--ipv6",
        "--signed",
        "--no-signed",
    }
)
_PUSH_VALUE_OPTIONS = frozenset({"-o", "--push-option", "--receive-pack", "--exec", "--repo"})


def _push_count(segments: Sequence[ShellSegment]) -> int:
    """Recognize pushes whose shared output can identify updated branches."""
    count = 0
    for segment in segments:
        if not segment.output_eligible:
            return 0
        tokens = list(segment.invocation_tokens)
        if not tokens or PurePath(tokens[0]).name != "git":
            continue
        try:
            index = _git_subcommand_index(tokens)
        except ValueError:
            return 0
        if index is None or tokens[index] != "push":
            continue
        arguments = iter(tokens[index + 1 :])
        for argument in arguments:
            option, equals, _ = argument.partition("=")
            if option in _PUSH_VALUE_OPTIONS:
                if not equals and next(arguments, None) is None:
                    return 0
                continue
            if argument == "--":
                continue
            if argument.startswith("--"):
                if option not in _PUSH_FLAGS:
                    return 0
                continue
            if argument.startswith("-"):
                if not (re.fullmatch(r"-[ufv46]+", argument) or argument.startswith("-o")):
                    return 0
                continue
            if (
                argument.startswith((":", "+:"))
                or any(
                    part.startswith("refs/") and not part.startswith("refs/heads/")
                    for part in argument.lstrip("+").split(":")
                )
                or argument == "tag"
            ):
                return 0
        count += 1
    return count


def push_operation(segments: Sequence[ShellSegment]) -> ShellPrOp | None:
    """Describe a push for deferred resolution without changing stdout attribution.

    A call that ran in a known checkout always gets one, since an alias or script
    can push without a recognizable ``git push`` in the command.
    """
    if not _push_count(segments) and not any(segment.cwd for segment in segments):
        return None
    return ShellPrOp(
        tracks=True,
        creates=False,
        target=None,
        content_only=True,
        resolve_result=functools.partial(pushed_prs, tuple(segments)),
        # A command can push and then fail, e.g. "git push -q; make test"; only the
        # reflog of a known checkout shows such a push.
        resolve_after_failure=any(segment.cwd for segment in segments),
    )


def pushed_prs(segments: Sequence[ShellSegment], result: object) -> list[PullRequestRef]:
    """Find open PRs whose head matches a branch positively updated by this push."""
    from omnigent.runner.pr_observer import _failed, _unfinished

    if _unfinished(result):
        return []
    # Output names a push only when the call succeeded; the reflog records one either way.
    targets = dict.fromkeys(() if _failed(result) else _output_targets(segments, result))
    pushes: dict[tuple[str, str, str], list[tuple[str, str, str, int]]] = {}
    for push in recent_pushes(segment.cwd for segment in segments if segment.cwd):
        remote = resolve_remote(push.url)
        if remote is not None and remote.provider == "github":
            target = (remote.host, remote.repository, push.branch)
            targets[target] = None
            pushes.setdefault(target, []).append(push.key)
    references, looked_up = _open_prs(targets)
    # A failed or skipped lookup leaves the push for the next shell call to retry.
    mark_reported(key for target in looked_up for key in pushes.get(target, []))
    return references


def _output_targets(
    segments: Sequence[ShellSegment], result: object
) -> list[tuple[str, str, str]]:
    """Return ``(host, repository, branch)`` for each update in literal ``git push`` output."""
    count = _push_count(segments)
    if not count:
        return []
    text = output_text(result)
    # Claude Bash merges process streams into stdout; its stderr holds diagnostics.
    # Other shell envelopes keep normal git status separate from command stdout.
    if (
        isinstance(result, dict)
        and result.get("interrupted") is not False
        and isinstance(result.get("stderr"), str)
    ):
        stderr = result["stderr"]
        if not any("--porcelain" in segment.invocation_tokens for segment in segments):
            text = stderr
    if re.search(r"(?m)^(?:fatal:|error:| ?!\s)", text):
        return []
    if len(re.findall(r"(?m)^(?:To |Everything up-to-date\s*$)", text)) > count:
        return []
    remote = None
    targets: dict[tuple[str, str, str], None] = {}
    for line in text.splitlines():
        if line.startswith("To "):
            url = line[3:].strip()
            # Git removes the SSH user from its scp-style status header.
            if re.fullmatch(r"[\w.-]+\.[\w.-]+:[^\s]+", url):
                url = "git@" + url
            remote = resolve_remote(url)
        elif remote is not None and remote.provider == "github":
            match = _PORCELAIN_UPDATE.fullmatch(line) or _NORMAL_UPDATE.fullmatch(line)
            if not match:
                remote = None
            elif not match[1].startswith("refs/") or match[1].startswith("refs/heads/"):
                branch = match[1].removeprefix("refs/heads/")
                targets[(remote.host, remote.repository, branch)] = None
    return list(targets)


def _open_prs(
    targets: Iterable[tuple[str, str, str]],
) -> tuple[list[PullRequestRef], set[tuple[str, str, str]]]:
    """Return the open PRs whose head is each ``(host, repository, branch)``.

    Also returns the targets GitHub answered for, whether or not they have a PR.
    """
    from omnigent.runner import github_resource

    references: dict[str, PullRequestRef] = {}
    answered: set[tuple[str, str, str]] = set()
    deadline = time.monotonic() + 5.0
    with github_resource._gh_deadline_scope(deadline):
        for host, repository, branch in targets:
            if time.monotonic() >= deadline:
                break
            endpoint = f"repos/{repository}/commits/{quote(branch, safe='')}/pulls"
            code, output, _ = github_resource._gh(
                ["api", "--hostname", host, endpoint], cwd=tempfile.gettempdir(), timeout=5.0
            )
            if code != 0:
                continue
            try:
                rows = json.loads(output)
            except ValueError:
                continue
            if not isinstance(rows, list):
                continue
            answered.add((host, repository, branch))
            for row in rows:
                if not isinstance(row, dict) or row.get("state") != "open":
                    continue
                head = row.get("head")
                if not isinstance(head, dict):
                    continue
                head_repo = head.get("repo")
                full_name = head_repo.get("full_name") if isinstance(head_repo, dict) else None
                if (
                    isinstance(full_name, str)
                    and full_name.lower() == repository.lower()
                    and head.get("ref") == branch
                    and (reference := pr_reference(row.get("html_url"))) is not None
                    and reference.provider == "github"
                    and reference.host == host
                ):
                    references[reference.url] = reference
    return list(references.values()), answered
