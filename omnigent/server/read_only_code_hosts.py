"""Code hosts whose pull requests the server serves read-only.

A server can list host patterns, such as ``"*.code.example.com"``, whose pull requests
it serves for reading only: their overviews offer no write capabilities, and the server
refuses their writes before they reach the session's host. A pattern is a host name,
matched case-insensitively, where ``*`` stands for any run of characters.
"""

from __future__ import annotations

from collections.abc import Iterable
from fnmatch import fnmatchcase
from typing import Any
from urllib.parse import urlsplit

from omnigent.server.pull_request_viewers import OWNER_CAPABILITIES

READ_ONLY_CODE_HOST_MESSAGE = "Changes to pull requests on this code host are turned off."
# The reason a refused write carries, for clients that tell it apart.
READ_ONLY_CODE_HOST_REASON = "read_only_code_host"
# The owner's capabilities that write to the code host. Choosing the host's account or
# the checkout's remote changes nothing there, so a read-only host keeps those.
CODE_HOST_WRITE_CAPABILITIES = OWNER_CAPABILITIES - {"account_switching", "base_remote_selection"}


def parse_patterns(value: object) -> tuple[str, ...]:
    """The host patterns in ``value``, a list of strings; anything else reads as none."""
    if not isinstance(value, (list, tuple)):
        return ()
    patterns = (item.strip().casefold() for item in value if isinstance(item, str))
    return tuple(dict.fromkeys(pattern for pattern in patterns if pattern))


def is_read_only_pull_request(pr_url: object, patterns: Iterable[str]) -> bool:
    """Whether ``pr_url`` is a pull request on a host matching one of ``patterns``."""
    if not isinstance(pr_url, str) or not pr_url:
        return False
    try:
        host = urlsplit(pr_url).hostname
    except ValueError:
        return False
    return bool(host) and any(fnmatchcase(host.casefold(), pattern) for pattern in patterns)


def mark_read_only(info: dict[str, Any]) -> dict[str, Any]:
    """An overview for a read-only pull request: ``read_only``, and no write capabilities."""
    marked = {**info, "read_only": True}
    capabilities = info.get("capabilities")
    if isinstance(capabilities, dict):
        marked["capabilities"] = {
            name: False if name in CODE_HOST_WRITE_CAPABILITIES else value
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
