"""Azure DevOps provider descriptor, covering dev.azure.com and legacy visualstudio.com hosts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit

from omnigent.git_providers import (
    FacetModules,
    GitProvider,
    Instances,
    ParsedPullRequest,
    ParsedRemote,
    host_of,
)

_PROVIDER_ID = "azure_devops"
_CANONICAL_HOST = "dev.azure.com"
# Accounts that predate dev.azure.com live at {org}.visualstudio.com.
_LEGACY_SUFFIX = ".visualstudio.com"
_SSH_HOSTS = frozenset({"ssh.dev.azure.com", "vs-ssh.visualstudio.com"})
_SCP_REMOTE = re.compile(r"^[\w.\-]+@(?P<host>[\w.\-]+):(?P<path>.+)$")
_PR_NUMBER = re.compile(r"[1-9][0-9]*")


@dataclass(frozen=True)
class AzureRepo:
    """An Azure DevOps repository. Names are percent-decoded and keep their case."""

    org: str
    project: str
    repo: str


def _decode(segment: str) -> str | None:
    """Percent-decode one path segment.

    Returns ``None`` for a segment that cannot name a resource: empty, ``.``, ``..``, holding
    ``/`` or control characters, or not valid UTF-8.
    """
    try:
        name = unquote(segment, errors="strict")
    except UnicodeDecodeError:
        return None
    if name in {"", ".", ".."} or "/" in name or not name.isprintable():
        return None
    return name


def _segments(path: str) -> list[str] | None:
    """Decode a slash-separated path, or return ``None`` when any segment is unusable."""
    names: list[str] = []
    for part in path.split("/"):
        name = _decode(part)
        if name is None:
            return None
        names.append(name)
    return names


def _split_org(host: str, segments: list[str]) -> tuple[str, list[str]] | None:
    """Split HTTPS path segments into the organization and the rest, or return ``None``.

    ``dev.azure.com`` puts the organization first; a legacy host carries it as the host label.
    """
    if host == _CANONICAL_HOST:
        return (segments[0], segments[1:]) if segments else None
    org = host.removesuffix(_LEGACY_SUFFIX)
    if host.endswith(_LEGACY_SUFFIX) and org and "." not in org and host not in _SSH_HOSTS:
        # Old accounts may put the collection name before the project.
        if segments and segments[0].lower() == "defaultcollection":
            segments = segments[1:]
        return org, segments
    return None


def _repo_from_path(host: str, segments: list[str]) -> AzureRepo | None:
    """Read the repository from decoded HTTPS path segments ending in ``_git/{repo}``."""
    located = _split_org(host, segments)
    if located is None:
        return None
    org, rest = located
    if len(rest) == 2 and rest[0].lower() == "_git":
        # A project's default repository has the project's name.
        return AzureRepo(org, rest[1], rest[1])
    if len(rest) == 3 and rest[1].lower() == "_git":
        return AzureRepo(org, rest[0], rest[2])
    return None


def _remote_path(path: str) -> str:
    """Drop the leading slash, trailing slash, and ``.git`` suffix of a remote's path."""
    return path.removeprefix("/").removesuffix("/").removesuffix(".git")


def _repo_from_ssh_path(path: str) -> AzureRepo | None:
    """Read ``v3/{org}/{project}/{repo}``, the path of an SSH remote, or return ``None``."""
    segments = _segments(_remote_path(path))
    if segments is None or len(segments) != 4:
        return None
    version, org, project, repo = segments
    return AzureRepo(org, project, repo) if version.lower() == "v3" else None


