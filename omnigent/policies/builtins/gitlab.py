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
    _GIT_GLOBAL_VALUE_OPTS,
    _ask,
    _classify_git,
    _classify_git_push,
    _deny,
    _git_subcommand_index,
    _parse_git_push_args,
    _unparsed_git_push,
    _worse,
)
from omnigent.policies.schema import PolicyEvent, PolicyResponse

# A project as ``(host, path)``; host ``None`` means "on any configured host".
_Project = tuple[str | None, str]

_SCP_REMOTE = re.compile(r"^[\w.-]+@([^:/]+):(.+)$")
# ``projects/<url-encoded path>`` in a ``glab api`` endpoint; numeric IDs don't match.
_API_PROJECT_PATH = re.compile(r"/?projects/([^/]+)(?:/(.*))?$")
# These settings can change destinations or add writes outside the parsed target.
_GIT_TARGET_CONFIG = re.compile(
    r"(?:url\..+\.(?:push)?insteadof|remote\..+|include(?:if\..+)?\.path|"
    r"submodule\..+\.url|push\.recursesubmodules)",
    re.IGNORECASE,
)
# API routes that write branch content; the destination is the ``branch`` parameter.
_API_BRANCH_WRITE = re.compile(r"repository/(?:files|commits|branches)(?:/|$)")
_API_MERGE_WRITE = re.compile(r"merge_requests/[0-9]+/(?:merge|rebase)$")
_API_METADATA_WRITE = re.compile(
    r"(?:issues|labels|milestones|variables|releases|pipelines|jobs)"
    r"(?:/[^/]+)?$"
)
# Variables that point glab at another host or config (see ``glab config --help``).
_HOST_ENV_VARS: frozenset[str] = frozenset(
    {"GITLAB_HOST", "GITLAB_URI", "GL_HOST", "GITLAB_API_HOST", "GLAB_CONFIG_DIR"}
)
_HOST_CONFIG_KEYS: frozenset[str] = frozenset(
    {"host", "gitlab_host", "gitlab_uri", "gl_host", "api_host", "gitlab_api_host"}
)


def _value_options(long_names: str, short_letters: str) -> tuple[frozenset[str], str]:
    """Build a ``(long options, short letters)`` pair from space-separated names."""
    return frozenset(f"--{name}" for name in long_names.split()), short_letters


