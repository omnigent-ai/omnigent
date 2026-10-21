"""GitHub's rules for the PR observer: ``gh pr`` and ``gh api`` commands and GitHub MCP tools.

The GitHub pull request facet calls these functions for its observer methods.
:mod:`omnigent.runner.pr_observer` applies the provider-neutral attribution
rules to their answers.
"""

from __future__ import annotations

import functools
import json
import re
from collections import Counter
from collections.abc import Sequence
from pathlib import PurePath

from omnigent.runner.git_providers import ShellPrOp, ShellSegment
from omnigent.runner.git_providers.tool_output import output_text, pr_reference, result_objects
from omnigent.runner.session_prs import PullRequestRef

_GRAPHQL_NAME = re.compile(r"[_A-Za-z][_0-9A-Za-z]*")
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


def _flag(tokens: list[str], *names: str, last: bool = False) -> str | None:
    # Scalar formatter flags use the final occurrence, including mixed aliases.
    indices = range(len(tokens) - 1, -1, -1) if last else range(len(tokens))
    for index in indices:
        token = tokens[index]
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


def _graphql_create_field(tokens: list[str]) -> str | None:
    """Recognize a single literal createPullRequest mutation and its response key."""
    if (
        tokens[0] != "api"
        or (_api_endpoint(tokens[1:]) or "").strip("/") != "graphql"
        or _api_method(tokens) != "POST"
    ):
        return None
    return _graphql_mutation_field(_api_field(tokens, "query") or "")


def _graphql_tokens(query: str) -> list[str] | None:
    """Skip strings and comments in one pass; reject unfinished string literals."""
    parts: list[str] = []
    index = 0
    while index < len(query):
        char = query[index]
        if char.isspace() or char == ",":
            index += 1
        elif char == "#":
            while index < len(query) and query[index] not in "\r\n":
                index += 1
        elif char == '"':
            delimiter = '"""' if query.startswith('"""', index) else '"'
            index += len(delimiter)
            while index < len(query):
                if delimiter == '"""' and query.startswith(r'\"""', index):
                    index += 4
                elif delimiter == '"' and query[index] == "\\":
                    index += 2
                elif query.startswith(delimiter, index):
                    index += len(delimiter)
                    break
                else:
                    index += 1
            else:
                return None
        elif match := _GRAPHQL_NAME.match(query, index):
            parts.append(match[0])
            index = match.end()
        else:
            parts.append(char)
            index += 1
    return parts


@functools.lru_cache(maxsize=32)
def _graphql_mutation_field(query: str) -> str | None:
    """Parse once per query text; the observer consults it several times per command."""
    parts = _graphql_tokens(query)
    if not parts or parts[0] != "mutation":
        return None
    depth = parentheses = 0
    fields: list[str] = []
    for index, part in enumerate(parts[1:], 1):
        if part == "(":
            parentheses += 1
        elif part == ")":
            parentheses -= 1
            if parentheses < 0:
                return None
        elif parentheses:
            continue
        elif part == "{":
            depth += 1
        elif part == "}":
            depth -= 1
            if depth == 0:
                # Multiple operations/fields are ambiguous, especially with --jq.
                if index != len(parts) - 1:
                    return None
                if fields == ["createPullRequest"]:
                    return "createPullRequest"
                if len(fields) == 3 and fields[1:] == [":", "createPullRequest"]:
                    return fields[0]
                return None
        elif depth == 1:
            fields.append(part)
        elif part == ":":
            # A nested alias such as ``url: body`` would relabel body text as identity.
            return None
    return None


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
    if _graphql_create_field(tokens) is not None:
        return True
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
    if _graphql_create_field(tokens) is not None:
        return True
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
    if field := _graphql_create_field(tokens):
        # A template prints any selected field, so its output has no identity provenance.
        if _flag(tokens, "--template", "-t") is not None:
            return True
        projection = _flag(tokens, "--jq", "-q", last=True)
        path = f".data.{field}.pullRequest"
        if projection is not None:
            return projection.strip() not in {path, f"{path}.url"}
        return False
    fields = _flag(tokens, "--json")
    return (
        tokens[:2] == ["pr", "diff"]
        or _flag(tokens, "--jq", "-q", last=True) in {".body", ".[].body"}
        or (
            tokens[0] == "pr"
            and fields is not None
            and set(fields.split(",")) <= {"body", "title"}
        )
    )


def _graphql_output_values(result: object, depth: int = 0) -> list[object]:
    """Unwrap output while preserving emitted JSON documents intact.

    Unknown bracket-prefixed logs are ambiguous with incomplete JSON arrays;
    only complete Git summaries and the success footer are recognized as text.
    """
    if depth > 6:
        return []
    if isinstance(result, dict):
        for key in ("stdout", "output", "aggregatedOutput", "structuredContent", "result", "text"):
            if key in result:
                return _graphql_output_values(result[key], depth + 1)
        if "content" in result:
            content = result["content"]
            return (
                _graphql_output_values(content, depth + 1)
                if isinstance(content, (str, list))
                else []
            )
        if any(key in result for key in ("metadata", "stderr", "exit_code", "exitCode")):
            return []
        return [result]
    if isinstance(result, list):
        return [
            value
            for block in result[:100]
            if isinstance(block, dict) and block.get("type") == "text"
            for value in _graphql_output_values(block.get("text"), depth + 1)
        ]
    if not isinstance(result, str):
        return [result]
    values: list[object] = []
    decoder = json.JSONDecoder()
    index = 0
    while index < len(result):
        if result[index].isspace():
            index += 1
            continue
        end = result.find("\n", index)
        if end == -1:
            end = len(result)
        line = result[index:end].strip()
        # Git's bracketed branch/object-ID summary is text, not a JSON array.
        if line == "[exit code: 0]" or re.fullmatch(
            r"\[[^\]\r\n]+ [0-9a-f]{4,64}\](?: .*)?", line
        ):
            index = end
            continue
        if result[index] in '{["' or line == "null":
            try:
                value, position = decoder.raw_decode(result, index)
            except ValueError:
                return []
            end = result.find("\n", position)
            if end == -1:
                end = len(result)
            if result[position:end].strip():
                return []
            values.append(value)
        else:
            values.append(line)
        index = end
    return values


