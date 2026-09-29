"""Azure DevOps Services REST client and credential chain.

Backs the pull request panel for repositories hosted on Azure DevOps. The client
takes the organization, project, and repository as arguments. Reading them from
a remote or pull request URL is the caller's job.

Only ``https://dev.azure.com`` is contacted. Corporate TLS proxies break
``vssps.dev.azure.com``, so this module never calls it and never resolves an
identity from a display name or an email address: :meth:`AzureDevOpsClient.connection_data`
returns the signed-in user's id directly. ``az devops`` and ``az repos`` are never
run. The only subprocess is ``az account get-access-token``.

:func:`resolve_token` and the client methods block. Call them from a worker thread.
"""

from __future__ import annotations

import base64
import json
import logging
import math
import os
import shutil
import subprocess
import threading
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote

import httpx

from omnigent.util.tls import client_ssl_context

_logger = logging.getLogger(__name__)

_ADO_HOST = "dev.azure.com"
_API_VERSION = "7.1"
# Preview-only resources reject a plain 7.1.
_PREVIEW_API_VERSION = "7.1-preview.1"
_PAGE_SIZE = 100
# Bound the change walk so a server that ignores $skip cannot loop forever.
_MAX_CHANGE_PAGES = 100
_MAX_ERROR_DETAIL_CHARS = 300

# Entra resource id of Azure DevOps.
_AZURE_DEVOPS_RESOURCE_ID = "499b84ac-1321-427f-aa17-267ca6975798"
_PAT_ENV = "AZURE_DEVOPS_EXT_PAT"
_TIMEOUT_ENV = "OMNIGENT_AZURE_DEVOPS_TIMEOUT_SECONDS"
_DEFAULT_TIMEOUT_SECONDS = 15.0
# Host processes are often launched without Homebrew on PATH.
_AZ_FALLBACK_PATHS = ("/opt/homebrew/bin/az", "/usr/local/bin/az")
_EXPIRY_SKEW_SECONDS = 300.0
_FAILURE_TTL_SECONDS = 60.0
# Cache lifetime for an az token whose expiry cannot be read.
_UNKNOWN_EXPIRY_TTL_SECONDS = 300.0


@dataclass(frozen=True)
class AzureToken:
    """An Azure DevOps credential.

    :ivar value: The bearer token or personal access token. Left out of ``repr``.
    :ivar kind: ``"bearer"`` for an Entra access token, ``"pat"`` for a personal access token.
    :ivar expires_at: Expiry in epoch seconds, or ``None`` when the credential does not expire.
    """

    value: str = field(repr=False)
    kind: Literal["bearer", "pat"]
    expires_at: float | None = None


def _timeout_seconds() -> float:
    """Return the request and ``az`` timeout, honoring the env override."""
    raw = os.environ.get(_TIMEOUT_ENV)
    if raw is not None:
        try:
            value = float(raw)
        except ValueError:
            value = 0.0
        if math.isfinite(value) and value > 0:
            return value
    return _DEFAULT_TIMEOUT_SECONDS


