"""Built-in GitHub access policy (MCP- and shell-agnostic).

A single factory, :func:`github_policy`, gating GitHub access across two
surfaces an Omnigent agent can reach GitHub through:

- **MCP tool calls** — both the official per-operation GitHub MCP server
  (``get_file_contents`` / ``create_or_update_file`` / ``push_files`` /
  ``create_pull_request`` / ``merge_pull_request`` …) and the layered
  HTTP-proxy wrapper that splits verbs at the tool-name level
  (``github_read_api_call`` / ``github_write_api_call``).
- **``git`` / ``gh`` shell commands** dispatched through ``sys_os_shell``
  (the built-in OS shell tool). The command string is parsed; commands that
  never touch the remote (``git status`` / ``git commit`` / ``git diff`` …)
  are ignored, while remote reads (``git clone`` / ``gh pr view``) and writes
  (``git push`` / ``gh pr create`` / ``gh repo delete``) are gated.

This deliberately does NOT cover the raw GitHub HTTP API hit directly from the
sandbox (``curl https://api.github.com`` or ``git`` over HTTPS bypassing the
shell tool). Enforcing that path requires an HTTP forwarding gateway, which is
a separate enforcement point the runtime does not have today.

Enforcement is a **pure allowlist** (no created-resource tracking, so the
policy is stateless and only ever inspects ``tool_call`` events):

- Reads are allowed everywhere when ``read_all`` is ``True`` (default). When
  ``read_all`` is ``False``, reads are restricted to ``read_repos``.
- Writes are restricted to ``write_repos`` and, when ``write_branches`` is set,
  to those branches.
- When a *shell* command is a gated read/write but its target repo (or, for a
  branch-restricted write, its target branch) cannot be determined from the
  command text — e.g. ``git push origin main``, where ``origin`` is a local
  remote alias the policy cannot resolve to a repo — the decision is **ASK**
  (human approval) rather than a guess. MCP tool calls carry structured
  ``owner``/``repo`` args, so an MCP write with no determinable repo is instead
  DENYed as anomalous.

Repos are matched case-insensitively as ``owner/repo`` (GitHub treats them so);
branches are matched exactly (git branch names are case-sensitive). Both bare
``owner/repo`` strings and full GitHub URLs (``https://github.com/owner/repo``,
``git@github.com:owner/repo.git``) are accepted in the allowlists and parsed
out of arguments / command text.

MCP-agnostic: tools are recognized by their *canonical* name after stripping a
server prefix (default ``mcp__github__`` / ``github__``; override via
``mcp_tool_prefixes``). A tool that carried a GitHub server prefix but cannot be
classified as read or write fails closed (DENY) — we will not let an
unrecognized GitHub operation through. Tools with no GitHub prefix are only
claimed when their canonical name is a known GitHub operation, so the policy
composes freely with non-GitHub policies (it abstains on everything else).

The factory must be referenced via ``function: {path, arguments}`` with a
non-empty ``arguments`` block (the registry declares it ``kind: "factory"``).

YAML usage::

    policies:
      github_guard:
        type: function
        function:
          path: omnigent.policies.builtins.github.github_policy
          arguments:
            read_all: false
            read_repos: ["my-org/service-a", "my-org/service-b"]
            write_repos: ["my-org/service-a"]
            write_branches: ["main", "develop"]
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

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
from omnigent.policies.schema import PolicyEvent, PolicyResponse

# ── Shared constants ──────────────────────────────────────────────────────────

# Tool-name prefixes stripped to obtain the canonical tool name. Longest-first
# so ``mcp__github__`` wins over a bare ``github__``.
_DEFAULT_TOOL_PREFIXES: tuple[str, ...] = ("mcp__github__", "github__")

# Shell tools whose command string this policy parses for git / gh invocations:
# every harness's, since most ``git push`` runs on a native harness.
_DEFAULT_SHELL_TOOLS: frozenset[str] = SHELL_TOOLS

# Owner/repo path after the URL authority has been validated. The authority is
# checked separately, so a ``github.com`` segment inside another host's path is ignored.
_REPO_PATH_PATTERN = re.compile(r"^([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+?)(?:\.git)?(?:/|$)")
# scp-style ``[user@]github.com:owner/repo``.
_REPO_SCP_PATTERN = re.compile(r"^(?:[^@/:]+@)?github\.com:(.+)$", re.IGNORECASE)

# Bare ``owner/repo`` with nothing else around it (no scheme, no host, no extra
# path). Used for allowlist entries and ``--repo owner/repo`` style args.
_REPO_BARE_PATTERN = re.compile(r"^([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+?)(?:\.git)?$")

# Pulls ``owner/repo`` out of a ``gh api`` REST path like ``repos/owner/repo/pulls``.
_REPO_API_PATH_PATTERN = re.compile(r"^repos/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)(?:/|$)")

# Arg keys (MCP) that, combined, name the owning org of a repo.
_OWNER_ARG_KEYS: tuple[str, ...] = ("owner", "org", "organization")
# Arg keys (MCP) whose value may be a full ``owner/repo`` string.
_FULL_REPO_ARG_KEYS: tuple[str, ...] = (
    "repository",
    "full_name",
    "repo",
    "name_with_owner",
    "nameWithOwner",
)
# Arg keys (MCP) carrying a *destination* branch / ref — the branch a write
# lands on. Excludes ``head`` / ``headRefName``: a PR's head is its source
# branch, not a write target, so gating it would wrongly block ``feature →
# main`` PRs.
_BRANCH_ARG_KEYS: tuple[str, ...] = (
    "branch",
    "ref",
    "base",
    "branch_name",
    "base_branch",
    "target_branch",
    "baseRefName",
    "ref_name",
)
# Nested arg containers (the layered wrapper nests REST params under these).
_NESTED_ARG_KEYS: tuple[str, ...] = (
    "params",
    "parameters",
    "arguments",
    "body",
    "payload",
    "input",
)


# ── Shared helpers ────────────────────────────────────────────────────────────


def _deny(reason: str) -> PolicyResponse:
    """
    Build a DENY response with a human-readable reason.

    :param reason: Why the operation was blocked, e.g.
        ``"Write restricted to write_repos; this call targets 'x/y'."``.
    :returns: A :class:`PolicyResponse` with a DENY decision.
    """
    return {"result": "DENY", "reason": reason}


def _ask(reason: str) -> PolicyResponse:
    """
    Build an ASK response with a human-readable reason.

    :param reason: Why human approval is needed, e.g.
        ``"Could not determine the target repo for this git push."``.
    :returns: A :class:`PolicyResponse` with an ASK decision.
    """
    return {"result": "ASK", "reason": reason}


def _canonical_tool_name(tool_name: str, prefixes: tuple[str, ...]) -> str:
    """
    Strip the first matching server prefix to get the canonical name.

    :param tool_name: Raw tool name, e.g. ``"mcp__github__create_pull_request"``
        or ``"github__github_read_api_call"``.
    :param prefixes: Prefixes to try, longest-first.
    :returns: The canonical name (``"create_pull_request"``), or *tool_name*
        unchanged when no prefix matches.
    """
    for prefix in prefixes:
        if tool_name.startswith(prefix):
            return tool_name[len(prefix) :]
    return tool_name


def _normalize_repo(value: str) -> str:
    """
    Reduce a repo reference to a bare lowercase ``owner/repo``.

    :param value: A bare ``owner/repo``, or a GitHub URL, e.g.
        ``"https://github.com/Octo/Repo/pull/1"`` or ``"git@github.com:Octo/Repo.git"``.
    :returns: The lowercased ``owner/repo`` (``"octo/repo"``), or empty string
        when no repo can be extracted.
    """
    value = value.strip()
    if not value:
        return ""
    bare_match = _REPO_BARE_PATTERN.match(value)
    if bare_match:
        return f"{bare_match.group(1)}/{bare_match.group(2)}".lower()
    return _repo_from_url(value)


def _repo_from_url(value: str, *, allow_subpath: bool = True) -> str:
    """
    Extract ``owner/repo`` from a GitHub URL/SCP ref, host-checked.

    Unlike :func:`_normalize_repo`, this never falls back to treating a bare
    ``a/b`` string as a repo. The URL scheme and host must name ``github.com``
    exactly, and percent-encoded or dot-segment paths are rejected.

    :param value: A value that may contain a GitHub URL, e.g.
        ``"https://github.com/Octo/Repo/pull/1"`` or
        ``"git@github.com:Octo/Repo.git"``.
    :param allow_subpath: Accept paths after the repository, such as a PR URL.
        Git push destinations set this to ``False``.
    :returns: Lowercased ``owner/repo``, or empty string if the value does
        not reference a ``github.com`` repo.
    """
    value = value.strip()
    scp_match = _REPO_SCP_PATTERN.fullmatch(value)
    if scp_match:
        path = scp_match.group(1)
    else:
        try:
            parsed = urlsplit(value)
        except ValueError:
            return ""
        if parsed.scheme.lower() not in {"http", "https", "ssh", "git"}:
            return ""
        if parsed.hostname != "github.com":
            return ""
        path = parsed.path.lstrip("/")
    if "%" in path or any(segment in {".", ".."} for segment in path.split("/")):
        return ""
    match = _REPO_PATH_PATTERN.match(path) if allow_subpath else _REPO_BARE_PATTERN.fullmatch(path)
    return f"{match.group(1)}/{match.group(2)}".lower() if match else ""


def _normalize_repos(values: list[str] | None) -> set[str]:
    """
    Normalize a list of repo references to a set of lowercase ``owner/repo``.

    :param values: Repo IDs and/or GitHub URLs. ``None`` → empty list.
    :returns: Set of normalized ``owner/repo``, un-parseable / empty entries
        dropped.
    """
    return {repo for value in (values or []) if (repo := _normalize_repo(value))}


def _normalize_branch(value: str) -> str:
    """
    Reduce a branch / ref reference to a bare branch name.

    Strips a ``refs/heads/`` (or ``heads/``) ref prefix and a ``owner:``
    cross-fork PR-head prefix; leaves the case unchanged (branch names are
    case-sensitive).

    :param value: A branch name or ref, e.g. ``"refs/heads/main"``,
        ``"some-fork:feature"``, or ``"main"``.
    :returns: The bare branch name (``"main"`` / ``"feature"``), or empty string.
    """
    value = value.strip()
    if not value:
        return ""
    for ref_prefix in ("refs/heads/", "heads/"):
        if value.startswith(ref_prefix):
            value = value[len(ref_prefix) :]
            break
    # A cross-fork PR head is ``owner:branch`` — keep only the branch.
    if ":" in value:
        value = value.split(":", 1)[1]
    return value.strip()


def _normalize_branches(values: list[str] | None) -> set[str]:
    """
    Normalize a list of branch references to a set of bare branch names.

    :param values: Branch names / refs. ``None`` → empty list.
    :returns: Set of bare branch names, empties dropped.
    """
    return {branch for value in (values or []) if (branch := _normalize_branch(value))}


def _first_str(args: dict[str, Any], keys: tuple[str, ...]) -> str | None:  # type: ignore[explicit-any]
    """
    Return the first string value among *keys* present in *args*.

    :param args: A tool-arguments dict.
    :param keys: Keys to try, in priority order, e.g. :data:`_OWNER_ARG_KEYS`.
    :returns: The first non-empty string value, or ``None``.
    """
    for key in keys:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _extract_repos_from_args(  # type: ignore[explicit-any]
    args: dict[str, Any],
    _depth: int = 0,
) -> set[str]:
    """
    Pull every candidate target repo out of a tool call's arguments.

    Handles three shapes: separate ``owner``/``org`` + ``repo`` keys, a single
    key whose value is already ``owner/repo``, and any string arg that is a
    GitHub URL. Recurses one level into nested REST-param containers (the
    layered HTTP-proxy wrapper nests ``org``/``repo`` under ``params``).

    :param args: The ``event["data"]["arguments"]`` dict, e.g.
        ``{"owner": "octo", "repo": "hello"}``.
    :param _depth: Internal recursion guard; callers leave it 0.
    :returns: Set of normalized ``owner/repo`` (empty for un-scopeable calls
        like a global search).
    """
    repos: set[str] = set()

    owner = _first_str(args, _OWNER_ARG_KEYS)
    name = args.get("repo")
    if owner and isinstance(name, str) and name.strip() and "/" not in name:
        combined = _normalize_repo(f"{owner}/{name.strip()}")
        if combined:
            repos.add(combined)

    for key in _FULL_REPO_ARG_KEYS:
        value = args.get(key)
        if isinstance(value, str) and "/" in value:
            normalized = _normalize_repo(value)
            if normalized:
                repos.add(normalized)

    for value in args.values():
        if isinstance(value, str):
            normalized = _repo_from_url(value)
            if normalized:
                repos.add(normalized)

    if _depth < 1:
        for key in _NESTED_ARG_KEYS:
            nested = args.get(key)
            if isinstance(nested, dict):
                repos |= _extract_repos_from_args(nested, _depth + 1)

    return repos


def _extract_branches_from_args(  # type: ignore[explicit-any]
    args: dict[str, Any],
    _depth: int = 0,
) -> set[str]:
    """
    Pull every candidate target branch out of a tool call's arguments.

    :param args: The ``event["data"]["arguments"]`` dict, e.g.
        ``{"branch": "main"}`` or ``{"ref": "refs/heads/dev"}``.
    :param _depth: Internal recursion guard; callers leave it 0.
    :returns: Set of bare branch names (empty when no branch arg is present).
    """
    branches: set[str] = set()
    for key in _BRANCH_ARG_KEYS:
        value = args.get(key)
        if isinstance(value, str):
            branch = _normalize_branch(value)
            if branch:
                branches.add(branch)
    if _depth < 1:
        for key in _NESTED_ARG_KEYS:
            nested = args.get(key)
            if isinstance(nested, dict):
                branches |= _extract_branches_from_args(nested, _depth + 1)
    return branches


# ── MCP tool classification ────────────────────────────────────────────────────

# Canonical (post-``github_`` strip) names that touch no repo and are always
# safe — discovery / planning helpers exposed by the wrapper servers.
_MCP_ALWAYS_ALLOW: frozenset[str] = frozenset({"get_service_info", "get_api_info"})

# Read tools whose canonical name does not start with a read verb prefix, so the
# verb heuristic alone would miss them. (Verb-prefixed reads like ``get_*`` /
# ``list_*`` / ``search_*`` are caught by the heuristic and need not be listed.)
_MCP_READ_TOOLS: frozenset[str] = frozenset(
    {
        "get_file_contents",
        "get_me",
        "pull_request_read",
        "issue_read",
    }
)

# Write tools, including ones whose verb prefix is not obviously "write"
# (``push_files``, ``fork_repository``, ``merge_pull_request`` ARE caught by the
# heuristic; this set is the authoritative list of the common write operations
# so they classify even when the verb heuristic is disabled for non-prefixed
# tools).
_MCP_WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "create_or_update_file",
        "push_files",
        "delete_file",
        "create_repository",
        "fork_repository",
        "create_branch",
        "delete_branch",
        "update_repository",
        "create_pull_request",
        "update_pull_request",
        "merge_pull_request",
        "create_pull_request_review",
        "add_pull_request_review_comment_to_pending_review",
        "request_copilot_review",
        "update_pull_request_branch",
        "create_issue",
        "update_issue",
        "add_issue_comment",
        "create_release",
        "delete_release",
        "create_gist",
        "update_gist",
        "run_workflow",
        "rerun_workflow_run",
        "rerun_failed_jobs",
        "cancel_workflow_run",
        "delete_workflow_run_logs",
        "assign_copilot_to_issue",
    }
)

# MCP write tools that are destructive (irreversible deletes). Gated separately
# by ``allow_destructive`` — denied by default even on allowed repos.
_MCP_DESTRUCTIVE_TOOLS: frozenset[str] = frozenset(
    {
        "delete_file",
        "delete_branch",
        "delete_release",
    }
)

# Verb prefixes used to classify GitHub-prefixed tools we don't list explicitly.
# Only applied when the raw tool name carried a GitHub server prefix (we are
# certain it is a GitHub tool); never used to claim un-prefixed tools, which
# could collide with other services (e.g. a ``create_document`` Google tool).
_READ_VERB_PREFIXES: tuple[str, ...] = ("get_", "list_", "search_", "find_", "fetch_")
_WRITE_VERB_PREFIXES: tuple[str, ...] = (
    "create_",
    "update_",
    "delete_",
    "merge_",
    "push_",
    "add_",
    "remove_",
    "set_",
    "fork_",
    "run_",
    "rerun_",
    "cancel_",
    "assign_",
    "submit_",
    "request_",
    "edit_",
    "close_",
    "reopen_",
    "lock_",
    "unlock_",
    "resolve_",
    "unresolve_",
    "transfer_",
    "rename_",
    "dismiss_",
    "approve_",
    "enable_",
    "disable_",
    "upload_",
    "star_",
    "pin_",
)


# Branch-targeted writes: operations that land content on a specific branch, so
# ``write_branches`` is *enforced* for them even when no branch is named (a
# missing branch means the repo default, which we cannot confirm → fail to the
# no-branch decision). Other writes (issues, comments, PR metadata, merges by
# number) are governed by ``write_repos`` only, but any branch they DO name is
# still checked.
_MCP_BRANCH_WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "create_or_update_file",
        "push_files",
        "delete_file",
        "create_branch",
        "delete_branch",
        "update_pull_request_branch",
    }
)


def _mcp_base(canonical: str) -> str:
    """
    Strip a leading ``github_`` operation prefix from a canonical tool name.

    The Databricks-hosted read-only wrapper prefixes each op with ``github_``
    (e.g. ``github_list_pull_requests``); the official server does not. Stripping
    it lets one set / verb table match both.

    :param canonical: Tool name after server-prefix stripping, e.g.
        ``"github_list_pull_requests"`` or ``"create_pull_request"``.
    :returns: The base operation name (``"list_pull_requests"`` /
        ``"create_pull_request"``).
    """
    return canonical[len("github_") :] if canonical.startswith("github_") else canonical


def _classify_mcp_tool(canonical: str, use_verb_heuristic: bool) -> str:
    """
    Classify a canonical GitHub MCP tool name as read / write / allow / unknown.

    :param canonical: Tool name after server-prefix stripping, e.g.
        ``"create_pull_request"`` or ``"github_read_api_call"``.
    :param use_verb_heuristic: Whether to fall back to verb-prefix matching.
        Only ``True`` when the raw name carried a GitHub server prefix (so we
        know it is a GitHub tool); ``False`` for un-prefixed tools, where the
        heuristic could mis-claim another service's tool.
    :returns: One of ``"read"``, ``"write"``, ``"allow"`` (no-repo metadata
        tool), or ``"unknown"`` (could not classify).
    """
    # Layered HTTP-proxy wrapper splits the verb in the tool name itself.
    if canonical.endswith("read_api_call"):
        return "read"
    if canonical.endswith("write_api_call"):
        return "write"
    base = _mcp_base(canonical)
    if base in _MCP_ALWAYS_ALLOW:
        return "allow"
    if base in _MCP_READ_TOOLS:
        return "read"
    if base in _MCP_WRITE_TOOLS:
        return "write"
    if use_verb_heuristic:
        if base.startswith(_WRITE_VERB_PREFIXES):
            return "write"
        if base.startswith(_READ_VERB_PREFIXES):
            return "read"
    return "unknown"


# ── Shell command classification ─────────────────────────────────────────────

# git subcommands that interact with a remote, by access kind.
_GIT_READ_SUBCMDS: frozenset[str] = frozenset({"clone", "fetch", "pull", "ls-remote"})
_GIT_WRITE_SUBCMDS: frozenset[str] = frozenset({"push"})

# Global options may consume an operand before the subcommand.
_GIT_GLOBAL_VALUE_OPTS: frozenset[str] = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env", "--attr-source"}
)
_GIT_GLOBAL_FLAG_OPTS: frozenset[str] = frozenset(
    {
        "-v",
        "--version",
        "-h",
        "--help",
        "-p",
        "--paginate",
        "-P",
        "--no-pager",
        "--html-path",
        "--man-path",
        "--info-path",
        "--bare",
        "--no-replace-objects",
        "--no-lazy-fetch",
        "--no-optional-locks",
        "--no-advice",
        "--literal-pathspecs",
        "--glob-pathspecs",
        "--noglob-pathspecs",
        "--icase-pathspecs",
    }
)


def _git_subcommand_index(tokens: list[str]) -> int | None:
    """Find the subcommand, rejecting incomplete or unsupported global options."""
    i = 1
    while i < len(tokens):
        token = tokens[i]
        if token == "--exec-path":
            # Without an attached value, Git prints its exec path and exits.
            return None
        if token in _GIT_GLOBAL_VALUE_OPTS:
            if i + 1 == len(tokens):
                raise ValueError("Missing Git global option value")
            i += 2
            continue
        option, separator, _ = token.partition("=")
        if (
            token in _GIT_GLOBAL_FLAG_OPTS
            or (separator and (option in _GIT_GLOBAL_VALUE_OPTS or option == "--exec-path"))
            or (token.startswith(("-C", "-c")) and len(token) > 2)
        ):
            i += 1
            continue
        if token.startswith("-"):
            raise ValueError("Unsupported Git global option")
        return i
    return None


# gh top-level command groups → the set of actions under each that write.
# Any action NOT listed here (and not an unknown group) is treated as a read.
_GH_WRITE_ACTIONS: dict[str, frozenset[str]] = {
    "pr": frozenset(
        {
            "create",
            "merge",
            "close",
            "edit",
            "comment",
            "review",
            "ready",
            "reopen",
            "lock",
            "unlock",
        }
    ),
    "issue": frozenset(
        {
            "create",
            "edit",
            "close",
            "comment",
            "reopen",
            "delete",
            "lock",
            "unlock",
            "transfer",
            "pin",
            "unpin",
        }
    ),
    "repo": frozenset(
        {"create", "delete", "fork", "edit", "rename", "archive", "sync", "set-default"}
    ),
    "release": frozenset({"create", "delete", "edit", "upload"}),
    "run": frozenset({"rerun", "cancel", "delete"}),
    "workflow": frozenset({"run", "enable", "disable"}),
    "secret": frozenset({"set", "delete"}),
    "label": frozenset({"create", "delete", "edit"}),
    "gist": frozenset({"create", "edit", "delete"}),
    "cache": frozenset({"delete"}),
    "codespace": frozenset({"create", "delete", "stop", "rebuild"}),
    "project": frozenset(
        {
            "create",
            "delete",
            "edit",
            "close",
            "mark-template",
            "field-create",
            "field-delete",
            "item-add",
            "item-archive",
            "item-create",
            "item-delete",
            "item-edit",
        }
    ),
    "variable": frozenset({"set", "delete"}),
    "ssh-key": frozenset({"add", "delete"}),
    "gpg-key": frozenset({"add", "delete"}),
}

# gh groups that are GitHub-aware but never touch a specific repo's contents —
# ignore them (auth, local config, etc.).
# gh groups → destructive action names (a subset of _GH_WRITE_ACTIONS). Gated
# separately by ``allow_destructive`` — denied by default even on allowed repos.
_GH_DESTRUCTIVE_ACTIONS: dict[str, frozenset[str]] = {
    "repo": frozenset({"delete"}),
    "release": frozenset({"delete"}),
    "issue": frozenset({"delete"}),
    "gist": frozenset({"delete"}),
    "cache": frozenset({"delete"}),
    "codespace": frozenset({"delete"}),
    "project": frozenset({"delete", "item-delete"}),
    "variable": frozenset({"delete"}),
    "ssh-key": frozenset({"delete"}),
    "gpg-key": frozenset({"delete"}),
    "secret": frozenset({"delete"}),
    "label": frozenset({"delete"}),
    "run": frozenset({"delete"}),
}

_GH_IGNORE_GROUPS: frozenset[str] = frozenset(
    {
        "auth",
        "config",
        "alias",
        "extension",
        "completion",
        "browse",
        "copilot",
        "licenses",
        "search",
        "status",
    }
)


@dataclass(frozen=True)
class _ShellOp:
    """
    One git / gh remote operation parsed from a shell command segment.

    :param kind: Access kind — ``"read"``, ``"write"``, ``"ignore"`` (local /
        non-repo command, no gating), or ``"unparseable"`` (a git/gh command
        that requires approval because its invocation could not be classified).
    :param repo: Normalized ``owner/repo`` target, or ``None`` when the command
        relied on a local remote alias / cwd remote we cannot resolve.
    :param branches: Branch names the write targets (empty when none could be
        determined or the op is a read).
    :param branch_targeted: Whether this op writes content to a specific branch
        (``git push``), so ``write_branches`` is enforced even when no branch
        could be parsed. ``False`` for ops that touch GitHub but not branch
        content (``gh issue create``, ``gh pr merge`` …).
    :param detail: Short description for the decision reason, e.g.
        ``"git push"`` or ``"gh pr create"``.
    :param destructive: Whether the operation is an irreversible delete, gated
        separately by ``allow_destructive``.
    :param tag_push: Whether this ``git push`` includes tags (``--tags``,
        ``--follow-tags``, or ``refs/tags/`` refspecs).
    :param force_push: Whether this ``git push`` uses a force flag
        (``--force``, ``-f``, ``--force-with-lease``, ``--force-if-includes``)
        or a ``+refspec`` force prefix.
    """

    kind: str
    repo: str | None
    branches: frozenset[str]
    branch_targeted: bool
    detail: str
    destructive: bool = False
    tag_push: bool = False
    force_push: bool = False


def _repo_from_tokens(tokens: list[str]) -> str | None:
    """
    Find a determinable repo in a token list (URL, bare owner/repo, api path).

    :param tokens: Argument tokens of a git/gh invocation.
    :returns: Normalized ``owner/repo``, or ``None`` if none is determinable.
    """
    for token in tokens:
        repo = _repo_from_url(token)
        if repo:
            return repo
        api_match = _REPO_API_PATH_PATTERN.search(token)
        if api_match:
            return f"{api_match.group(1)}/{api_match.group(2)}".lower()
    return None


def _gh_api_endpoint(rest: list[str]) -> str | None:
    """Return the endpoint operand, excluding option values such as request fields."""
    value_options = frozenset(
        {
            "-X",
            "--method",
            "-H",
            "--header",
            "-f",
            "--field",
            "-F",
            "--raw-field",
            "--input",
            "-q",
            "--jq",
            "-t",
            "--template",
            "--hostname",
            "-R",
            "--repo",
        }
    )
    flag_only = frozenset(
        {"--paginate", "--slurp", "--silent", "--verbose", "-i", "--include", "--cache"}
    )
    i = 0
    while i < len(rest):
        token = rest[i]
        if token == "--":
            return rest[i + 1] if i + 1 < len(rest) else None
        if token in value_options:
            i += 2
            continue
        if token.startswith("-"):
            if (
                "=" not in token
                and not token.startswith(("-f", "-F", "-H", "-X", "-q", "-t"))
                and token not in flag_only
            ):
                return None
            i += 1
            continue
        return token
    return None


def _repo_from_gh_api_endpoint(endpoint: str | None) -> str | None:
    """Return the repository named by a REST endpoint on github.com, or ``None``."""
    if endpoint is None:
        return None
    if "://" in endpoint:
        try:
            parsed = urlsplit(endpoint)
        except ValueError:
            return None
        if parsed.scheme.lower() != "https" or parsed.hostname != "api.github.com":
            return None
        path = parsed.path.lstrip("/")
    else:
        path = endpoint.lstrip("/")
    if "%" in path or any(segment in {".", ".."} for segment in path.split("/")):
        return None
    match = _REPO_API_PATH_PATTERN.match(path)
    return f"{match.group(1)}/{match.group(2)}".lower() if match else None


_GIT_PUSH_VALUE_OPTIONS = frozenset(
    {"--repo", "--receive-pack", "--exec", "--push-option", "--recurse-submodules"}
)
_GIT_PUSH_BOOLEAN_OPTIONS = frozenset(
    {
        "--all",
        "--branches",
        "--mirror",
        "--delete",
        "--no-recurse-submodules",
        "--tags",
        "--dry-run",
        "--porcelain",
        "--force",
        "--force-with-lease",
        "--force-if-includes",
        "--thin",
        "--set-upstream",
        "--progress",
        "--prune",
        "--follow-tags",
        "--signed",
        "--atomic",
        "--verbose",
        "--quiet",
        "--verify",
        "--ipv4",
        "--ipv6",
    }
)
_GIT_PUSH_SHORT_OPTIONS = frozenset({"v", "q", "d", "n", "f", "u", "4", "6"})


def _parse_git_push_args(args: list[str]) -> tuple[str | None, list[str], set[str]] | None:
    """Isolate the push remote and refspecs without treating option values as targets."""
    positionals: list[str] = []
    flags: set[str] = set()
    repo_option: str | None = None
    options_done = False
    i = 0
    while i < len(args):
        token = args[i]
        i += 1
        if options_done or not token.startswith("-") or token == "-":
            positionals.append(token)
            continue
        if token == "--":
            options_done = True
            continue
        if token.startswith("--"):
            name, equal, attached = token.partition("=")
            if name in _GIT_PUSH_VALUE_OPTIONS:
                if not equal:
                    if i == len(args):
                        return None
                    attached = args[i]
                    i += 1
                if name == "--repo":
                    if repo_option is not None:
                        return None
                    repo_option = attached
                flags.add(name)
            elif name in _GIT_PUSH_BOOLEAN_OPTIONS or (
                name.startswith("--no-") and "--" + name[5:] in _GIT_PUSH_BOOLEAN_OPTIONS
            ):
                if equal and name not in {"--force-with-lease", "--signed"}:
                    return None
                flags.add(name)
            else:
                return None
            continue
        for offset, option in enumerate(token[1:]):
            if option not in _GIT_PUSH_SHORT_OPTIONS and option != "o":
                return None
            flags.add("-" + option)
            if option == "o":
                if offset == len(token) - 2:
                    if i == len(args):
                        return None
                    i += 1
                break
    if repo_option is not None:
        # The interaction of --repo and positional arguments is ambiguous here.
        if positionals:
            return None
        return repo_option, [], flags
    return (positionals[0] if positionals else None), positionals[1:], flags


def _unparsed_git_push(args: list[str]) -> _ShellOp:
    """Preserve destructive push indicators when an option obscures the remote."""
    is_force = any(
        token in {"--force", "--force-with-lease", "--force-if-includes"}
        or token.startswith(("--force-with-lease=", "--force-if-includes=", "+"))
        or (token.startswith("-") and not token.startswith("--") and "f" in token[1:])
        for token in args
    )
    is_destructive = any(
        token in {"--delete", "--mirror", "--prune"}
        or (token.startswith("-") and not token.startswith("--") and "d" in token[1:])
        or token.startswith(":")
        for token in args
    )
    tag_push = any(
        token in {"--tags", "--follow-tags", "--mirror"}
        or token.startswith("refs/tags/")
        or ":refs/tags/" in token
        for token in args
    )
    return _ShellOp(
        kind="write",
        repo=None,
        branches=frozenset(),
        branch_targeted=True,
        detail="git push with unsupported options",
        destructive=is_destructive,
        tag_push=tag_push,
        force_push=is_force or "--mirror" in args,
    )


def _flag_value(tokens: list[str], names: frozenset[str]) -> str | None:
    """
    Return the value of the first matching ``--flag value`` / ``--flag=value``.

    :param tokens: Argument tokens.
    :param names: Flag spellings to match, e.g. ``{"--repo", "-R"}``.
    :returns: The flag's value, or ``None`` when the flag is absent.
    """
    for i, token in enumerate(tokens):
        for name in names:
            if token == name and i + 1 < len(tokens):
                return tokens[i + 1]
            if token.startswith(name + "="):
                return token[len(name) + 1 :]
    return None


def _classify_git(tokens: list[str]) -> _ShellOp | None:
    """
    Classify a ``git`` invocation into a remote :class:`_ShellOp`.

    :param tokens: Tokens starting at ``git``, e.g. ``["git", "push", "origin", "main"]``.
    :returns: A :class:`_ShellOp` for remote read/write subcommands or an
        unparseable invocation; ``None`` for local-only commands with
        recognized global options (status / commit / diff / branch / …).
    """
    if len(tokens) < 2:
        return None
    try:
        idx = _git_subcommand_index(tokens)
    except ValueError:
        return _ShellOp(
            kind="unparseable",
            repo=None,
            branches=frozenset(),
            branch_targeted=False,
            detail="incomplete or unsupported Git global options",
        )
    if idx is None:
        return None
    sub = tokens[idx]
    args = tokens[idx + 1 :]
    if sub in _GIT_READ_SUBCMDS:
        return _ShellOp(
            kind="read",
            repo=_repo_from_tokens(args),
            branches=frozenset(),
            branch_targeted=False,
            detail=f"git {sub}",
        )
    if sub in _GIT_WRITE_SUBCMDS:
        # ``git push [flags] [<remote>] [<refspec>...]`` — the remote is the first
        # positional after option parsing (a URL we can resolve, or an alias we
        # cannot); the rest are refspecs whose right-hand side is the destination.
        parsed = _parse_git_push_args(args)
        if parsed is None:
            return _unparsed_git_push(args)
        remote, refspecs, flags = parsed
        repo = _repo_from_url(remote, allow_subpath=False) if remote else None
        branches: set[str] = set()
        tag_push = bool(flags & {"--tags", "--follow-tags", "--mirror"})
        for refspec in refspecs:
            dest = refspec.split(":", 1)[1] if ":" in refspec else refspec
            dest = dest.lstrip("+")
            if dest.startswith("refs/tags/"):
                tag_push = True
                continue
            branch = _normalize_branch(dest)
            if branch:
                branches.add(branch)
        # Detect delete semantics: ``--delete`` / ``-d`` flag, or a refspec
        # starting with ``:`` (empty left side = delete the remote branch).
        is_destructive = bool(flags & {"--delete", "-d", "--mirror", "--prune"}) or any(
            refspec.startswith(":") for refspec in refspecs
        )
        # Detect force-push from parsed flags and ``+refspec`` prefixes.
        is_force = bool(
            flags & {"--force", "-f", "--force-with-lease", "--force-if-includes", "--mirror"}
        ) or any(refspec.startswith("+") for refspec in refspecs)
        if flags & {"--all", "--branches", "--mirror", "--prune"}:
            # These modes may affect refs beyond any explicit refspec.
            branches.clear()
        return _ShellOp(
            kind="write",
            repo=repo,
            branches=frozenset(branches),
            branch_targeted=True,
            detail="git push",
            destructive=is_destructive,
            tag_push=tag_push,
            force_push=is_force,
        )
    return None


def _classify_gh(tokens: list[str]) -> _ShellOp | None:
    """
    Classify a ``gh`` invocation into a :class:`_ShellOp`.

    :param tokens: Tokens starting at ``gh``, e.g.
        ``["gh", "pr", "create", "--repo", "o/r", "--base", "main"]``.
    :returns: A :class:`_ShellOp`, or ``None`` for ignored gh groups (auth /
        config) and bare ``gh`` with no subcommand.
    """
    i = 1
    global_repo = None
    while i < len(tokens) and tokens[i].startswith("-"):
        option = tokens[i]
        if option in {"--help", "-h", "--version"}:
            return None
        if option in {"-R", "--repo"} and i + 1 < len(tokens):
            global_repo = tokens[i + 1]
            i += 2
        elif option.startswith(("-R=", "--repo=")):
            global_repo = option.split("=", 1)[1]
            i += 1
        else:
            return _ShellOp(
                kind="unparseable",
                repo=None,
                branches=frozenset(),
                branch_targeted=False,
                detail="unsupported gh global option",
            )
    if i >= len(tokens):
        return None
    group = tokens[i]
    if group in _GH_IGNORE_GROUPS:
        return None
    rest = tokens[i + 1 :]

    explicit_repo = _flag_value(rest, frozenset({"--repo", "-R"})) or global_repo
    repo = _normalize_repo(explicit_repo) if explicit_repo is not None else None
    # Only ``--base`` is a write destination; ``--head`` is the PR's source branch.
    base = _flag_value(rest, frozenset({"--base", "-B"}))
    branches = _normalize_branches([base] if base else None)

    if group == "api":
        return _classify_gh_api(rest)

    action = rest[0] if rest and not rest[0].startswith("-") else ""
    if explicit_repo is None and len(rest) > 1 and not rest[1].startswith("-"):
        # Only the first positional operand can designate a target. Body text,
        # file paths and other options may contain unrelated GitHub URLs.
        if group == "repo" and action in {"create", "delete", "fork", "edit", "archive", "sync"}:
            repo = _normalize_repo(rest[1]) or None
        elif group in {"pr", "issue"} and action not in {"", "create"}:
            repo = _repo_from_url(rest[1]) or None
    is_write = action in _GH_WRITE_ACTIONS.get(group, frozenset())
    detail = f"gh {group} {action}".strip()
    if is_write:
        # gh writes are not inherently branch-targeted (they act on PRs/issues/
        # repos by number/name); a branch named via --base is still checked.
        is_destructive = action in _GH_DESTRUCTIVE_ACTIONS.get(group, frozenset())
        return _ShellOp(
            kind="write",
            repo=repo,
            branches=frozenset(branches),
            branch_targeted=False,
            detail=detail,
            destructive=is_destructive,
        )
    # Known read group, or an unknown group treated conservatively as a read
    # (reads are the safer default — they are only gated when read_all is off).
    return _ShellOp(
        kind="read", repo=repo, branches=frozenset(), branch_targeted=False, detail=detail
    )


def _classify_gh_api(rest: list[str]) -> _ShellOp:
    """
    Classify a ``gh api`` call (REST method decides read vs write).

    ``gh api`` defaults to GET (read); an explicit write method
    (``-X POST`` / ``--method PATCH`` / …) or the presence of field flags
    (``-f`` / ``--field`` / ``--raw-field`` / ``--input``, which make gh default
    to POST) marks it a write. The repository comes only from the endpoint operand.

    :param rest: Tokens after ``gh api``, e.g. ``["repos/o/r/pulls", "-X", "POST"]``.
    :returns: A read or write :class:`_ShellOp` for the api call.
    """
    methods: list[str] = []
    for i, token in enumerate(rest):
        if token in {"-X", "--method"}:
            methods.append(rest[i + 1].upper() if i + 1 < len(rest) else "")
        elif token.startswith(("-X=", "--method=")):
            methods.append(token.split("=", 1)[1].upper())
        elif token.startswith("-X") and len(token) > 2:
            methods.append(token[2:].upper())
    if any(method not in {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"} for method in methods):
        return _ShellOp(
            kind="unparseable",
            repo=None,
            branches=frozenset(),
            branch_targeted=False,
            detail="unsupported gh api method",
        )
    has_fields = any(
        t in {"-f", "--field", "-F", "--raw-field", "--input"}
        or t.startswith(("--field=", "--raw-field=", "--input="))
        or (t.startswith(("-f", "-F")) and "=" in t)
        for t in rest
    )
    write_methods = {"POST", "PUT", "PATCH", "DELETE"}
    is_write = any(method in write_methods for method in methods) or has_fields
    hostname = _flag_value(rest, frozenset({"--hostname"}))
    api_repo = (
        _repo_from_gh_api_endpoint(_gh_api_endpoint(rest))
        if hostname is None or hostname.lower() in {"github.com", "api.github.com"}
        else None
    )
    return _ShellOp(
        kind="write" if is_write else "read",
        repo=api_repo,
        branches=frozenset(),
        branch_targeted=False,
        detail="gh api",
        destructive="DELETE" in methods,
    )


def _classify_shell_command(command: str, _depth: int = 0) -> list[_ShellOp]:
    """
    Parse a shell command string into the git / gh remote ops it performs.

    Commands wrapped in a shell interpreter (``bash -c "git push …"``) or
    ``eval`` are unwrapped and their inner command parsed recursively, so the
    wrapper cannot be used to slip a git/gh write past the gate.

    :param command: The ``sys_os_shell`` command argument, e.g.
        ``"git add . && git push origin main"``.
    :param _depth: Internal recursion guard for nested shell wrappers; callers
        leave it 0.
    :returns: One :class:`_ShellOp` per git/gh remote invocation found. Local
        git commands and non-git/gh segments produce no op. A segment that
        mentions git/gh but whose real command cannot be resolved — it does not
        tokenize, or a wrapper's own flags left an option as the head — yields
        an ``"unparseable"`` op so the caller can ASK rather than silently allow.
    """
    if _depth > MAX_SHELL_NESTING:
        return [
            _ShellOp(
                kind="unparseable",
                repo=None,
                branches=frozenset(),
                branch_targeted=False,
                detail="shell command exceeds the nesting limit",
            )
        ]
    ops: list[_ShellOp] = []
    try:
        segments = split_command_segments(command)
    except UnsupportedShellSyntax as exc:
        segments = exc.completed_segments
        ops.append(
            _ShellOp(
                kind="unparseable",
                repo=None,
                branches=frozenset(),
                branch_targeted=False,
                detail=str(exc),
            )
        )
    for segment in segments:
        unreadable = False
        try:
            tokens = shlex.split(segment)
        except ValueError:
            # Unbalanced quotes etc. If it looks like a git/gh command we can't
            # read, surface it for approval instead of guessing.
            unreadable = True
            tokens = []
        else:
            tokens = real_invocation_tokens(tokens)
            # A leading option means some wrapper's own flags were not modelled,
            # so the real command was never reached. Same fail-safe as an
            # un-tokenizable segment rather than a silent abstain. A
            # process-spawning utility (find -exec, xargs, perl -e, python -c,
            # awk, make) likewise runs the gated command as a child whose argv is
            # not modelled here, so route it through the same fail-safe.
            unreadable = is_unresolved_invocation(tokens) or spawns_gated_command_as_child(tokens)
        if unreadable:
            if re.search(r"\b(git|gh)\b", segment):
                ops.append(
                    _ShellOp(
                        kind="unparseable",
                        repo=None,
                        branches=frozenset(),
                        branch_targeted=False,
                        detail=segment[:60],
                    )
                )
            continue
        if not tokens:
            continue
        # Unwrap shell-interpreter (``bash -c "<cmd>"``) and ``eval`` wrappers so
        # the inner command is gated rather than passing through unrecognized.
        inner = unwrap_shell_command(tokens)
        if inner is not None:
            ops.extend(_classify_shell_command(inner, _depth + 1))
            continue
        # A shell interpreter with no ``-c`` reads its program from stdin or a
        # script file (``printf '…' | sh``, ``sh script.sh``), which this parser
        # cannot see. The gated git/gh could be in that opaque program — for
        # ``… | sh`` the payload is even in a different segment — so its absence
        # from this segment's tokens is not safe evidence. Surface it for
        # approval rather than abstain → ALLOW.
        if is_shell_interpreter(tokens):
            ops.append(
                _ShellOp(
                    kind="unparseable",
                    repo=None,
                    branches=frozenset(),
                    branch_targeted=False,
                    detail=segment[:60],
                )
            )
            continue
        if tokens[0] == "git":
            op = _classify_git(tokens)
        elif tokens[0] == "gh":
            op = _classify_gh(tokens)
        else:
            op = None
        if op is not None:
            ops.append(op)
    return ops


# ── The policy factory ─────────────────────────────────────────────────────────


def github_policy(
    *,
    read_all: bool = True,
    read_repos: list[str] | None = None,
    write_repos: list[str] | None = None,
    write_branches: list[str] | None = None,
    allow_destructive: bool = False,
    deny_tag_push: bool = True,
    deny_force_push: bool = True,
    mcp_tool_prefixes: list[str] | None = None,
    shell_tools: list[str] | None = None,
    deny_reason: str = "GitHub operation blocked by policy.",
) -> Callable[[PolicyEvent], PolicyResponse | None]:
    """
    Build a GitHub access policy callable covering MCP and git/gh shell surfaces.

    :param read_all: When ``True`` (default), all reads are allowed. When
        ``False``, reads are restricted to ``read_repos`` and a read whose
        target repo cannot be determined is denied (MCP) or asked (shell).
    :param read_repos: Repos readable when ``read_all`` is ``False``, as
        ``owner/repo`` or GitHub URLs, e.g. ``["octo/hello"]``. ``None`` → none.
    :param write_repos: Repos the agent may write to, as ``owner/repo`` or URLs.
        ``None`` → no repo is writable.
    :param write_branches: Branches writable within an allowed repo, e.g.
        ``["main", "develop"]``. ``None`` / empty means branches are not
        restricted (any branch on an allowed repo is writable).
    :param allow_destructive: When ``False`` (default), irreversible destructive
        operations (deletes) are denied even on allowed repos. Set to ``True``
        to let destructive operations through normal write gating.
    :param deny_tag_push: When ``True`` (default), pushing tags to remotes via
        ``git push --tags``, ``git push --follow-tags``, or explicit
        ``refs/tags/`` refspecs is denied. Tags are immutable references that
        downstream CI/CD and release tooling depend on; an agent pushing a tag
        can trigger releases, deployments, or break semver expectations.
    :param deny_force_push: When ``True`` (default), ``git push`` with force
        flags (``--force``, ``-f``, ``--force-with-lease``,
        ``--force-if-includes``), bundled short flags containing ``f``
        (e.g. ``-uf``), and ``+refspec`` force prefixes are denied regardless
        of repo/branch allowlists. Set to ``False`` to allow force pushes
        through normal repo/branch gating.
    :param mcp_tool_prefixes: GitHub MCP server name-prefixes to strip when
        canonicalizing MCP tool names. ``None`` uses the standard
        ``mcp__github__`` / ``github__``.
    :param shell_tools: Names of the shell / terminal tools whose ``command``
        argument is parsed for ``git`` / ``gh`` invocations. ``None`` uses every
        harness's shell tool (:data:`~omnigent.policies.builtins._shell.SHELL_TOOLS`
        — Omnigent, Claude/Codex, Cursor, Pi, Hermes, Goose). Override this only
        if the agent exposes shell access through a differently-named tool (e.g.
        a custom terminal); list every such tool, since git/gh run through any
        tool not listed here are not inspected by this policy.
    :param deny_reason: Reason prefix attached to DENY decisions.
    :returns: A one-argument policy callable returning a :class:`PolicyResponse`
        or ``None`` (abstain → ALLOW).
    """
    allowed_read_repos = _normalize_repos(read_repos)
    allowed_write_repos = _normalize_repos(write_repos)
    allowed_write_branches = _normalize_branches(write_branches)
    prefixes = (
        tuple(mcp_tool_prefixes) if mcp_tool_prefixes is not None else _DEFAULT_TOOL_PREFIXES
    )
    shell_tool_names = (
        frozenset(shell_tools) if shell_tools is not None else frozenset(_DEFAULT_SHELL_TOOLS)
    )

    def _gate_read_repo(
        repos: set[str], *, undeterminable: PolicyResponse
    ) -> PolicyResponse | None:
        """
        Apply the read allowlist to a set of target repos.

        :param repos: Target repos extracted from the call (may be empty).
        :param undeterminable: Decision to return when *repos* is empty under
            restricted-read mode (DENY for MCP, ASK for shell).
        :returns: ``None`` to allow, or a DENY / *undeterminable* decision.
        """
        if read_all:
            return None
        if not repos:
            return undeterminable
        if repos <= allowed_read_repos:
            return None
        return _deny(
            f"{deny_reason} Read is restricted to the configured repos; "
            f"this call targets {sorted(repos)}."
        )

    def _gate_write(
        repos: set[str],
        branches: set[str],
        *,
        branch_targeted: bool,
        no_repo: PolicyResponse,
        no_branch: PolicyResponse,
    ) -> PolicyResponse | None:
        """
        Apply the write repo + branch allowlists to a write operation.

        Any branch the call names is checked against ``write_branches``. A
        *branch-targeted* write (file write, push, branch create) with no
        determinable branch additionally fails to *no_branch*, because it would
        otherwise land on the repo's default branch unchecked. A write that is
        not branch-targeted (issue, comment, PR merge by number) is governed by
        ``write_repos`` alone when it names no branch.

        :param repos: Target repos extracted from the call (may be empty).
        :param branches: Target branches extracted from the call (may be empty).
        :param branch_targeted: Whether the op writes content to a branch.
        :param no_repo: Decision when no repo could be determined (DENY for MCP,
            ASK for shell).
        :param no_branch: Decision for a branch-targeted write whose branch could
            not be determined while branches are restricted (DENY for MCP, ASK
            for shell).
        :returns: ``None`` to allow, or a DENY / *no_repo* / *no_branch* decision.
        """
        if not repos:
            return no_repo
        if not (repos <= allowed_write_repos):
            return _deny(
                f"{deny_reason} Write is restricted to the configured repos; "
                f"this call targets {sorted(repos)}."
            )
        if allowed_write_branches:
            if branches:
                bad = branches - allowed_write_branches
                if bad:
                    return _deny(
                        f"{deny_reason} Write is restricted to branches "
                        f"{sorted(allowed_write_branches)}; this call targets {sorted(bad)}."
                    )
            elif branch_targeted:
                return no_branch
        return None

    def _evaluate_mcp(raw_tool: str, args: dict[str, Any]) -> PolicyResponse | None:  # type: ignore[explicit-any]
        """
        Evaluate a GitHub MCP ``tool_call`` against the allowlists.

        :param raw_tool: Raw tool name, e.g. ``"mcp__github__create_pull_request"``.
        :param args: The tool's ``arguments`` dict.
        :returns: A :class:`PolicyResponse`, or ``None`` to abstain (not a
            GitHub tool, or an allowed operation).
        """
        canonical = _canonical_tool_name(raw_tool, prefixes)
        had_github_prefix = canonical != raw_tool
        cls = _classify_mcp_tool(canonical, use_verb_heuristic=had_github_prefix)

        if cls == "unknown":
            if had_github_prefix:
                # A GitHub-prefixed tool we cannot classify: fail closed rather
                # than let an unknown GitHub operation bypass the policy.
                return _deny(f"{deny_reason} Unrecognized GitHub tool {raw_tool!r}.")
            return None
        if cls == "allow":
            return None

        repos = _extract_repos_from_args(args)
        if cls == "read":
            return _gate_read_repo(
                repos,
                undeterminable=_deny(
                    f"{deny_reason} Read is restricted to the configured repos and this "
                    f"call's target repo could not be determined."
                ),
            )
        # cls == "write"
        branches = _extract_branches_from_args(args)
        write_result = _gate_write(
            repos,
            branches,
            branch_targeted=_mcp_base(canonical) in _MCP_BRANCH_WRITE_TOOLS,
            no_repo=_deny(f"{deny_reason} Write call carries no identifiable target repo."),
            no_branch=_deny(
                f"{deny_reason} Write is restricted to branches "
                f"{sorted(allowed_write_branches)} and this call's target branch could "
                f"not be determined."
            ),
        )
        if write_result is not None:
            return write_result
        # Destructive check fires after the repo allowlist so a destructive op
        # on a non-allowed repo still gets the repo DENY, not this one.
        if not allow_destructive and _mcp_base(canonical) in _MCP_DESTRUCTIVE_TOOLS:
            return _deny(
                f"{deny_reason} Destructive operation {raw_tool!r} is blocked by default. "
                f"Set allow_destructive=true to permit deletes."
            )
        return None

    def _evaluate_shell(command: str) -> PolicyResponse | None:
        """
        Evaluate a ``sys_os_shell`` command's git/gh ops against the allowlists.

        Returns the most restrictive decision across every remote op in the
        command (DENY beats ASK beats ALLOW), so ``git add . && git push`` is
        gated on the push.

        :param command: The shell command string.
        :returns: A :class:`PolicyResponse`, or ``None`` to abstain (no gated
            git/gh op, or all ops allowed).
        """
        decision: PolicyResponse | None = None
        for op in _classify_shell_command(command):
            decision = _worse(decision, _gate_shell_op(op))
        return decision

    def _gate_shell_op(op: _ShellOp) -> PolicyResponse | None:
        """
        Apply the allowlists to a single parsed shell op.

        :param op: The parsed git/gh remote operation.
        :returns: ``None`` to allow, or a DENY / ASK decision.
        """
        if op.kind == "unparseable":
            return _ask(
                f"Could not parse a git/gh command ({op.detail!r}) to check it against "
                f"the GitHub policy. Approve to run it?"
            )
        if op.kind == "read":
            return _gate_read_repo(
                {op.repo} if op.repo else set(),
                undeterminable=_ask(
                    f"Reads are restricted to the configured repos, but the target repo "
                    f"of `{op.detail}` could not be determined. Approve?"
                ),
            )
        if op.kind == "write" and op.force_push and deny_force_push:
            return _deny(
                "Force push is blocked by policy. Remove the force flag "
                "(--force / -f / --force-with-lease / --force-if-includes / "
                "+refspec) or set deny_force_push=False."
            )
        if op.kind == "write":
            if op.destructive and not allow_destructive:
                return _deny(
                    f"{deny_reason} Destructive operation `{op.detail}` is blocked by "
                    f"default. Set allow_destructive=true to permit deletes."
                )
            if deny_tag_push and op.tag_push:
                return _deny(
                    f"{deny_reason} Pushing tags is blocked by policy (deny_tag_push is enabled)."
                )
            return _gate_write(
                {op.repo} if op.repo else set(),
                set(op.branches),
                branch_targeted=op.branch_targeted,
                no_repo=_ask(
                    f"The target repo of `{op.detail}` could not be determined "
                    f"(e.g. a local remote alias). Approve this write?"
                ),
                no_branch=_ask(
                    f"`{op.detail}` writes to an allowed repo but its target branch could "
                    f"not be determined, and writes are branch-restricted. Approve?"
                ),
            )
        return None

    def _evaluate(event: PolicyEvent) -> PolicyResponse | None:
        """
        Evaluate one policy event against the GitHub access rules.

        Acts on ``tool_call`` events only: shell tools are routed to command
        parsing, GitHub MCP tools to structured allowlist gating, everything
        else is abstained from.

        :param event: The policy event.
        :returns: A :class:`PolicyResponse`, or ``None`` to abstain.
        """
        if event.get("type") != "tool_call":
            return None
        data = event.get("data")
        if not isinstance(data, dict):
            return None
        raw_tool = data.get("name")
        if not isinstance(raw_tool, str):
            return None
        args = data.get("arguments")
        args = args if isinstance(args, dict) else {}

        if raw_tool in shell_tool_names:
            command = args.get("command")
            if not isinstance(command, str) or not command.strip():
                return None
            return _evaluate_shell(command)

        return _evaluate_mcp(raw_tool, args)

    return _evaluate


def _worse(
    current: PolicyResponse | None, candidate: PolicyResponse | None
) -> PolicyResponse | None:
    """
    Return the more restrictive of two decisions (DENY > ASK > ALLOW).

    :param current: The decision accumulated so far (``None`` = ALLOW/abstain).
    :param candidate: A new decision to fold in (``None`` = ALLOW/abstain).
    :returns: Whichever decision is more restrictive; ``current`` on a tie.
    """
    rank = {"DENY": 3, "ASK": 2}
    current_rank = rank.get(current["result"], 1) if current else 1
    candidate_rank = rank.get(candidate["result"], 1) if candidate else 1
    return candidate if candidate_rank > current_rank else current


# ── Registry ───────────────────────────────────────────────────────────────────

POLICY_REGISTRY: list[dict[str, Any]] = [  # type: ignore[explicit-any]
    {
        "handler": "omnigent.policies.builtins.github.github_policy",
        "kind": "factory",
        "name": "GitHub Repo & Branch Access",
        "description": (
            "Controls GitHub access across MCP tools (official per-operation server and the "
            "github_read_api_call / github_write_api_call HTTP-proxy wrapper) and git/gh shell "
            "commands. Supports Omnigent, Claude/Codex, Cursor, Pi, Hermes, and Goose shell "
            "tools. Restricts reads to read_repos (unless read_all), and "
            "writes to write_repos plus optional write_branches. Shell commands whose target repo "
            "or branch cannot be determined return ASK for human approval."
        ),
        "params_schema": {
            "type": "object",
            "properties": {
                "read_all": {
                    "type": "boolean",
                    "description": "Allow all reads. When false, restrict reads to read_repos.",
                    "default": True,
                },
                "read_repos": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Repos (owner/repo or URLs) readable when read_all is false.",
                },
                "write_repos": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Repos (owner/repo or GitHub URLs) the agent may write to.",
                },
                "write_branches": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Branches writable within an allowed repo. Empty = any branch.",
                },
                "allow_destructive": {
                    "type": "boolean",
                    "description": "Allow irreversible destructive operations (deletes). "
                    "When false (default), deletes are denied even on allowed repos.",
                    "default": False,
                },
                "deny_tag_push": {
                    "type": "boolean",
                    "description": "Block pushing tags to remotes (--tags, --follow-tags, "
                    "refs/tags/ refspecs). Tags are immutable references that downstream "
                    "CI/CD depends on.",
                    "default": True,
                },
                "deny_force_push": {
                    "type": "boolean",
                    "description": "Deny git push with force flags (--force, -f, "
                    "--force-with-lease, --force-if-includes), bundled short flags "
                    "(-uf), and +refspec force prefixes. Default true.",
                    "default": True,
                },
                "mcp_tool_prefixes": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "GitHub MCP server name-prefixes to strip when matching "
                    "tools (default: mcp__github__, github__).",
                },
                "shell_tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Shell/terminal tools whose command arg is parsed for "
                    "git/gh; git/gh run through tools not listed here are not "
                    "inspected (default: every harness's shell tool).",
                },
            },
        },
    },
]
