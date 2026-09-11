"""Extract PR identities from completed shell and GitHub MCP calls."""

from __future__ import annotations

import json
import logging
import re
import shlex
from pathlib import PurePath
from urllib.parse import urlsplit

from omnigent.policies.builtins._shell import (
    MAX_SHELL_NESTING,
    SHELL_TOOLS,
    real_invocation_tokens,
    unwrap_shell_command,
)
from omnigent.runner.session_prs import PullRequestRef, SessionPrRegistry, observation_key

_logger = logging.getLogger(__name__)
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


def _reference(value: object) -> PullRequestRef | None:
    if not isinstance(value, str):
        return None
    try:
        return PullRequestRef.from_url(value.rstrip(".,);]"))
    except ValueError:
        return None


def _result_parts(result: object, depth: int = 0) -> list[dict[str, object] | str]:
    """Unwrap tool envelopes and JSON text, excluding body/description fields."""
    if depth > 6:
        return []
    if isinstance(result, str):
        # Compound shell output can interleave JSON responses, URL lines, and logs.
        parts: list[dict[str, object] | str] = []
        decoder = json.JSONDecoder()
        index = 0
        while index < len(result):
            if result[index].isspace():
                index += 1
                continue
            position = index
            if result[index] in '{["':
                try:
                    value, position = decoder.raw_decode(result, index)
                except ValueError as error:
                    # Keep incomplete JSON together instead of re-parsing its nested lines.
                    position = error.pos if isinstance(error, json.JSONDecodeError) else index
                else:
                    line_end = result.find("\n", position)
                    if not result[position : line_end if line_end != -1 else len(result)].strip():
                        parts.extend(_result_parts(value, depth + 1))
                        index = position
                        continue
            end = result.find("\n", position)
            if end == -1:
                end = len(result)
            parts.append(result[index:end])
            index = end
        return parts
    if isinstance(result, list):
        return [part for item in result[:100] for part in _result_parts(item, depth + 1)]
    if not isinstance(result, dict):
        return []
    found: list[dict[str, object] | str] = [result]
    for key in (
        "content",
        "structuredContent",
        "text",
        "result",
        "data",
        "pull_request",
        "stdout",
        "output",
        "aggregatedOutput",
        "metadata",
    ):
        if key in result:
            found.extend(_result_parts(result[key], depth + 1))
    return found


def _objects(result: object) -> list[dict[str, object]]:
    return [part for part in _result_parts(result) if isinstance(part, dict)]


def _failed(result: object) -> bool:
    for obj in _objects(result):
        if obj.get("isError") is True or obj.get("is_error") is True:
            return True
        for key in ("exit_code", "exitCode", "returncode"):
            if isinstance(obj.get(key), int) and obj[key] != 0:
                return True
        if obj.get("session_id") is not None and obj.get("exit_code") is None:
            return True
        if obj.get("backgroundTaskId") or obj.get("background_task_id"):
            return True
        if obj.get("interrupted") is True or obj.get("status") in ("running", "in_progress"):
            return True
        if obj.get("success") is False or obj.get("cancelled") is True:
            return True
    return False


def _output_text(result: object) -> str:
    return "\n".join(part for part in _result_parts(result) if isinstance(part, str))


def _join_shell_lines(command: str) -> str:
    """Apply shell line continuations while preserving single-quoted literals."""
    result: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(command):
        char = command[index]
        if quote != "'" and char == "\\" and index + 1 < len(command):
            following = command[index + 1]
            if following != "\n":
                result.extend((char, following))
            index += 2
            continue
        if quote is None and char == "#" and (index == 0 or command[index - 1] in " \t\r\n;&|()"):
            end = command.find("\n", index)
            if end == -1:
                result.append(command[index:])
                break
            result.append(command[index:end])
            index = end
            continue
        if char in {"'", '"'}:
            if quote is None:
                quote = char
            elif quote == char:
                quote = None
        result.append(char)
        index += 1
    return "".join(result)


