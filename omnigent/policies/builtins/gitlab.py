"""Host-aware access policy for GitLab ``glab`` and remote ``git`` commands.

The policy deliberately requires a single configured GitLab instance.  That
keeps self-managed/Dedicated credentials and allowlists from applying to an
unrelated Git remote with the same project path.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from omnigent.policies.builtins._shell import (
    SHELL_TOOLS,
    real_invocation_tokens,
    split_command_segments,
)
from omnigent.policies.schema import PolicyEvent, PolicyResponse


def _deny(reason: str) -> PolicyResponse:
    return {"result": "DENY", "reason": reason}


def _ask(reason: str) -> PolicyResponse:
    return {"result": "ASK", "reason": reason}


def _instance_host(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("gitlab_host must be an HTTPS instance URL without credentials")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("gitlab_host must not include a path, query, or fragment")
    return parsed.netloc.lower()


def _repo_pattern(host: str) -> re.Pattern[str]:
    escaped = re.escape(host)
    return re.compile(
        rf"(?<![A-Za-z0-9._-]){escaped}(?::|/)([A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)+)(?:\.git)?(?:[?#]|$)",
        re.IGNORECASE,
    )


def _normalize_project(value: str, *, url_pattern: re.Pattern[str]) -> str:
    value = value.strip().strip("/")
    match = url_pattern.search(value)
    if match:
        value = match.group(1)
    elif "://" in value or "@" in value:
        return ""
    value = value.removesuffix(".git").strip("/")
    parts = value.split("/")
    if len(parts) < 2 or any(not part or part in {".", ".."} for part in parts):
        return ""
    if not all(re.fullmatch(r"[A-Za-z0-9._-]+", part) for part in parts):
        return ""
    return value.lower()


def _flag_value(tokens: list[str], names: set[str]) -> str | None:
    for index, token in enumerate(tokens):
        for name in names:
            if token == name and index + 1 < len(tokens):
                return tokens[index + 1]
            if token.startswith(name + "="):
                return token[len(name) + 1 :]
    return None


def _is_write_glab(tokens: list[str]) -> bool:
    if len(tokens) < 2:
        return False
    group, action = tokens[0], tokens[1]
    return group in {
        "mr",
        "issue",
        "label",
        "release",
        "variable",
        "project",
        "repo",
    } and action in {
        "create",
        "edit",
        "update",
        "merge",
        "close",
        "reopen",
        "approve",
        "delete",
        "remove",
        "set",
    }


def gitlab_policy(
    *,
    gitlab_host: str,
    read_all: bool = True,
    read_repos: list[str] | None = None,
    write_repos: list[str] | None = None,
    write_branches: list[str] | None = None,
    shell_tools: list[str] | None = None,
    mcp_tool_prefixes: list[str] | None = None,
) -> Callable[[PolicyEvent], PolicyResponse | None]:
    """Build a GitLab policy for one configured GitLab.com or self-managed host.

    ``read_repos`` and ``write_repos`` accept nested GitLab project paths or URLs
    for ``gitlab_host``. Writes require an explicit ``write_repos`` allowlist.
    Unknown shell write targets ask for approval; unknown GitLab MCP writes deny.
    """
    host = _instance_host(gitlab_host)
    url_pattern = _repo_pattern(host)

    def normalize(value: str) -> str:
        return _normalize_project(value, url_pattern=url_pattern)

    allowed_reads = {project for value in read_repos or [] if (project := normalize(value))}
    allowed_writes = {project for value in write_repos or [] if (project := normalize(value))}
    branches = {
        value.removeprefix("refs/heads/") for value in write_branches or [] if value.strip()
    }
    shells = frozenset(shell_tools or SHELL_TOOLS)
    prefixes = tuple(mcp_tool_prefixes or ("mcp__gitlab__", "gitlab__"))

    def gate(
        project: str | None, *, write: bool, branch: str | None, unknown: str
    ) -> PolicyResponse | None:
        if not write and read_all:
            return None
        allowed = allowed_writes if write else allowed_reads
        if not project:
            return (
                _ask(unknown)
                if unknown == "shell"
                else _deny("GitLab target project could not be determined.")
            )
        if project not in allowed:
            verb = "Write" if write else "Read"
            return _deny(
                f"{verb} is restricted to configured GitLab projects; target is {project!r}."
            )
        if write and branches:
            if not branch:
                return (
                    _ask("GitLab write target branch could not be determined.")
                    if unknown == "shell"
                    else _deny("GitLab write target branch could not be determined.")
                )
            if branch not in branches:
                return _deny(
                    f"Write is restricted to branches {sorted(branches)}; target is {branch!r}."
                )
        return None

    def project_from_tokens(tokens: list[str]) -> str | None:
        candidate = _flag_value(tokens, {"-R", "--repo", "--project"})
        if candidate and (project := normalize(candidate)):
            return project
        for token in tokens:
            if project := normalize(token):
                return project
        return None

    def shell_decision(command: str) -> PolicyResponse | None:
        decision: PolicyResponse | None = None
        for segment in split_command_segments(command):
            try:
                tokens = real_invocation_tokens(shlex.split(segment, posix=True))
            except ValueError:
                tokens = []
            if not tokens:
                continue
            executable = tokens[0].rsplit("/", 1)[-1]
            args = tokens[1:]
            if executable == "glab":
                environment_host = re.search(r"(?:^|\s)GLAB_HOST=([^\s]+)", segment)
                selected_host = _flag_value(args, {"--hostname", "-h"}) or (
                    environment_host.group(1) if environment_host else host
                )
                if selected_host.lower() != host:
                    continue
                write = _is_write_glab(args)
                result = gate(
                    project_from_tokens(args),
                    write=write,
                    branch=_flag_value(args, {"--target-branch", "--base"}),
                    unknown="shell",
                )
            elif (
                executable == "git"
                and args
                and args[0] in {"clone", "fetch", "pull", "push", "ls-remote"}
            ):
                remote = next((arg for arg in args[1:] if not arg.startswith("-")), "")
                project = normalize(remote)
                if remote and not project and ("://" in remote or "@" in remote):
                    continue
                write = args[0] == "push"
                branch = next(
                    (arg for arg in args[2:] if not arg.startswith("-") and "/" not in arg), None
                )
                result = gate(project, write=write, branch=branch, unknown="shell")
            else:
                continue
            if result and (decision is None or result["result"] == "DENY"):
                decision = result
        return decision

    def mcp_decision(name: str, arguments: dict[str, Any]) -> PolicyResponse | None:
        canonical = next(
            (name[len(prefix) :] for prefix in prefixes if name.startswith(prefix)), None
        )
        if canonical is None:
            return None
        lowered = canonical.lower()
        write = any(
            word in lowered
            for word in (
                "create",
                "update",
                "edit",
                "merge",
                "delete",
                "close",
                "approve",
                "write",
                "push",
            )
        )
        read = any(word in lowered for word in ("get", "list", "search", "read", "view", "diff"))
        if not write and not read:
            return _deny(f"Unrecognized GitLab MCP operation {name!r}.")
        project_value = next(
            (
                value
                for key in ("project", "project_id", "repo", "repository", "path_with_namespace")
                if isinstance((value := arguments.get(key)), str)
            ),
            "",
        )
        branch = next(
            (
                value
                for key in ("target_branch", "base", "branch", "ref")
                if isinstance((value := arguments.get(key)), str)
            ),
            None,
        )
        return gate(normalize(project_value), write=write, branch=branch, unknown="mcp")

    def evaluate(event: PolicyEvent) -> PolicyResponse | None:
        if event.get("type") != "tool_call" or not isinstance((data := event.get("data")), dict):
            return None
        name = data.get("name")
        arguments = data.get("arguments")
        if not isinstance(name, str):
            return None
        arguments = arguments if isinstance(arguments, dict) else {}
        if name in shells and isinstance(arguments.get("command"), str):
            return shell_decision(arguments["command"])
        return mcp_decision(name, arguments)

    return evaluate


POLICY_REGISTRY: list[dict[str, Any]] = [
    {
        "handler": "omnigent.policies.builtins.gitlab.gitlab_policy",
        "kind": "factory",
        "name": "GitLab Project & Branch Access",
        "description": (
            "Restricts glab, GitLab MCP, and explicit GitLab git remotes to one configured "
            "GitLab host and project allowlists."
        ),
        "params_schema": {
            "type": "object",
            "properties": {
                "gitlab_host": {
                    "type": "string",
                    "description": "Configured GitLab HTTPS instance URL.",
                },
                "read_all": {"type": "boolean", "default": True},
                "read_repos": {"type": "array", "items": {"type": "string"}},
                "write_repos": {"type": "array", "items": {"type": "string"}},
                "write_branches": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["gitlab_host"],
        },
    }
]
