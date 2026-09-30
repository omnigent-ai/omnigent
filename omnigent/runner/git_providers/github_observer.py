"""GitHub's rules for the PR observer: ``gh pr`` and ``gh api`` commands and GitHub MCP tools.

The GitHub pull request facet calls these functions for its observer methods.
:mod:`omnigent.runner.pr_observer` applies the provider-neutral attribution
rules to their answers.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import PurePath

from omnigent.runner.git_providers import ShellPrOp, ShellSegment
from omnigent.runner.git_providers.tool_output import output_text, pr_reference, result_objects
from omnigent.runner.session_prs import PullRequestRef

_PR_WRITES = {
    "create",
    "edit",
    "merge",
    "close",
    "reopen",
    "ready",
    "lock",
    "unlock",
    "update-branch",
}
_MCP_REVIEWS = {
    "create_pull_request_review",
    "submit_pending_pull_request_review",
    "pull_request_review_write",
}
_MCP_ACTIONS = {
    "create_pull_request",
    "update_pull_request",
    "merge_pull_request",
    "update_pull_request_branch",
    *_MCP_REVIEWS,
}
# The ``write_api_call`` proxy names a REST operation in its ``endpoint`` argument.
_WRITE_API_OPERATIONS = {
    "pull_requests.create": "create_pull_request",
    "pull_requests.update": "update_pull_request",
    "pull_requests.merge": "merge_pull_request",
    "pulls.create": "create_pull_request",
    "pulls.update": "update_pull_request",
    "pulls.merge": "merge_pull_request",
}


def _flag(tokens: list[str], *names: str) -> str | None:
    for index, token in enumerate(tokens):
        for name in names:
            if token == name and index + 1 < len(tokens):
                return tokens[index + 1]
            if token.startswith(name + "="):
                return token[len(name) + 1 :]
            if len(name) == 2 and token.startswith(name) and len(token) > 2:
                return token[2:]
    return None


def _api_endpoint(tokens: list[str]) -> str | None:
    values = {
        "--method",
        "-X",
        "--field",
        "-F",
        "--raw-field",
        "-f",
        "--jq",
        "-q",
        "--template",
        "-t",
        "--hostname",
        "--input",
        "--header",
        "-H",
        "--cache",
        "--preview",
        "-p",
    }
    switches = {"--paginate", "--slurp", "--silent", "--include", "-i", "--verbose"}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("-"):
            return token
        if token in values:
            index += 2
        elif token in switches or any(
            token.startswith(flag + "=") or (len(flag) == 2 and token.startswith(flag))
            for flag in values
        ):
            index += 1
        else:
            return None
    return None


def _api_method(tokens: list[str]) -> str:
    method = _flag(tokens, "--method", "-X")
    if method is None:
        method = (
            "POST" if _flag(tokens, "--field", "--raw-field", "-f", "-F", "--input") else "GET"
        )
    return method.upper()


def _api_field(tokens: list[str], field: str) -> str | None:
    flags = ("--field", "--raw-field", "-f", "-F")
    index = 0
    while index < len(tokens):
        value = _flag(tokens[index : index + 2], *flags)
        if value is not None:
            key, separator, content = value.partition("=")
            if key == field and separator:
                return content
            if tokens[index] in flags:
                index += 1
        index += 1
    return None


def _changes_review_state(event: object) -> bool:
    return isinstance(event, str) and event.upper() in {"APPROVE", "REQUEST_CHANGES"}


def _tracks_pr(tokens: list[str]) -> bool:
    """Track PR changes, excluding reads and comment-only interactions."""
    if tokens[0] == "pr":
        if len(tokens) < 2:
            return False
        if tokens[1] == "review":
            return bool({"--approve", "-a", "--request-changes", "-r"}.intersection(tokens[2:]))
        return tokens[1] in _PR_WRITES
    if tokens[0] != "api" or _api_method(tokens) not in {"POST", "PATCH", "PUT", "DELETE"}:
        return False
    endpoint = (_api_endpoint(tokens[1:]) or "").split("?", 1)[0]
    path = endpoint.strip("/").split("/")
    # GraphQL POSTs can be queries or comment mutations; HTTP method alone is insufficient.
    if path[0] != "repos" or len(path) < 4:
        return False
    resource = path[3:]
    if "comments" in resource:
        return False
    if "reviews" in resource:
        return _changes_review_state(_api_field(tokens, "event"))
    return True


def _creates_pr(tokens: list[str]) -> bool:
    if tokens[:2] == ["pr", "create"]:
        return True
    if tokens[0] != "api":
        return False
    endpoint = (_api_endpoint(tokens[1:]) or "").split("?", 1)[0]
    return (
        _api_method(tokens) == "POST"
        and re.fullmatch(r"/?repos/[^/]+/[^/]+/pulls/?", endpoint) is not None
    )


def _positional_target(tokens: list[str]) -> str | None:
    # Unknown flags are deliberately ambiguous; output URLs can still identify the PR.
    values = {
        "--repo",
        "-R",
        "--title",
        "-t",
        "--body",
        "-b",
        "--body-file",
        "-F",
        "--base",
        "-B",
        "--add-assignee",
        "--remove-assignee",
        "--add-label",
        "--remove-label",
        "--add-project",
        "--remove-project",
        "--add-reviewer",
        "--remove-reviewer",
        "--milestone",
        "-m",
        "--subject",
        "--author-email",
        "--match-head-commit",
        "--branch",
        "--reason",
        "--json",
        "--jq",
        "-q",
        "--template",
        "--color",
    }
    switches = {
        "--approve",
        "-a",
        "--request-changes",
        "-r",
        "--comment",
        "-c",
        "--delete-branch",
        "-d",
        "--admin",
        "--auto",
        "--disable-auto",
        "--merge",
        "--squash",
        "-s",
        "--rebase",
        "--draft",
        "--undo",
        "--force",
        "-f",
        "--detach",
        "--remove-milestone",
        "--edit-last",
        "--create-if-none",
        "--yes",
        "--web",
        "-w",
        "--comments",
        "--patch",
        "--name-only",
    }
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("-"):
            return token
        if token in values:
            index += 2
        elif token in switches or any(
            token.startswith(flag + "=") or (len(flag) == 2 and token.startswith(flag))
            for flag in values
        ):
            index += 1
        else:
            return None
    return None


def _target(repository: object, number: object, host: str = "github.com") -> PullRequestRef | None:
    if isinstance(repository, str) and isinstance(number, (str, int)):
        parts = repository.split("/")
        if len(parts) == 3:
            host, repository = parts[0], "/".join(parts[1:])
        return pr_reference(f"https://{host}/{repository}/pull/{number}")
    return None


def _command_target(tokens: list[str]) -> PullRequestRef | None:
    host = _flag(tokens, "--hostname") or "github.com"
    if tokens[0] == "api":
        endpoint = (_api_endpoint(tokens[1:]) or "").split("?", 1)[0]
        match = re.match(r"/?repos/([^/]+/[^/]+)/pulls/([1-9][0-9]*)(?:/|$)", endpoint)
        return _target(match[1], match[2], host) if match else None
    if tokens[0] == "pr" and len(tokens) > 1 and tokens[1] != "create":
        target = _positional_target(tokens[2:])
        if ref := pr_reference(target):
            return ref
        if target and target.isdigit():
            return _target(_flag(tokens, "--repo", "-R"), target, host)
    return None


def _content_only(tokens: list[str]) -> bool:
    fields = _flag(tokens, "--json")
    return (
        tokens[:2] == ["pr", "diff"]
        or _flag(tokens, "--jq", "-q") in {".body", ".[].body"}
        or (
            tokens[0] == "pr"
            and fields is not None
            and set(fields.split(",")) <= {"body", "title"}
        )
    )


def _gh_arguments(segment: ShellSegment) -> list[str] | None:
    """Return the arguments of a ``gh`` segment, or ``None`` for another program.

    Leading ``-R`` / ``--repo`` options move after the subcommand's arguments, and a
    ``GH_HOST`` assignment becomes ``--hostname``, so the rules read both as flags.
    """
    if PurePath(segment.invocation_tokens[0]).name != "gh":
        return None
    args, prefix = list(segment.invocation_tokens[1:]), []
    while args and args[0].startswith("-"):
        if args[0] in {"-R", "--repo"} and len(args) > 1:
            prefix.extend(args[:2])
            args = args[2:]
        elif args[0].startswith(("-R", "--repo=")):
            prefix.append(args[0])
            args = args[1:]
        else:
            break
    host = next((t.split("=", 1)[1] for t in segment.raw_tokens if t.startswith("GH_HOST=")), None)
    if host:
        prefix.extend(["--hostname", host])
    return [*args, *prefix]


def shell_pr_operations(segments: Sequence[ShellSegment]) -> list[ShellPrOp]:
    """Return one op per ``gh pr`` or ``gh api`` segment, in order.

    Other ``gh`` commands, such as ``gh auth`` and ``gh config``, are setup that
    neither identifies a PR nor hides one, so they produce no op.
    """
    ops: list[ShellPrOp] = []
    for segment in segments:
        tokens = _gh_arguments(segment)
        if not tokens or tokens[0] not in {"pr", "api"}:
            continue
        ops.append(
            ShellPrOp(
                tracks=_tracks_pr(tokens),
                creates=_creates_pr(tokens),
                target=_command_target(tokens),
                content_only=_content_only(tokens),
            )
        )
    return ops


def _mcp_prs(
    arguments: dict[str, object], result: object, *, created: bool
) -> list[PullRequestRef]:
    """Prefer structured identity; fall back to an unambiguous URL in output text."""
    owner, repo = arguments.get("owner"), arguments.get("repo")
    repository = (
        f"{owner}/{repo}".lower() if isinstance(owner, str) and isinstance(repo, str) else None
    )
    host = arguments.get("hostname", arguments.get("host"))
    host = host if isinstance(host, str) else "github.com"
    number = arguments.get("pullNumber", arguments.get("pull_number"))
    target = _target(repository, number, host) if not created else None

    def matches(ref: PullRequestRef) -> bool:
        return (repository is None or ref.repository == repository) and (
            target is None or ref.number == target.number
        )

    references = []
    for obj in result_objects(result):
        ref = pr_reference(obj.get("html_url", obj.get("url"))) or _target(
            repository, obj.get("number"), host
        )
        if ref and matches(ref):
            references.append(ref)
    if references:
        return references
    if target:
        return [target]
    urls = {
        ref.url: ref
        for url in re.findall(r"https://[^\s<>\"'`]+", output_text(result))
        if (ref := pr_reference(url)) and matches(ref)
    }
    return list(urls.values()) if len(urls) == 1 else []


def mcp_prs(
    tool_name: str, arguments: dict[str, object], result: object
) -> tuple[list[PullRequestRef], bool] | None:
    """Read a call of a GitHub pull request MCP tool on any server, or of ``write_api_call``.

    :returns: ``(references, created)``; empty references for a review that
        only comments; ``None`` when the call is not a GitHub PR operation.
    """
    name = tool_name.rsplit("__", 1)[-1].removeprefix("github_")
    if name == "write_api_call":
        endpoint = arguments.get("endpoint")
        name = _WRITE_API_OPERATIONS.get(endpoint, "") if isinstance(endpoint, str) else ""
        params = arguments.get("params")
        if isinstance(params, dict):
            arguments = {**params, "owner": params.get("owner", params.get("org"))}
    if name not in _MCP_ACTIONS:
        return None
    if name in _MCP_REVIEWS and not _changes_review_state(arguments.get("event")):
        return [], False
    created = name == "create_pull_request"
    return _mcp_prs(arguments, result, created=created), created
