"""Read the Forgejo and Gitea REST API (``/api/v1``) with the execution host's CLI logins.

A token goes only to the exact HTTPS authority of the forgejo-cli or tea login it
came from. Other owned hosts, such as Codeberg by default or configured hosts, are
read anonymously, which serves public repositories. Methods block; call them from
a worker thread.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Literal
from urllib.parse import quote

import httpx

from omnigent.git_providers import EnvInstances
from omnigent.git_providers.forgejo import ForgejoProvider, LoginFile, login_files
from omnigent.git_providers.gitlab import instance_authority
from omnigent.util.tls import client_ssl_context

REQUEST_BUDGET_SECONDS = 8.0
# Forgejo caps list pages at its MAX_RESPONSE_ITEMS, 50 by default.
_PAGE_SIZE = 50
_MAX_PAGES = 10

LoginSource = Literal["forgejo-cli", "tea", "configured"]


@dataclass(frozen=True)
class ForgejoLogin:
    """How Omnigent reaches one Forgejo authority; ``token`` is ``None`` for anonymous reads."""

    authority: str
    token: str | None = field(repr=False)
    source: LoginSource


def _fj_logins(text: str) -> list[ForgejoLogin]:
    hosts = json.loads(text).get("hosts")
    found = []
    for host, login in hosts.items() if isinstance(hosts, dict) else ():
        token = login.get("token") if isinstance(login, dict) else None
        authority = instance_authority(host) if isinstance(host, str) else None
        if authority and isinstance(token, str) and token:
            found.append(ForgejoLogin(authority, token, "forgejo-cli"))
    return found


def _tea_logins(text: str) -> list[ForgejoLogin]:
    import yaml

    config = yaml.safe_load(text)
    entries = config.get("logins") if isinstance(config, dict) else None
    found = []
    for login in entries if isinstance(entries, list) else ():
        if not isinstance(login, dict) or login.get("auth_method") == "oauth":
            # OAuth logins keep their current token in tea's credential store.
            continue
        url, token = login.get("url"), login.get("token")
        authority = instance_authority(url) if isinstance(url, str) else None
        if authority and isinstance(token, str) and token:
            found.append(ForgejoLogin(authority, token, "tea"))
    return found


@lru_cache(maxsize=4)
def _cli_logins(file: LoginFile) -> tuple[ForgejoLogin, ...]:
    import yaml

    try:
        text = file.path.read_text(encoding="utf-8")
        return tuple(_tea_logins(text) if file.tool == "tea" else _fj_logins(text))
    except (OSError, UnicodeError, ValueError, AttributeError, yaml.YAMLError):
        return ()


def logins() -> dict[str, ForgejoLogin]:
    """Return every owned authority with the login that reaches it; forgejo-cli wins over tea."""
    table = {
        authority: ForgejoLogin(authority, None, "configured")
        for authority in ForgejoProvider().authorities(EnvInstances())
    }
    for file in login_files():
        for login in _cli_logins(file):
            if login.authority in table and table[login.authority].token is None:
                table[login.authority] = login
    return table


class ForgejoError(ValueError):
    """A request failed; the message is user-facing and never holds a token.

    :ivar status: The HTTP status, or ``0`` when no response arrived.
    """

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class ForgejoTimeoutError(ForgejoError):
    """A request exhausted the shared deadline."""


def _json(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise ForgejoError("Forgejo returned an invalid API response.") from exc


def _authority(url: httpx.URL) -> str:
    host = f"[{url.host}]" if ":" in url.host else url.host
    return host if url.port in {None, 443} else f"{host}:{url.port}"


class ForgejoClient:
    """Read-only requests to one authority, bounded by one panel request's deadline.

    Use it as a context manager so its connections close.
    """

    def __init__(
        self,
        host: str,
        *,
        deadline: float | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        authority = instance_authority(host)
        login = logins().get(authority) if authority else None
        if login is None:
            raise ForgejoError(
                "Add this Forgejo instance with fj, tea, or "
                "OMNIGENT_GIT_PROVIDER_FORGEJO_HOSTS on the execution host."
            )
        self.login = login
        self.deadline = (
            deadline if deadline is not None else time.monotonic() + REQUEST_BUDGET_SECONDS
        )
        headers = {"Accept": "application/json"}
        if login.token:
            headers["Authorization"] = f"token {login.token}"
        self._http = httpx.Client(
            base_url=f"https://{login.authority}/api/v1/",
            headers=headers,
            verify=client_ssl_context(),
            follow_redirects=False,
            transport=transport,
        )

    def __enter__(self) -> ForgejoClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._http.close()

    def _send(
        self, segments: Sequence[str | int], params: Mapping[str, str | int]
    ) -> httpx.Response:
        """GET percent-encoded ``segments`` on this client's authority only."""
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ForgejoTimeoutError("Forgejo request timed out. Refresh to retry.")
        if any(str(segment) in {"", ".", ".."} for segment in segments):
            raise ForgejoError("Invalid Forgejo API path.")
        path = "/".join(quote(str(segment), safe="") for segment in segments)
        request = self._http.build_request("GET", path, params=params, timeout=remaining)
        if request.url.scheme != "https" or _authority(request.url) != self.login.authority:
            raise ForgejoError("Refusing to send a Forgejo request to another host.")
        try:
            response = self._http.send(request)
        except httpx.TimeoutException as exc:
            raise ForgejoTimeoutError("Forgejo request timed out. Refresh to retry.") from exc
        except httpx.HTTPError as exc:
            raise ForgejoError(f"Could not reach Forgejo at {self.login.authority}.") from exc
        if response.status_code in {401, 403, 404}:
            raise ForgejoError(
                "Forgejo could not find this resource, or this login cannot read it.",
                response.status_code,
            )
        if not response.is_success:
            raise ForgejoError(
                f"Forgejo returned HTTP {response.status_code}.", response.status_code
            )
        return response

    def object(self, *segments: str | int, **params: str | int) -> dict[str, Any]:
        """GET a JSON object, rejecting any other shape."""
        value = _json(self._send(segments, params))
        if not isinstance(value, dict):
            raise ForgejoError("Forgejo returned an invalid object response.")
        return value

    def text(self, *segments: str | int) -> str:
        """GET a text resource such as a ``.diff``."""
        return self._send(segments, {}).text

    def raw(self, *segments: str | int, **params: str | int) -> bytes:
        """GET raw file bytes."""
        return self._send(segments, params).content

    def items(self, *segments: str | int) -> list[dict[str, Any]]:
        """GET a whole unpaginated JSON list, such as an issue's comments."""
        values = _json(self._send(segments, {}))
        if not isinstance(values, list) or any(not isinstance(v, dict) for v in values):
            raise ForgejoError("Forgejo returned an invalid list response.")
        return values

    def pages(
        self, *segments: str | int, **params: str | int
    ) -> tuple[list[dict[str, Any]], bool]:
        """Read bounded list pages, reporting a partial list instead of dropping it."""
        values: list[dict[str, Any]] = []
        for page in range(1, _MAX_PAGES + 1):
            try:
                response = self._send(segments, {**params, "page": page, "limit": _PAGE_SIZE})
                batch = _json(response)
                if not isinstance(batch, list) or any(not isinstance(v, dict) for v in batch):
                    raise ForgejoError("Forgejo returned an invalid list response.")
            except ForgejoError:
                if values:
                    return values, True
                raise
            values.extend(batch)
            more = response.headers.get("X-HasMore")
            if not batch or (more == "false" if more else len(batch) < _PAGE_SIZE):
                return values, False
        return values, True
