"""GitLab URLs, including nested projects on CLI-configured private instances."""

from __future__ import annotations

import ipaddress
import os
import re
import sys
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from omnigent.git_providers import (
    FacetModules,
    GitProvider,
    Instances,
    ParsedPullRequest,
    ParsedRemote,
)

_PROJECT = r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+"
_MR = re.compile(
    rf"/({_PROJECT})/-/merge_requests/([1-9][0-9]*)(?:/(?:diffs|commits|pipelines))?/?"
)
_SCP = re.compile(r"^[\w.-]+@([^:/]+):(.+)$")


def _glab_config_paths() -> list[Path]:
    """Follow glab's override, legacy, platform user, then system config order."""
    if override := os.environ.get("GLAB_CONFIG_DIR"):
        return [Path(override) / "config.yml"]
    home = Path.home()
    legacy = home / ".config"
    if sys.platform == "darwin":
        config_home = home / "Library/Application Support"
        config_dirs = [
            home / "Library/Preferences",
            Path("/Library/Application Support"),
            Path("/Library/Preferences"),
            legacy,
        ]
    elif sys.platform == "win32":
        config_home = Path(os.environ.get("LOCALAPPDATA") or home / "AppData/Local")
        config_dirs = [
            Path(os.environ.get("PROGRAMDATA") or "C:/ProgramData"),
            Path(os.environ.get("APPDATA") or home / "AppData/Roaming"),
        ]
    else:
        config_home, config_dirs = legacy, [Path("/etc/xdg")]
    if override := os.environ.get("XDG_CONFIG_HOME"):
        if Path(override).is_absolute():
            config_home = Path(override)
    if override := os.environ.get("XDG_CONFIG_DIRS"):
        config_dirs = [Path(p) for p in override.split(os.pathsep) if Path(p).is_absolute()]
    return [p / "glab-cli/config.yml" for p in dict.fromkeys([legacy, config_home, *config_dirs])]


@lru_cache(maxsize=1)
def _read_glab_hosts(path: Path, mtime_ns: int, size: int) -> frozenset[str]:
    """Read glab's host identities, retaining only HTTPS authorities."""
    import yaml

    del mtime_ns, size  # File metadata invalidates the cache after login/logout.
    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        return frozenset()
    configured_hosts = config.get("hosts") if isinstance(config, dict) else None
    if not isinstance(configured_hosts, dict):
        return frozenset()
    hosts = set()
    for host, values in configured_hosts.items():
        if not isinstance(host, str) or not isinstance(values, dict):
            continue
        protocol = values.get("api_protocol") or "https"
        if protocol == "https" and (authority := instance_authority(host)):
            hosts.add(authority)
    return frozenset(hosts)


def _glab_configured_hosts() -> frozenset[str]:
    """Follow CLI host changes without confusing a logout with an unknown forge."""
    try:
        for path in _glab_config_paths():
            try:
                info = path.stat()
            except OSError:
                continue
            return _read_glab_hosts(path, info.st_mtime_ns, info.st_size)
    except RuntimeError:
        pass  # No home directory is available.
    return frozenset()


def instance_authority(value: str) -> str | None:
    """Normalize an HTTPS origin/authority, retaining a non-default API port."""
    if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value):
        return None
    try:
        parsed = urlsplit(value if "://" in value else f"https://{value}")
        host, port = (parsed.hostname or "").lower(), parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return None
    if ":" in host:
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            return None
        host = f"[{host}]"
    elif not all(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", p) for p in host.split(".")
    ):
        return None
    return host if port in {None, 443} else f"{host}:{port}"


def _valid_project(path: str) -> bool:
    return re.fullmatch(_PROJECT, path) is not None and all(
        p not in {".", ".."} for p in path.split("/")
    )


class GitLabProvider:
    """GitLab.com and CLI-configured instances; OAuth configuration is unnecessary."""

    id = "gitlab"
    display_name = "GitLab"
    request_name = "merge request"
    number_prefix = "!"
    default_hosts = ("gitlab.com",)
    facets = FacetModules(pull_requests="omnigent.runner.git_providers.gitlab")

    def authorities(self, instances: Instances) -> frozenset[str]:
        """Return configured origins without retaining credentials."""
        values = [*self.default_hosts, *instances.hosts_for(self.id)]
        values.extend(os.environ.get(name, "") for name in ("GITLAB_HOST", "GLAB_HOST"))
        values.extend(_glab_configured_hosts())
        return frozenset(host for value in values if value and (host := instance_authority(value)))

    def matches_host(self, host: str, instances: Instances) -> bool:
        """Match the complete HTTPS authority, including its configured port."""
        return instance_authority(host) in self.authorities(instances)

    def parse_remote_url(self, url: str, instances: Instances) -> ParsedRemote | None:
        """Resolve HTTPS or SSH remotes without mistaking an SSH port for an API port."""
        candidate = url.strip()
        if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in candidate):
            return None
        scp = _SCP.fullmatch(candidate)
        authority = None
        try:
            parsed = urlsplit(candidate)
            if scp:
                ssh_host, path = scp[1].lower(), scp[2]
            elif (
                parsed.scheme == "ssh"
                and parsed.hostname
                and parsed.password is None
                and not parsed.query
                and not parsed.fragment
            ):
                ssh_host, path = parsed.hostname.lower(), parsed.path
            elif (
                parsed.scheme == "https"
                and parsed.password is None
                and not parsed.query
                and not parsed.fragment
            ):
                authority = instance_authority(f"https://{parsed.netloc.rsplit('@', 1)[-1]}")
                if authority not in self.authorities(instances):
                    return None
                path = parsed.path
                ssh_host = None
            else:
                return None
            if ssh_host is not None:
                matches = [
                    h
                    for h in self.authorities(instances)
                    if urlsplit(f"https://{h}").hostname == ssh_host
                ]
                if len(matches) != 1:
                    return None
                authority = matches[0]
        except ValueError:
            return None
        repository = path.strip("/").removesuffix(".git")
        if authority is None or not _valid_project(repository):
            return None
        return ParsedRemote(provider=self.id, host=authority, repository=repository.lower())

    def parse_pr_url(self, url: str, instances: Instances) -> ParsedPullRequest | None:
        """Parse an MR IID, not its instance-global ID, on a trusted instance."""
        if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url.strip()):
            return None
        try:
            parsed = urlsplit(url.strip())
            authority = instance_authority(f"{parsed.scheme}://{parsed.netloc}")
            match = _MR.fullmatch(parsed.path)
            if (
                authority is None
                or authority not in self.authorities(instances)
                or match is None
                or not _valid_project(match[1])
            ):
                return None
            number = int(match[2])
        except ValueError:
            return None
        repository = match[1].lower()
        return ParsedPullRequest(
            provider=self.id,
            host=authority,
            repository=repository,
            number=number,
            url=f"https://{authority}/{repository}/-/merge_requests/{number}",
        )


PROVIDER: GitProvider = GitLabProvider()
