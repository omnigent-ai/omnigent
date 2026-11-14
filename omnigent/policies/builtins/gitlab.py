"""Built-in GitLab access policy (shell surface).

A single factory, :func:`gitlab_policy`, gating GitLab access through ``git``
and ``glab`` shell commands dispatched through any harness's shell tool. It
mirrors :func:`omnigent.policies.builtins.github.github_policy`:

- Reads are allowed everywhere when ``read_all`` is ``True`` (default), else
  restricted to ``read_repos``.
- Writes are restricted to ``write_repos`` and, when set, ``write_branches``.
- Deletes (including ``git push --mirror`` / ``--prune``) are denied unless
  ``allow_destructive``; force pushes and tag pushes are denied unless
  ``deny_force_push`` / ``deny_tag_push`` are turned off.
- A gated command whose project (or, for a branch-restricted push, branch)
  cannot be determined — e.g. ``git push origin main`` — returns ASK.

Projects are GitLab paths, including nested groups (``group/sub/project``),
matched case-insensitively. Only the hosts in ``hosts`` are GitLab: the policy
does not trust the glab CLI config or environment, which the agent could edit.
A bare allowlist entry matches that path on every configured host; a URL entry
pins it to one host. ``git`` commands addressed to another forge's built-in host
(github.com, for one) are left to that forge's policy, so the two compose; a
remote on any other host is treated as an unknown project.

Not covered: GitLab MCP server tools, and raw HTTP calls (``curl``) to the
GitLab API, which need an HTTP forwarding gateway.

YAML usage::

    policies:
      gitlab_guard:
        type: function
        function:
          path: omnigent.policies.builtins.gitlab.gitlab_policy
          arguments:
            hosts: ["gitlab.com", "gitlab.example.com"]
            write_repos: ["my-group/service-a"]
            write_branches: ["main", "develop"]
"""

from __future__ import annotations

import itertools
import re
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from omnigent.git_providers import host_of, providers
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

# Forge-neutral git push parsing and decision helpers; a candidate to move to a
# shared module once a third forge policy needs them.
from omnigent.policies.builtins.github import (
    _ask,
    _classify_git,
    _deny,
    _flag_value,
    _git_subcommand_index,
    _normalize_branches,
    _worse,
)
from omnigent.policies.schema import PolicyEvent, PolicyResponse

# A project as ``(host, path)``; host ``None`` means "on any configured host".
_Project = tuple[str | None, str]

_SCP_REMOTE = re.compile(r"^[\w.-]+@([^:/]+):(.+)$")
# ``projects/<url-encoded path>`` in a ``glab api`` endpoint; numeric IDs don't match.
_API_PROJECT_PATH = re.compile(r"(?:^|/)projects/([^/?#]*%2[Ff][^/?#]*)")
# ``url.<base>.insteadOf`` / ``pushInsteadOf`` rewrite a remote URL after it is parsed.
_URL_REWRITE_KEY = re.compile(r"url\..+\.(?:push)?insteadof", re.IGNORECASE)
# API routes that write branch content; the destination is the ``branch`` parameter.
_API_BRANCH_WRITE = re.compile(r"/repository/(?:files|commits|branches)(?:[/?]|$)")
_GLAB_API_FIELD_FLAGS: frozenset[str] = frozenset({"-f", "--raw-field", "-F", "--field", "--form"})
# Variables that point glab at another host or config (see ``glab config --help``).
_HOST_ENV_VARS: frozenset[str] = frozenset(
    {"GITLAB_HOST", "GITLAB_URI", "GL_HOST", "GITLAB_API_HOST", "GLAB_CONFIG_DIR"}
)
_HOST_CONFIG_KEYS: frozenset[str] = frozenset(
    {"host", "gitlab_host", "gitlab_uri", "gl_host", "api_host", "gitlab_api_host"}
)

# Long options of ``git push`` (``git push -h``), for expanding the abbreviations git accepts.
_GIT_PUSH_OPTIONS: tuple[str, ...] = tuple(
    f"--{name}"
    for name in [
        "all",
        "atomic",
        "branches",
        "delete",
        "dry-run",
        "exec",
        "follow-tags",
        "force",
        "force-if-includes",
        "force-with-lease",
        "ipv4",
        "ipv6",
        "mirror",
        "porcelain",
        "progress",
        "prune",
        "push-option",
        "quiet",
        "receive-pack",
        "recurse-submodules",
        "repo",
        "set-upstream",
        "signed",
        "tags",
        "thin",
        "verbose",
        "verify",
    ]
)


def _value_options(long_names: str, short_letters: str) -> tuple[frozenset[str], str]:
    """Build a ``(long options, short letters)`` pair from space-separated names."""
    return frozenset(f"--{name}" for name in long_names.split()), short_letters


