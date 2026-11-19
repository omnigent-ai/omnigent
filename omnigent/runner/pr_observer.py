"""Extract PR identities from completed shell and MCP tool calls.

The attribution rules here are provider-neutral. Each git provider's pull
request facet recognizes its own shell commands, output objects, and MCP tools.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shlex
import threading
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace

from omnigent.git_providers import load_facet, providers
from omnigent.policies.builtins._shell import (
    HEREDOC_DELIMITER,
    MAX_SHELL_NESTING,
    SHELL_TOOLS,
    real_invocation_tokens,
    skip_command_substitution,
    unwrap_shell_command,
)
from omnigent.runner import pr_reports
from omnigent.runner.git_providers import PullRequestFacet, ShellPrOp, ShellSegment
from omnigent.runner.git_providers.tool_output import output_text, pr_reference, result_objects
from omnigent.runner.session_prs import PullRequestRef, SessionPrRegistry, observation_key

_logger = logging.getLogger(__name__)
# Ids of the providers that already failed once in this process.
_failed_providers: set[str] = set()
_failed_providers_lock = threading.Lock()
_lookup_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="pr-observer")
_pending_lookups: set[tuple[str, str]] = set()
_pending_lookups_lock = threading.Lock()


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


# Shell syntax beyond simple commands, ``;``, ``&&``, ``||``, ``|``, ``( )``, redirections and
# quoting. A command containing any of it keeps the flat parse the observer had before.
_UNMODELED_SHELL = re.compile(
    r"\b(?:case|esac|function|eval|if|then|else|elif|fi|do|done|while|until|for|select)\b"
    r"|[{}]|\(\s*\)|\(\s*\(|=\s*\(|\[\[|[@?*+!<>]\(|\|&|\$\[|\$\(|`"
    r"|(?:^|[\s;&|()])!(?=\s)"
)


def _has_unmodeled_shell(command: str) -> bool:
    """Whether *command* has unmodeled syntax, with or without line continuations."""
    return bool(
        _UNMODELED_SHELL.search(command) or _UNMODELED_SHELL.search(re.sub(r"\\\n", "", command))
    )


# Words that start unmodeled syntax, checked on the decoded invocation.
_UNMODELED_WORDS = frozenset(
    {
        "case",
        "esac",
        "function",
        "eval",
        "if",
        "then",
        "else",
        "elif",
        "fi",
        "do",
        "done",
        "while",
        "until",
        "for",
        "select",
        "{",
        "}",
        "[[",
        "!",
    }
)
# Operators, longest first. Quoted and backslash-escaped characters are never operators.
_SHELL_OPERATORS = ("&&", "||", "(", ")", ";", "&", "|", "\n")


def _shell_items(command: str) -> list[list[str] | str] | None:
    """Split *command* at unquoted operators, returning each simple command's words.

    Quotes and backslashes are honored while scanning, so ``"("`` and ``\\(`` stay
    words. The characters of ``>&``, ``<&``, ``&>`` and ``>|`` are not separators.
    Returns None when the command cannot be lexed.
    """
    items: list[list[str] | str] = []
    chunk: list[str] = []
    quote: str | None = None
    index = 0
    try:
        while index < len(command):
            char = command[index]
            if quote == "'":
                quote = None if char == "'" else quote
                chunk.append(char)
                index += 1
            elif char == "\\" and index + 1 < len(command):
                chunk.append(command[index : index + 2])
                index += 2
            elif quote == '"':
                quote = None if char == '"' else quote
                chunk.append(char)
                index += 1
            elif char in "'\"":
                quote = char
                chunk.append(char)
                index += 1
            else:
                operator = next(
                    (op for op in _SHELL_OPERATORS if command.startswith(op, index)), None
                )
                if operator is None:
                    if char in " \t\r" and not chunk and items and items[-1] in {"&&", "||", "|"}:
                        # Leading whitespace after a connective is not part of a word.
                        index += 1
                        continue
                    chunk.append(char)
                    index += 1
                    continue
                if operator == "\n" and items and items[-1] in {"&&", "||", "|"} and not chunk:
                    # A newline after an operator continues the command.
                    index += 1
                    continue
                # An escaped ``>`` is a literal, so the character after it is a separator.
                last = chunk[-1] if chunk else ""
                previous_char = "" if len(last) == 2 and last[0] == "\\" else last[-1:]
                if (
                    operator == "&"
                    and (command.startswith("&>", index) or previous_char in ("<", ">"))
                ) or (operator == "|" and previous_char == ">"):
                    chunk.append(char)
                    index += 1
                    continue
                items.append(shlex.split("".join(chunk)))
                chunk = []
                items.append(";" if operator == "\n" else operator)
                index += len(operator)
        items.append(shlex.split("".join(chunk)))
    except ValueError:
        return None
    return items


def _shell_steps(
    command: str, depth: int = 0, *, or_lists: bool = False
) -> list[ShellSegment | str] | None:
    """Split a command into simple commands and operators, unwrapping nested shell strings.

    Operators are ``"&&"``, ``"||"``, ``"|"``, ``"&"``, ``"("``, ``")"``, and ``";"``
    (also for newlines). A nested shell runs in a child process, so its steps sit
    between ``"("`` and ``")"``.

    Without *or_lists*, a command with ``||`` or one that cannot be lexed returns
    None, since which of its commands ran is unknown.
    """
    if depth > MAX_SHELL_NESTING:
        return None
    joined, redirected = _prepare_shell_text(command)
    items = _shell_items(joined)
    if items is None:
        return None
    output_eligible = not redirected
    steps: list[ShellSegment | str] = []
    for item in items:
        if isinstance(item, str):
            if item == "||" and not or_lists:
                return None
            if item in {"&", "|", "||"}:
                output_eligible = False
            steps.append(item)
            continue
        tokens = real_invocation_tokens(item)
        if not tokens:
            continue
        if tokens[0] in _UNMODELED_WORDS:
            return None
        inner = unwrap_shell_command(tokens)
        if inner is None:
            steps.append(ShellSegment(raw_tokens=tuple(item), invocation_tokens=tuple(tokens)))
            continue
        # Escaping can hide unmodeled syntax from the outer check, so check each decoded layer.
        if _has_unmodeled_shell(inner):
            return None
        nested = _shell_steps(inner, depth + 1, or_lists=or_lists)
        if nested is None:
            return None
        if not any(isinstance(step, ShellSegment) for step in nested):
            output_eligible = False
        steps.extend(["(", *nested, ")"])
    # Preparation can strip a ``#`` that is part of a word, and with it a closing parenthesis.
    depth = 0
    for step in steps:
        depth += (step == "(") - (step == ")")
        if depth < 0:
            return None
    if depth:
        return None
    if output_eligible:
        return steps
    return [
        replace(step, output_eligible=False) if isinstance(step, ShellSegment) else step
        for step in steps
    ]


def _shell_segments(command: str) -> list[ShellSegment]:
    """Split a command into simple commands, unwrapping nested shell strings in place.

    A command with ``||`` or one that cannot be lexed has no segments, since
    which of its commands ran is unknown. A command with unmodeled shell syntax
    gets the flat parse.
    """
    if _has_unmodeled_shell(command):
        return _flat_shell_segments(command)
    steps = _shell_steps(command)
    if steps is None:
        return _flat_shell_segments(command)
    return [step for step in steps if isinstance(step, ShellSegment)]


def _flat_shell_segments(command: str, depth: int = 0) -> list[ShellSegment]:
    """The flat parse for commands with unmodeled shell syntax.

    It splits on ``;``, ``&``, ``|`` and newlines, but not on parentheses, and
    a ``||`` drops the whole command. This is the parse the observer used before
    subshell and operator tracking.
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
            if token == "||":
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
            nested = _flat_shell_segments(inner, depth + 1)
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


def _shell_operations(
    tool_name: str, arguments: dict[str, object], facets: list[_ProviderFacet]
) -> list[tuple[str, ShellPrOp]]:
    if tool_name not in SHELL_TOOLS and tool_name not in {"exec_command", "run_command"}:
        return []
    command = arguments.get("command", arguments.get("cmd"))
    if not isinstance(command, str) or len(command) > 100_000:
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


def _resolve_directory(current: str | None, argument: str) -> str | None:
    """Return the absolute directory a ``cd``-style *argument* names, if known statically."""
    if "$" in argument or "`" in argument or argument == "-":
        return None
    path = os.path.expanduser(argument)
    if not os.path.isabs(path):
        if current is None:
            return None
        path = os.path.join(current, path)
    return os.path.normpath(path)


# A directory state is the directories a shell may be in at one point, in first-seen order.
_Directories = tuple[str | None, ...]
# Most directories tracked at one point. Beyond this, tracking gives up.
_MAX_CHECKOUT_DIRECTORIES = 8
# Deepest subshell nesting tracked before giving up.
_MAX_SUBSHELL_DEPTH = 16


class _TooComplex(Exception):
    """Directory tracking needs more state than it keeps."""


def _merge_directories(*groups: _Directories) -> _Directories:
    """Return the distinct directories in *groups*, in first-seen order."""
    merged = tuple(dict.fromkeys(directory for group in groups for directory in group))
    if len(merged) > _MAX_CHECKOUT_DIRECTORIES:
        raise _TooComplex()
    return merged


class _CheckoutWalker:
    """Walk shell steps, recording the directories each ``git`` call may run in.

    Each command maps the directories it may start in to the directories it may
    leave with, as a success state and a failure state. A ``cd`` is assumed to
    succeed. ``exit`` and ``return`` are not modeled, so a later command is still
    reported where they ran; that over-reports, but never drops a reachable command.
    A pipeline stage or a subshell runs in a child shell, so its directory changes
    do not reach the parent. A background job (``&``) does the same.
    """

    def __init__(self, steps: list[ShellSegment | str]) -> None:
        self._steps = steps
        self._position = 0
        self._depth = 0
        self.found: dict[str, None] = {}

    def run(self, start: _Directories) -> None:
        directories = start
        while self._position < len(self._steps):
            before = self._position
            ok, fail = self._list(directories)
            directories = _merge_directories(ok, fail)
            # Skip a closer with no opener, or any step that no branch consumed.
            if self._peek_operator() in {")", "}"} or self._position == before:
                self._position += 1

    def _peek(self) -> ShellSegment | str | None:
        return self._steps[self._position] if self._position < len(self._steps) else None

    def _peek_operator(self) -> str | None:
        step = self._peek()
        return step if isinstance(step, str) else None

    def _list(self, directories: _Directories) -> tuple[_Directories, _Directories]:
        current = directories
        while True:
            ok, fail = self._and_or(current)
            separator = self._peek_operator()
            if separator not in {";", "&"}:
                return ok, fail
            self._position += 1
            if separator == "&":
                # A background job leaves the directory unchanged.
                ok = fail = current
            else:
                current = _merge_directories(ok, fail)

    def _and_or(self, directories: _Directories) -> tuple[_Directories, _Directories]:
        ok, fail = self._pipeline(directories)
        while (operator := self._peek_operator()) in {"&&", "||"}:
            self._position += 1
            if operator == "&&":
                right_ok, right_fail = self._pipeline(ok)
                ok, fail = right_ok, _merge_directories(fail, right_fail)
            else:
                right_ok, right_fail = self._pipeline(fail)
                ok, fail = _merge_directories(ok, right_ok), right_fail
        return ok, fail

    def _pipeline(self, directories: _Directories) -> tuple[_Directories, _Directories]:
        outcome = self._stage(directories)
        if self._peek_operator() != "|":
            return outcome
        while self._peek_operator() == "|":
            self._position += 1
            self._stage(directories)
        return directories, directories

    def _stage(self, directories: _Directories) -> tuple[_Directories, _Directories]:
        step = self._peek()
        if step == "(":
            self._position += 1
            self._depth += 1
            if self._depth > _MAX_SUBSHELL_DEPTH:
                raise _TooComplex()
            self._list(directories)
            self._depth -= 1
            if self._peek_operator() == ")":
                self._position += 1
            return directories, directories
        if isinstance(step, ShellSegment):
            self._position += 1
            return self._simple(directories, step)
        return directories, directories

    def _simple(
        self, directories: _Directories, segment: ShellSegment
    ) -> tuple[_Directories, _Directories]:
        tokens = segment.invocation_tokens
        if tokens[0] in {"cd", "pushd"}:
            target = tokens[1] if len(tokens) > 1 else "~"
            resolved = [_resolve_directory(directory, target) for directory in directories]
            return _merge_directories(tuple(resolved)), ()
        if os.path.basename(tokens[0]) == "git":
            for directory in directories:
                git_directory = directory
                if len(tokens) > 2 and tokens[1] == "-C":
                    git_directory = _resolve_directory(directory, tokens[2])
                if git_directory is not None:
                    self.found[git_directory] = None
        return directories, directories


def _checkout_steps(command: str) -> list[ShellSegment | str]:
    """Return the steps the checkout walker runs for *command*."""
    steps = None if _has_unmodeled_shell(command) else _shell_steps(command, or_lists=True)
    if steps is None:
        steps = []
        for segment in _flat_shell_segments(command):
            steps.extend([segment, ";"])
    return steps


def _git_checkouts(
    tool_name: str, arguments: dict[str, object], result: object, cwd: str | None
) -> list[str]:
    """Return the directories a shell call ran ``git`` in, following ``cd`` and ``git -C``.

    *cwd* is where the call started, when the caller knows it. ``sys_os_shell``
    may run under a sandbox that hides other directories, so only the directory
    it reports running in counts.
    """
    if tool_name not in SHELL_TOOLS and tool_name not in {"exec_command", "run_command"}:
        return []
    command = arguments.get("command", arguments.get("cmd"))
    if not isinstance(command, str) or len(command) > 100_000:
        return []
    if tool_name == "sys_os_shell":
        segments = _shell_segments(command)
        reported = result.get("cwd") if isinstance(result, dict) else None
        runs_git = any(os.path.basename(s.invocation_tokens[0]) == "git" for s in segments)
        return [reported] if runs_git and isinstance(reported, str) and reported else []
    steps = _checkout_steps(command)
    workdir = arguments.get("workdir")
    start = _resolve_directory(cwd, workdir) if isinstance(workdir, str) and workdir else cwd
    walker = _CheckoutWalker(steps)
    with contextlib.suppress(_TooComplex):
        walker.run((start,))
    return list(walker.found)[:4]


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
) -> None:
    """Best-effort observer; failures never change execution or tool results.

    *cwd* is the directory a shell call started in, when the caller knows it.
    """
    if not successful or not session_id:
        return
    try:
        from omnigent.runner.pr_resource import infer_checkout_pr

        # A checkout's branch PR is inferred from its state, whatever pushed it.
        for checkout in _git_checkouts(
            tool_name, arguments, _shell_result(tool_name, result), cwd
        ):
            _lookup_pool.submit(infer_checkout_pr, session_id, checkout)
    except Exception:
        _logger.warning(
            "Failed to infer session PRs", extra={"session_id": session_id}, exc_info=True
        )
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
        if not _failed(shell_result):
            for index, (provider_id, op) in enumerate(
                _shell_operations(tool_name, arguments, _facets())
            ):
                if op.resolve_result is None:
                    continue
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
                        op.resolve_result,
                        shell_result,
                        source,
                        observation_id,
                        op.creates,
                    )
                except Exception:
                    with _pending_lookups_lock:
                        _pending_lookups.discard(key)
                    raise
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
        cwd=cwd if isinstance(cwd, str) and os.path.isabs(cwd) else None,
    )