def _gh_commands(command: str, depth: int = 0) -> list[list[str]]:
    if depth > MAX_SHELL_NESTING:
        return []
    found = []
    lexer = shlex.shlex(_join_shell_lines(command), posix=True, punctuation_chars=";&|\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    segments: list[list[str]] = [[]]
    try:
        for token in lexer:
            if token == "||":
                return []
            if token and all(char in ";&|\n" for char in token):
                segments.append([])
            else:
                segments[-1].append(token)
    except ValueError:
        return []
    for segment in segments:
        tokens = real_invocation_tokens(segment)
        if not tokens:
            continue
        inner = unwrap_shell_command(tokens)
        if inner is not None:
            found.extend(_gh_commands(inner, depth + 1))
        elif tokens and PurePath(tokens[0]).name == "gh":
            args, prefix = tokens[1:], []
            while args and args[0].startswith("-"):
                if args[0] in {"-R", "--repo"} and len(args) > 1:
                    prefix.extend(args[:2])
                    args = args[2:]
                elif args[0].startswith(("-R", "--repo=")):
                    prefix.append(args[0])
                    args = args[1:]
                else:
                    break
            host = next((t.split("=", 1)[1] for t in segment if t.startswith("GH_HOST=")), None)
            if host:
                prefix.extend(["--hostname", host])
            found.append([*args, *prefix])
    return found


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
        return _reference(f"https://{host}/{repository}/pull/{number}")
    return None


def _command_target(tokens: list[str]) -> PullRequestRef | None:
    host = _flag(tokens, "--hostname") or "github.com"
    if tokens[0] == "api":
        endpoint = (_api_endpoint(tokens[1:]) or "").split("?", 1)[0]
        match = re.match(r"/?repos/([^/]+/[^/]+)/pulls/([1-9][0-9]*)(?:/|$)", endpoint)
        return _target(match[1], match[2], host) if match else None
    if tokens[0] == "pr" and len(tokens) > 1 and tokens[1] != "create":
        target = _positional_target(tokens[2:])
        if ref := _reference(target):
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


# Reads whose canonical output stays terse (a URL or status line, never a
# rendered body/diff that could quote arbitrary PR links).
_TERSE_READS = {"comment", "review", "checkout", "checks"}


def _terse_read_targets(commands: list[list[str]]) -> tuple[set[str], set[int]] | None:
    """Exclusion keys for untracked reads with terse output, or None when unsafe."""
    urls: set[str] = set()
    numbers: set[int] = set()
    for tokens in commands:
        if tokens[0] != "pr" or len(tokens) < 2 or tokens[1] not in _TERSE_READS:
            return None
        if ref := _command_target(tokens):
            urls.add(ref.url)
            numbers.add(ref.number)
        elif (target := _positional_target(tokens[2:])) and target.isdigit():
            numbers.add(int(target))
    return urls, numbers


def _created_pr_from_output(
    result: object, exclusions: tuple[set[str], set[int]]
) -> PullRequestRef | None:
    """The single output identity left once the reads' own URLs are removed."""
    urls, numbers = exclusions
    values: list[object] = [obj.get("html_url", obj.get("url")) for obj in _objects(result)]
    values.extend(
        line.strip() for line in _output_text(result).splitlines() if len(line.split()) == 1
    )
    candidates: dict[str, PullRequestRef] = {}
    for value in values:
        if not isinstance(value, str):
            continue
        ref = _reference(value)
        # Creation URLs are bare; a fragment marks a comment/review permalink.
        if ref is None or urlsplit(value.strip()).fragment:
            continue
        if ref.url not in urls and ref.number not in numbers:
            candidates[ref.url] = ref
    refs = list(candidates.values())
    return refs[0] if len(refs) == 1 else None


def _created_pr_metadata(result: object) -> PullRequestRef | None:
    """Read Claude's creation identity from tool metadata, never rendered stdout."""
    if not isinstance(result, dict):
        return None
    operation = result.get("gitOperation")
    if not isinstance(operation, dict):
        return None
    pr = operation.get("pr")
    if not isinstance(pr, dict) or pr.get("action") != "created":
        return None
    return _reference(pr.get("url"))


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
    for obj in _objects(result):
        ref = _reference(obj.get("html_url", obj.get("url"))) or _target(
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
        for url in re.findall(r"https://[^\s<>\"'`]+", _output_text(result))
        if (ref := _reference(url)) and matches(ref)
    }
    return list(urls.values()) if len(urls) == 1 else []


def extract_prs(
    tool_name: str, arguments: dict[str, object], result: object
) -> tuple[list[PullRequestRef], bool]:
    """Return positively identified PRs and whether the operation created them."""
    if _failed(result):
        return [], False
    references: list[PullRequestRef] = []
    created = False
    if tool_name in SHELL_TOOLS or tool_name in {"exec_command", "run_command"}:
        command = arguments.get("command", arguments.get("cmd"))
        if not isinstance(command, str) or len(command) > 100_000:
            return [], False
        # Unrelated setup commands do not affect PR associations.
        gh_commands = [
            tokens for tokens in _gh_commands(command) if tokens and tokens[0] in {"pr", "api"}
        ]
        commands = [tokens for tokens in gh_commands if _tracks_pr(tokens)]
        if not commands:
            return [], False
        text = _output_text(result)
        if re.search(
            r"(?:^|\n)(?:\[exit code: -?[1-9][0-9]*\]"
            r"|Process exited with code -?[1-9][0-9]*)\s*\Z",
            text,
        ):
            return [], False
        created = all(_creates_pr(tokens) for tokens in commands)
        references = [ref for tokens in commands if (ref := _command_target(tokens))]
        if any(_creates_pr(tokens) for tokens in commands) and (
            ref := _created_pr_metadata(result)
        ):
            references.append(ref)
        # Shared stdout cannot attribute a result to a write when reads/comments also ran.
        if len(commands) == len(gh_commands) and (
            len(commands) > 1 or not _content_only(commands[0])
        ):
            for obj in _objects(result):
                if ref := _reference(obj.get("html_url", obj.get("url"))):
                    references.append(ref)
            # A single operation's known identity makes rendered body links redundant.
            if len(commands) > 1 or not references:
                for line in text.splitlines():
                    if len(line.split()) == 1 and (ref := _reference(line.strip())):
                        references.append(ref)
        elif created and not references and not any(_content_only(t) for t in commands):
            # Shell calls carry no creation metadata, so a creation mixed with
            # terse reads keeps its identity when exactly one unclaimed URL remains.
            untracked = [tokens for tokens in gh_commands if not _tracks_pr(tokens)]
            exclusions = _terse_read_targets(untracked)
            if exclusions is not None and (ref := _created_pr_from_output(result, exclusions)):
                references = [ref]
    else:
        name = tool_name.rsplit("__", 1)[-1].removeprefix("github_")
        if name == "write_api_call":
            endpoint = arguments.get("endpoint")
            operations = {
                "pull_requests.create": "create_pull_request",
                "pull_requests.update": "update_pull_request",
                "pull_requests.merge": "merge_pull_request",
                "pulls.create": "create_pull_request",
                "pulls.update": "update_pull_request",
                "pulls.merge": "merge_pull_request",
            }
            name = operations.get(endpoint, "") if isinstance(endpoint, str) else ""
            params = arguments.get("params")
            if isinstance(params, dict):
                arguments = {**params, "owner": params.get("owner", params.get("org"))}
        if name not in _MCP_ACTIONS:
            return [], False
        if name in _MCP_REVIEWS and not _changes_review_state(arguments.get("event")):
            return [], False
        created = name == "create_pull_request"
        references = _mcp_prs(arguments, result, created=created)
    return list({ref.url: ref for ref in references}.values()), created


def observe_tool_completion(
    session_id: str,
    *,
    tool_name: str,
    arguments: dict[str, object],
    result: object,
    call_id: str = "",
    source: str = "tool",
    successful: bool = True,
) -> None:
    """Best-effort observer; failures never change execution or tool results."""
    if not successful or not session_id:
        return
    try:
        references, created = extract_prs(tool_name, arguments, result)
        SessionPrRegistry(session_id).record(
            references,
            relationship="created" if created else "worked_on",
            source=source,
            observation_id=observation_key(source, call_id, [tool_name, arguments, result]),
        )
    except (OSError, ValueError, TypeError, TimeoutError):
        _logger.warning(
            "Failed to record session PRs", extra={"session_id": session_id}, exc_info=True
        )


def observe_hook(session_id: str, payload: dict[str, object]) -> None:
    """Bind a native hook to its relay's session rather than trusting provider IDs."""
    if payload.get("hook_event_name") != "PostToolUse":
        return
    name, arguments = payload.get("tool_name"), payload.get("tool_input")
    if not isinstance(name, str) or not isinstance(arguments, dict):
        return
    call_id = payload.get("tool_use_id")
    observe_tool_completion(
        session_id,
        tool_name=name,
        arguments=arguments,
        result=payload.get("tool_response", payload.get("tool_output")),
        call_id=call_id if isinstance(call_id, str) else "",
        source="native_hook",
    )