# Options whose value is the next token, from each subcommand's ``git <sub> -h``.
_GIT_VALUE_OPTIONS: dict[str, tuple[frozenset[str], str]] = {
    "push": _value_options("repo receive-pack exec push-option recurse-submodules", "o"),
    "clone": _value_options(
        "jobs template reference reference-if-able origin branch revision upload-pack depth "
        "shallow-since shallow-exclude separate-git-dir ref-format config server-option "
        "filter bundle-uri",
        "jobuc",
    ),
    "fetch": _value_options(
        "upload-pack jobs depth shallow-since shallow-exclude deepen refmap server-option "
        "negotiation-tip filter",
        "jo",
    ),
    "pull": _value_options(
        "cleanup strategy strategy-option upload-pack depth shallow-since shallow-exclude "
        "deepen refmap server-option negotiation-tip",
        "sXo",
    ),
    "ls-remote": _value_options("upload-pack sort server-option", "o"),
}


def _glab_actions(read: str = "", write: str = "", destructive: str = "") -> dict[str, str]:
    """Map each comma-separated action to its kind; destructive actions are writes too."""
    kinds = {"read": read, "write": write, "destructive": destructive}
    return {
        action.strip(): kind
        for kind, names in kinds.items()
        for action in names.split(",")
        if action
    }


# glab groups → action → kind, from glab's built-in help (including hidden aliases
# such as ``mr new``). Nested commands use two-word keys; unlisted actions ASK.
_GLAB_ACTIONS: dict[str, dict[str, str]] = {
    "mr": _glab_actions(
        read="approvers,checkout,diff,issues,list,ls,show,view,note list",
        write="accept,approve,close,comment,create,for,merge,new,note,rebase,reopen,revoke,"
        "sub,subscribe,todo,unsub,unsubscribe,update,"
        "note create,note draft,note reopen,note resolve,note update",
        destructive="del,delete,note delete",
    ),
    "issue": _glab_actions(
        read="list,ls,show,view,board view",
        write="board create,close,comment,create,new,note,reopen,sub,subscribe,unsub,"
        "unsubscribe,update",
        destructive="del,delete",
    ),
    "incident": _glab_actions(
        read="list,ls,show,view",
        write="close,comment,note,reopen,sub,subscribe,unsub,unsubscribe",
    ),
    "work-items": _glab_actions(read="list,ls", write="create,update", destructive="delete"),
    "repo": _glab_actions(
        read="archive,clone,contributors,list,ls,prune,search,view,remote add",
        write="create,fork,mirror,transfer,update,members add,members remove,publish catalog",
        destructive="delete",
    ),
    "release": _glab_actions(
        read="download,list,ls,view", write="create,upload", destructive="delete"
    ),
    "ci": _glab_actions(
        read="artifact,config,get,lint,list,ls,status,trace,view,config compile",
        write="cancel,create,retry,run,run-trig,trigger",
        destructive="delete",
    ),
    "schedule": _glab_actions(read="list", write="create,run,update", destructive="delete"),
    "variable": _glab_actions(
        read="export,get,list,ls",
        write="create,import,new,set,update",
        destructive="delete,remove",
    ),
    "label": _glab_actions(read="get,list,ls", write="create,edit,new", destructive="delete"),
    "milestone": _glab_actions(read="get,list", write="create,edit", destructive="delete"),
    "snippet": _glab_actions(write="create,new"),
    "securefile": _glab_actions(
        read="download,get,list,ls,show",
        write="create,update,upload",
        destructive="delete,remove,rm",
    ),
    "packages": _glab_actions(read="download,list,ls", write="upload", destructive="delete,rm"),
    "deploy-key": _glab_actions(read="get,list", write="add", destructive="delete"),
    "ssh-key": _glab_actions(read="get,list", write="add", destructive="delete"),
    "gpg-key": _glab_actions(read="get,list", write="add", destructive="delete"),
    "token": _glab_actions(read="list,ls", write="create,new,rotate", destructive="revoke,rm"),
    "runner": _glab_actions(
        read="jobs,list,ls,managers", write="assign,unassign,update", destructive="delete"
    ),
    "runner-controller": _glab_actions(
        read="get,list,scope list,token list",
        write="create,update,scope create,token create,token rotate",
        destructive="delete,scope delete,token revoke",
    ),
    "container-registry": _glab_actions(
        read="repository list,repository view,tag list,tag view",
        destructive="repository delete,tag delete",
    ),
    "opentofu": _glab_actions(
        read="state download,state list",
        write="state lock,state unlock",
        destructive="state delete",
    ),
    "cluster": _glab_actions(
        read="agent check-manifest-usage,agent list", write="agent bootstrap,agent get-token"
    ),
    "security": _glab_actions(read="config status", write="config disable,config enable"),
    # Everything but ``sync`` edits local stack metadata only.
    "stack": _glab_actions(
        read="amend,create,delete,first,infer,last,list,ls,move,new,next,prev,reorder,save,switch",
        write="sync",
    ),
    "todo": _glab_actions(read="list,ls", write="done"),
    "job": _glab_actions(read="artifact"),
    "iteration": _glab_actions(read="list,ls"),
    "user": _glab_actions(read="events"),
    "changelog": _glab_actions(read="generate"),
    "search": _glab_actions(read="semantic"),
    "attestation": _glab_actions(read="verify"),
}
_GLAB_GROUP_ALIASES: dict[str, str] = {
    "cr": "container-registry",
    "pipe": "ci",
    "pipeline": "ci",
    "project": "repo",
    "tf": "opentofu",
}
# glab groups that never touch a project's contents. Others, such as ``mcp
# serve`` or ``duo``, can run arbitrary glab commands and ASK.
_GLAB_IGNORE_GROUPS: frozenset[str] = frozenset(
    {
        "alias",
        "artifact-registry",
        "auth",
        "check-update",
        "completion",
        "govern",
        "help",
        "skills",
        "version",
        "whatsnew",
    }
)

