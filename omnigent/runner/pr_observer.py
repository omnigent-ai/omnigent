"""Extract PR identities from completed shell and MCP tool calls.

The attribution rules here are provider-neutral. Each git provider's pull
request facet recognizes its own shell commands, output objects, and MCP tools.
"""

from __future__ import annotations

import functools
import json
import logging
import os
import re
import shlex
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace

from omnigent.git_providers import load_facet, providers, resolve_remote
from omnigent.policies.builtins._shell import (
    HEREDOC_DELIMITER,
    MAX_SHELL_NESTING,
    SHELL_TOOLS,
    real_invocation_tokens,
    skip_command_substitution,
    unwrap_shell_command,
)
from omnigent.runner import pr_reports, pushed_branches
from omnigent.runner.git_providers import (
    BranchPullRequest,
    BranchPullRequestsFacet,
    BranchWrite,
    PullRequestFacet,
    ShellPrOp,
    ShellSegment,
)
from omnigent.runner.git_providers.tool_output import output_text, pr_reference, result_objects
from omnigent.runner.session_prs import PullRequestRef, SessionPrRegistry, observation_key

_logger = logging.getLogger(__name__)
# Ids of the providers that already failed once in this process.
_failed_providers: set[str] = set()
_failed_providers_lock = threading.Lock()
_lookup_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="pr-observer")
_pending_lookups: set[tuple[str, str]] = set()
_pending_lookups_lock = threading.Lock()
_pushes = pushed_branches.SessionPushes()
# The forge stamps a PR's creation time; the runner's clock may lag it.
_CLOCK_SKEW_S = 120.0


def _failed(result: object) -> bool:
    for obj in result_objects(result):
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
    return bool(
        re.search(
            r"(?:^|\n)(?:\[exit code: -?[1-9][0-9]*\]"
            r"|Process exited with code -?[1-9][0-9]*)\s*\Z",
            output_text(result),
        )
    )


def _arithmetic_end(command: str, start: int) -> int | None:
    """Return the index just past the ``)`` closing the ``(`` at *start*, or ``None``."""
    depth = 0
    for index in range(start, len(command)):
        if command[index] == "(":
            depth += 1
        elif command[index] == ")":
            depth -= 1
            if depth == 0:
                return index + 1
    return None


def _parameter_expansion_end(command: str, start: int) -> int | None:
    """Return the index just past the ``}`` closing the ``${`` at *start*, or ``None``."""
    depth = 0
    quote: str | None = None
    index = start
    while index < len(command):
        char = command[index]
        if quote == "'":
            if char == "'":
                quote = None
        elif char == "\\":
            index += 1
        elif quote == '"':
            if char == '"':
                quote = None
        elif char in "'\"":
            quote = char
        elif command.startswith("${", index):
            depth += 1
            index += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    return None


# Longest ``<<`` operator text, delimiter included, read before giving up on it.
_HEREDOC_OPERATOR_LIMIT = 256


def _heredoc_operator_text(command: str, start: int) -> tuple[str, bool]:
    """Return the operator text at *start* without continuations, and whether it was cut short.

    Only a fixed-size window is read, so each operator costs at most the limit.
    """
    window = command[start : start + _HEREDOC_OPERATOR_LIMIT]
    text: list[str] = []
    index = 0
    while index < len(window):
        if window.startswith("\\\n", index):
            index += 2
        elif window[index] == "\n":
            return "".join(text), False
        else:
            text.append(window[index])
            index += 1
    return "".join(text), start + len(window) < len(command)


def _logical_line(command: str, start: int, *, join: bool = True) -> tuple[str, int]:
    """Return the line at *start* and the index after it, joining backslash-newlines if *join*."""
    parts: list[str] = []
    index = start
    while True:
        end = command.find("\n", index)
        physical = command[index:] if end == -1 else command[index:end]
        index = len(command) if end == -1 else end + 1
        trailing = len(physical) - len(physical.rstrip("\\"))
        if not join or end == -1 or trailing % 2 == 0:
            parts.append(physical)
            return "".join(parts), index
        parts.append(physical[:-1])


@dataclass(frozen=True)
class _Heredoc:
    delimiter: str
    strip_tabs: bool
    # A quoted delimiter keeps the body literal, so backslash-newline doesn't join lines.
    quoted: bool


