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

from omnigent.policies.builtins._shell import (
    MAX_SHELL_NESTING,
    real_invocation_tokens,
    split_command_segments,
    unwrap_shell_command,
)
from omnigent.policies.schema import PolicyEvent, PolicyResponse

# ── Shared constants ──────────────────────────────────────────────────────────

# Tool-name prefixes stripped to obtain the canonical tool name. Longest-first
# so ``mcp__github__`` wins over a bare ``github__``.
_DEFAULT_TOOL_PREFIXES: tuple[str, ...] = ("mcp__github__", "github__")

# Shell tools whose command string this policy parses for git / gh invocations.
_DEFAULT_SHELL_TOOLS: tuple[str, ...] = ("sys_os_shell",)

# Pulls ``owner/repo`` out of any GitHub URL (HTTPS, SSH, scp-style). The
# ``[:/]+`` after the host matches both ``github.com/owner`` and the scp-style
# ``github.com:owner``. A trailing ``.git`` and any path past the repo (``/pull/1``,
# ``?tab=x``) are dropped. The ``(?<![A-Za-z0-9._-])`` guard anchors the host to a
# DNS label boundary so a look-alike like ``notgithub.com`` / ``mygithub.com``
# (alnum prefix) OR ``evil-github.com`` / ``evil_github.com`` (hyphen / underscore
# prefix — both legal DNS-label characters) does NOT match and leak a foreign repo
# into the allowlist check. The class MUST include ``-`` and ``_``: omitting them
# let an attacker host whose name merely contains ``github.com`` after a label
# separator be parsed as the real ``github.com`` and inherit an allowed repo's
# verdict, so a ``git push`` to ``evil-github.com/<allowed-owner>/<allowed-repo>``
# would exfiltrate to the attacker host while the policy returned ALLOW.
_REPO_URL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9._-])github\.com[:/]+([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+?)(?:\.git)?(?:[/?#]|$)"
)

# Bare ``owner/repo`` with nothing else around it (no scheme, no host, no extra
# path). Used for allowlist entries and ``--repo owner/repo`` style args.
_REPO_BARE_PATTERN = re.compile(r"^([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+?)(?:\.git)?$")

# Pulls ``owner/repo`` out of a ``gh api`` REST path like ``repos/owner/repo/pulls``.
_REPO_API_PATH_PATTERN = re.compile(r"(?:^|/)repos/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)")

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
    url_match = _REPO_URL_PATTERN.search(value)
    if url_match:
        return f"{url_match.group(1)}/{url_match.group(2)}".lower()
    bare_match = _REPO_BARE_PATTERN.match(value)
    if bare_match:
        return f"{bare_match.group(1)}/{bare_match.group(2)}".lower()
    return ""


def _repo_from_url(value: str) -> str:
    """
    Extract ``owner/repo`` from a GitHub URL/SCP ref, host-checked.

    Unlike :func:`_normalize_repo`, this never falls back to treating a bare
    ``a/b`` string as a repo — it matches only when the value actually
    references the ``github.com`` host. Host validation is done by
    ``_REPO_URL_PATTERN``'s negative lookbehind, which rejects look-alike
    hosts such as ``evil-github.com`` (a plain ``"github.com" in value``
    substring test would not). Use this when scanning arbitrary argument
    values or command tokens, where a bare ``a/b`` could be unrelated text.

    :param value: A value that may contain a GitHub URL, e.g.
        ``"https://github.com/Octo/Repo/pull/1"`` or
        ``"git@github.com:Octo/Repo.git"``.
    :returns: Lowercased ``owner/repo``, or empty string if the value does
        not reference a ``github.com`` repo.
    """
    url_match = _REPO_URL_PATTERN.search(value)
    if url_match:
        return f"{url_match.group(1)}/{url_match.group(2)}".lower()
    return ""


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
}

# gh groups that are GitHub-aware but never touch a specific repo's contents —
# ignore them (auth, local config, etc.).
_GH_IGNORE_GROUPS: frozenset[str] = frozenset(
    {"auth", "config", "alias", "extension", "completion"}
)


@dataclass(frozen=True)
class _ShellOp:
    """
    One git / gh remote operation parsed from a shell command segment.

    :param kind: Access kind — ``"read"``, ``"write"``, ``"ignore"`` (local /
        non-repo command, no gating), or ``"unparseable"`` (a git/gh write we
        could not tokenize — fail to ASK).
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
    """

    kind: str
    repo: str | None
    branches: frozenset[str]
    branch_targeted: bool
    detail: str


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
    :returns: A :class:`_ShellOp` for remote read/write subcommands, or ``None``
        for local-only git commands (status / commit / diff / branch / …),
        which are never gated.
    """
    if len(tokens) < 2:
        return None
    sub = tokens[1]
    args = tokens[2:]
    if sub in _GIT_READ_SUBCMDS:
        return _ShellOp(
            kind="read",
            repo=_repo_from_tokens(args),
            branches=frozenset(),
            branch_targeted=False,
            detail=f"git {sub}",
        )
    if sub in _GIT_WRITE_SUBCMDS:
        # ``git push [flags] [<remote>] [<refspec>...]`` — first non-flag token
        # is the remote (a URL we can resolve, or an alias we cannot); the rest
        # are refspecs whose right-hand side is the destination branch.
        positionals = [t for t in args if not t.startswith("-")]
        repo = _repo_from_tokens(args)
        branches: set[str] = set()
        for refspec in positionals[1:]:
            dest = refspec.split(":", 1)[1] if ":" in refspec else refspec
            branch = _normalize_branch(dest)
            if branch:
                branches.add(branch)
        return _ShellOp(
            kind="write",
            repo=repo,
            branches=frozenset(branches),
            branch_targeted=True,
            detail="git push",
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
    if len(tokens) < 2:
        return None
    group = tokens[1]
    if group in _GH_IGNORE_GROUPS:
        return None
    rest = tokens[2:]

    repo = _flag_value(rest, frozenset({"--repo", "-R"})) or _repo_from_tokens(rest)
    repo = _normalize_repo(repo) if repo else None
    # Only ``--base`` is a write destination; ``--head`` is the PR's source branch.
    base = _flag_value(rest, frozenset({"--base", "-B"}))
    branches = _normalize_branches([base] if base else None)

    if group == "api":
        return _classify_gh_api(rest, repo)

    action = rest[0] if rest and not rest[0].startswith("-") else ""
    is_write = action in _GH_WRITE_ACTIONS.get(group, frozenset())
    detail = f"gh {group} {action}".strip()
    if is_write:
        # gh writes are not inherently branch-targeted (they act on PRs/issues/
        # repos by number/name); a branch named via --base is still checked.
        return _ShellOp(
            kind="write", repo=repo, branches=branches, branch_targeted=False, detail=detail
        )
    # Known read group, or an unknown group treated conservatively as a read
    # (reads are the safer default — they are only gated when read_all is off).
    return _ShellOp(
        kind="read", repo=repo, branches=frozenset(), branch_targeted=False, detail=detail
    )


def _classify_gh_api(rest: list[str], repo: str | None) -> _ShellOp:
    """
    Classify a ``gh api`` call (REST method decides read vs write).

    ``gh api`` defaults to GET (read); an explicit write method
    (``-X POST`` / ``--method PATCH`` / …) or the presence of field flags
    (``-f`` / ``--field`` / ``--raw-field`` / ``--input``, which make gh default
    to POST) marks it a write.

    :param rest: Tokens after ``gh api``, e.g. ``["repos/o/r/pulls", "-X", "POST"]``.
    :param repo: Repo already extracted from ``--repo`` / a URL, if any.
    :returns: A read or write :class:`_ShellOp` for the api call.
    """
    method = _flag_value(rest, frozenset({"-X", "--method"}))
    has_fields = any(
        t in {"-f", "--field", "-F", "--raw-field", "--input"}
        or t.startswith(("-f=", "--field=", "-F=", "--raw-field="))
        for t in rest
    )
    write_methods = {"POST", "PUT", "PATCH", "DELETE"}
    is_write = (method is not None and method.upper() in write_methods) or (
        method is None and has_fields
    )
    api_repo = repo or _repo_from_tokens(rest)
    return _ShellOp(
        kind="write" if is_write else "read",
        repo=api_repo,
        branches=frozenset(),
        branch_targeted=False,
        detail="gh api",
    )


def _segment_hides_gated_invocation(tokens: list[str]) -> bool:
    """
    Whether a gated git/gh invocation sits behind an unrecognized leading token.

    :func:`real_invocation_tokens` only strips the command wrappers we know
    about; one we don't (``stdbuf``, ``nice``, ``timeout`` …) leaves the real
    ``git`` / ``gh`` command deeper in the token list, where the first-token
    dispatch in :func:`_classify_shell_command` misses it and the segment is
    silently allowed. This is the fail-closed backstop: spot a ``git
    <remote-subcmd>`` or ``gh <non-ignored-group>`` token pair anywhere past the
    first token so the caller can surface it for approval instead.

    Matching on a *command + subcommand* token pair (not a bare mention) keeps
    benign text like ``echo "git push later"`` — one quoted token, no adjacent
    pair — from tripping the gate.

    :param tokens: Real-invocation tokens of a segment whose leading token was
        not itself recognized as ``git`` / ``gh``.
    :returns: ``True`` if a gated git/gh invocation appears behind the leading
        token, else ``False``.
    """
    gated_git = _GIT_READ_SUBCMDS | _GIT_WRITE_SUBCMDS
    for i in range(1, len(tokens) - 1):
        word, following = tokens[i], tokens[i + 1]
        if word == "git" and following in gated_git:
            return True
        if word == "gh" and not following.startswith("-") and following not in _GH_IGNORE_GROUPS:
            return True
    return False


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
        starts with git/gh but cannot be tokenized — or hides a gated git/gh
        invocation behind an unrecognized leading wrapper — yields an
        ``"unparseable"`` op so the caller can ASK rather than silently allow.
    """
    if _depth > MAX_SHELL_NESTING:
        return []
    ops: list[_ShellOp] = []
    for segment in split_command_segments(command):
        try:
            tokens = shlex.split(segment)
        except ValueError:
            # Unbalanced quotes etc. If it looks like a git/gh command we can't
            # read, surface it for approval instead of guessing.
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
        tokens = real_invocation_tokens(tokens)
        if not tokens:
            continue
        # Unwrap shell-interpreter (``bash -c "<cmd>"``) and ``eval`` wrappers so
        # the inner command is gated rather than passing through unrecognized.
        inner = unwrap_shell_command(tokens)
        if inner is not None:
            ops.extend(_classify_shell_command(inner, _depth + 1))
            continue
        if tokens[0] == "git":
            op = _classify_git(tokens)
        elif tokens[0] == "gh":
            op = _classify_gh(tokens)
        elif _segment_hides_gated_invocation(tokens):
            # An unrecognized leading wrapper hides a real git/gh invocation —
            # fail closed (ASK) rather than let it through unchecked.
            op = _ShellOp(
                kind="unparseable",
                repo=None,
                branches=frozenset(),
                branch_targeted=False,
                detail=segment[:60],
            )
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
    :param mcp_tool_prefixes: GitHub MCP server name-prefixes to strip when
        canonicalizing MCP tool names. ``None`` uses the standard
        ``mcp__github__`` / ``github__``.
    :param shell_tools: Names of the shell / terminal tools whose ``command``
        argument is parsed for ``git`` / ``gh`` invocations. ``None`` uses the
        built-in OS shell, ``["sys_os_shell"]``. Override this if the agent
        exposes shell access through a differently-named tool (e.g. a custom
        terminal); list every such tool, since git/gh run through any tool not
        listed here are not inspected by this policy.
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
        return _gate_write(
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
        if op.kind == "write":
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
            "commands run via sys_os_shell. Restricts reads to read_repos (unless read_all), and "
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
                    "inspected (default: sys_os_shell).",
                },
            },
        },
    },
]