def parse_azure_devops_remote(url: str) -> AzureRepo | None:
    """Parse an HTTPS, ``ssh://``, or scp-style SSH remote of an Azure DevOps repository.

    :param url: e.g. ``"https://dev.azure.com/org/project/_git/repo"``,
        ``"ssh://git@ssh.dev.azure.com/v3/org/project/repo"``, or
        ``"git@ssh.dev.azure.com:v3/org/project/repo"``. The ``.git`` suffix is optional.
    :returns: The repository, or ``None`` when *url* is not an Azure DevOps remote.
    """
    candidate = url.strip()
    scp = _SCP_REMOTE.match(candidate)
    if scp is not None:
        if scp["host"].lower() not in _SSH_HOSTS:
            return None
        return _repo_from_ssh_path(scp["path"])
    try:
        parts = urlsplit(candidate)
        host = parts.hostname or ""
    except ValueError:
        return None
    # The host part after any credentials must be the bare host, without a port.
    if parts.netloc.rpartition("@")[2].lower() != host:
        return None
    if parts.scheme == "ssh":
        return _repo_from_ssh_path(parts.path) if host in _SSH_HOSTS else None
    if parts.scheme != "https":
        return None
    segments = _segments(_remote_path(parts.path))
    return None if segments is None else _repo_from_path(host, segments)


def canonical_pr_url(org: str, project: str, repo: str, pr_id: int) -> str:
    """Return the stable URL of a pull request, the identity the session registry dedupes on.

    Azure DevOps names are case-insensitive, so they are lower-cased.
    """
    org, project, repo = (quote(name.lower(), safe="") for name in (org, project, repo))
    return f"https://{_CANONICAL_HOST}/{org}/{project}/_git/{repo}/pullrequest/{pr_id}"


def parse_azure_devops_pr_url(url: str) -> ParsedPullRequest | None:
    """Normalize an HTTPS Azure DevOps pull request URL to its ``dev.azure.com`` form.

    Query strings and fragments are dropped. URLs with credentials or a port are rejected.
    """
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname or ""
    except ValueError:
        return None
    if parts.scheme != "https" or parts.netloc.lower() != host:
        return None
    segments = _segments(parts.path.removeprefix("/").removesuffix("/"))
    if segments is None or len(segments) < 2:
        return None
    *repo_path, marker, number = segments
    if marker.lower() != "pullrequest" or _PR_NUMBER.fullmatch(number) is None:
        return None
    repo = _repo_from_path(host, repo_path)
    if repo is None:
        return None
    try:
        pr_id = int(number)
    except ValueError:
        # The number exceeds Python's integer string conversion limit.
        return None
    return ParsedPullRequest(
        provider=_PROVIDER_ID,
        host=_CANONICAL_HOST,
        repository=f"{repo.org}/{repo.project}/{repo.repo}".lower(),
        number=pr_id,
        url=canonical_pr_url(repo.org, repo.project, repo.repo, pr_id),
    )


class AzureDevOpsProvider:
    """Azure DevOps Services: dev.azure.com and the legacy visualstudio.com hosts."""

    id = _PROVIDER_ID
    display_name = "Azure DevOps"
    default_hosts = ("dev.azure.com", "ssh.dev.azure.com")
    facets = FacetModules(
        pull_requests="omnigent.runner.git_providers.azure_devops",
    )

    def matches_host(self, host: str, instances: Instances) -> bool:  # noqa: ARG002
        """Claim the default hosts and any ``*.visualstudio.com`` host."""
        # Configured hosts stay unclaimed: the URL parsers read only Azure DevOps Services hosts.
        host = host.lower()
        return bool(host) and (host in self.default_hosts or host.endswith(_LEGACY_SUFFIX))

    def parse_remote_url(self, url: str, instances: Instances) -> ParsedRemote | None:
        """Parse an HTTPS or SSH remote to ``org/project/repo`` on ``dev.azure.com``."""
        host = host_of(url)
        if host is None or not self.matches_host(host, instances):
            return None
        repo = parse_azure_devops_remote(url)
        if repo is None:
            return None
        return ParsedRemote(
            provider=self.id,
            host=_CANONICAL_HOST,
            repository=f"{repo.org}/{repo.project}/{repo.repo}",
        )

    def parse_pr_url(self, url: str, instances: Instances) -> ParsedPullRequest | None:
        """Normalize an Azure DevOps pull request URL, or return ``None``."""
        host = host_of(url)
        if host is None or not self.matches_host(host, instances):
            return None
        return parse_azure_devops_pr_url(url)


PROVIDER: GitProvider = AzureDevOpsProvider()
