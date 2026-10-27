"""Network-free attribution of successful fj, tea, Forgejo MCP, and AGit pull request writes."""

from __future__ import annotations

import functools
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePath

from omnigent.git_providers import EnvInstances, instance_authority
from omnigent.git_providers.forgejo import ForgejoProvider
from omnigent.runner.git_providers import ShellPrOp, ShellSegment
from omnigent.runner.git_providers.tool_output import result_objects
from omnigent.runner.session_prs import PullRequestRef


@dataclass(frozen=True)
class _Verb:
    """A ``fj pr`` or ``tea pulls`` subcommand and the flags that take no value.

    Every other flag takes a value. ``optional`` flags take the next token only when it
    is not itself a flag, as clap reads them. ``prints_url`` is true when the command's
    own output ends with the pull request's URL.
    """

    tracks: bool
    creates: bool = False
    switches: frozenset[str] = frozenset()
    optional: frozenset[str] = frozenset()
    prints_url: bool = False


_READ = _Verb(False)
_HELP = frozenset({"-h", "--help"})
_FJ_VERBS = {
    "create": _Verb(True, True, frozenset({"-A", "--autofill", "-w", "--web", "-a", "--agit"})),
    "merge": _Verb(
        True, switches=frozenset({"-d", "--delete"}), optional=frozenset({"-m", "--message"})
    ),
    "close": _Verb(True, optional=frozenset({"-w", "--with-msg"})),
    "edit": _Verb(True),
    "assign": _Verb(True),
    "unassign": _Verb(True),
}
# ``fj pr edit [PR] <command>``; editing a comment does not change the pull request.
_FJ_EDIT_COMMANDS = {"title": True, "body": True, "labels": True, "comment": False}
_TEA_SWITCHES = frozenset(
    {"--draft", "--ready", "--agit", "--allow-maintainer-edits", "--edits", "--comments"}
)
_TEA_VERBS = {
    "create": _Verb(True, True, _TEA_SWITCHES, prints_url=True),
    "merge": _Verb(True, switches=_TEA_SWITCHES),
    "close": _Verb(True, switches=_TEA_SWITCHES),
    "reopen": _Verb(True, switches=_TEA_SWITCHES),
    "edit": _Verb(True, switches=_TEA_SWITCHES, prints_url=True),
    "approve": _Verb(True, switches=_TEA_SWITCHES, prints_url=True),
    "reject": _Verb(True, switches=_TEA_SWITCHES, prints_url=True),
}
_TEA_ALIASES = {
    "c": "create",
    "m": "merge",
    "open": "reopen",
    "e": "edit",
    "lgtm": "approve",
    "a": "approve",
}
_TEA_COMMANDS = frozenset({"pulls", "pull", "pr"})
# Flags that take a value and may come before the subcommand.
_FJ_LEADING_VALUES = frozenset({"-H", "--host", "-C", "--cwd", "--style", "-R", "--remote"})
_TEA_LEADING_VALUES = frozenset(
    {"--login", "-l", "--repo", "-r", "--remote", "-R", "--output", "-o"}
)
# fj's Fluent messages wrap each value in Unicode isolation marks (U+2068, U+2069).
_CREATED_NUMBER = re.compile("(?m)^[^#\n]*#\u2068?([1-9][0-9]*)\u2069?: ")
_PUSH_BANNER = re.compile(
    r"(?m)^remote:[^\n]*pull request[^\n]*:[ \t\r]*\nremote:[ \t]+(https://\S+?)[ \t\r]*$"
)
_PUSH_FAILED = re.compile(r"(?m)^(?:error: failed to push some refs|fatal:)")
_GIT_VALUE_OPTIONS = frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace"})
_PUSH_VALUE_OPTIONS = frozenset({"-o", "--push-option", "--repo", "--receive-pack", "--exec"})


@dataclass(frozen=True)
class _McpTool:
    """A pull request MCP tool; ``create_method`` names the ``method`` value that creates."""

    create_method: str | None = None
    review_state: str | None = None


_MCP_TOOLS = {
    # forgejo-mcp; its create, update, and merge tools share GitHub's names.
    "create_pull_review": _McpTool(review_state="state"),
    "submit_pull_review": _McpTool(review_state="state"),
    "create_review_requests": _McpTool(),
    "delete_review_requests": _McpTool(),
    # gitea-mcp
    "pull_request_write": _McpTool(create_method="create"),
}
_STATE_CHANGING_REVIEWS = frozenset({"APPROVE", "APPROVED", "REQUEST_CHANGES"})


def reference(value: object) -> PullRequestRef | None:
    """Read only a Forgejo pull request URL on an owned authority."""
    if not isinstance(value, str):
        return None
    parsed = ForgejoProvider().parse_pr_url(value, EnvInstances())
    return PullRequestRef(**vars(parsed)) if parsed else None


