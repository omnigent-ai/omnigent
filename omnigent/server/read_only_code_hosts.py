"""Code hosts whose pull requests the server serves read-only.

A server can list host patterns, such as ``"*.code.example.com"``, whose pull requests
it serves for reading only: their overviews offer no write capabilities, and the server
refuses their writes before they reach the session's host. A pattern is a host name,
matched case-insensitively, where ``*`` stands for any run of characters and every other
character, ``?`` and ``[`` included, stands for itself. A URL or a path, such as
``"https://code.example.com"`` or ``"code.example.com/team"``, is no host name and is
ignored. The server reads its config's
list when it starts, so a change takes a restart. Only the server's routes are limited:
an agent in the session that runs ``gh`` itself isn't.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

from omnigent.git_providers import EnvInstances, resolve_pr_url
from omnigent.server.pull_request_viewers import OWNER_CAPABILITIES, READ_CAPABILITIES

READ_ONLY_CODE_HOST_MESSAGE = "Changes to pull requests on this code host are turned off."
# The reason a refused write carries, for clients that tell it apart.
READ_ONLY_CODE_HOST_REASON = "read_only_code_host"
# Choosing the host's account or the checkout's remote changes nothing on the code host.
_HOST_ONLY_CAPABILITIES = frozenset({"account_switching", "base_remote_selection"})
# What a read-only host keeps; every other capability, an unknown one included, is turned off.
_KEPT_CAPABILITIES = READ_CAPABILITIES | _HOST_ONLY_CAPABILITIES
# The owner's capabilities that write to the code host.
CODE_HOST_WRITE_CAPABILITIES = OWNER_CAPABILITIES - _HOST_ONLY_CAPABILITIES


def is_host_pattern(entry: object) -> bool:
    """Whether ``entry`` is a host name pattern: text that isn't blank, a URL or a path."""
    return isinstance(entry, str) and bool(entry.strip()) and "/" not in entry


def parse_patterns(value: object) -> tuple[str, ...]:
    """The host patterns in ``value``, a list of strings; anything else reads as none."""
    if not isinstance(value, (list, tuple)):
        return ()
    patterns = (item.strip().casefold() for item in value if is_host_pattern(item))
    return tuple(dict.fromkeys(patterns))


def is_read_only_pull_request(pr_url: object, patterns: Iterable[str]) -> bool:
    """Whether ``pr_url`` is a pull request on a host matching one of ``patterns``.

    Checks the host as written and the host the session's host resolves it to, such as
    ``dev.azure.com`` for a legacy ``*.visualstudio.com`` URL. While any pattern is set, a
    URL whose host can't be told is read-only too. Resolution uses the providers the server
    knows, so a provider installed only on the session's host that maps a URL to another
    host isn't matched against that host.
    """
    patterns = tuple(patterns)
    if not patterns or not isinstance(pr_url, str) or not pr_url:
        return False
    hosts = _pull_request_hosts(pr_url)
    if not hosts:
        return True
    return any(_matches(host, pattern) for host in hosts for pattern in patterns)


def refuses_change(target: object, patterns: Iterable[str]) -> bool:
    """Whether a change to ``target``, the PR it names or the server's first, is refused.

    While any pattern is set, a change with no target is refused: the session's host would
    pick the pull request itself.
    """
    patterns = tuple(patterns)
    if not patterns:
        return False
    if not isinstance(target, str) or not target:
        return True
    return is_read_only_pull_request(target, patterns)


def _pull_request_hosts(pr_url: str) -> set[str]:
    """The host ``pr_url`` names, and the one provider resolution normalizes it to."""
    hosts: set[str] = set()
    stripped = pr_url.strip()
    try:
        written = urlsplit(stripped).hostname
    except ValueError:
        written = None
    if written:
        hosts.add(written.casefold())
    resolved = resolve_pr_url(stripped, EnvInstances())
    if resolved is not None and resolved.host:
        hosts.add(resolved.host.casefold())
    return hosts


def _matches(host: str, pattern: str) -> bool:
    """Whether ``host`` matches ``pattern``, where only ``*`` is a wildcard."""
    expression = ".*".join(re.escape(part) for part in pattern.split("*"))
    return re.fullmatch(expression, host) is not None


def mark_read_only(info: dict[str, Any]) -> dict[str, Any]:
    """An overview for a read-only pull request: ``read_only``, and only reads kept.

    Unknown capability names are turned off, since a newer host's may write to the code host.
    """
    marked = {**info, "read_only": True}
    capabilities = info.get("capabilities")
    if isinstance(capabilities, dict):
        marked["capabilities"] = {
            name: value if name in _KEPT_CAPABILITIES else False
            for name, value in capabilities.items()
        }
    return marked


def selected_pull_request(info: dict[str, Any]) -> object:
    """The pull request an overview is about: its selected URL, else its ``pr``'s."""
    selected = info.get("selected_pr_url")
    if isinstance(selected, str) and selected:
        return selected
    pr = info.get("pr")
    return pr.get("url") if isinstance(pr, dict) else None