_GLAB_REPO_FLAGS: frozenset[str] = frozenset({"-R", "--repo"})
_GLAB_TARGET_BRANCH_FLAGS: frozenset[str] = frozenset({"-b", "--target-branch"})
# Flags whose value is a separate token, so it is not mistaken for an action.
_GLAB_VALUE_FLAGS: frozenset[str] = _GLAB_REPO_FLAGS | frozenset({"--hostname", "-g", "--group"})
_GLAB_HELP_FLAGS: frozenset[str] = frozenset({"-h", "--help", "-v", "--version"})
# ``glab api --help``: every flag that takes a value.
_GLAB_API_VALUE_FLAGS: frozenset[str] = frozenset(
    [
        "--hostname",
        "-X",
        "--method",
        "-H",
        "--header",
        "-f",
        "--raw-field",
        "-F",
        "--field",
        "--form",
        "--input",
        "--placeholder",
        "--output",
    ]
)


@dataclass(frozen=True)
class _GitLabOp:
    """
    One GitLab remote operation parsed from a shell command segment.

    :param kind: ``"read"``, ``"write"``, or ``"unparseable"`` (needs approval).
    :param project: Target project, or ``None`` when it relies on a local
        remote alias, the working directory's remote, or an unknown host.
    :param branches: Destination branches of a write (empty when unknown).
    :param branch_targeted: Whether the op writes branch content (``git push``).
    :param detail: Short description for the decision reason, e.g. ``"glab mr merge"``.
    :param destructive: Whether the op is an irreversible delete.
    :param tag_push: Whether a ``git push`` includes tags.
    :param force_push: Whether a push forces.
    """

    kind: str
    project: _Project | None
    branches: frozenset[str] = frozenset()
    branch_targeted: bool = False
    detail: str = ""
    destructive: bool = False
    tag_push: bool = False
    force_push: bool = False


def _unparseable(detail: str) -> _GitLabOp:
    """Build an op that requires approval because it could not be classified."""
    return _GitLabOp(kind="unparseable", project=None, detail=detail)


def _hostname(authority: str) -> str:
    """Return the hostname of an authority, dropping any port."""
    return urlsplit(f"https://{authority}").hostname or authority


def _project_path(path: str) -> str | None:
    """
    Reduce a URL path to a lowercased GitLab project path.

    :param path: e.g. ``"/group/sub/proj.git"`` or ``"/group/proj/-/merge_requests/1"``.
    :returns: ``"group/sub/proj"``, or ``None`` when the path is not a project.
    """
    # Dot segments anywhere (even after ``/-/``) can resolve to another project.
    if {".", ".."} & set(unquote(path).split("/")):
        return None
    path = path.split("/-/", 1)[0].strip("/").removesuffix(".git")
    return path.lower() if _valid_project(path) else None


def _parse_url(value: str, hosts: frozenset[str]) -> tuple[bool, _Project | None]:
    """
    Parse a remote or web URL.

    :param value: e.g. ``"https://gitlab.com/g/p.git"`` or ``"git@gitlab.com:g/p.git"``.
    :param hosts: Configured GitLab authorities.
    :returns: ``(is_url, project)``. ``is_url`` is ``False`` for non-URL text;
        ``project`` is ``None`` for a URL that is not a project on a configured host.
    """
    value = value.strip()
    scp = _SCP_REMOTE.fullmatch(value)
    if scp and "://" not in value:
        ssh_host, path = scp[1].lower(), scp[2]
        matches = [host for host in hosts if _hostname(host) == ssh_host]
        project = _project_path(path)
        return True, (matches[0], project) if matches and project else None
    if "://" not in value:
        return False, None
    try:
        parsed = urlsplit(value)
        if parsed.scheme == "ssh":
            ssh_host = (parsed.hostname or "").lower()
            matches = [host for host in hosts if _hostname(host) == ssh_host]
            authority = matches[0] if matches else None
        else:
            authority = instance_authority(f"https://{parsed.netloc.rsplit('@', 1)[-1]}")
    except ValueError:
        return True, None
    project = _project_path(parsed.path)
    if authority is None or authority not in hosts or project is None:
        return True, None
    return True, (authority, project)


def _parse_project_ref(value: str, hosts: frozenset[str]) -> _Project | None:
    """
    Parse an allowlist entry or ``glab -R`` value.

    :param value: A URL, ``"host/group/proj"`` with a configured host, or a bare
        ``"group/proj"``.
    :param hosts: Configured GitLab authorities.
    :returns: The project, or ``None`` when *value* does not name one.
    """
    is_url, project = _parse_url(value, hosts)
    if is_url:
        return project
    first, _, rest = value.strip().partition("/")
    for host in hosts:
        if first.lower() in (host, _hostname(host)):
            path = _project_path(rest)
            return (host, path) if path else None
    path = _project_path(value)
    return (None, path) if path else None