def _epoch(value: object) -> float | None:
    """Return ``value`` as epoch seconds when it is a finite number, else ``None``."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _token_from_file() -> AzureToken | None:
    """Read a bearer token from the token file, ignoring it when expired or malformed."""
    # Reserved for a future credential part that writes this file.
    try:
        path = Path.home() / ".config" / "omnigent" / "azure-devops" / "token.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, RuntimeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("access_token")
    expires_at = _epoch(payload.get("expires_at"))
    if not isinstance(value, str) or not value or expires_at is None or expires_at <= time.time():
        return None
    return AzureToken(value, "bearer", expires_at)


def _token_from_env() -> AzureToken | None:
    """Return the personal access token from ``AZURE_DEVOPS_EXT_PAT``, if set."""
    pat = os.environ.get(_PAT_ENV, "").strip()
    return AzureToken(pat, "pat") if pat else None


def _find_az() -> str | None:
    """Locate the ``az`` executable, trying the usual Homebrew paths after PATH."""
    found = shutil.which("az")
    if found:
        return found
    for candidate in _AZ_FALLBACK_PATHS:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _az_expiry(payload: dict[str, Any]) -> float | None:
    """Read the expiry from ``az`` output: ``expires_on`` (epoch) or ``expiresOn`` (local time)."""
    expires_on = _epoch(payload.get("expires_on"))
    if expires_on is not None:
        return expires_on
    local = payload.get("expiresOn")
    if not isinstance(local, str):
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return time.mktime(time.strptime(local, fmt))
        except (ValueError, OverflowError):
            continue
    return None


def _token_from_az_cli() -> AzureToken | None:
    """Ask ``az`` for an Entra access token. Any failure yields ``None``."""
    az = _find_az()
    if az is None:
        return None
    argv = [
        az,
        "account",
        "get-access-token",
        "--resource",
        _AZURE_DEVOPS_RESOURCE_ID,
        "-o",
        "json",
    ]
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=_timeout_seconds(), check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _logger.debug("azure_devops: az token request failed: %s", type(exc).__name__)
        return None
    if result.returncode != 0:
        _logger.debug("azure_devops: az token request exited with %s", result.returncode)
        return None
    try:
        payload = json.loads(result.stdout)
    except ValueError:
        _logger.debug("azure_devops: az token output is not JSON")
        return None
    value = payload.get("accessToken") if isinstance(payload, dict) else None
    if not isinstance(value, str) or not value:
        return None
    return AzureToken(value, "bearer", _az_expiry(payload))


@dataclass(frozen=True)
class _AzProbe:
    """One ``az`` probe result and the time it stops being reused."""

    token: AzureToken | None
    valid_until: float


_az_probe: _AzProbe | None = None
_az_lock = threading.Lock()


def _probe_valid_until(token: AzureToken | None, now: float) -> float:
    """Return when a probe result stops being reused."""
    if token is None:
        return now + _FAILURE_TTL_SECONDS
    if token.expires_at is None:
        return now + _UNKNOWN_EXPIRY_TTL_SECONDS
    return token.expires_at - _EXPIRY_SKEW_SECONDS


def _cached_az_token() -> AzureToken | None:
    """Return the ``az`` token, spawning ``az`` only when the cached result has lapsed."""
    global _az_probe
    with _az_lock:
        probe = _az_probe
        if probe is not None and time.time() < probe.valid_until:
            return probe.token
        token = _token_from_az_cli()
        _az_probe = _AzProbe(token, _probe_valid_until(token, time.time()))
        return token


def reset_token_cache() -> None:
    """Forget the cached ``az`` result so the next :func:`resolve_token` probes again."""
    global _az_probe
    with _az_lock:
        _az_probe = None


def resolve_token() -> AzureToken | None:
    """Return the first available Azure DevOps credential, or ``None``.

    Sources, first match wins:

    1. ``~/.config/omnigent/azure-devops/token.json`` (a bearer token, until it expires).
    2. The ``AZURE_DEVOPS_EXT_PAT`` environment variable (a personal access token).
    3. ``az account get-access-token`` for the Azure DevOps resource (a bearer token).

    The file and the variable are read on every call. The ``az`` result is reused until
    5 minutes before its expiry, and a failed probe is not repeated for 60 seconds.
    Blocks while ``az`` runs.

    :returns: The credential, or ``None`` when no source has one.
    """
    return _token_from_file() or _token_from_env() or _cached_az_token()


class AzureDevOpsError(Exception):
    """A request to Azure DevOps failed.

    :ivar status: The HTTP status code, or ``0`` when no request was sent.
    """

    def __init__(self, status: int, message: str = "") -> None:
        super().__init__(message or f"Azure DevOps request failed with status {status}")
        self.status = status


def _authorization(token: AzureToken) -> str:
    """Return the ``Authorization`` header value for ``token``."""
    if token.kind == "bearer":
        return f"Bearer {token.value}"
    if token.kind == "pat":
        encoded = base64.b64encode(f":{token.value}".encode()).decode("ascii")
        return f"Basic {encoded}"
    raise ValueError(f"unknown Azure DevOps token kind: {token.kind!r}")


def _error_detail(response: httpx.Response) -> str:
    """Return the message Azure DevOps put in an error body, or an empty string."""
    try:
        body = response.json()
    except ValueError:
        return ""
    message = body.get("message") if isinstance(body, dict) else None
    return message[:_MAX_ERROR_DETAIL_CHARS] if isinstance(message, str) else ""


def _parse(response: httpx.Response) -> Any:
    """Return the JSON body of ``response``.

    :raises AzureDevOpsError: On a non-2xx status, or a body that is not JSON. A rejected
        credential can come back as an HTML sign-in page with a 2xx status.
    """
    status = response.status_code
    if not response.is_success:
        detail = _error_detail(response)
        raise AzureDevOpsError(
            status, f"Azure DevOps returned {status}" + (f": {detail}" if detail else "")
        )
    try:
        return response.json()
    except ValueError as exc:
        raise AzureDevOpsError(
            status, f"Azure DevOps returned a non-JSON response (status {status})"
        ) from exc


def _list_field(payload: Any, name: str) -> list[dict[str, Any]]:
    """Read a list response without treating a malformed body as a successful empty list."""
    items = payload.get(name) if isinstance(payload, dict) else None
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise AzureDevOpsError(0, "Azure DevOps returned an invalid list response")
    return items


def _pull_request_segments(project: str, repo: str, pr_id: int) -> list[str | int]:
    """Return the path segments of a pull request resource."""
    return [project, "_apis", "git", "repositories", repo, "pullrequests", pr_id]


class AzureDevOpsClient:
    """Read-only client for the Azure DevOps Services REST API.

    Every request goes to ``https://dev.azure.com/{org}``. An Entra token is sent as
    ``Authorization: Bearer`` and a personal access token as ``Authorization: Basic``.
    Methods return parsed JSON. A non-2xx response raises :class:`AzureDevOpsError`.
    Transport failures raise :class:`httpx.HTTPError` unchanged. ``timeout`` replaces
    the configured request timeout, in seconds. With ``deadline``, a ``time.monotonic()``
    value, no request's timeout outlasts it, and a request made after it raises
    :class:`httpx.TimeoutException` without being sent. One client can serve several
    threads at once.
    """

    def __init__(
        self,
        org: str,
        token: AzureToken,
        *,
        timeout: float | None = None,
        deadline: float | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._timeout = _timeout_seconds() if timeout is None else timeout
        self._deadline = deadline
        self._client = httpx.Client(
            base_url=f"https://{_ADO_HOST}/{quote(org, safe='')}",
            headers={"Authorization": _authorization(token), "Accept": "application/json"},
            verify=client_ssl_context(),
            timeout=self._timeout,
            follow_redirects=False,
            # Injectable transport for tests (httpx.MockTransport); None uses
            # the real network.
            transport=transport,
        )

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._client.close()

    def __enter__(self) -> AzureDevOpsClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _send(
        self,
        segments: Sequence[str | int],
        params: Sequence[tuple[str, str | int]] = (),
        *,
        api_version: str = _API_VERSION,
    ) -> httpx.Response:
        """GET ``segments`` under the organization URL and return the raw response.

        Each segment is percent-encoded, so an argument cannot add path structure. The
        request is refused before it is sent unless it targets ``https://dev.azure.com``.

        :raises AzureDevOpsError: With status ``0`` when the URL is not on ``dev.azure.com``.
        :raises httpx.TimeoutException: When the deadline has passed; nothing is sent.
        """
        path = "/".join(quote(str(segment), safe="") for segment in segments)
        timeout = self._timeout
        if self._deadline is not None:
            timeout = min(timeout, self._deadline - time.monotonic())
        request = self._client.build_request(
            "GET",
            path,
            params=[*params, ("api-version", api_version)],
            timeout=max(timeout, 0.0),
        )
        if request.url.scheme != "https" or request.url.host != _ADO_HOST:
            raise AzureDevOpsError(0, f"Refusing to send a request outside https://{_ADO_HOST}")
        if timeout <= 0:
            raise httpx.TimeoutException("The request deadline has passed", request=request)
        return self._client.send(request, follow_redirects=False)

    def _get_json(
        self,
        segments: Sequence[str | int],
        params: Sequence[tuple[str, str | int]] = (),
        *,
        api_version: str = _API_VERSION,
    ) -> Any:
        """GET ``segments`` and return the parsed JSON body of a 2xx response."""
        return _parse(self._send(segments, params, api_version=api_version))

    def connection_data(self) -> dict[str, Any]:
        """Return the signed-in user's connection data.

        Callers read ``authenticatedUser.id`` and ``providerDisplayName``.

        :raises AzureDevOpsError: On a non-2xx response.
        """
        return self._get_json(["_apis", "connectionData"], api_version=_PREVIEW_API_VERSION)

    def find_pull_requests(
        self, project: str, repo: str, source_branch: str, status: str = "all"
    ) -> list[dict[str, Any]]:
        """List the pull requests whose source branch is ``source_branch``.

        :param source_branch: The short branch name, without ``refs/heads/``.
        :param status: ``active``, ``completed``, ``abandoned``, or ``all``.
        :raises AzureDevOpsError: On a non-2xx response.
        """
        payload = self._get_json(
            [project, "_apis", "git", "repositories", repo, "pullrequests"],
            [
                ("searchCriteria.sourceRefName", f"refs/heads/{source_branch}"),
                ("searchCriteria.status", status),
            ],
        )
        return _list_field(payload, "value")

    def get_pull_request(self, project: str, repo: str, pr_id: int) -> dict[str, Any]:
        """Return one pull request.

        :raises AzureDevOpsError: On a non-2xx response, including 404.
        """
        return self._get_json(_pull_request_segments(project, repo, pr_id))

    def iterations(self, project: str, repo: str, pr_id: int) -> list[dict[str, Any]]:
        """List the iterations (pushes) of a pull request.

        :raises AzureDevOpsError: On a non-2xx response.
        """
        payload = self._get_json([*_pull_request_segments(project, repo, pr_id), "iterations"])
        return _list_field(payload, "value")

    def iteration_changes(
        self, project: str, repo: str, pr_id: int, iteration: int, compare_to: int = 0
    ) -> list[dict[str, Any]]:
        """List every change entry of a pull request iteration.

        Pages through the endpoint 100 entries at a time.

        :param iteration: The iteration id.
        :param compare_to: The iteration to compare against, or ``0`` for the merge base.
        :raises AzureDevOpsError: On a non-2xx response.
        """
        pages = self.iteration_change_pages(project, repo, pr_id, iteration, compare_to)
        return [entry for page in pages for entry in page]

    def iteration_change_pages(
        self, project: str, repo: str, pr_id: int, iteration: int, compare_to: int = 0
    ) -> Iterator[list[dict[str, Any]]]:
        """Yield the change entries of a pull request iteration, one page of 100 at a time.

        Each page is requested when the previous one has been consumed.

        :param iteration: The iteration id.
        :param compare_to: The iteration to compare against, or ``0`` for the merge base.
        :raises AzureDevOpsError: On a non-2xx response.
        """
        segments = [
            *_pull_request_segments(project, repo, pr_id),
            "iterations",
            iteration,
            "changes",
        ]
        skip = 0
        for _ in range(_MAX_CHANGE_PAGES):
            payload = self._get_json(
                segments,
                [
                    ("$compareTo", compare_to),
                    ("$top", _PAGE_SIZE),
                    ("$skip", skip),
                ],
            )
            batch = _list_field(payload, "changeEntries")
            yield batch
            next_skip = payload.get("nextSkip")
            if next_skip is not None:
                if isinstance(next_skip, bool) or not isinstance(next_skip, int):
                    raise AzureDevOpsError(0, "Azure DevOps returned invalid change pagination")
                if next_skip == 0:
                    return
                if next_skip <= skip:
                    raise AzureDevOpsError(0, "Azure DevOps repeated a change page")
                skip = next_skip
                continue
            if len(batch) < _PAGE_SIZE:
                return
            skip += len(batch)
        raise AzureDevOpsError(
            0, "Azure DevOps changed-file page limit reached; the list is incomplete"
        )

    def statuses(self, project: str, repo: str, pr_id: int) -> list[dict[str, Any]]:
        """List the statuses posted to a pull request.

        :raises AzureDevOpsError: On a non-2xx response.
        """
        payload = self._get_json([*_pull_request_segments(project, repo, pr_id), "statuses"])
        return _list_field(payload, "value")

    def policy_evaluations(
        self, project: str, project_id: str, pr_id: int
    ) -> list[dict[str, Any]]:
        """List the branch policy evaluations of a pull request.

        :param project_id: The project's GUID, which the artifact id needs.
        :raises AzureDevOpsError: On a non-2xx response.
        """
        payload = self._get_json(
            [project, "_apis", "policy", "evaluations"],
            [("artifactId", f"vstfs:///CodeReview/CodeReviewId/{project_id}/{pr_id}")],
            api_version=_PREVIEW_API_VERSION,
        )
        return _list_field(payload, "value")

    def threads(self, project: str, repo: str, pr_id: int) -> list[dict[str, Any]]:
        """List the comment threads of a pull request.

        :raises AzureDevOpsError: On a non-2xx response.
        """
        payload = self._get_json([*_pull_request_segments(project, repo, pr_id), "threads"])
        return _list_field(payload, "value")

    def item_content(self, project: str, repo: str, path: str, commit: str) -> str | None:
        """Return the text of ``path`` at ``commit``.

        :param path: The path in the repository, for example ``/src/app.py``.
        :param commit: The commit id.
        :returns: The file content, or ``None`` when the file does not exist at that
            commit (a 404) or has no text content.
        :raises AzureDevOpsError: On a non-2xx response other than 404.
        """
        response = self._send(
            [project, "_apis", "git", "repositories", repo, "items"],
            [
                ("path", path),
                ("versionDescriptor.versionType", "commit"),
                ("versionDescriptor.version", commit),
                ("includeContent", "true"),
                ("$format", "json"),
            ],
        )
        if response.status_code == 404:
            return None
        payload = _parse(response)
        content = payload.get("content") if isinstance(payload, dict) else None
        return content if isinstance(content, str) else None