# Options whose value is the next token, from each subcommand's ``git <sub> -h``.
_GIT_VALUE_OPTIONS: dict[str, tuple[frozenset[str], str]] = {
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

_GLAB_ACTION_ALIASES = {
    "accept": "merge",
    "ls": "list",
    "show": "view",
    "new": "create",
    "del": "delete",
    "rm": "delete",
    "remove": "delete",
    "comment": "note",
    "sub": "subscribe",
    "unsub": "unsubscribe",
}


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


# Unknown options ASK; short aliases are specific to their command.
_GLAB_COMMON_OPTIONS = _option_spec("repo:R hostname", "help:h")
_GLAB_API_OPTIONS = _option_spec(
    "hostname method:X header:H raw-field:f field:F form input placeholder output",
    "help:h include:i paginate silent",
)
_GLAB_COMMAND_OPTIONS = {
    ("mr", "merge"): _option_spec(
        "message:m sha squash-message", "auto-merge rebase:r remove-source-branch:d squash:s yes:y"
    ),
    ("mr", "create"): _option_spec(
        "assignee:a attach description:d description-file head:H label:l milestone:m "
        "related-issue:i reviewer source-branch:s target-branch:b template title:t",
        "allow-collaboration auto-merge copy-issue-labels create-source-branch draft fill:f "
        "fill-commit-body no-editor push recover remove-source-branch signoff "
        "squash-before-merge web:w wip yes:y",
    ),
    ("mr", "update"): _option_spec(
        "assignee:a attach description:d description-file label:l milestone:m reviewer "
        "target-branch title:t unlabel:u",
        "draft fill:f fill-commit-body lock-discussion ready:r remove-source-branch "
        "squash-before-merge unassign unlock-discussion wip yes:y",
    ),
    ("mr", "view"): _option_spec("output:F", "comments:c system-logs:s web:w"),
    ("mr", "list"): _option_spec(
        "assignee:a author label:l milestone:m page:p per-page:P", "all:A closed:c merged:M"
    ),
    ("mr", "note"): _option_spec("message:m", "unique"),
    ("issue", "create"): _option_spec(
        "assignee:a description:d label:l milestone:m title:t", "confidential:c yes:y web:w"
    ),
    ("issue", "note"): _option_spec("message:m", "unique"),
    ("repo", "delete"): _option_spec(switches="yes:y"),
    ("stack", "sync"): _option_spec(switches="skip-push"),
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


@dataclass(frozen=True)
class _GitLabOp:
    """
    One GitLab remote operation parsed from a shell command segment.

    :param kind: ``"read"``, ``"write"``, or ``"unparseable"`` (needs approval).
    :param project: Target project, or ``None`` when it relies on a local
        remote alias, the working directory's remote, or an unknown host.
    :param branches: ``None`` if inapplicable, empty if unknown, else destinations.
    :param detail: Short description for the decision reason, e.g. ``"glab mr merge"``.
    :param destructive: Whether the op is an irreversible delete.
    :param tag_push: Whether a ``git push`` includes tags.
    :param force_push: Whether a push forces.
    :param uncertain_effects: Whether additional targets or effects require approval.
    """

    kind: str
    project: _Project | None
    branches: frozenset[str] | None = None
    detail: str = ""
    destructive: bool = False
    tag_push: bool = False
    force_push: bool = False
    uncertain_effects: bool = False


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


def _git_read_arguments(sub: str, args: list[str]) -> tuple[list[str], list[str]] | None:
    """Consume read-command options, retaining clone configuration for inspection."""
    value_names, value_letters = _GIT_VALUE_OPTIONS[sub]
    operands: list[str] = []
    configuration: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        index += 1
        if token == "--":
            operands.extend(args[index:])
            break
        config_option = False
        if token.startswith("--"):
            name, attached, value = token.partition("=")
            matches = (
                [name]
                if name in value_names
                else [option for option in value_names if option.startswith(name)]
            )
            if not matches:
                continue
            if len(matches) != 1:
                return None
            config_option = matches[0] == "--config"
        elif token.startswith("-") and len(token) > 1:
            for position, letter in enumerate(token[1:], start=1):
                if letter in value_letters:
                    value = token[position + 1 :]
                    attached = bool(value)
                    config_option = sub == "clone" and letter == "c"
                    break
            else:
                continue
        else:
            operands.append(token)
            continue
        if not attached:
            if index == len(args):
                return None
            value = args[index]
            index += 1
        if config_option:
            configuration.append(value)
    return operands, configuration


def _uncertain_git_config(value: str, *, dynamic: bool = False) -> bool:
    """Check a configuration assignment for unresolved destinations or side effects."""
    key, _, setting = value.partition("=")
    if not _GIT_TARGET_CONFIG.fullmatch(key):
        return False
    return not (
        key.lower() == "push.recursesubmodules"
        and not dynamic
        and setting.lower() in {"check", "no", "false", "off", "0"}
    )


def _git_changes_targets(tokens: list[str], index: int) -> bool:
    """Inspect global configuration and persistent config/remote changes."""
    position = 1
    while position < index:
        token = tokens[position]
        position += 1
        name, _, value = token.partition("=")
        if token in _GIT_GLOBAL_VALUE_OPTS:
            name, value = token, tokens[position]
            position += 1
        elif token.startswith("-c") and token != "-c":
            name, value = "-c", token[2:]
        if name in {"-c", "--config-env"} and _uncertain_git_config(
            value, dynamic=name == "--config-env"
        ):
            return True
    sub, args = tokens[index], tokens[index + 1 :]
    if sub == "config":
        return any(
            _uncertain_git_config(
                token
                + ("=" + args[offset + 1] if "=" not in token and offset + 1 < len(args) else "")
            )
            for offset, token in enumerate(args)
        )
    while args and args[0] in {"-v", "--verbose"}:
        args = args[1:]
    return sub == "remote" and bool(args) and args[0] not in {"show", "get-url"}


def _classify_git_command(
    tokens: list[str], hosts: frozenset[str], other_forge_hosts: frozenset[str]
) -> _GitLabOp | None:
    """Retain parsed push targets, options and uncertainty through authorization."""
    try:
        index = _git_subcommand_index(tokens)
    except ValueError:
        return _unparseable("incomplete or unsupported Git global options")
    if index is None:
        return None
    if _git_changes_targets(tokens, index):
        return _unparseable("git configuration with uncertain targets")
    sub, args = tokens[index], tokens[index + 1 :]
    uncertain_effects = False
    if sub == "push":
        parsed = _parse_git_push_args(args, allow_abbreviations=True)
        op = _classify_git_push(parsed) if parsed else _unparsed_git_push(args)
        remote = parsed.remote if parsed else None
        if parsed:
            uncertain_effects = any(
                value not in {"check", "no"}
                for value in parsed.option_values.get("--recurse-submodules", [])
            )
    elif sub in _GIT_VALUE_OPTIONS:
        read_args = _git_read_arguments(sub, args)
        if read_args is None:
            return _unparseable(f"git {sub} options")
        operands, configuration = read_args
        if any(_uncertain_git_config(value) for value in configuration):
            return _unparseable("git clone configuration with uncertain targets")
        remote = operands[0] if operands else None
        op = _classify_git(tokens)
        assert op is not None
    else:
        return None
    is_url, project = _parse_url(remote, hosts) if remote else (False, None)
    if is_url and project is None and host_of(remote or "") in other_forge_hosts:
        return None
    return _GitLabOp(
        kind=op.kind,
        project=project,
        branches=op.branches if op.branch_targeted else None,
        detail=op.detail,
        destructive=op.destructive,
        tag_push=op.tag_push,
        force_push=op.force_push,
        uncertain_effects=uncertain_effects,
    )


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


def _glab_host_override(
    env_tokens: list[str], args: _GlabArguments, hosts: frozenset[str]
) -> str | None:
    """Resolve explicit hosts; conflicting or unowned authorities are uncertain."""
    values = args.values("hostname")
    for token in env_tokens:
        name, equal, assigned = token.partition("=")
        if equal and name in _HOST_ENV_VARS:
            if name == "GLAB_CONFIG_DIR":
                return ""
            values.append(assigned)
    if not values:
        return None
    authorities = {instance_authority(value) for value in values}
    if len(authorities) != 1:
        return ""
    authority = authorities.pop()
    return authority if authority in hosts else ""


def _canonical_glab_action(group: str, action: str) -> str:
    """Resolve aliases before choosing an action's options and effects."""
    words = action.split()
    words[-1] = _GLAB_ACTION_ALIASES.get(words[-1], words[-1])
    canonical = " ".join(words)
    return canonical if canonical in _GLAB_ACTIONS[group] else action


def _classify_glab(
    tokens: list[str], env_tokens: list[str], hosts: frozenset[str]
) -> _GitLabOp | None:
    """Parse a supported command, then classify its target and effects."""
    if len(tokens) < 2 or tokens[1] in {"-h", "--help", "-v", "--version"}:
        return None
    group = _GLAB_GROUP_ALIASES.get(tokens[1], tokens[1])
    if group in _GLAB_IGNORE_GROUPS:
        return None
    rest = tokens[2:]
    if group == "api":
        args = _parse_glab_arguments(rest, _GLAB_API_OPTIONS)
        if args is None:
            return _unparseable("glab api options")
        override = _glab_host_override(env_tokens, args, hosts)
        if override == "":
            return _unparseable("glab api with an uncertain host")
        return None if args.enabled("help") else _classify_glab_api(args, override)
    if group == "config":
        # Editing CLI configuration can redirect later commands.
        if rest[:1] in (["get"], ["path"]):
            return None
        return _unparseable("glab config changing CLI configuration")
    actions = _GLAB_ACTIONS.get(group)
    if actions is None:
        return _unparseable(f"glab {group}")
    if not rest or rest == ["--help"] or rest == ["-h"]:
        return None
    action = " ".join(rest[:2])
    count = 2
    if action not in actions:
        action, count = rest[0], 1
    if action not in actions:
        return _unparseable(f"glab {group} {action}")
    action = _canonical_glab_action(group, action)
    detail = f"glab {group} {action}"
    args = _parse_glab_arguments(
        rest[count:], _GLAB_COMMON_OPTIONS | _GLAB_COMMAND_OPTIONS.get((group, action), {})
    )
    if args is None:
        return _unparseable(f"{detail} options")
    if args.enabled("help"):
        return None
    if (group, action) == ("repo", "fork"):
        return _unparseable("glab repo fork with an unresolved destination project")
    override = _glab_host_override(env_tokens, args, hosts)
    if override == "":
        return _unparseable(f"{detail} with an uncertain host")
    if group in {"mr", "issue", "incident"} and any("://" in operand for operand in args.operands):
        return _unparseable(f"{detail} with a positional URL selecting another target")
    selectors = args.values("repo")
    # repo commands take their target as an operand, even when -R is present.
    if group == "repo" and action in {"clone", "delete", "view", "archive"}:
        selectors.extend(args.operands[:1])
    if len(set(selectors)) > 1:
        return _unparseable(f"{detail} with conflicting project selectors")
    project = _parse_project_ref(selectors[0], hosts) if selectors else None
    if selectors and project is None:
        return _unparseable(f"{detail} with an unknown project")
    if project is not None and override:
        if project[0] is not None and project[0] != override:
            return _unparseable(f"{detail} with conflicting hosts")
        project = (override, project[1])
    kind = actions[action]
    if kind == "read":
        return _GitLabOp(kind="read", project=project, detail=detail)
    uncertain_effects = args.enabled("recover") or args.last("head") is not None
    uncertain_effects = uncertain_effects or (
        (group, action) == ("mr", "create")
        and any(args.enabled(name) for name in ("fill", "push", "create-source-branch"))
    )
    branches = None
    if target := args.last("target-branch"):
        branches = frozenset({target})
    if (group == "mr" and (action in {"merge", "rebase"} or args.enabled("auto-merge"))) or (
        (group, action) == ("stack", "sync") and not args.enabled("skip-push")
    ):
        branches = frozenset()
    return _GitLabOp(
        kind="write",
        project=project,
        branches=branches,
        detail=detail,
        destructive=kind == "destructive" or args.enabled("remove-source-branch"),
        force_push=(group, action) == ("stack", "sync") and not args.enabled("skip-push"),
        uncertain_effects=uncertain_effects,
    )


def _classify_glab_api(args: _GlabArguments, override: str | None) -> _GitLabOp:
    """Use only parsed options and the API path to identify a request's effects."""
    methods = {value.upper() for value in args.values("method")}
    if len(methods) > 1 or len(args.operands) != 1:
        return _unparseable("glab api with an ambiguous method or endpoint")
    method = methods.pop() if methods else ""
    fields = [value for name, value in args.options if name in {"field", "raw-field", "form"}]
    has_input = args.last("input") is not None
    is_write = method not in {"", "GET", "HEAD"} or (not method and bool(fields or has_input))
    try:
        endpoint = urlsplit(args.operands[0])
    except ValueError:
        return _unparseable("glab api with an invalid endpoint")
    if endpoint.scheme or endpoint.netloc or endpoint.fragment:
        return _unparseable("glab api with an absolute URL or fragment")
    if {".", ".."} & set(unquote(endpoint.path).split("/")):
        return _unparseable("glab api with an uncertain project path")
    match = _API_PROJECT_PATH.fullmatch(endpoint.path)
    project = None
    route = ""
    if match:
        path = unquote(match[1])
        project = (override or None, path.lower()) if _valid_project(path) else None
        route = unquote(match[2] or "").rstrip("/")
    branches = None
    uncertain_effects = False
    parameters = parse_qsl(endpoint.query, keep_blank_values=True)
    parameters += [(key, value) for key, _, value in (field.partition("=") for field in fields)]
    effective_method = method or ("POST" if is_write else "GET")
    if is_write:
        if _API_BRANCH_WRITE.match(route):
            branches = _api_branches(parameters, has_input)
        elif _API_MERGE_WRITE.fullmatch(route):
            branches = frozenset()
        elif (effective_method == "POST" and route == "merge_requests") or (
            effective_method == "PUT" and re.fullmatch(r"merge_requests/[0-9]+", route)
        ):
            uncertain_effects = has_input or any(
                key in {"target_project_id", "source_project_id"} for key, _ in parameters
            )
            if effective_method == "POST" or any(key == "target_branch" for key, _ in parameters):
                branches = _api_branches(parameters, has_input, key="target_branch")
        elif effective_method == "DELETE" and re.fullmatch(r"merge_requests/[0-9]+", route):
            pass
        elif not _API_METADATA_WRITE.fullmatch(route):
            uncertain_effects = True
    removes_branch = any(
        key in {"should_remove_source_branch", "remove_source_branch"}
        and value.lower() not in {"false", "0"}
        for key, value in parameters
    )
    if is_write and _API_MERGE_WRITE.fullmatch(route) and has_input:
        uncertain_effects = True
    return _GitLabOp(
        kind="write" if is_write else "read",
        project=project,
        branches=branches,
        detail="glab api",
        destructive=method == "DELETE" or (is_write and removes_branch),
        uncertain_effects=uncertain_effects,
    )


def _api_branches(
    parameters: list[tuple[str, str]], has_input: bool, *, key: str = "branch"
) -> frozenset[str]:
    """Identify explicit branch destinations; opaque bodies and values stay unknown."""
    if has_input:
        return frozenset()
    values = [value for name, value in parameters if name == key]
    if any(not value or value.startswith(("@", ":", "[", "{")) for value in values):
        return frozenset()
    return frozenset(values)


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
        executable = tokens[0].replace("\\", "/").rsplit("/", 1)[-1].removesuffix(".exe")
        if executable == "git":
            op = _classify_git_command(tokens, hosts, other_forge_hosts)
        elif executable == "glab":
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
    allowed_branches = set(write_branches or [])
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
                f"{deny_reason} Force push is blocked by policy. Remove the force flag "
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
        if op.uncertain_effects:
            return _ask(
                f"The additional targets or effects of `{op.detail}` are unknown. Approve?"
            )
        if allowed_branches and op.branches is not None:
            bad = set(op.branches) - allowed_branches
            if bad:
                return _deny(
                    f"{deny_reason} Write is restricted to branches "
                    f"{sorted(allowed_branches)}; this call targets {sorted(bad)}."
                )
            if not op.branches:
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
                "deny_reason": {
                    "type": "string",
                    "description": "Reason prefix attached to DENY decisions.",
                    "default": "GitLab operation blocked by policy.",
                },
            },
        },
    },
]