def _project_matches(project: _Project, allowed: set[_Project]) -> bool | None:
    """
    Whether *project* is in *allowed*; a bare entry (host ``None``) matches any host.

    :returns: ``None`` when a project of unknown host matches only host-pinned entries.
    """
    host, path = project
    if (None, path) in allowed:
        return True
    if host is not None:
        return (host, path) in allowed
    return None if any(allowed_path == path for _, allowed_path in allowed) else False


def _git_arguments(sub: str, args: list[str]) -> tuple[list[str], list[str], str | None]:
    """
    Split ``git <sub>`` arguments into flags and positionals, dropping option values.

    Long options are expanded from the abbreviations git accepts (``--force-w``);
    short bundles keep only their flag letters (``-uo ci.skip`` → ``-u``).

    :param sub: A remote subcommand, e.g. ``"push"``.
    :param args: Tokens after the subcommand.
    :returns: ``(flags, positionals, repo_option)``, where *repo_option* is the
        value of ``git push --repo``.
    """
    value_names, value_letters = _GIT_VALUE_OPTIONS[sub]
    flags: list[str] = []
    positionals: list[str] = []
    repo_option: str | None = None
    index = 0
    while index < len(args):
        token = args[index]
        index += 1
        if token == "--":
            positionals.extend(args[index:])
            break
        if token.startswith("--"):
            name, has_value, value = token.partition("=")
            if sub == "push":
                expansions = [option for option in _GIT_PUSH_OPTIONS if option.startswith(name)]
                name = name if name in expansions or len(expansions) != 1 else expansions[0]
            # An abbreviation of a value option also takes the next token.
            if not has_value and any(option.startswith(name) for option in value_names):
                value = args[index] if index < len(args) else ""
                index += 1
            if name == "--repo":
                repo_option = value
            elif not any(option.startswith(name) for option in value_names):
                flags.append(name + has_value + value)
        elif token.startswith("-") and len(token) > 1:
            letters = ""
            for position, letter in enumerate(token[1:], start=1):
                if letter in value_letters:
                    # The value is the rest of the token, else the next token.
                    if position == len(token) - 1:
                        index += 1
                    break
                letters += letter
            if letters:
                flags.append(f"-{letters}")
        else:
            positionals.append(token)
    return flags, positionals, repo_option


def _classify_git_command(
    tokens: list[str], hosts: frozenset[str], other_forge_hosts: frozenset[str]
) -> _GitLabOp | None:
    """
    Classify a ``git`` invocation, reusing the GitHub policy's push parsing.

    :param tokens: Tokens starting at ``git``.
    :param hosts: Configured GitLab authorities.
    :param other_forge_hosts: Hosts of other git providers, left to their policies.
    :returns: An op, or ``None`` for local commands and other forges' remotes.
    """
    if _rewrites_remote_urls(tokens):
        return _unparseable("git config rewriting remote URLs")
    op = _classify_git(tokens)
    if op is None:
        return None
    if op.kind == "unparseable":
        return _unparseable(op.detail)
    index = _git_subcommand_index(tokens)
    if index is None:
        return None
    sub = tokens[index]
    flags, positionals, repo_option = _git_arguments(sub, tokens[index + 1 :])
    if not positionals and repo_option:
        positionals = [repo_option]
    if sub == "push":
        # Re-parse without option values, which could otherwise pass for refspecs.
        op = _classify_git(["git", "push", *flags, *positionals]) or op
    remote = positionals[0] if positionals else None
    is_url, project = _parse_url(remote, hosts) if remote else (False, None)
    if is_url and project is None and host_of(remote or "") in other_forge_hosts:
        return None
    short_letters = "".join(flag[1:] for flag in flags if not flag.startswith("--"))
    mirror = sub == "push" and "--mirror" in flags
    return _GitLabOp(
        kind=op.kind,
        project=project,
        branches=op.branches,
        branch_targeted=op.branch_targeted,
        detail=op.detail,
        # ``--mirror`` force-updates and deletes remote refs; ``--prune`` deletes them.
        destructive=op.destructive
        or mirror
        or (sub == "push" and ("--prune" in flags or "d" in short_letters)),
        tag_push=op.tag_push or mirror,
        force_push=op.force_push or mirror,
    )


def _rewrites_remote_urls(tokens: list[str]) -> bool:
    """
    Whether a ``git`` call sets URL rewriting, which changes the remote it reaches.

    Covers ``-c`` / ``--config-env`` before the subcommand and ``git config``
    writing the key for later commands.
    """
    try:
        index = _git_subcommand_index(tokens)
    except ValueError:
        return False  # _classify_git reports these as unparseable.
    end = len(tokens) if index is not None and tokens[index] == "config" else index
    return any(
        _URL_REWRITE_KEY.match(token.removeprefix("-c").removeprefix("--config-env="))
        for token in tokens[1:end]
    )


def _glab_positionals(args: list[str], value_flags: frozenset[str]) -> list[str]:
    """Return non-flag tokens, skipping the values of *value_flags*."""
    positionals: list[str] = []
    skip_next = False
    for token in args:
        if skip_next:
            skip_next = False
            continue
        if token in value_flags:
            skip_next = True
            continue
        if not token.startswith("-"):
            positionals.append(token)
    return positionals


