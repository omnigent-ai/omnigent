"""Azure DevOps hooks for the session PR observer: ``az repos pr`` commands and PR objects.

Most ``az repos pr`` commands name a PR only by its organization-wide ``--id``, so
the PR usually comes from the JSON the command prints, through :func:`pr_from_object`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import PurePath
from urllib.parse import quote, urlsplit

from omnigent.git_providers.azure_devops import (
    AzureRepo,
    canonical_pr_url,
    parse_azure_devops_remote,
)
from omnigent.runner.git_providers import ShellPrOp, ShellSegment
from omnigent.runner.session_prs import PullRequestRef

# Subcommands that change a PR; every other ``az repos pr`` subcommand only reads.
_WRITES = frozenset(
    {
        ("create",),
        ("update",),
        ("set-vote",),
        ("reviewer", "add"),
        ("reviewer", "remove"),
        ("work-item", "add"),
        ("work-item", "remove"),
        ("policy", "queue"),
    }
)
# Command groups under ``az repos pr``; the next token is their subcommand.
_SUBGROUPS = frozenset({"policy", "reviewer", "work-item"})
# Global ``az`` options that take a value and can come before ``repos``.
_GLOBAL_VALUE_OPTIONS = frozenset({"--output", "-o", "--query"})
# Fields that hold PR content, whose text never identifies the PR.
_CONTENT_FIELDS = frozenset({"description", "title"})
# Pull request ids are 32-bit integers.
_PR_ID = re.compile(r"[1-9][0-9]{0,9}")


def _pr_arguments(tokens: tuple[str, ...]) -> tuple[str, ...] | None:
    """Return the tokens after ``az [global options] repos pr``, or ``None`` for other commands."""
    if PurePath(tokens[0]).name != "az":
        return None
    index = 1
    while index < len(tokens) and tokens[index].startswith("-"):
        index += 2 if tokens[index] in _GLOBAL_VALUE_OPTIONS else 1
    if tokens[index : index + 2] != ("repos", "pr"):
        return None
    return tokens[index + 2 :]


def _option(arguments: Sequence[str], *names: str) -> str | None:
    """Return the last value of ``--name value`` or ``--name=value``, as ``az`` reads options."""
    value = None
    index = 0
    while index < len(arguments):
        name, separator, attached = arguments[index].partition("=")
        if name in names:
            if separator:
                value = attached
            elif index + 1 < len(arguments):
                index += 1
                value = arguments[index]
        index += 1
    return value


def _reference(repo: AzureRepo, number: int) -> PullRequestRef | None:
    """Return the canonical reference of PR ``number`` in ``repo``, or ``None``."""
    try:
        return PullRequestRef.from_url(canonical_pr_url(repo.org, repo.project, repo.repo, number))
    except ValueError:
        return None


def _repo_in(org_url: str, project: str, repository: str) -> AzureRepo | None:
    """Return a repository under an organization URL in either host form, or ``None``."""
    return parse_azure_devops_remote(
        f"{org_url.rstrip('/')}/{quote(project, safe='')}/_git/{quote(repository, safe='')}"
    )


def _target(arguments: Sequence[str]) -> PullRequestRef | None:
    """Return the PR that ``--org``, ``--project``, ``--repository``, and ``--id`` name together.

    The ``az repos pr`` commands that take ``--id`` identify the PR by organization
    and id only, so a target is rarely available; that is expected.
    """
    org = _option(arguments, "--org", "--organization")
    project = _option(arguments, "--project", "-p")
    repository = _option(arguments, "--repository", "-r")
    pr_id = _option(arguments, "--id")
    if not (org and project and repository and pr_id and _PR_ID.fullmatch(pr_id)):
        return None
    repo = _repo_in(org, project, repository)
    return None if repo is None else _reference(repo, int(pr_id))


def _content_only(query: str | None) -> bool:
    """Return whether a JMESPath ``--query`` selects only the description and title."""
    if query is None:
        return False
    fields = query.strip()
    if fields.startswith("[") and fields.endswith("]"):
        fields = fields[1:-1]
    return all(field.strip() in _CONTENT_FIELDS for field in fields.split(","))


def shell_pr_operations(segments: Sequence[ShellSegment]) -> list[ShellPrOp]:
    """Return one op per segment that runs ``az repos pr``, in order; reads do not track."""
    operations: list[ShellPrOp] = []
    for segment in segments:
        arguments = _pr_arguments(segment.invocation_tokens)
        if arguments is None:
            continue
        depth = 2 if arguments and arguments[0] in _SUBGROUPS else 1
        subcommand, options = arguments[:depth], arguments[depth:]
        operations.append(
            ShellPrOp(
                tracks=subcommand in _WRITES,
                creates=subcommand == ("create",),
                target=_target(options),
                content_only=_content_only(_option(options, "--query")),
            )
        )
    return operations


def _org_url(api_url: str) -> str | None:
    """Return the organization URL that a REST API URL starts with, or ``None``."""
    try:
        parts = urlsplit(api_url)
        host = parts.hostname or ""
    except ValueError:
        return None
    segments = parts.path.split("/")[1:]
    if parts.scheme != "https" or "_apis" not in segments:
        return None
    if host == "dev.azure.com":
        # The organization is the first path segment, before ``_apis``.
        return f"https://{host}/{segments[0]}" if segments.index("_apis") > 0 else None
    return f"https://{host}" if host.endswith(".visualstudio.com") else None


def _object_repo(repository: Mapping[str, object]) -> AzureRepo | None:
    """Return the repository that a PR object's ``repository`` field names, or ``None``.

    The first match wins: ``webUrl``, ``remoteUrl``, then ``name`` and ``project.name``
    under the organization of the REST API ``url``, often the only fields a PR has.
    """
    for key in ("webUrl", "remoteUrl"):
        url = repository.get(key)
        if isinstance(url, str) and (repo := parse_azure_devops_remote(url)) is not None:
            return repo
    name = repository.get("name")
    project = repository.get("project")
    project_name = project.get("name") if isinstance(project, Mapping) else None
    api_url = repository.get("url")
    if not (isinstance(name, str) and isinstance(project_name, str) and isinstance(api_url, str)):
        return None
    org_url = _org_url(api_url)
    return None if org_url is None else _repo_in(org_url, project_name, name)


def pr_from_object(obj: Mapping[str, object]) -> PullRequestRef | None:
    """Return the PR of a pull request object from ``az`` or the REST API, or ``None``.

    The object's own ``url`` is a REST API URL, so the PR comes from ``pullRequestId``
    and the ``repository`` field.
    """
    number = obj.get("pullRequestId")
    if isinstance(number, bool) or not isinstance(number, int):
        return None
    repository = obj.get("repository")
    repo = _object_repo(repository) if isinstance(repository, Mapping) else None
    return None if repo is None else _reference(repo, number)


def mcp_prs(
    tool_name: str,  # noqa: ARG001 - no Azure DevOps MCP server is supported
    arguments: dict[str, object],  # noqa: ARG001 - no Azure DevOps MCP server is supported
    result: object,  # noqa: ARG001 - no Azure DevOps MCP server is supported
) -> tuple[list[PullRequestRef], bool] | None:
    """Return ``None``: no Azure DevOps MCP tool is recognized."""
    return None