def _arguments(tokens: Sequence[str], verb: _Verb) -> tuple[list[str], dict[str, str]] | None:
    """Split arguments into positionals and flags; ``None`` when a value is missing."""
    positional: list[str] = []
    flags: dict[str, str] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        name, equals, value = token.partition("=")
        if not token.startswith("-") or token == "-":
            positional.append(token)
        elif equals or name in verb.switches or name in _HELP:
            flags[name] = value if equals else ""
        elif name in verb.optional:
            following = tokens[index + 1] if index + 1 < len(tokens) else "-"
            flags[name] = "" if following.startswith("-") else following
            index += 0 if following.startswith("-") else 1
        elif index + 1 < len(tokens):
            index += 1
            flags[name] = tokens[index]
        else:
            return None
        index += 1
    return positional, flags


def _pull_url(host: str | None, repository: str | None, number: str) -> PullRequestRef | None:
    authority = instance_authority(host) if host else None
    if authority is None or not repository or not number.isdigit():
        return None
    return reference(f"https://{authority}/{repository}/pulls/{number}")


def _fj_repository(value: str | None, host: str | None) -> tuple[str | None, str | None]:
    """Split fj's ``[host/]owner/repo`` into its host and repository."""
    if not value or "/" not in value:
        return host, None
    *prefix, owner, name = value.removesuffix(".git").split("/")
    return ("/".join(prefix) or host), f"{owner}/{name}"


def _fj_target(value: str | None, host: str | None) -> PullRequestRef | None:
    """Read fj's ``[[host/]owner/repo#]N`` pull request argument."""
    if not value:
        return None
    repository, separator, number = value.rpartition("#")
    if not separator or repository.count("/") < 1:
        return None
    target_host, target_repository = _fj_repository(repository, host)
    return _pull_url(target_host, target_repository, number)


def _fj_created(host: str | None, repository: str | None, text: str) -> list[PullRequestRef]:
    """fj prints only ``#N`` for a new pull request, so the repository must be in argv."""
    match = _CREATED_NUMBER.search(text)
    created = _pull_url(host, repository, match[1]) if match else None
    return [created] if created else []


def _no_identity(text: str) -> list[PullRequestRef]:
    """The command prints no pull request URL; never read one from shared output."""
    del text
    return []


def _leading(tokens: Sequence[str], values: frozenset[str]) -> tuple[int, dict[str, str]]:
    """Read the flags before a subcommand; return the subcommand's index and the flags."""
    index, flags = 0, {}
    while index < len(tokens) and tokens[index].startswith("-"):
        name, equals, value = tokens[index].partition("=")
        if not equals and name in values:
            index += 1
            value = tokens[index] if index < len(tokens) else ""
        flags[name] = value
        index += 1
    return index, flags


def _fj_operation(tokens: Sequence[str]) -> ShellPrOp | None:
    """Describe ``fj [flags] pr [flags] <verb>``, or return ``None`` for other fj commands."""
    index, flags = _leading(tokens[1:], _FJ_LEADING_VALUES)
    if tokens[1 + index : 2 + index] != ("pr",):
        return None
    rest = tokens[2 + index :]
    index, pr_flags = _leading(rest, _FJ_LEADING_VALUES)
    verb_name = rest[index] if index < len(rest) else ""
    verb = _FJ_VERBS.get(verb_name, _READ)
    parsed = _arguments(rest[index + 1 :], verb)
    if parsed is None or not verb.tracks:
        return ShellPrOp(False, False, None, parsed is None)
    arguments, verb_flags = parsed
    flags = {**flags, **pr_flags, **verb_flags}
    if _HELP & flags.keys() or (verb.creates and {"-w", "--web"} & flags.keys()):
        return ShellPrOp(False, False, None, False)
    host = flags.get("-H", flags.get("--host"))
    if verb.creates:
        target_host, repository = _fj_repository(flags.get("-r", flags.get("--repo")), host)
        parse = functools.partial(_fj_created, target_host, repository)
        return ShellPrOp(True, True, None, False, parse_output=parse)
    tracks = True
    if verb_name == "edit":
        command = next((a for a in arguments if a in _FJ_EDIT_COMMANDS), None)
        tracks = command is not None and _FJ_EDIT_COMMANDS[command]
        arguments = arguments[: arguments.index(command)] if command else arguments
    pull = flags.get("-p", flags.get("--pr")) if verb_name in {"assign", "unassign"} else None
    target = _fj_target(pull or next(iter(arguments), None), host)
    return ShellPrOp(tracks, False, target, False, parse_output=_no_identity)