def _expand_short_flags(args: list[str], value_letters: str) -> list[str]:
    """
    Split short-flag bundles the way glab's flag parser reads them.

    ``-XDELETE`` and ``-X=DELETE`` become ``-X DELETE``; ``-iXDELETE`` becomes
    ``-i -X DELETE``. A value letter takes the rest of its token as its value.
    """
    expanded: list[str] = []
    for token in args:
        if not token.startswith("-") or token.startswith("--") or len(token) <= 2:
            expanded.append(token)
            continue
        for position, letter in enumerate(token[1:], start=1):
            expanded.append(f"-{letter}")
            if letter in value_letters:
                if rest := token[position + 1 :]:
                    expanded.append(rest.removeprefix("="))
                break
    return expanded


def _flag_values(args: list[str], names: frozenset[str]) -> list[str]:
    """Return every value of *names*, given as ``--flag value`` or ``--flag=value``."""
    values: list[str] = []
    index = 0
    while index < len(args):
        name, has_value, value = args[index].partition("=")
        if has_value and name in names:
            values.append(value)
        elif args[index] in names and index + 1 < len(args):
            values.append(args[index + 1])
            index += 1
        index += 1
    return values


def _glab_host_override(
    env_tokens: list[str], args: list[str], hosts: frozenset[str]
) -> str | None:
    """
    Return the host a ``glab`` call is redirected to, if any.

    :param env_tokens: Tokens before ``glab`` (``GITLAB_HOST=…`` assignments).
    :param args: Tokens after the group.
    :returns: The normalized authority, ``""`` for an override to a host this
        policy does not own, or ``None`` when there is no override.
    """
    value = _flag_value(args, frozenset({"--hostname"}))
    for token in env_tokens:
        name, _, assigned = token.partition("=")
        if name in _HOST_ENV_VARS:
            value = assigned
    if value is None:
        return None
    authority = instance_authority(value)
    return authority if authority in hosts else ""


def _repo_positional(positionals: list[str], hosts: frozenset[str]) -> _Project | None:
    """Return the one project among ``glab repo`` arguments, else ``None`` (ambiguous)."""
    candidates = {
        project for value in positionals if (project := _parse_project_ref(value, hosts))
    }
    return candidates.pop() if len(candidates) == 1 else None


def _classify_glab(
    tokens: list[str], env_tokens: list[str], hosts: frozenset[str]
) -> _GitLabOp | None:
    """
    Classify a ``glab`` invocation.

    :param tokens: Tokens starting at ``glab``, e.g. ``["glab", "mr", "merge", "12"]``.
    :param env_tokens: Tokens before ``glab`` in the segment.
    :param hosts: Configured GitLab authorities.
    :returns: An op, or ``None`` for ignored groups (auth), help, and bare ``glab``.
    """
    if len(tokens) < 2 or tokens[1] in _GLAB_IGNORE_GROUPS or tokens[1] in _GLAB_HELP_FLAGS:
        return None
    if tokens[1].startswith("-"):
        # glab's only root flags are help and version; anything else hides the group.
        return _unparseable(f"glab {tokens[1]}")
    group, args = _GLAB_GROUP_ALIASES.get(tokens[1], tokens[1]), tokens[2:]
    override = _glab_host_override(env_tokens, args, hosts)
    if override == "":
        return _unparseable(f"glab {group} against a host outside the GitLab policy")
    if group == "config":
        positionals = _glab_positionals(args, frozenset({"--host", "-h"}))
        changes_host = positionals[:1] == ["edit"] or (
            positionals[:1] == ["set"]
            and len(positionals) > 1
            and positionals[1].lower() in _HOST_CONFIG_KEYS
        )
        return _unparseable("glab config changing the GitLab host") if changes_host else None
    if group == "api":
        return _classify_glab_api(_expand_short_flags(args, "XHfF"), override)

    args = _expand_short_flags(args, "Rbg")
    repo_flags = _flag_values(args, _GLAB_REPO_FLAGS)
    if len(set(repo_flags)) > 1:
        return _unparseable(f"glab {group} with several -R values")
    repo_flag = repo_flags[0] if repo_flags else None
    positionals = _glab_positionals(args, _GLAB_VALUE_FLAGS)
    if repo_flag:
        project = _parse_project_ref(repo_flag, hosts)
        if project is None:
            return _unparseable(f"glab {group} -R {repo_flag}")
    elif group == "repo":
        project = _repo_positional(positionals[1:], hosts)
    else:
        project = None
    if project is not None and override and project[0] is None:
        project = (override, project[1])

    if not args or args[0] in _GLAB_HELP_FLAGS:
        return None  # ``glab <group>`` with no action prints help.
    actions = _GLAB_ACTIONS.get(group)
    if actions is None:
        return _unparseable(f"glab {group}")
    # The action words come first; a flag before them may take one of them as its value.
    leading = list(itertools.takewhile(lambda token: not token.startswith("-"), args))
    if not leading:
        return _unparseable(f"glab {group} {args[0]}")
    action = " ".join(leading[:2])
    if action not in actions:
        action = leading[0]
    kind = actions.get(action)
    detail = f"glab {group} {action}"
    if kind is None:
        return _unparseable(detail)
    if kind == "read":
        return _GitLabOp(kind="read", project=project, detail=detail)
    target = _flag_value(args, _GLAB_TARGET_BRANCH_FLAGS)
    # ``mr merge -d`` (also bundled, as in ``-sd``) deletes the source branch.
    removes_branch = (group, action) == ("mr", "merge") and any(
        (token.startswith("-") and not token.startswith("--") and "d" in token)
        or (token.startswith("--remove-source-branch") and token != "--remove-source-branch=false")
        for token in args
    )
    return _GitLabOp(
        kind="write",
        project=project,
        branches=frozenset(_normalize_branches([target] if target else None)),
        detail=detail,
        destructive=kind == "destructive" or removes_branch,
        # ``stack sync`` force-pushes the stack's branches unless --skip-push.
        force_push=(group, action) == ("stack", "sync") and "--skip-push" not in args,
    )