def _heredoc_operator(command: str, start: int) -> _Heredoc | None:
    """Read the ``<<`` at *start* when its delimiter is a plain or simply quoted word."""
    line, truncated = _heredoc_operator_text(command, start)
    match = HEREDOC_DELIMITER.match(line)
    if match is None or (truncated and match.end() == len(line)):
        return None
    delimiter = match.group("delimiter")
    quoted = delimiter[0] in "'\""
    if quoted:
        delimiter = delimiter[1:-1]
    # A carriage return ends no shell word, so a CRLF delimiter keeps it.
    if line.startswith("\r", match.end()):
        delimiter += "\r"
    return _Heredoc(delimiter, bool(match.group("tabs")), quoted)


def _heredoc_body_end(command: str, start: int, heredoc: _Heredoc) -> int | None:
    """Return the index just past the delimiter line closing a body that starts at *start*.

    ``None`` means no delimiter line follows, which suggests the ``<<`` was misread.
    """
    index = start
    while index < len(command):
        line, index = _logical_line(command, index, join=not heredoc.quoted)
        if (line.lstrip("\t") if heredoc.strip_tabs else line) == heredoc.delimiter:
            return index
    return None


def _prepare_shell_text(command: str, *, model_syntax: bool = True) -> tuple[str, bool]:
    """Join shell continuations and detect unquoted redirection without changing argv.

    With *model_syntax*, an unquoted ``$(...)`` stays one word and here-document
    bodies are dropped as data. A substitution or delimiter too complex to bound
    falls back to plain lexing of the whole command.
    """
    result: list[str] = []
    quote: str | None = None
    redirected = False
    heredocs: list[_Heredoc] = []
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
            # Drop the comment but keep its newline: shlex would swallow both.
            end = command.find("\n", index)
            if end == -1:
                break
            index = end
            continue
        if model_syntax and quote is None and command.startswith("$(", index):
            end = (
                _arithmetic_end(command, index + 1)
                if command.startswith("$((", index)
                else skip_command_substitution(command, index)
            )
            if end is None:
                return _prepare_shell_text(command, model_syntax=False)
            # Escaping every symbol keeps the substitution's text as one shlex word.
            result.extend(c if c.isalnum() else "\\" + c for c in command[index:end])
            index = end
            continue
        if model_syntax and quote is None and command.startswith("${", index):
            # A ``<<`` inside an expansion's word is text, not a redirection.
            end = _parameter_expansion_end(command, index)
            if end is None:
                return _prepare_shell_text(command, model_syntax=False)
            result.append(command[index:end])
            index = end
            continue
        if (
            model_syntax
            and quote is None
            and command.startswith("((", index)
            and (index == 0 or command[index - 1] in " \t\n;&|(")
        ):
            # An arithmetic command's ``<<`` is a shift, not a here-document.
            end = _arithmetic_end(command, index)
            if end is None:
                return _prepare_shell_text(command, model_syntax=False)
            result.append(command[index:end])
            index = end
            continue
        if model_syntax and quote is None and command.startswith("<<<", index):
            redirected = True
            result.append("<<<")
            index += 3
            continue
        if model_syntax and quote is None and command.startswith("<<", index):
            heredoc = _heredoc_operator(command, index)
            if heredoc is None:
                return _prepare_shell_text(command, model_syntax=False)
            heredocs.append(heredoc)
            redirected = True
            result.append("<<")
            index += 2
            continue
        if quote is None and char == "\n" and heredocs:
            result.append(char)
            index += 1
            for heredoc in heredocs:
                end = _heredoc_body_end(command, index, heredoc)
                if end is None:
                    return _prepare_shell_text(command, model_syntax=False)
                index = end
            heredocs.clear()
            continue
        if quote is None and char in "<>":
            redirected = True
        if char in {"'", '"'}:
            if quote is None:
                quote = char
            elif quote == char:
                quote = None
        result.append(char)
        index += 1
    return "".join(result), redirected


