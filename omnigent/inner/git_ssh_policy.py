"""Validate exact repository-scoped Git SSH bindings."""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path

from omnigent.errors import OmnigentError

from .datamodel import GitSshBinding

_GIT_SSH_HOST = re.compile(
    r"\A(?=.{1,253}\Z)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z"
)
_GIT_SSH_COMPONENT = re.compile(r"\A[A-Za-z0-9_.-]+\Z")
_GIT_SSH_USER = re.compile(r"\A[A-Za-z_][A-Za-z0-9_.-]{0,63}\Z")


def parse_git_ssh_bindings(raw: object) -> list[GitSshBinding] | None:
    """Parse exact Git-over-SSH bindings with no shell-interpreted fields."""
    if raw is None:
        return None
    if not isinstance(raw, list) or not raw:
        raise OmnigentError("os_env.sandbox.git_ssh must be a non-empty list")
    bindings: list[GitSshBinding] = []
    seen: set[tuple[str, int, str, str]] = set()
    fields = {
        "host",
        "port",
        "username",
        "repository",
        "operations",
        "identity_file",
        "known_hosts_file",
        "allowed_cidrs",
        "allow_loopback",
    }
    for index, entry in enumerate(raw):
        label = f"os_env.sandbox.git_ssh[{index}]"
        if not isinstance(entry, dict) or set(entry) - fields:
            raise OmnigentError(f"{label} must be a mapping with supported fields only")
        host = entry.get("host")
        if not isinstance(host, str) or not _GIT_SSH_HOST.fullmatch(host):
            raise OmnigentError(f"{label}.host must be an exact ASCII DNS name or IPv4 address")
        host = host.lower()
        port = entry.get("port", 22)
        if type(port) is not int or not 1 <= port <= 65535:
            raise OmnigentError(f"{label}.port must be an integer from 1 to 65535")
        username = entry.get("username", "git")
        if not isinstance(username, str) or not _GIT_SSH_USER.fullmatch(username):
            raise OmnigentError(f"{label}.username is invalid")
        repository = entry.get("repository")
        if not isinstance(repository, str) or not repository:
            raise OmnigentError(f"{label}.repository must be a non-empty string")
        components = repository.lstrip("/").split("/")
        if repository.startswith(("//", "-")) or any(
            part in ("", ".", "..") or not _GIT_SSH_COMPONENT.fullmatch(part)
            for part in components
        ):
            raise OmnigentError(f"{label}.repository contains an unsafe path component")
        operations = entry.get("operations", ["fetch"])
        if (
            not isinstance(operations, list)
            or not operations
            or any(op not in ("fetch", "push") for op in operations)
        ):
            raise OmnigentError(f"{label}.operations must contain fetch and/or push")
        paths: dict[str, str] = {}
        for key in ("identity_file", "known_hosts_file"):
            value = entry.get(key)
            if not isinstance(value, str) or not Path(value).expanduser().is_absolute():
                raise OmnigentError(f"{label}.{key} must be an absolute path")
            paths[key] = str(Path(value).expanduser())
        raw_cidrs = entry.get("allowed_cidrs", [])
        if not isinstance(raw_cidrs, list) or any(not isinstance(v, str) for v in raw_cidrs):
            raise OmnigentError(f"{label}.allowed_cidrs must be a list of CIDRs")
        cidrs: list[str] = []
        for value in raw_cidrs:
            try:
                cidrs.append(str(ipaddress.ip_network(value, strict=True)))
            except ValueError as exc:
                raise OmnigentError(
                    f"{label}.allowed_cidrs contains invalid CIDR {value!r}"
                ) from exc
        allow_loopback = entry.get("allow_loopback", False)
        if type(allow_loopback) is not bool:
            raise OmnigentError(f"{label}.allow_loopback must be a boolean")
        if allow_loopback:
            try:
                local_host = ipaddress.ip_address(host)
            except ValueError as exc:
                raise OmnigentError(
                    f"{label}.allow_loopback requires a loopback IP literal"
                ) from exc
            exact_cidr = f"{local_host}/{local_host.max_prefixlen}"
            if not local_host.is_loopback or cidrs != [exact_cidr]:
                raise OmnigentError(f"{label}.allow_loopback requires the exact host CIDR")
        key = (host, port, username, repository)
        if key in seen:
            raise OmnigentError(f"{label} duplicates a Git SSH binding")
        seen.add(key)
        bindings.append(
            GitSshBinding(
                host=host,
                port=port,
                username=username,
                repository=repository,
                operations=frozenset(operations),
                identity_file=paths["identity_file"],
                known_hosts_file=paths["known_hosts_file"],
                allowed_cidrs=tuple(cidrs),
                allow_loopback=allow_loopback,
            )
        )
    return bindings