def _classify_glab_api(args: list[str], override: str | None) -> _GitLabOp:
    """
    Classify ``glab api``: GET by default, POST once any field flag is given.

    :param args: Tokens after ``glab api``, short-flag bundles already split;
        the first positional is the endpoint.
    :param override: Host from ``--hostname`` / ``GITLAB_HOST``, if any.
    :returns: A read or write op; ``DELETE`` requests are destructive.
    """
    methods = {value.upper() for value in _flag_values(args, frozenset({"-X", "--method"}))}
    if len(methods) > 1:
        return _unparseable("glab api with several methods")
    method = methods.pop() if methods else ""
    fields = _flag_values(args, _GLAB_API_FIELD_FLAGS)
    has_input = bool(_flag_values(args, frozenset({"--input"})))
    is_write = method not in {"", "GET", "HEAD"} or (not method and bool(fields or has_input))
    positionals = _glab_positionals(args, _GLAB_API_VALUE_FLAGS)
    endpoint = positionals[0] if positionals else ""
    if "://" in endpoint:
        return _unparseable("glab api with an absolute URL")
    project = None
    matches = _API_PROJECT_PATH.findall(endpoint)
    # A ``..`` segment could step from the matched project into another one.
    if len(matches) == 1 and ".." not in unquote(endpoint).split("/"):
        path = _project_path(unquote(matches[0]))
        project = (override or None, path) if path else None
    branch_targeted = is_write and bool(_API_BRANCH_WRITE.search(endpoint))
    return _GitLabOp(
        kind="write" if is_write else "read",
        project=project,
        branches=_api_branches(endpoint, fields, has_input) if branch_targeted else frozenset(),
        branch_targeted=branch_targeted,
        detail="glab api",
        destructive=method == "DELETE",
    )


def _api_branches(endpoint: str, fields: list[str], has_input: bool) -> frozenset[str]:
    """
    Return the ``branch`` a ``glab api`` write targets, or none when it can't be known.

    :param endpoint: The API endpoint, which may carry a query string.
    :param fields: ``key=value`` values of the field flags.
    :param has_input: Whether ``--input`` supplies a request body.
    """
    if has_input:
        return frozenset()
    query = parse_qsl(urlsplit(endpoint).query)
    values = [value for key, value in query if key == "branch"]
    values += [
        value for key, _, value in (field.partition("=") for field in fields) if key == "branch"
    ]
    # ``@file`` reads the value from a file; ``:name`` is a placeholder glab fills in.
    if any(value.startswith(("@", ":")) for value in values):
        return frozenset()
    return frozenset(_normalize_branches(values))


def _sets_unowned_host(raw_tokens: list[str], hosts: frozenset[str]) -> str | None:
    """
    Return a variable that ``export``/``env``/prefix assignments point at an unowned
    host, or one that injects git config (``GIT_CONFIG_COUNT`` …), which can rewrite URLs.
    """
    for token in raw_tokens:
        name, has_value, value = token.partition("=")
        if has_value and name in _HOST_ENV_VARS and instance_authority(value) not in hosts:
            return name
        if has_value and name.startswith("GIT_CONFIG"):
            return name
    return None


