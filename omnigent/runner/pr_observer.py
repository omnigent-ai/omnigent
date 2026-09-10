"""Extract PR identities from completed shell and GitHub MCP calls."""

from __future__ import annotations

import json
import logging
import re
import shlex
from pathlib import PurePath

from omnigent.policies.builtins._shell import (
    MAX_SHELL_NESTING,
    SHELL_TOOLS,
    real_invocation_tokens,
    unwrap_shell_command,
)
from omnigent.runner.session_prs import PullRequestRef, SessionPrRegistry, observation_key

_logger = logging.getLogger(__name__)
_PR_ACTIONS = {
    "create",
    "edit",
    "review",
    "checkout",
    "merge",
    "ready",
    "reopen",
    "close",
    "comment",
}
_MCP_ACTIONS = {
    "create_pull_request",
    "update_pull_request",
    "merge_pull_request",
    "update_pull_request_branch",
    "create_pull_request_review",
    "submit_pending_pull_request_review",
    "pull_request_review_write",
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
        try:
            return _result_parts(json.loads(result), depth + 1)
        except ValueError:
            return [result]
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
        commands = _gh_commands(command)
        text = _output_text(result)
        if re.search(r"\[exit code: [1-9]|Process exited with code [1-9]", text):
            return [], False
        actions = [tokens[1] for tokens in commands if len(tokens) > 1 and tokens[0] == "pr"]
        # A combined output cannot establish which conditional command produced a URL.
        if any(action not in _PR_ACTIONS for action in actions) or (
            len(commands) > 1 and len(actions) != len(commands)
        ):
            return [], False
        created = bool(actions) and all(action == "create" for action in actions)
        for tokens in commands:
            if tokens and tokens[0] == "pr":
                index = 0
                if len(tokens) <= index + 1 or tokens[index + 1] not in _PR_ACTIONS:
                    continue
                action = tokens[index + 1]
                # Standalone URLs are gh's operation result, not links embedded in a body.
                for line in text.splitlines():
                    ref = _reference(line.strip())
                    if ref:
                        references.append(ref)
                if action != "create":
                    target = _positional_target(tokens[index + 2 :])
                    ref = _reference(target)
                    if ref is None and target and target.isdigit():
                        ref = _target(
                            _flag(tokens, "--repo", "-R"),
                            target,
                            _flag(tokens, "--hostname") or "github.com",
                        )
                    if ref:
                        references.append(ref)
            elif tokens and tokens[0] == "api":
                method = _flag(tokens, "--method", "-X")
                writes = method in {"POST", "PATCH", "PUT"} or (
                    method is None and _flag(tokens, "--field", "--raw-field", "-f", "-F")
                )
                if not writes:
                    continue
                for obj in _objects(result):
                    if ref := _reference(obj.get("html_url", obj.get("url"))):
                        references.append(ref)
                endpoint = _api_endpoint(tokens[1:])
                pr_endpoint = re.fullmatch(
                    r"/?repos/([^/]+/[^/]+)/pulls(?:/([1-9][0-9]*))?/?",
                    (endpoint or "").split("?", 1)[0],
                )
                created = bool(pr_endpoint and pr_endpoint[2] is None and method in {None, "POST"})
                # A direct URL projection omits the JSON envelope. Body/list projections
                # can contain unrelated PR links and are not association evidence.
                if pr_endpoint and _flag(tokens, "--jq", "-q") in {".html_url", ".url"}:
                    projected = {
                        ref.url: ref
                        for line in text.splitlines()
                        if (ref := _reference(line.strip()))
                    }
                    if len(projected) == 1:
                        ref = next(iter(projected.values()))
                        if ref.repository == pr_endpoint[1].lower() and (
                            pr_endpoint[2] is None or ref.number == int(pr_endpoint[2])
                        ):
                            references.append(ref)
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