def _shell_segments(command: str, depth: int = 0, *, tolerant: bool = False) -> list[ShellSegment]:
    """Split a command into simple commands, unwrapping nested shell strings in place.

    A command with ``||`` or one that cannot be lexed has no segments, since
    which of its commands ran is unknown. *tolerant* splits at ``||`` too, for
    callers that only need the commands that may have run.
    """
    if depth > MAX_SHELL_NESTING:
        return []
    found: list[ShellSegment] = []
    joined, redirected = _prepare_shell_text(command)
    lexer = shlex.shlex(joined, posix=True, punctuation_chars=";&|\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    segments: list[list[str]] = [[]]
    output_eligible = not redirected
    try:
        for token in lexer:
            if token == "||" and not tolerant:
                return []
            if token and all(char in ";&|\n" for char in token):
                if token.strip(";\n") not in {"", "&&"}:
                    output_eligible = False
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
            nested = _shell_segments(inner, depth + 1, tolerant=tolerant)
            if not nested:
                output_eligible = False
            found.extend(nested)
        else:
            found.append(ShellSegment(raw_tokens=tuple(segment), invocation_tokens=tuple(tokens)))
    return found if output_eligible else [replace(item, output_eligible=False) for item in found]


def _log_provider_failure(provider_id: str, step: str) -> None:
    """Warn with a traceback the first time a provider fails in this process, then use debug.

    Call from an exception handler. The observer runs after every tool call, so a provider
    that always fails would otherwise log a warning each time.
    """
    with _failed_providers_lock:
        first = provider_id not in _failed_providers
        _failed_providers.add(provider_id)
    _logger.log(
        logging.WARNING if first else logging.DEBUG,
        "Git provider %s failed in %s; ignoring its answer",
        provider_id,
        step,
        exc_info=True,
    )


@dataclass(frozen=True)
class _ProviderFacet:
    """A provider's pull request facet whose observer hooks never raise.

    A hook that raises is logged and answers as if its provider recognized nothing,
    so the other providers' answers still count.
    """

    provider_id: str
    facet: PullRequestFacet

    def shell_pr_operations(self, segments: Sequence[ShellSegment]) -> list[ShellPrOp]:
        """Return the provider's ops for *segments*, or none when it fails."""
        try:
            return list(self.facet.shell_pr_operations(segments))
        except Exception:  # noqa: BLE001 — one provider's bug must not hide the others' PRs
            _log_provider_failure(self.provider_id, "shell_pr_operations")
            return []

    def pr_from_object(self, obj: Mapping[str, object]) -> PullRequestRef | None:
        """Return the PR the provider reads from *obj*, or ``None`` when it fails."""
        try:
            return self.facet.pr_from_object(obj)
        except Exception:  # noqa: BLE001 — one provider's bug must not hide the others' PRs
            _log_provider_failure(self.provider_id, "pr_from_object")
            return None

    def mcp_prs(
        self, tool_name: str, arguments: dict[str, object], result: object
    ) -> tuple[list[PullRequestRef], bool] | None:
        """Return the provider's answer for an MCP tool call, or ``None`` when it fails."""
        try:
            answer = self.facet.mcp_prs(tool_name, arguments, result)
            if answer is None:
                return None
            references, created = answer
            return list(references), created
        except Exception:  # noqa: BLE001 — one provider's bug must not hide the others' PRs
            _log_provider_failure(self.provider_id, "mcp_prs")
            return None

    def branch_prs(self, host: str, repository: str, branch: str) -> list[BranchPullRequest]:
        """Return the open PRs of a branch, or none when the provider cannot look them up."""
        if not isinstance(self.facet, BranchPullRequestsFacet):
            return []
        try:
            return list(self.facet.branch_prs(host, repository, branch))
        except Exception:  # noqa: BLE001 — one provider's bug must not hide the others' PRs
            _log_provider_failure(self.provider_id, "branch_prs")
            return []

    def shell_branch_writes(self, segments: Sequence[ShellSegment]) -> list[BranchWrite]:
        """Return the branches the segments update through the provider's API."""
        if not isinstance(self.facet, BranchPullRequestsFacet):
            return []
        try:
            return list(self.facet.shell_branch_writes(segments))
        except Exception:  # noqa: BLE001 — one provider's bug must not hide the others' PRs
            _log_provider_failure(self.provider_id, "shell_branch_writes")
            return []

    def mcp_branch_writes(self, tool_name: str, arguments: dict[str, object]) -> list[BranchWrite]:
        """Return the branches an MCP tool call updates through the provider's API."""
        if not isinstance(self.facet, BranchPullRequestsFacet):
            return []
        try:
            return list(self.facet.mcp_branch_writes(tool_name, arguments))
        except Exception:  # noqa: BLE001 — one provider's bug must not hide the others' PRs
            _log_provider_failure(self.provider_id, "mcp_branch_writes")
            return []


def _facets() -> list[_ProviderFacet]:
    """Load each provider's pull request facet, in registration order.

    A facet module that fails to import for any reason is skipped.
    """
    facets: list[_ProviderFacet] = []
    for descriptor in providers():
        try:
            facet = load_facet(descriptor.id, "pull_requests")
        except Exception:  # noqa: BLE001 — a broken provider must not stop the others
            _log_provider_failure(descriptor.id, "load_facet")
            continue
        if facet is not None:
            facets.append(_ProviderFacet(descriptor.id, facet))
    return facets


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
    return pr_reference(pr.get("url"))


def _object_pr(obj: dict[str, object], facets: list[_ProviderFacet]) -> PullRequestRef | None:
    """Read the generic URL fields of an output object, then provider-specific fields."""
    if ref := pr_reference(obj.get("html_url", obj.get("url"))):
        return ref
    for facet in facets:
        if ref := facet.pr_from_object(obj):
            return ref
    return None


def _shell_command(tool_name: str, arguments: dict[str, object]) -> str | None:
    if tool_name not in SHELL_TOOLS and tool_name not in {"exec_command", "run_command"}:
        return None
    command = arguments.get("command", arguments.get("cmd"))
    if not isinstance(command, str) or len(command) > 100_000:
        return None
    return command


def _shell_operations(
    tool_name: str,
    arguments: dict[str, object],
    facets: list[_ProviderFacet],
    segments: Sequence[ShellSegment] | None = None,
) -> list[tuple[str, ShellPrOp]]:
    if segments is None:
        command = _shell_command(tool_name, arguments)
        if command is None:
            return []
        segments = _shell_segments(command)
    return [
        (facet.provider_id, op) for facet in facets for op in facet.shell_pr_operations(segments)
    ]


def _shell_result(tool_name: str, result: object) -> object:
    """Return *result* with ``sys_os_shell``'s serialized envelope decoded."""
    if tool_name == "sys_os_shell" and isinstance(result, str):
        # This tool serializes its result envelope; native shell strings are stdout.
        try:
            envelope = json.loads(result)
        except ValueError:
            return result
        if isinstance(envelope, dict) and "stdout" in envelope and "exit_code" in envelope:
            return envelope
    return result


def extract_prs(
    tool_name: str, arguments: dict[str, object], result: object
) -> tuple[list[PullRequestRef], bool]:
    """Return positively identified PRs and whether the operation created them."""
    result = _shell_result(tool_name, result)
    if _failed(result):
        return [], False
    facets = _facets()
    references: list[PullRequestRef] = []
    created = False
    if tool_name in SHELL_TOOLS or tool_name in {"exec_command", "run_command"}:
        # Every recognized PR command, reads included; ``commands`` are those that change a PR.
        provider_ops = [
            (provider_id, op)
            for provider_id, op in _shell_operations(tool_name, arguments, facets)
            if op.resolve_result is None
        ]
        ops = [op for _, op in provider_ops]
        commands = [op for op in ops if op.tracks]
        if not commands:
            return [], False
        text = output_text(result)
        created = all(op.creates for op in commands)
        references = [target for op in commands if (target := op.target) is not None]
        if any(op.creates for op in commands) and (ref := _created_pr_metadata(result)):
            references.append(ref)
        # Reads, comments, and content-only output make shared stdout ambiguous.
        if (
            len(commands) == len(ops)
            and any(op.target is None for op in commands)
            and not any(op.content_only for op in commands)
        ):
            for provider_id, op in provider_ops:
                if op.target is not None:
                    continue
                try:
                    if op.parse_result is not None:
                        parsed = op.parse_result(result)
                    elif op.parse_output is not None:
                        parsed = op.parse_output(text)
                    else:
                        continue
                    references.extend(ref for ref in parsed if ref.provider == provider_id)
                except Exception:  # noqa: BLE001 — one parser must not hide other providers
                    _log_provider_failure(
                        provider_id, "parse_result" if op.parse_result else "parse_output"
                    )
            if any(
                op.parse_result is None and op.parse_output is None and op.target is None
                for op in commands
            ):
                for obj in result_objects(result):
                    if ref := _object_pr(obj, facets):
                        references.append(ref)
                # A single operation's known identity makes rendered body links redundant.
                if len(commands) > 1 or not references:
                    for line in text.splitlines():
                        if len(line.split()) == 1 and (ref := pr_reference(line.strip())):
                            references.append(ref)
    else:
        # The first provider that claims the tool answers for it.
        for facet in facets:
            answer = facet.mcp_prs(tool_name, arguments, result)
            if answer is not None:
                references, created = answer
                break
    return list({ref.url: ref for ref in references}.values()), created


def _record_resolved_prs(
    session_id: str,
    provider_id: str,
    resolve: Callable[[object], Sequence[PullRequestRef]],
    result: object,
    source: str,
    observation_id: str,
    created: bool = False,
) -> None:
    try:
        references = [ref for ref in resolve(result) if ref.provider == provider_id]
        SessionPrRegistry(session_id).record(
            references,
            relationship="created" if created else "worked_on",
            source=source,
            observation_id=observation_id,
        )
        pr_reports.notify(session_id)
    except Exception:  # noqa: BLE001 — optional lookups must never affect tool execution
        _log_provider_failure(provider_id, "resolve_result")
    finally:
        with _pending_lookups_lock:
            _pending_lookups.discard((session_id, observation_id))


def _start_directory(arguments: dict[str, object], result: object, cwd: str | None) -> str | None:
    """Return the absolute directory a shell call started in, when the call says."""
    reported = result.get("cwd") if isinstance(result, dict) else None
    argument = arguments.get("cwd")
    directory = next(
        (value for value in (reported, argument, cwd) if isinstance(value, str) and value), None
    )
    workdir = arguments.get("workdir")
    if isinstance(workdir, str) and workdir:
        directory = os.path.join(directory, workdir) if directory else workdir
    return os.path.normpath(directory) if directory and os.path.isabs(directory) else None


def _scan_pushes(
    session_id: str,
    directories: Sequence[str],
    started_at: float | None,
    finished_at: float,
) -> tuple[list[pushed_branches.BranchKey], pushed_branches.Window]:
    """Read the branches a shell call pushed from the checkouts it touched.

    :returns: The newly pushed branches and the call's time window.
    """
    checkouts = {directory: pushed_branches.checkout(directory) for directory in directories}
    own = {item.toplevel: item for item in checkouts.values() if item is not None}
    touched = list(own.values())
    for toplevel in _pushes.remember(session_id, own):
        if toplevel not in own and (item := pushed_branches.checkout(toplevel)):
            touched.append(item)
    since = _pushes.window(session_id, started_at, finished_at)
    keys: list[pushed_branches.BranchKey] = []
    for push in pushed_branches.pushed_branches(touched, since, finished_at):
        remote = resolve_remote(push.url)
        if remote is None:
            continue
        key = (remote.provider, remote.host, remote.repository, push.branch)
        if _pushes.claim(session_id, (push.repository, *key, push.sha)):
            keys.append(key)
    return keys, (since, finished_at)


def _record_branch_prs(
    session_id: str,
    facets: Sequence[_ProviderFacet],
    branches: Sequence[tuple[pushed_branches.BranchKey, pushed_branches.Window | None]],
    source: str,
) -> None:
    """Record the open PRs of pending branches the session updated; the rest stay pending.

    A PR opened inside a branch's window, its pushing call's, counts as created by it.
    """
    by_provider = {facet.provider_id: facet for facet in facets}
    for key, window in branches:
        provider_id, host, repository, branch = key
        facet = by_provider.get(provider_id)
        if facet is None:
            continue
        try:
            found = [
                pr
                for pr in facet.branch_prs(host, repository, branch)
                if pr.reference.provider == provider_id
            ]
            if not found:
                continue
            created = {
                pr.reference.url
                for pr in found
                if window is not None
                and pr.created_at is not None
                and window[0] - pushed_branches.WINDOW_SLACK_S
                <= pr.created_at
                <= window[1] + _CLOCK_SKEW_S
            }
            registry = SessionPrRegistry(session_id)
            for relationship, references in (
                ("created", [pr.reference for pr in found if pr.reference.url in created]),
                ("worked_on", [pr.reference for pr in found if pr.reference.url not in created]),
            ):
                registry.record(references, relationship=relationship, source=source)
            _pushes.resolved(session_id, key)
            pr_reports.notify(session_id)
        except Exception:  # noqa: BLE001 — optional lookups must never affect tool execution
            _log_provider_failure(provider_id, "branch_prs")


def _recheck(
    session_id: str, facets: Sequence[_ProviderFacet], now: float, *, force: bool = False
) -> None:
    if branches := _pushes.due(session_id, now, force=force):
        _lookup_pool.submit(_record_branch_prs, session_id, facets, branches, "branch_recheck")


def start_observing(session_id: str) -> None:
    """Attribute only pushes from now on to the session's calls that report no start time."""
    _pushes.start(session_id, time.time())


def recheck_pending(session_id: str) -> None:
    """Look up the session's pushed branches that had no PR yet, e.g. as a turn ends."""
    if not session_id:
        return
    try:
        _recheck(session_id, _facets(), time.time(), force=True)
    except Exception:
        _logger.warning(
            "Failed to recheck pushed branches", extra={"session_id": session_id}, exc_info=True
        )


def observe_tool_completion(
    session_id: str,
    *,
    tool_name: str,
    arguments: dict[str, object],
    result: object,
    call_id: str = "",
    source: str = "tool",
    successful: bool = True,
    cwd: str | None = None,
    started_at: float | None = None,
    finished_at: float | None = None,
    repository_visible: bool = True,
) -> None:
    """Best-effort observer; failures never change execution or tool results.

    :param cwd: The directory a shell call started in, when its result does not say.
    :param started_at: When the call started, as a Unix timestamp, when known.
    :param finished_at: When the call finished, if before this observation runs.
    :param repository_visible: Whether the runner sees the call's writes to its checkouts.
        Pushes are read from git's own state when it does, else from ``git push`` output.
    """
    if not successful or not session_id:
        return
    finished_at = time.time() if finished_at is None else finished_at
    try:
        references, created = extract_prs(tool_name, arguments, result)
        SessionPrRegistry(session_id).record(
            references,
            relationship="created" if created else "worked_on",
            source=source,
            observation_id=observation_key(source, call_id, [tool_name, arguments, result]),
        )
        if references:
            pr_reports.notify(session_id)
        shell_result = _shell_result(tool_name, result)
        failed = _failed(shell_result)
        facets = _facets()
        command = _shell_command(tool_name, arguments)
        segments: list[ShellSegment] = []
        pushed: list[pushed_branches.BranchKey] = []
        window: pushed_branches.Window | None = None
        if command is not None:
            strict = _shell_segments(command)
            annotated, directories = pushed_branches.with_directories(
                strict or _shell_segments(command, tolerant=True),
                _start_directory(arguments, shell_result, cwd),
            )
            segments = annotated if strict else []
            if repository_visible:
                # git's state shows a push even when the command's exit status hides it.
                pushed, window = _scan_pushes(session_id, directories, started_at, finished_at)
        if command is not None and not failed:
            for index, (provider_id, op) in enumerate(
                _shell_operations(tool_name, arguments, facets, segments)
            ):
                resolve = op.resolve_result
                if resolve is None:
                    continue
                if op.push_output and pushed:
                    scanned = {key[1:] for key in pushed if key[0] == provider_id}
                    resolve = functools.partial(resolve, scanned=frozenset(scanned))
                observation_id = observation_key(
                    f"{source}:{provider_id}:{index}:resolved",
                    call_id,
                    [tool_name, arguments, result],
                )
                key = (session_id, observation_id)
                with _pending_lookups_lock:
                    if key in _pending_lookups:
                        continue
                    _pending_lookups.add(key)
                try:
                    _lookup_pool.submit(
                        _record_resolved_prs,
                        session_id,
                        provider_id,
                        resolve,
                        shell_result,
                        source,
                        observation_id,
                        op.creates,
                    )
                except Exception:
                    with _pending_lookups_lock:
                        _pending_lookups.discard(key)
                    raise
        writes = (
            []
            if failed
            else [
                (facet.provider_id, write.host, write.repository, write.branch)
                for facet in facets
                for write in (
                    facet.shell_branch_writes(segments)
                    if command is not None
                    else facet.mcp_branch_writes(tool_name, arguments)
                )
            ]
        )
        branches = [*((key, window) for key in pushed), *((key, None) for key in writes)]
        # Pending before the first lookup, so a recheck while it runs still covers them.
        for key, branch_window in branches:
            _pushes.add_pending(session_id, key, finished_at, branch_window)
        if branches:
            _lookup_pool.submit(_record_branch_prs, session_id, facets, branches, source)
        _recheck(session_id, facets, finished_at)
    except Exception:
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
    call_id, cwd = payload.get("tool_use_id"), payload.get("cwd")
    observe_tool_completion(
        session_id,
        tool_name=name,
        arguments=arguments,
        result=payload.get("tool_response", payload.get("tool_output")),
        call_id=call_id if isinstance(call_id, str) else "",
        source="native_hook",
        cwd=cwd if isinstance(cwd, str) else None,
    )
