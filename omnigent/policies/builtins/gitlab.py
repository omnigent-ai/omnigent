"""Project permissions for a bounded set of glab issue and merge-request commands.

Only explicitly supported command/option combinations can run automatically.
Git remote commands, MR writes, and custom API requests require approval.
The policy examines shell text; existing CLI configuration must be trusted.
GitLab MCP tools and direct HTTP clients are outside this policy's scope.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from omnigent.git_providers.gitlab import _valid_project, instance_authority
from omnigent.policies.builtins._shell import (
    MAX_SHELL_NESTING,
    SHELL_TOOLS,
    UnsupportedShellSyntax,
    is_shell_interpreter,
    is_unresolved_invocation,
    real_invocation_tokens,
    spawns_gated_command_as_child,
    split_command_segments,
    unwrap_shell_command,
)
from omnigent.policies.builtins.github import _ask, _deny, _git_subcommand_index, _worse
from omnigent.policies.schema import PolicyEvent, PolicyResponse

_Project = tuple[str | None, str]
_GATED_COMMAND = re.compile(r"\b(?:git|glab)\b", re.IGNORECASE)
_UNMODELLED_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_CLI_ENVIRONMENT = frozenset(
    {
        "GITLAB_HOST",
        "GITLAB_URI",
        "GL_HOST",
        "GLAB_HOST",
        "GITLAB_API_HOST",
        "GLAB_API_HOST",
        "GLAB_CONFIG_DIR",
        "HOME",
        "XDG_CONFIG_HOME",
        "XDG_CONFIG_DIRS",
        "PATH",
    }
)
_LOCAL_GIT_COMMANDS = frozenset(
    {
        "add",
        "commit",
        "diff",
        "log",
        "show",
        "status",
        "branch",
        "tag",
        "rev-parse",
    }
)
_ACTION_ALIASES = {"ls": "list", "show": "view", "new": "create", "comment": "note"}


def _option_spec(values: str = "", switches: str = "") -> dict[str, tuple[str, bool]]:
    """Declare canonical long names and optional short aliases, e.g. ``repo:R``."""
    options: dict[str, tuple[str, bool]] = {}
    for names, takes_value in ((values, True), (switches, False)):
        for declaration in names.split():
            name, _, short = declaration.partition(":")
            options[f"--{name}"] = (name, takes_value)
            if short:
                options[f"-{short}"] = (name, takes_value)
    return options


_COMMON_OPTIONS = _option_spec("repo:R", "help:h")
_COMMAND_OPTIONS = {
    ("issue", "list"): _option_spec(
        "assignee:a author label:l milestone:m page:p per-page:P output:O search", "all:A closed:c"
    ),
    ("issue", "view"): _option_spec("output:F page:p per-page:P", "comments:c system-logs:s"),
    ("issue", "create"): _option_spec("title:t description:d label:l", "confidential:c yes:y"),
    ("issue", "note"): _option_spec("message:m"),
    ("mr", "list"): _option_spec(
        "assignee:a author label:l page:p per-page:P output:F search", "all:A closed:c merged:M"
    ),
    ("mr", "view"): _option_spec("output:F page:p per-page:P", "comments:c system-logs:s"),
    ("mr", "diff"): _option_spec("color", "raw"),
}


@dataclass(frozen=True)
class _GlabArguments:
    """Options retain their values; operands can never become options later."""

    options: tuple[tuple[str, str], ...]
    operands: tuple[str, ...]

    def values(self, name: str) -> list[str]:
        return [value for option, value in self.options if option == name]

    def last(self, name: str) -> str | None:
        values = self.values(name)
        return values[-1] if values else None

    def enabled(self, name: str) -> bool:
        return self.last(name) == "true"


def _parse_glab_arguments(
    args: list[str], options: dict[str, tuple[str, bool]]
) -> _GlabArguments | None:
    """Consume argv once, following pflag value ownership and short clusters."""
    parsed: list[tuple[str, str]] = []
    operands: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        index += 1
        if token == "--":
            operands.extend(args[index:])
            break
        if not token.startswith("-") or token == "-":
            operands.append(token)
            continue
        long_option = token.startswith("--")
        remainder = token if long_option else token[1:]
        while remainder:
            if long_option:
                flag, equal, value = remainder.partition("=")
                remainder = ""
            else:
                flag, remainder = "-" + remainder[0], remainder[1:]
                equal = "=" if remainder.startswith("=") else ""
                value = remainder[1:] if equal else remainder
            spec = options.get(flag)
            if spec is None:
                return None
            name, takes_value = spec
            if takes_value:
                if not equal and (long_option or not remainder):
                    if index >= len(args):
                        return None
                    value = args[index]
                    index += 1
                remainder = ""
            elif equal:
                if value.lower() not in {"true", "false", "t", "f", "1", "0"}:
                    return None
                value = "true" if value.lower() in {"true", "t", "1"} else "false"
                remainder = ""
            else:
                value = "true"
            parsed.append((name, value))
    return _GlabArguments(tuple(parsed), tuple(operands))


def _project_ref(value: str, hosts: frozenset[str]) -> _Project | None:
    """Keep project paths separate from optional, explicitly configured HTTPS hosts."""
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    host = None
    path = value
    if "://" in value:
        try:
            parsed = urlsplit(value)
        except ValueError:
            return None
        host = instance_authority(f"{parsed.scheme}://{parsed.netloc}")
        if host not in hosts or parsed.query or parsed.fragment:
            return None
        path = parsed.path.removeprefix("/")
    else:
        first, separator, rest = value.partition("/")
        if separator and instance_authority(first) in hosts:
            host, path = instance_authority(first), rest
    path = path.removesuffix(".git")
    return (host, path.lower()) if _valid_project(path) and "-" not in path.split("/") else None


def _has_unmodelled_shell_effects(command: str) -> bool:
    """Expansion and redirection can change the arguments delivered to a command."""
    # These quoting/escape forms have different meanings in POSIX, cmd and PowerShell.
    if any(char in command for char in "\\'^%‘’‚‛“”„‟"):
        return True
    quote = ""
    for index, char in enumerate(command):
        if char == "!" and (
            (index > 0 and not command[index - 1].isspace())
            or (index + 1 < len(command) and not command[index + 1].isspace())
        ):
            return True
        if char == quote:
            quote = ""
        elif char == '"' and not quote:
            quote = char
        elif char in {"$", "`"} or (not quote and char in "*?[]{}~<>"):
            return True
    return False


def _evaluate_shell(
    command: str,
    check: Callable[[list[str]], PolicyResponse | None],
    depth: int = 0,
) -> PolicyResponse | None:
    """Reuse shell boundaries and preserve the strictest decision across a command."""
    if depth > MAX_SHELL_NESTING:
        return _ask("GitLab policy cannot inspect further nested shell commands. Approve?")
    decision = (
        _ask("Unmodelled shell control characters require approval.")
        if _UNMODELLED_CONTROL.search(command)
        else None
    )
    try:
        segments = split_command_segments(command)
    except UnsupportedShellSyntax as error:
        segments = error.completed_segments
        decision = _ask("GitLab policy cannot fully inspect this shell syntax. Approve?")
    for segment in segments:
        if _UNMODELLED_CONTROL.search(segment):
            continue
        if _has_unmodelled_shell_effects(segment):
            decision = _worse(
                decision,
                _ask("Shell escapes, expansion or redirection require approval."),
            )
            continue
        try:
            raw_tokens = shlex.split(segment)
        except ValueError:
            if _GATED_COMMAND.search(segment):
                decision = _worse(decision, _ask("Could not parse the git/glab command. Approve?"))
            continue
        if any(
            (name := token.partition("=")[0]) in _CLI_ENVIRONMENT
            or name.startswith(("GITLAB_", "GLAB_", "GIT_CONFIG"))
            for token in raw_tokens
            if "=" in token
        ):
            decision = _worse(decision, _ask("CLI environment changes require approval."))
        tokens = real_invocation_tokens(raw_tokens)
        while tokens[:1] == ["!"]:
            tokens = real_invocation_tokens(tokens[1:])
        if not tokens:
            continue
        if is_unresolved_invocation(tokens) or spawns_gated_command_as_child(tokens):
            if _GATED_COMMAND.search(" ".join(raw_tokens)):
                decision = _worse(
                    decision, _ask("The git/glab invocation is unresolved. Approve?")
                )
            continue
        inner = unwrap_shell_command(tokens)
        if inner is not None:
            if is_shell_interpreter(tokens) and inner.startswith(("-", "+")):
                decision = _worse(
                    decision, _ask("Shell options after the command-string flag require approval.")
                )
            else:
                decision = _worse(decision, _evaluate_shell(inner, check, depth + 1))
        elif is_shell_interpreter(tokens):
            decision = _worse(decision, _ask("This shell program requires approval."))
        elif tokens[0] in {"alias", "function"} and _GATED_COMMAND.search(segment):
            decision = _worse(decision, _ask("Changing git/glab commands requires approval."))
        else:
            decision = _worse(decision, check(tokens))
    return decision


def gitlab_policy(
    *,
    hosts: list[str] | None = None,
    read_all: bool = True,
    read_repos: list[str] | None = None,
    write_repos: list[str] | None = None,
    shell_tools: list[str] | None = None,
    deny_reason: str = "GitLab operation blocked by policy.",
) -> Callable[[PolicyEvent], PolicyResponse | None]:
    """Allow supported glab activities only on explicitly selected GitLab projects.

    :param hosts: Trusted GitLab HTTPS authorities; defaults to gitlab.com.
    :param read_all: Allow supported reads across projects on the configured hosts.
    :param read_repos: Readable project paths or host-pinned HTTPS project URLs.
    :param write_repos: Projects on which issues may be created or commented on.
    :param shell_tools: Tools whose command argument is checked; defaults to all shell tools.
    :param deny_reason: Prefix for DENY explanations.
    :returns: A policy returning DENY, ASK, or None when it abstains.
    """
    authorities = [
        instance_authority(value) for value in (hosts if hosts is not None else ["gitlab.com"])
    ]
    if not authorities or any(host is None for host in authorities):
        raise ValueError("gitlab_policy: hosts must contain valid HTTPS authorities")
    owned_hosts = frozenset(host for host in authorities if host is not None)

    def projects(values: list[str] | None) -> set[_Project]:
        result = set()
        for value in values or []:
            project = _project_ref(value, owned_hosts)
            if project is None:
                raise ValueError(f"gitlab_policy: invalid project entry {value!r}")
            result.add(project)
        return result

    allowed_read, allowed_write = projects(read_repos), projects(write_repos)
    tools = SHELL_TOOLS if shell_tools is None else frozenset(shell_tools)

    def check(tokens: list[str]) -> PolicyResponse | None:
        executable = (
            tokens[0].replace("\\", "/").rsplit("/", 1)[-1].casefold().removesuffix(".exe")
        )
        if executable == "git":
            try:
                index = _git_subcommand_index(tokens)
            except ValueError:
                index = None
            if index == 1 and tokens[index] in _LOCAL_GIT_COMMANDS:
                return None
            return _ask("Git remote operations and configuration require approval.")
        if executable != "glab":
            if _GATED_COMMAND.search(" ".join(tokens)):
                return _ask("This command form mentions git/glab but cannot be resolved. Approve?")
            return None
        if len(tokens) < 3:
            return _ask("Choose a supported glab action and an explicit project. Approve?")
        group, action = tokens[1:3]
        action = _ACTION_ALIASES.get(action, action)
        options = _COMMAND_OPTIONS.get((group, action))
        if options is None:
            if group in {"issue", "mr", "repo", "project"} and action in {"delete", "del", "rm"}:
                return _deny(f"{deny_reason} GitLab deletion is not permitted.")
            return _ask("This glab action requires approval.")
        args = _parse_glab_arguments(tokens[3:], _COMMON_OPTIONS | options)
        if args is None:
            return _ask("Unsupported or incomplete glab options require approval.")
        if args.enabled("help"):
            return None
        selectors = args.values("repo")
        if not selectors or len(set(selectors)) != 1:
            return _ask("Select one explicit GitLab server and project with --repo. Approve?")
        project = _project_ref(selectors[0], owned_hosts)
        if project is None or project[0] is None:
            return _ask("The GitLab server and project could not be established. Approve?")
        needs_number = action in {"view", "diff", "note"}
        if (
            needs_number
            and (len(args.operands) != 1 or not re.fullmatch(r"[1-9][0-9]*", args.operands[0]))
        ) or (not needs_number and args.operands):
            return _ask("Unsupported glab operands require approval; use a numeric issue/MR ID.")
        write = (group, action) in {("issue", "create"), ("issue", "note")}
        allowed = allowed_write if write else allowed_read
        if (
            (write or not read_all)
            and project not in allowed
            and (None, project[1]) not in allowed
        ):
            return _deny(
                f"{deny_reason} Project {project[0]}/{project[1]} is not permitted "
                f"for this {'write' if write else 'read'}."
            )
        if write:
            required = ("title", "description") if action == "create" else ("message",)
            if any(not args.last(name) or args.last(name) == "-" for name in required):
                return _ask("Provide explicit issue text; interactive input requires approval.")
        return None

    def evaluate(event: PolicyEvent) -> PolicyResponse | None:
        if event.get("type") != "tool_call":
            return None
        data = event.get("data")
        if not isinstance(data, dict) or data.get("name") not in tools:
            return None
        arguments = data.get("arguments")
        command = arguments.get("command") if isinstance(arguments, dict) else None
        if not isinstance(command, str) or not command.strip():
            return None
        return _evaluate_shell(command, check)

    return evaluate


POLICY_REGISTRY: list[dict[str, Any]] = [  # type: ignore[explicit-any]
    {
        "handler": "omnigent.policies.builtins.gitlab.gitlab_policy",
        "kind": "factory",
        "name": "GitLab Project Access",
        "description": (
            "Project permissions for supported glab issue and MR reads, issue creation, "
            "and issue comments. Other GitLab operations require approval."
        ),
        "params_schema": {
            "type": "object",
            "properties": {
                "hosts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": ["gitlab.com"],
                    "description": "Trusted GitLab HTTPS authorities.",
                },
                "read_all": {
                    "type": "boolean",
                    "default": True,
                    "description": "Allow supported reads across projects on configured hosts.",
                },
                "read_repos": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Readable project paths or HTTPS project URLs.",
                },
                "write_repos": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Projects permitting issue creation and comments.",
                },
                "shell_tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Shell tools to inspect; defaults to all harness shell tools.",
                },
                "deny_reason": {
                    "type": "string",
                    "default": "GitLab operation blocked by policy.",
                    "description": "Prefix for DENY explanations.",
                },
            },
        },
    },
]
