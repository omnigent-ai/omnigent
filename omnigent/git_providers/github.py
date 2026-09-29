"""GitHub provider descriptor, covering github.com and GitHub Enterprise Server."""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlsplit

from omnigent.git_providers import (
    FacetModules,
    GitProvider,
    Instances,
    ParsedPullRequest,
    ParsedRemote,
)

# An unindented key in the gh CLI's hosts.yml names a host gh is signed in to.
_HOSTS_YML_KEY = re.compile(r"^([A-Za-z0-9.-]+):\s*$")
_SCP_REMOTE = re.compile(r"^[\w.\-]+@(?P<host>[\w.\-]+):(?P<path>.+)$")
# URL-style remotes may name a port, e.g. ``ssh://git@ghe.example.com:7999/o/r.git``.
_URL_REMOTE = re.compile(r"^\w+://(?:[^@/]+@)?(?P<host>[\w.\-]+)(?::\d+)?/(?P<path>.+)$")


def _valid_hostname(host: str) -> bool:
    """Validate bounded ASCII DNS labels without hostname regex backtracking."""
    if len(host) > 253 or not host.isascii():
        return False
    labels = host.split(".")
    return (
        len(labels) > 1
        and all(
            1 <= len(label) <= 63
            and label[0].isalnum()
            and label[-1].isalnum()
            and label.replace("-", "").isalnum()
            for label in labels
        )
        and labels[-1][0].isalpha()
    )


def _gh_config_dir() -> str:
    """Return the gh CLI config dir, chosen in gh's own order.

    ``GH_CONFIG_DIR``, else ``$XDG_CONFIG_HOME/gh``, else ``%AppData%\\GitHub CLI``
    on Windows, else ``~/.config/gh``.

    :raises RuntimeError: When the home directory is needed but unknown.
    """
    override = (os.environ.get("GH_CONFIG_DIR") or "").strip()
    if override:
        return override
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        return os.path.join(xdg_config_home, "gh")
    app_data = os.environ.get("APPDATA")
    if os.name == "nt" and app_data:
        return os.path.join(app_data, "GitHub CLI")
    return os.path.join(Path.home(), ".config", "gh")


def _read_gh_hosts(hosts_path: str) -> frozenset[str]:
    """Return the lower-cased top-level host keys of the ``hosts.yml`` at *hosts_path*.

    An unreadable file names no hosts.
    """
    try:
        with open(hosts_path, encoding="utf-8", errors="replace") as hosts_file:
            text = hosts_file.read()
    except OSError:
        return frozenset()
    return frozenset(
        match[1].lower() for line in text.splitlines() if (match := _HOSTS_YML_KEY.match(line))
    )


# The last parsed ``hosts.yml`` with the ``(path, mtime_ns, size)`` it was read at. A missing
# file has ``(path, None, None)``. The tuple is replaced whole, so threads need no lock.
_hosts_cache: tuple[tuple[str, int | None, int | None], frozenset[str]] | None = None


def _gh_signed_in_hosts() -> frozenset[str]:
    """Return the lower-cased top-level host keys of gh's ``hosts.yml``, if readable.

    Every URL that resolves to another provider asks GitHub first, so the parsed set is
    reused until the file's path, modification time, or size changes. A call that finds
    the file unchanged costs one ``os.stat``. A missing or unreadable file names no hosts
    until it appears or changes.
    """
    global _hosts_cache
    try:
        hosts_path = os.path.join(_gh_config_dir(), "hosts.yml")
    except RuntimeError:
        # Path.home() found no home directory.
        return frozenset()
    try:
        info = os.stat(hosts_path)
    except OSError:
        signature: tuple[str, int | None, int | None] = (hosts_path, None, None)
    else:
        signature = (hosts_path, info.st_mtime_ns, info.st_size)
    cached = _hosts_cache
    if cached is not None and cached[0] == signature:
        return cached[1]
    hosts = frozenset() if signature[1] is None else _read_gh_hosts(hosts_path)
    _hosts_cache = (signature, hosts)
    return hosts


class GitHubProvider:
    """github.com, plus GitHub Enterprise Server hosts that gh or Omnigent knows."""

    id = "github"
    display_name = "GitHub"
    default_hosts = ("github.com",)
    facets = FacetModules(
        pull_requests="omnigent.runner.git_providers.github",
    )

    def matches_host(self, host: str, instances: Instances) -> bool:
        """Claim github.com, ``GH_HOST``, configured instances, and gh's signed-in hosts.

        GitHub Enterprise Server users have their host in ``GH_HOST`` or in gh's
        ``hosts.yml``, so their remotes keep resolving to GitHub.
        """
        host = host.lower()
        if not host:
            return False
        if host in self.default_hosts:
            return True
        if host == (os.environ.get("GH_HOST") or "").strip().lower():
            return True
        if host in {configured.lower() for configured in instances.hosts_for(self.id)}:
            return True
        return host in _gh_signed_in_hosts()

    def parse_remote_url(self, url: str, instances: Instances) -> ParsedRemote | None:
        """Parse an HTTPS, SSH, or scp-style remote on a GitHub host to ``owner/repo``."""
        candidate = url.strip()
        match = _SCP_REMOTE.match(candidate) or _URL_REMOTE.match(candidate)
        if match is None:
            return None
        parts = match["path"].removesuffix(".git").strip("/").split("/")
        if len(parts) < 2 or not parts[-1] or not parts[-2]:
            return None
        host = match["host"].lower()
        if not self.matches_host(host, instances):
            return None
        return ParsedRemote(provider=self.id, host=host, repository=f"{parts[-2]}/{parts[-1]}")

    def parse_pr_url(
        self,
        url: str,
        instances: Instances,  # noqa: ARG002 - GitHub PR URLs parse on any host
    ) -> ParsedPullRequest | None:
        """Normalize an HTTPS GitHub PR URL, rejecting non-PR and credential-bearing URLs."""
        try:
            parsed = urlsplit(url.strip())
            host = (parsed.hostname or "").lower()
        except ValueError:
            return None
        match = re.fullmatch(
            r"/([\w.-]+/[\w.-]+)/pull/([1-9][0-9]*)(?:/(?:files|commits|checks))?/?",
            parsed.path,
            flags=re.ASCII,
        )
        if (
            parsed.scheme != "https"
            or not _valid_hostname(host)
            or parsed.netloc.lower() != host
            or match is None
        ):
            return None
        repository = match[1].lower()
        if any(part in {".", ".."} for part in repository.split("/")):
            return None
        try:
            number = int(match[2])
        except ValueError:
            # The number exceeds Python's integer string conversion limit.
            return None
        return ParsedPullRequest(
            provider=self.id,
            host=host,
            repository=repository,
            number=number,
            url=f"https://{host}/{repository}/pull/{number}",
        )


PROVIDER: GitProvider = GitHubProvider()
