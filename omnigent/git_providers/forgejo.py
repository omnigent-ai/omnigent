"""Forgejo, Gitea, and Codeberg URLs, including instances from forgejo-cli and tea logins."""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from omnigent.git_providers import (
    FacetModules,
    GitProvider,
    Instances,
    ParsedPullRequest,
    ParsedRemote,
    instance_authority,
)

_NAME = r"[A-Za-z0-9_.-]+"
_REPOSITORY = re.compile(rf"{_NAME}/{_NAME}")
_PULL = re.compile(rf"/({_NAME}/{_NAME})/pulls/([1-9][0-9]*)(?:/(?:files|commits))?/?")
_SCP = re.compile(r"^[\w.-]+@([^:/]+):(.+)$")
# tea writes one ``url:`` key per login; no other section of its config has one.
_TEA_URL = re.compile(r"""^\s*(?:-\s+)?url:\s*["']?([^"'\s]+)["']?\s*$""")

LoginTool = Literal["forgejo-cli", "tea"]


@dataclass(frozen=True)
class LoginFile:
    """A CLI's saved-login file; its stat makes it a cache key that changes on login."""

    tool: LoginTool
    path: Path
    mtime_ns: int
    size: int


def _fj_keys_paths(home: Path) -> list[Path]:
    """forgejo-cli's data directory, then the legacy one it still reads."""
    if sys.platform == "darwin":
        base = home / "Library/Application Support"
        return [base / "forgejo-cli.forgejo-cli/keys.json", base / "Cyborus.forgejo-cli/keys.json"]
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or home / "AppData/Roaming")
        return [
            base / "forgejo-cli/forgejo-cli/data/keys.json",
            base / "Cyborus/forgejo-cli/data/keys.json",
        ]
    data_home = Path(os.environ.get("XDG_DATA_HOME") or "")
    return [
        (data_home if data_home.is_absolute() else home / ".local/share") / "forgejo-cli/keys.json"
    ]


def _tea_config_paths(home: Path) -> list[Path]:
    """tea's XDG config file, then its legacy ``~/.tea/tea.yml``."""
    config_home = Path(os.environ.get("XDG_CONFIG_HOME") or "")
    if not config_home.is_absolute():
        if sys.platform == "darwin":
            config_home = home / "Library/Application Support"
        elif sys.platform == "win32":
            config_home = Path(os.environ.get("LOCALAPPDATA") or home / "AppData/Local")
        else:
            config_home = home / ".config"
    return [config_home / "tea/config.yml", home / ".tea/tea.yml"]


def login_files() -> tuple[LoginFile, ...]:
    """Return the login file each CLI would read, skipping a CLI that has none."""
    try:
        home = Path.home()
    except RuntimeError:
        return ()
    found = []
    for tool, paths in (
        ("forgejo-cli", _fj_keys_paths(home)),
        ("tea", _tea_config_paths(home)),
    ):
        for path in paths:
            try:
                info = path.stat()
            except OSError:
                continue
            found.append(LoginFile(tool, path, info.st_mtime_ns, info.st_size))
            break
    return tuple(found)


@lru_cache(maxsize=4)
def _login_authorities(file: LoginFile) -> frozenset[str]:
    """Read the HTTPS authorities a CLI has logins for, never their tokens."""
    try:
        text = file.path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return frozenset()
    if file.tool == "tea":
        values = [match[1] for line in text.splitlines() if (match := _TEA_URL.match(line))]
    else:
        try:
            hosts = json.loads(text).get("hosts")
        except (ValueError, AttributeError):
            return frozenset()
        # fj keys a login by host, port, and path, without a scheme.
        values = list(hosts) if isinstance(hosts, dict) else []
    return frozenset(
        authority
        for value in values
        if isinstance(value, str) and (authority := instance_authority(value))
    )


class ForgejoProvider:
    """Codeberg and any instance a forgejo-cli or tea login names; Gitea shares the API."""

    id = "forgejo"
    display_name = "Forgejo"
    request_name = "pull request"
    number_prefix = "#"
    default_hosts = ("codeberg.org",)
    facets = FacetModules(pull_requests="omnigent.runner.git_providers.forgejo")

    def authorities(self, instances: Instances) -> frozenset[str]:
        """Return the HTTPS authorities this provider owns, without reading any token."""
        values = [*self.default_hosts, *instances.hosts_for(self.id)]
        for file in login_files():
            values.extend(_login_authorities(file))
        return frozenset(host for value in values if (host := instance_authority(value)))

    def matches_host(self, host: str, instances: Instances) -> bool:
        """Match the complete HTTPS authority, including a nondefault port."""
        return instance_authority(host) in self.authorities(instances)

    def parse_remote_url(self, url: str, instances: Instances) -> ParsedRemote | None:
        """Resolve an HTTPS remote by authority, or an SSH remote by its unique hostname."""
        candidate = url.strip()
        if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in candidate):
            return None
        authorities = self.authorities(instances)
        scp = _SCP.fullmatch(candidate)
        try:
            parsed = urlsplit(candidate)
            if parsed.password is not None or parsed.query or parsed.fragment:
                return None
            if parsed.scheme == "https" and not scp:
                authority = instance_authority(f"https://{parsed.netloc.rsplit('@', 1)[-1]}")
                path = parsed.path
            else:
                if scp:
                    ssh_host, path = scp[1].lower(), scp[2]
                elif parsed.scheme == "ssh" and parsed.hostname:
                    ssh_host, path = parsed.hostname.lower(), parsed.path
                else:
                    return None
                matches = [a for a in authorities if urlsplit(f"https://{a}").hostname == ssh_host]
                authority = matches[0] if len(matches) == 1 else None
        except ValueError:
            return None
        repository = path.strip("/").removesuffix(".git")
        if authority is None or authority not in authorities or not _valid_repository(repository):
            return None
        return ParsedRemote(provider=self.id, host=authority, repository=repository.lower())

    def parse_pr_url(self, url: str, instances: Instances) -> ParsedPullRequest | None:
        """Parse ``/owner/repo/pulls/N`` on an owned authority, without credentials."""
        candidate = url.strip()
        if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in candidate):
            return None
        try:
            parsed = urlsplit(candidate)
            authority = instance_authority(f"{parsed.scheme}://{parsed.netloc}")
            match = _PULL.fullmatch(parsed.path)
            if (
                authority is None
                or match is None
                or not _valid_repository(match[1])
                or authority not in self.authorities(instances)
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
            url=f"https://{authority}/{repository}/pulls/{number}",
        )


def _valid_repository(path: str) -> bool:
    return _REPOSITORY.fullmatch(path) is not None and all(
        part not in {".", ".."} for part in path.split("/")
    )


PROVIDER: GitProvider = ForgejoProvider()
