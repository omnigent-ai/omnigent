"""The GitHub host the GitHub App connection talks to.

github.com by default. A GitHub Enterprise Server deployment sets
``OMNIGENT_GITHUB_HOST`` (and, when its API is not at the usual
``https://<host>/api/v3``, ``OMNIGENT_GITHUB_API_URL``) on the server. Everything
that serves the connection reads it from here: the OAuth and API endpoints, the
repository picker, and the credential-helper scope and ``gh`` host that managed
sandboxes are given. Sandboxes don't read the environment themselves; the server
reports a non-default host in the credential broker's payload.

Standard library only (like the rest of :mod:`omnigent.git_providers`), so the
server, the host and the sandbox's credential helper can all import it.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

HOST_ENV_VAR = "OMNIGENT_GITHUB_HOST"
API_URL_ENV_VAR = "OMNIGENT_GITHUB_API_URL"

DEFAULT_HOST = "github.com"
DEFAULT_API_URL = "https://api.github.com"

# A DNS name (letters, digits, hyphens, dots) with an optional port. No scheme,
# path, userinfo or whitespace: the value becomes part of URLs and of a git
# config key, so anything else is a misconfiguration.
_HOST_RE = re.compile(
    r"^(?=.{1,253}(?::\d{1,5})?$)"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*"
    r"(?::\d{1,5})?$"
)


@dataclass(frozen=True)
class GitHubHost:
    """Where the GitHub connection's web and API endpoints live.

    :param host: The web host, e.g. ``"github.com"`` or ``"ghe.example.com"``.
    :param api_url: The REST API base without a trailing slash, e.g.
        ``"https://api.github.com"`` or ``"https://ghe.example.com/api/v3"``.
    """

    host: str = DEFAULT_HOST
    api_url: str = DEFAULT_API_URL

    @property
    def web_url(self) -> str:
        """The web base URL, e.g. ``"https://github.com"``."""
        return f"https://{self.host}"

    @property
    def is_default(self) -> bool:
        """Whether this is github.com with its usual API."""
        return self.host == DEFAULT_HOST and self.api_url == DEFAULT_API_URL


def normalize_host(value: str | None) -> str | None:
    """Return *value* as a lower-cased host name, or ``None`` when it isn't one.

    :param value: A candidate host, e.g. ``"GHE.example.com"``.
    :returns: ``"ghe.example.com"``, or ``None`` for an empty or invalid value
        (a URL, a path, userinfo, whitespace).
    """
    if not value:
        return None
    candidate = value.strip().lower()
    if not _HOST_RE.match(candidate):
        return None
    return candidate


def _normalize_api_url(value: str) -> str:
    parts = urlsplit(value.strip())
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError(f"{API_URL_ENV_VAR} must be an https:// URL, got {value!r}")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError(
            f"{API_URL_ENV_VAR} must not carry credentials, a query or a fragment: {value!r}"
        )
    if normalize_host(parts.netloc) is None:
        raise ValueError(f"{API_URL_ENV_VAR} has an invalid host: {value!r}")
    return f"https://{parts.netloc.lower()}{parts.path.rstrip('/')}"


def parse_github_host(host: str | None, api_url: str | None) -> GitHubHost:
    """Validate a configured host and API URL.

    An unset host means github.com. When only the host is set to something
    other than github.com, the API defaults to ``https://<host>/api/v3``, where
    GitHub Enterprise Server serves it.

    :param host: The configured web host, or ``None``/empty for github.com.
    :param api_url: The configured API base URL, or ``None``/empty to derive it.
    :returns: The validated :class:`GitHubHost`.
    :raises ValueError: When either value is malformed.
    """
    if host and host.strip():
        resolved_host = normalize_host(host)
        if resolved_host is None:
            raise ValueError(
                f"{HOST_ENV_VAR} must be a bare host name such as ghe.example.com, got {host!r}"
            )
    else:
        resolved_host = DEFAULT_HOST
    if api_url and api_url.strip():
        resolved_api = _normalize_api_url(api_url)
    elif resolved_host == DEFAULT_HOST:
        resolved_api = DEFAULT_API_URL
    else:
        resolved_api = f"https://{resolved_host}/api/v3"
    return GitHubHost(host=resolved_host, api_url=resolved_api)


def github_host_from_env(env: Mapping[str, str] | None = None) -> GitHubHost:
    """Read :data:`HOST_ENV_VAR` and :data:`API_URL_ENV_VAR`.

    :param env: The environment to read, defaulting to :data:`os.environ`.
    :returns: The configured host; github.com when neither is set.
    :raises ValueError: When either value is malformed.
    """
    source = os.environ if env is None else env
    return parse_github_host(source.get(HOST_ENV_VAR), source.get(API_URL_ENV_VAR))
