"""Extract PR identities from completed shell and MCP tool calls.

The attribution rules here are provider-neutral. Each git provider's pull
request facet recognizes its own shell commands, output objects, and MCP tools.
"""

from __future__ import annotations

import logging
import re
import shlex
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from omnigent.git_providers import load_facet, providers
from omnigent.policies.builtins._shell import (
    MAX_SHELL_NESTING,
    SHELL_TOOLS,
    real_invocation_tokens,
    unwrap_shell_command,
)
from omnigent.runner.git_providers import PullRequestFacet, ShellPrOp, ShellSegment
from omnigent.runner.git_providers.tool_output import output_text, pr_reference, result_objects
from omnigent.runner.session_prs import PullRequestRef, SessionPrRegistry, observation_key

_logger = logging.getLogger(__name__)
# Ids of the providers that already failed once in this process.
_failed_providers: set[str] = set()
_failed_providers_lock = threading.Lock()


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
    return False


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


def _shell_segments(command: str, depth: int = 0) -> list[ShellSegment]:
    """Split a command into simple commands, unwrapping nested shell strings in place.

    A command with ``||`` or one that cannot be lexed has no segments, since
    which of its commands ran is unknown.
    """
    if depth > MAX_SHELL_NESTING:
        return []
    found: list[ShellSegment] = []
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
            found.extend(_shell_segments(inner, depth + 1))
        else:
            found.append(ShellSegment(raw_tokens=tuple(segment), invocation_tokens=tuple(tokens)))
    return found


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


def extract_prs(
    tool_name: str, arguments: dict[str, object], result: object
) -> tuple[list[PullRequestRef], bool]:
    """Return positively identified PRs and whether the operation created them."""
    if _failed(result):
        return [], False
    facets = _facets()
    references: list[PullRequestRef] = []
    created = False
    if tool_name in SHELL_TOOLS or tool_name in {"exec_command", "run_command"}:
        command = arguments.get("command", arguments.get("cmd"))
        if not isinstance(command, str) or len(command) > 100_000:
            return [], False
        segments = _shell_segments(command)
        # Every recognized PR command, reads included; ``commands`` are those that change a PR.
        ops = [op for facet in facets for op in facet.shell_pr_operations(segments)]
        commands = [op for op in ops if op.tracks]
        if not commands:
            return [], False
        text = output_text(result)
        if re.search(
            r"(?:^|\n)(?:\[exit code: -?[1-9][0-9]*\]"
            r"|Process exited with code -?[1-9][0-9]*)\s*\Z",
            text,
        ):
            return [], False
        created = all(op.creates for op in commands)
        references = [target for op in commands if (target := op.target) is not None]
        if any(op.creates for op in commands) and (ref := _created_pr_metadata(result)):
            references.append(ref)
        # Shared stdout cannot attribute a result to a write when reads/comments also ran.
        if (
            len(commands) == len(ops)
            and any(op.target is None for op in commands)
            and (len(commands) > 1 or not commands[0].content_only)
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
    except Exception:  # noqa: BLE001 — the observer must never change a tool call's result
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