def _classify_shell_command(
    command: str, hosts: frozenset[str], other_forge_hosts: frozenset[str], _depth: int = 0
) -> list[_GitLabOp]:
    """
    Parse a shell command string into the GitLab remote ops it performs.

    Shell wrappers (``bash -c "…"``, ``eval``) are unwrapped and parsed
    recursively; a segment mentioning git/glab whose real command cannot be
    resolved yields an ``"unparseable"`` op so the caller asks.

    :param command: The shell tool's command argument.
    :param hosts: Configured GitLab authorities.
    :param other_forge_hosts: Hosts of other git providers, left to their policies.
    :param _depth: Recursion guard for nested wrappers; callers leave it 0.
    :returns: One op per gated git/glab invocation.
    """
    if _depth > MAX_SHELL_NESTING:
        return [_unparseable("shell command exceeds the nesting limit")]
    ops: list[_GitLabOp] = []
    try:
        segments = split_command_segments(command)
    except UnsupportedShellSyntax as exc:
        segments = exc.completed_segments
        ops.append(_unparseable(str(exc)))
    for segment in segments:
        try:
            raw_tokens = shlex.split(segment)
        except ValueError:
            unreadable, raw_tokens, tokens = True, [], []
        else:
            tokens = real_invocation_tokens(raw_tokens)
            unreadable = is_unresolved_invocation(tokens) or spawns_gated_command_as_child(tokens)
        if unreadable:
            if re.search(r"\b(git|glab)\b", segment):
                ops.append(_unparseable(segment[:60]))
            continue
        # A host variable persists for later glab calls, so ASK wherever it is set.
        if variable := _sets_unowned_host(raw_tokens, hosts):
            ops.append(_unparseable(f"{variable} set to a host outside the GitLab policy"))
        if not tokens:
            continue
        inner = unwrap_shell_command(tokens)
        if inner is not None:
            ops.extend(_classify_shell_command(inner, hosts, other_forge_hosts, _depth + 1))
            continue
        if is_shell_interpreter(tokens):
            ops.append(_unparseable(segment[:60]))
            continue
        if tokens[0] == "git":
            op = _classify_git_command(tokens, hosts, other_forge_hosts)
        elif tokens[0] == "glab":
            env_tokens = raw_tokens[: len(raw_tokens) - len(tokens)]
            op = _classify_glab(tokens, env_tokens, hosts)
        else:
            op = None
        if op is not None:
            ops.append(op)
    return ops


def gitlab_policy(
    *,
    hosts: list[str] | None = None,
    read_all: bool = True,
    read_repos: list[str] | None = None,
    write_repos: list[str] | None = None,
    write_branches: list[str] | None = None,
    allow_destructive: bool = False,
    deny_tag_push: bool = True,
    deny_force_push: bool = True,
    shell_tools: list[str] | None = None,
    deny_reason: str = "GitLab operation blocked by policy.",
) -> Callable[[PolicyEvent], PolicyResponse | None]:
    """
    Build a GitLab access policy callable covering git/glab shell commands.

    :param hosts: GitLab hosts this policy owns, e.g.
        ``["gitlab.com", "gitlab.example.com"]``. ``None`` → ``["gitlab.com"]``.
    :param read_all: When ``True`` (default), all reads are allowed. When
        ``False``, reads are restricted to ``read_repos``.
    :param read_repos: Projects readable when ``read_all`` is ``False``, as
        ``group/project`` or URLs. ``None`` → none.
    :param write_repos: Projects the agent may write to. ``None`` → none.
        An entry that is not a project on a configured host raises ``ValueError``.
    :param write_branches: Branches writable within an allowed project.
        ``None`` / empty means any branch.
    :param allow_destructive: Let irreversible deletes through normal write
        gating. Default ``False`` denies them even on allowed projects.
    :param deny_tag_push: Deny ``git push --tags`` / ``--follow-tags`` /
        ``refs/tags/`` refspecs (default ``True``).
    :param deny_force_push: Deny ``git push`` force flags (``--force``, ``-f``,
        ``--force-with-lease``, ``--force-if-includes``), bundled short flags
        containing ``f`` (``-uf``), and ``+refspec`` prefixes regardless of the
        allowlists (default ``True``).
    :param shell_tools: Shell tools whose ``command`` argument is parsed.
        ``None`` uses every harness's shell tool.
    :param deny_reason: Reason prefix attached to DENY decisions.
    :returns: A one-argument policy callable returning a :class:`PolicyResponse`
        or ``None`` (abstain → ALLOW).
    """
    gitlab_hosts = frozenset(
        authority
        for value in (hosts if hosts is not None else ["gitlab.com"])
        if (authority := instance_authority(value))
    )
    # Other forges' built-in hosts are left to their policies; unknown hosts ASK.
    other_forge_hosts = frozenset(
        host
        for provider in providers()
        if provider.id != "gitlab"
        for host in provider.default_hosts
        if instance_authority(host) not in gitlab_hosts
    )

    def _projects(values: list[str] | None, name: str) -> set[_Project]:
        projects = set()
        for value in values or []:
            project = _parse_project_ref(value, gitlab_hosts)
            if project is None:
                raise ValueError(
                    f"gitlab_policy: {name} entry {value!r} is not a project on one of the "
                    f"configured hosts {sorted(gitlab_hosts)}"
                )
            projects.add(project)
        return projects

    allowed_read = _projects(read_repos, "read_repos")
    allowed_write = _projects(write_repos, "write_repos")
    allowed_branches = _normalize_branches(write_branches)
    shell_tool_names = frozenset(shell_tools) if shell_tools is not None else SHELL_TOOLS

    def _describe(project: _Project) -> str:
        host, path = project
        return f"{host}/{path}" if host else path

    def _unknown_host_reason(op: _GitLabOp) -> str:
        return (
            f"`{op.detail}` targets an allowed project path, but its host could not be "
            f"determined and the allowlist pins that project to a host. Approve?"
        )

    def _gate(op: _GitLabOp) -> PolicyResponse | None:
        """
        Apply the allowlists to one parsed op.

        :param op: The parsed git/glab operation.
        :returns: ``None`` to allow, or a DENY / ASK decision.
        """
        if op.kind == "unparseable":
            return _ask(
                f"Could not parse a git/glab command ({op.detail!r}) to check it against "
                f"the GitLab policy. Approve to run it?"
            )
        if op.kind == "read":
            if read_all:
                return None
            if op.project is None:
                return _ask(
                    f"Reads are restricted to the configured projects, but the target "
                    f"project of `{op.detail}` could not be determined. Approve?"
                )
            matched = _project_matches(op.project, allowed_read)
            if matched:
                return None
            if matched is None:
                return _ask(_unknown_host_reason(op))
            return _deny(
                f"{deny_reason} Read is restricted to the configured projects; "
                f"this call targets {_describe(op.project)!r}."
            )
        if op.force_push and deny_force_push:
            return _deny(
                "Force push is blocked by policy. Remove the force flag "
                "(--force / -f / --force-with-lease / --force-if-includes / "
                "+refspec) or set deny_force_push=False."
            )
        if op.destructive and not allow_destructive:
            return _deny(
                f"{deny_reason} Destructive operation `{op.detail}` is blocked by "
                f"default. Set allow_destructive=true to permit deletes."
            )
        if op.tag_push and deny_tag_push:
            return _deny(
                f"{deny_reason} Pushing tags is blocked by policy (deny_tag_push is enabled)."
            )
        if op.project is None:
            return _ask(
                f"The target project of `{op.detail}` could not be determined "
                f"(e.g. a local remote alias or an unknown host). Approve this write?"
            )
        matched = _project_matches(op.project, allowed_write)
        if matched is None:
            return _ask(_unknown_host_reason(op))
        if not matched:
            return _deny(
                f"{deny_reason} Write is restricted to the configured projects; "
                f"this call targets {_describe(op.project)!r}."
            )
        if allowed_branches:
            bad = set(op.branches) - allowed_branches
            if bad:
                return _deny(
                    f"{deny_reason} Write is restricted to branches "
                    f"{sorted(allowed_branches)}; this call targets {sorted(bad)}."
                )
            if op.branch_targeted and not op.branches:
                return _ask(
                    f"`{op.detail}` writes to an allowed project but its target branch "
                    f"could not be determined, and writes are branch-restricted. Approve?"
                )
        return None

    def _evaluate(event: PolicyEvent) -> PolicyResponse | None:
        """
        Evaluate one ``tool_call`` event; returns the most restrictive decision
        across every git/glab op in the command (DENY > ASK > ALLOW).
        """
        if event.get("type") != "tool_call":
            return None
        data = event.get("data")
        if not isinstance(data, dict) or data.get("name") not in shell_tool_names:
            return None
        args = data.get("arguments")
        command = args.get("command") if isinstance(args, dict) else None
        if not isinstance(command, str) or not command.strip():
            return None
        decision: PolicyResponse | None = None
        for op in _classify_shell_command(command, gitlab_hosts, other_forge_hosts):
            decision = _worse(decision, _gate(op))
        return decision

    return _evaluate