def _tea_printed(text: str) -> list[PullRequestRef]:
    """tea ends its pull request details with the pull request's URL."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    printed = reference(lines[-1]) if lines else None
    return [printed] if printed else []


def _tea_operation(tokens: Sequence[str]) -> ShellPrOp | None:
    """Describe ``tea pulls|pull|pr [flags] [verb]``, or return ``None`` for other commands."""
    if len(tokens) < 2 or tokens[1] not in _TEA_COMMANDS:
        return None
    index, flags = _leading(tokens[2:], _TEA_LEADING_VALUES)
    rest = tokens[2 + index :]
    verb_name = _TEA_ALIASES.get(rest[0], rest[0]) if rest else ""
    verb = _TEA_VERBS.get(verb_name, _READ)
    parsed = _arguments(rest[1:], verb)
    if parsed is None or not verb.tracks:
        return ShellPrOp(False, False, None, parsed is None)
    flags = {**flags, **parsed[1]}
    if _HELP & flags.keys():
        return ShellPrOp(False, False, None, False)
    # tea names a pull request only by its index, so the printed URL is its identity.
    if not verb.prints_url:
        parse = _no_identity
    elif "--output" in flags or "-o" in flags:
        parse = None
    else:
        parse = _tea_printed
    return ShellPrOp(True, verb.creates, None, False, parse_output=parse)


def _push_output(text: str) -> list[PullRequestRef]:
    """Read Forgejo's pull request banner from a successful AGit push."""
    if _PUSH_FAILED.search(text):
        return []
    return [ref for match in _PUSH_BANNER.finditer(text) if (ref := reference(match[1]))]


def _git_push(tokens: Sequence[str]) -> ShellPrOp | None:
    """Recognize ``git push`` and whether it targets an AGit ``refs/for/`` ref."""
    args = list(tokens[1:])
    while args and args[0].startswith("-"):
        flag = args.pop(0)
        if flag in _GIT_VALUE_OPTIONS:
            if not args:
                return None
            args.pop(0)
    if not args or args.pop(0) != "push":
        return None
    positional = []
    while args:
        arg = args.pop(0)
        if arg == "--":
            positional.extend(args)
            break
        if arg in _PUSH_VALUE_OPTIONS:
            if not args:
                return ShellPrOp(False, False, None, True)
            args.pop(0)
        elif arg in {"--dry-run", "-n"}:
            return ShellPrOp(False, False, None, True)
        elif not arg.startswith("-"):
            positional.append(arg)
    agit = any(spec.partition(":")[2].startswith("refs/for/") for spec in positional[1:])
    return ShellPrOp(agit, False, None, False, parse_output=_push_output if agit else None)


def shell_pr_operations(segments: Sequence[ShellSegment]) -> list[ShellPrOp]:
    """Describe reads too, so mixed command output cannot masquerade as a write."""
    operations: list[ShellPrOp] = []
    pushes: list[ShellPrOp] = []
    for segment in segments:
        tokens = segment.invocation_tokens
        program = PurePath(tokens[0]).name
        if program == "git":
            if op := _git_push(tokens):
                pushes.append(op)
        elif program == "fj":
            if op := _fj_operation(tokens):
                operations.append(op)
        elif program == "tea" and (op := _tea_operation(tokens)):
            operations.append(op)
    # Ordinary pushes are not pull request operations, but beside an AGit push
    # they share its remote output.
    if any(op.tracks for op in pushes):
        operations.extend(pushes)
    return operations


def pr_from_object(obj: Mapping[str, object]) -> PullRequestRef | None:
    """Forgejo objects name their pull request by ``html_url``, which the observer reads."""
    del obj
    return None


def _mcp_references(arguments: dict[str, object], result: object) -> list[PullRequestRef]:
    """Pull requests the result names that match the tool's owner, repo, and number."""
    owner, repo = arguments.get("owner"), arguments.get("repo")
    number = arguments.get("index", arguments.get("pull_number"))
    found = []
    for obj in result_objects(result):
        for key in ("html_url", "pull_request_url"):
            ref = reference(obj.get(key))
            if ref is None:
                continue
            if isinstance(owner, str) and isinstance(repo, str):
                if ref.repository != f"{owner}/{repo}".lower():
                    continue
            if isinstance(number, (int, float, str)) and str(number).removesuffix(".0") != str(
                ref.number
            ):
                continue
            found.append(ref)
    return found


def mcp_prs(
    tool_name: str, arguments: dict[str, object], result: object
) -> tuple[list[PullRequestRef], bool] | None:
    """Claim a Forgejo MCP write only when its result names a Forgejo pull request."""
    name = tool_name.rsplit("__", 1)[-1].removeprefix("forgejo_").removeprefix("gitea_")
    tool = _MCP_TOOLS.get(name)
    if tool is None:
        return None
    references = _mcp_references(arguments, result)
    if not references:
        return None
    created = tool.create_method is not None and arguments.get("method") == tool.create_method
    if tool.review_state is not None:
        state = arguments.get(tool.review_state)
        if not isinstance(state, str) or state.upper() not in _STATE_CHANGING_REVIEWS:
            return [], False
    return references, created