def _graphql_prs(
    result: object,
    *,
    field: str,
    projection: str,
    expected_count: int,
    explicit_targets: frozenset[str],
) -> list[PullRequestRef]:
    """Read only the response shape requested by the validated creation command."""
    values = _graphql_output_values(result)
    path = f".data.{field}.pullRequest"
    if projection == f"{path}.url":
        if any(value is None for value in values):
            return []
        references = [
            ref
            for value in values
            if isinstance(value, str)
            and len(value.split()) == 1
            and (ref := pr_reference(value))
            and ref.url not in explicit_targets
        ]
        return references if len(references) <= expected_count else []
    references: list[PullRequestRef] = []
    response_count = 0
    for value in values:
        if not isinstance(value, dict):
            continue
        if projection == path:
            pr = value
        elif not projection:
            data = value.get("data")
            if not isinstance(data, dict) or field not in data:
                continue
            response_count += 1
            mutation = data[field]
            pr = mutation.get("pullRequest") if isinstance(mutation, dict) else None
        else:
            continue
        if (
            isinstance(pr, dict)
            and (ref := pr_reference(pr.get("url")))
            and ref.url not in explicit_targets
        ):
            references.append(ref)
    if not projection and response_count > expected_count:
        return []
    # Extra projected objects or a null result leave their command attribution ambiguous.
    if projection == path and (
        any(value is None for value in values) or len(references) > expected_count
    ):
        return []
    return references


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


def _graphql_setup(segment: ShellSegment) -> bool:
    """Recognize supported checkout preparation before PR-producing commands."""
    raw = " ".join(segment.raw_tokens)
    if re.fullmatch(r"[A-Za-z_]\w*=\$\(gh api repos/[\w.-]+/[\w.-]+ (?:--jq|-q) \.node_id\)", raw):
        return True
    tokens = list(segment.invocation_tokens)
    if tokens[0] == "cd":
        return len(tokens) == 2
    if tokens[0] == "set":
        return tokens[1:] in (["-e"], ["-eu"], ["-euo", "pipefail"])
    if PurePath(tokens[0]).name != "git":
        return False
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in {"-c", "-C", "--git-dir", "--work-tree"}:
            index += 2
        elif token == "--no-pager" or token.startswith(("-c", "-C", "--git-dir=", "--work-tree=")):
            index += 1
        else:
            return token in {"add", "commit", "push"}
    return False


def _graphql_output_eligible(segments: Sequence[ShellSegment]) -> bool:
    """Require known output producers and preserve checkout setup before PR operations."""
    seen_pr = False
    for segment in segments:
        if not segment.output_eligible:
            return False
        tokens = _gh_arguments(segment)
        if tokens:
            if any(
                token == "--silent" or token.startswith("--silent=") for token in tokens
            ) or not (_creates_pr(tokens) or _command_target(tokens)):
                return False
            seen_pr = True
        elif seen_pr or not _graphql_setup(segment):
            return False
    return True


def shell_pr_operations(segments: Sequence[ShellSegment]) -> list[ShellPrOp]:
    """Return one op per ``gh pr`` or ``gh api`` segment, in order.

    Other ``gh`` commands produce no op. GraphQL output additionally requires
    known stdout producers and a stream without redirects or pipes.
    """
    commands: list[tuple[list[str], str | None, str]] = []
    for segment in segments:
        tokens = _gh_arguments(segment)
        if not tokens or tokens[0] not in {"pr", "api"}:
            continue
        field = _graphql_create_field(tokens)
        projection = (_flag(tokens, "--jq", "-q", last=True) or "").strip()
        commands.append((tokens, field, projection))
    output_eligible = not any(field for _, field, _ in commands) or _graphql_output_eligible(
        segments
    )
    explicit_targets = frozenset(
        target.url for tokens, _, _ in commands if (target := _command_target(tokens))
    )
    object_count = sum(
        field is not None and projection == f".data.{field}.pullRequest"
        for _, field, projection in commands
    )
    url_count = sum(
        projection == f".data.{field}.pullRequest.url"
        if field is not None
        else tokens[:2] == ["pr", "create"] or (_creates_pr(tokens) and projection == ".html_url")
        for tokens, field, projection in commands
    )
    response_counts = Counter(
        field for _, field, projection in commands if field is not None and not projection
    )
    ops: list[ShellPrOp] = []
    for tokens, field, projection in commands:
        parse_result = None
        if field:
            expected_count = (
                response_counts[field]
                if not projection
                else object_count
                if projection == f".data.{field}.pullRequest"
                else url_count
            )
            parse_result = functools.partial(
                _graphql_prs,
                field=field,
                projection=projection,
                expected_count=expected_count,
                explicit_targets=explicit_targets,
            )
        ops.append(
            ShellPrOp(
                tracks=_tracks_pr(tokens),
                creates=_creates_pr(tokens),
                target=_command_target(tokens),
                content_only=_content_only(tokens) or (field is not None and not output_eligible),
                parse_result=parse_result,
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
