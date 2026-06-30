"""Helpers for opening Omnigent conversation URLs from CLI frontends."""

from __future__ import annotations

import subprocess
import sys
import urllib.parse
import webbrowser
from collections.abc import Callable

# Databricks workspace-hosted omnigent: the API proxy and the web UI are
# mounted on different workspace paths. ``conversation_url`` maps the
# server (API) base onto the UI mount so browser links land on the SPA
# instead of the JSON API.
WORKSPACE_API_PATH = "/api/2.0/omnigent"
WORKSPACE_UI_PATH = "/omnigent"


def is_workspace_hosted_url(base_url: str) -> bool:
    """
    Whether *base_url* is a Databricks workspace-hosted Omnigent mount.

    True for the API proxy mount (``https://<ws>/api/2.0/omnigent``) the
    CLI connects to on a workspace. Used to suppress UI a workspace
    deployment shouldn't surface (e.g. the startup banner's server-version
    row, since a workspace build reports no meaningful version string).

    :param base_url: Omnigent server base URL, e.g.
        ``"https://example.databricks.com/api/2.0/omnigent"``.
    :returns: ``True`` when the URL path is the workspace API mount.
    """
    return urllib.parse.urlsplit(base_url.rstrip("/")).path == WORKSPACE_API_PATH


def display_server_url(base_url: str) -> str:
    """
    Map an Omnigent server base URL to the user-facing form to show.

    Databricks workspace-hosted servers are connected to on the API proxy
    mount (``https://<ws>/api/2.0/omnigent``), but the URL a user
    recognizes — and that the web UI lives on — is the workspace SPA mount
    (``https://<ws>/omnigent``). Rewrites the API path to the UI path for
    those (dropping any ``?o=<org>`` query), so the startup banner shows
    the clean ``/omnigent`` URL instead of the internal API path. Every
    other URL (local ``http://127.0.0.1:<port>``, a custom remote) is
    returned unchanged apart from a trailing-slash trim.

    :param base_url: Omnigent server base URL, e.g.
        ``"https://example.databricks.com/api/2.0/omnigent"`` or
        ``"http://127.0.0.1:6767"``.
    :returns: The display URL, e.g.
        ``"https://example.databricks.com/omnigent"`` or
        ``"http://127.0.0.1:6767"``.
    """
    parsed = urllib.parse.urlsplit(base_url.rstrip("/"))
    if parsed.path == WORKSPACE_API_PATH:
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, WORKSPACE_UI_PATH, "", ""))
    return base_url.rstrip("/")


def conversation_url(base_url: str, conversation_id: str) -> str:
    """
    Build the browser URL for an Omnigent conversation.

    For Databricks workspace-hosted servers
    (``https://<ws>/api/2.0/omnigent``) the web UI lives on the
    workspace SPA mount, so the link becomes
    ``https://<ws>/omnigent/c/<id>`` — with the ``?o=<org>``
    workspace selector appended when ``omnigent login`` recorded the
    org id.

    :param base_url: Omnigent server base URL, e.g. ``"http://127.0.0.1:6767"``.
    :param conversation_id: Conversation id, e.g. ``"conv_abc123"``.
    :returns: Browser URL, e.g. ``"http://127.0.0.1:6767/c/conv_abc123"``.
    """
    encoded_id = urllib.parse.quote(conversation_id, safe="")
    parsed = urllib.parse.urlsplit(base_url.rstrip("/"))
    if parsed.path == WORKSPACE_API_PATH:
        from omnigent.cli_auth import load_databricks_org_id

        org_id = load_databricks_org_id(base_url)
        return urllib.parse.urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                f"{WORKSPACE_UI_PATH}/c/{encoded_id}",
                urllib.parse.urlencode({"o": org_id}) if org_id else "",
                "",
            )
        )
    return f"{base_url.rstrip('/')}/c/{encoded_id}"


def open_conversation_url(url: str) -> bool:
    """
    Open a conversation URL in the user's default browser.

    On macOS this invokes ``open <url>`` directly so the CLI matches
    the native platform behavior users expect. Other platforms use
    :mod:`webbrowser` as the standard-library default-browser
    abstraction.

    :param url: Absolute browser URL, e.g.
        ``"http://127.0.0.1:6767/c/conv_abc123"``.
    :returns: ``True`` when an opener accepted the URL, otherwise
        ``False``.
    :raises OSError: If the platform opener cannot be executed.
    """
    if sys.platform == "darwin":
        completed = subprocess.run(
            ["open", url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return completed.returncode == 0
    return webbrowser.open(url)


def open_conversation_link_if_enabled(
    *,
    base_url: str,
    conversation_id: str,
    enabled: bool,
    warn: Callable[[str], None] | None = None,
) -> None:
    """
    Open a conversation link when the CLI config enables it.

    :param base_url: Omnigent server base URL, e.g. ``"http://127.0.0.1:6767"``.
    :param conversation_id: Conversation id, e.g. ``"conv_abc123"``.
    :param enabled: ``True`` when the user opted into automatic browser opens.
    :param warn: Optional warning sink. Receives a complete warning
        message when the opener fails.
    :returns: None.
    """
    if not enabled:
        return
    url = conversation_url(base_url, conversation_id)
    try:
        opened = open_conversation_url(url)
    except OSError as exc:
        if warn is not None:
            warn(f"Warning: failed to open conversation URL {url}: {exc}")
        return
    if not opened and warn is not None:
        warn(f"Warning: no browser opener accepted conversation URL {url}")