# ── Registry ───────────────────────────────────────────────────────────────────

POLICY_REGISTRY: list[dict[str, Any]] = [  # type: ignore[explicit-any]
    {
        "handler": "omnigent.policies.builtins.gitlab.gitlab_policy",
        "kind": "factory",
        "name": "GitLab Project & Branch Access",
        "description": (
            "Controls GitLab access through git and glab shell commands on gitlab.com and "
            "self-managed hosts. Restricts reads to read_repos (unless read_all), and writes "
            "to write_repos plus optional write_branches. Deletes, force pushes, and tag "
            "pushes are denied by default. Commands whose target project or branch cannot "
            "be determined return ASK for human approval."
        ),
        "params_schema": {
            "type": "object",
            "properties": {
                "hosts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "GitLab hosts this policy owns (default: gitlab.com).",
                },
                "read_all": {
                    "type": "boolean",
                    "description": "Allow all reads. When false, restrict reads to read_repos.",
                    "default": True,
                },
                "read_repos": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Projects (group/project or URLs) readable when read_all "
                    "is false.",
                },
                "write_repos": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Projects (group/project or URLs) the agent may write to.",
                },
                "write_branches": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Branches writable within an allowed project. Empty = any.",
                },
                "allow_destructive": {
                    "type": "boolean",
                    "description": "Allow irreversible destructive operations (deletes). "
                    "When false (default), deletes are denied even on allowed projects.",
                    "default": False,
                },
                "deny_tag_push": {
                    "type": "boolean",
                    "description": "Block pushing tags (--tags, --follow-tags, refs/tags/).",
                    "default": True,
                },
                "deny_force_push": {
                    "type": "boolean",
                    "description": "Deny git push with force flags (--force, -f, "
                    "--force-with-lease, --force-if-includes), bundled short flags "
                    "(-uf), and +refspec force prefixes. Default true.",
                    "default": True,
                },
                "shell_tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Shell/terminal tools whose command arg is parsed for "
                    "git/glab (default: every harness's shell tool).",
                },
            },
        },
    },
]
