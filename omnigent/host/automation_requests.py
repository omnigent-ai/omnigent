"""Make a scheduled-task automation API call for the server, as this host's owner.

The server provisions a project's Slack channel watcher through the project
Lead's host, so it needs no credential for the automation workspace. The host
signs each call with its owner's Databricks login for the URL's own workspace,
and the token is only ever sent to that workspace.

Only HTTPS calls to a ``…/scheduled-tasks`` path are made; anything else is
refused without resolving a credential.
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import urlsplit

import httpx

from omnigent.host.frames import HostAutomationRequestFrame, HostAutomationResultFrame

_logger = logging.getLogger(__name__)

_METHODS = frozenset({"GET", "POST", "PATCH", "DELETE"})
# The scheduled-task collection, one task, or its state; nothing else.
_TASK_PATH = re.compile(
    r"^/[A-Za-z0-9._~/-]*/scheduled-tasks(?:/[A-Za-z0-9._@-]+){0,2}(?:/state)?$"
)
_TIMEOUT_S = 30.0
_BODY_MAX_CHARS = 64 * 1024


def _refuse(frame: HostAutomationRequestFrame, reason: str) -> HostAutomationResultFrame:
    return HostAutomationResultFrame(request_id=frame.request_id, status_code=0, error=reason)


def check_automation_request(frame: HostAutomationRequestFrame) -> str | None:
    """Why the host won't make this call, or ``None`` when it may."""
    parts = urlsplit(frame.url)
    if frame.method not in _METHODS:
        return f"method {frame.method!r} isn't allowed"
    if parts.scheme != "https" or not parts.hostname:
        return "only https URLs are allowed"
    if parts.username or parts.password or parts.fragment:
        return "the URL may not carry credentials or a fragment"
    if not _TASK_PATH.match(parts.path):
        return "only scheduled-task automation calls are allowed"
    return None


def run_automation_request(
    frame: HostAutomationRequestFrame,
    *,
    resolve_auth=None,
    transport: httpx.BaseTransport | None = None,
) -> HostAutomationResultFrame:
    """Make the call and report the API's answer; blocking, so run it off the event loop.

    :param resolve_auth: ``(workspace_url) -> (httpx.Auth, resolved_host)``;
        defaults to the host's Databricks login lookup.
    """
    refusal = check_automation_request(frame)
    if refusal is not None:
        return _refuse(frame, refusal)
    hostname = (urlsplit(frame.url).hostname or "").lower()
    if resolve_auth is None:
        from omnigent.inner.databricks_executor import _resolve_databricks_auth_for_host

        resolve_auth = _resolve_databricks_auth_for_host
    try:
        auth, resolved = resolve_auth(f"https://{hostname}")
    except Exception as exc:  # noqa: BLE001 — reported to the server as a sign-in problem
        return _refuse(
            frame,
            f"The project owner's host has no Databricks login for {hostname}. On that "
            f"machine run: databricks auth login --host https://{hostname} ({exc})",
        )
    resolved_host = (
        urlsplit(resolved if "://" in resolved else f"https://{resolved}").hostname or ""
    )
    if resolved_host.lower() != hostname:
        return _refuse(frame, f"the host's login is for {resolved_host}, not {hostname}")
    try:
        with httpx.Client(timeout=_TIMEOUT_S, transport=transport, follow_redirects=False) as http:
            response = http.request(
                frame.method,
                frame.url,
                auth=auth,
                content=json.dumps(frame.body) if frame.body is not None else None,
                headers={"Content-Type": "application/json"} if frame.body is not None else None,
            )
    except httpx.HTTPError as exc:
        return _refuse(frame, f"the automation service is unreachable from the host: {exc}")
    return HostAutomationResultFrame(
        request_id=frame.request_id,
        status_code=response.status_code,
        body=response.text[:_BODY_MAX_CHARS],
    )
