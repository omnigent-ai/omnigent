"""Helpers for opening Omnigent conversation URLs from CLI frontends."""

from __future__ import annotations

import re
import subprocess
import sys
import urllib.parse
import webbrowser
from collections.abc import Callable

# The server-URL shape (API mount, UI mount, display mapping) lives in
# ``omnigent.server_url`` — the one representation of a server URL. The
# helpers below only borrow it to build browser links.
from omnigent.server_url import WORKSPACE_UI_PATH, ServerUrl

# Client-side SPA route for one conversation (see web/src/App.tsx's
# ``c/:conversationId``). ``conversation_url`` appends it; ``strip_conversation_path``
# is the inverse, for a URL copied out of the browser's address bar.
_CONVERSATION_PATH_RE = re.compile(r"/c/[^/]+/?$")


def strip_conversation_path(url: str) -> str:
    """
    Drop a trailing ``/c/<conversation_id>`` from a server URL.

    The web UI's address bar shows ``<base>/c/<id>`` for an open
    conversation, so that is what a user copies when asked for "the
    omnigent URL". It is a client-side route, not a server mount: the SPA
    catch-all answers ``GET <base>/c/<id>/v1/me`` with a ``200`` HTML shell,
    so such a URL passes an auth probe and is accepted as a server, then
    every real API call 404s because no router owns that prefix. Trimming
    the route recovers the base the API actually lives on.

    :param url: A server URL, possibly a copied conversation link, e.g.
        ``"https://app.databricksapps.com/c/9bed9ec6"``.
    :returns: The URL without the conversation route, e.g.
        ``"https://app.databricksapps.com"``.
    """
    stripped = url.rstrip("/")
    parsed = urllib.parse.urlsplit(stripped)
    trimmed = _CONVERSATION_PATH_RE.sub("", parsed.path)
    if trimmed == parsed.path:
        return stripped
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, trimmed, parsed.query, parsed.fragment)
    )


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
    server = ServerUrl.from_api_base(base_url)
    if server.is_workspace_hosted:
        parsed = urllib.parse.urlsplit(server.api_base)
        return urllib.parse.urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                f"{WORKSPACE_UI_PATH}/c/{encoded_id}",
                urllib.parse.urlencode({"o": server.org_id}) if server.org_id else "",
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
