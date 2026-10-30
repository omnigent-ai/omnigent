"""Deep links that open a session's workspace in a desktop code editor.

The editor runs on the viewer's own machine, so a link must name a folder that
machine can reach: over SSH via the destination the host reported in
``host.hello`` (``host.ssh_target`` in its ``config.yaml``), or as a local path
when the session runs on that same machine. Only the client can tell the
second case apart, so local links are marked ``local_only``.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote

from omnigent.stores.host_store import Host

# (URL scheme the editor registers with the OS, display label), default first.
CODE_EDITORS: tuple[tuple[str, str], ...] = (("cursor", "Cursor"), ("vscode", "VS Code"))


@dataclass(frozen=True)
class EditorLink:
    """One way to open the workspace.

    :param editor: Editor key, e.g. ``"cursor"``.
    :param label: Display name, e.g. ``"Cursor"``.
    :param url: Deep link, e.g.
        ``"cursor://vscode-remote/ssh-remote+arclet_dev.ssh/home/me/repo"``.
    :param local_only: Opens a path on the clicking machine, so it only works
        when the session's host is that machine.
    """

    editor: str
    label: str
    url: str
    local_only: bool


def build_editor_links(
    *,
    workspace: str | None,
    host: Host | None,
    viewer_id: str | None,
) -> list[EditorLink] | None:
    """Build editor links for a session, or ``None`` when none can work.

    :param workspace: The session's absolute working directory, e.g.
        ``"/home/me/repo"``.
    :param host: The session's host row, or ``None`` when unbound.
    :param viewer_id: The requesting user. Links use the viewer's own SSH
        credentials, so only the host's owner gets them.
    :returns: One link per editor, or ``None``.
    """
    if not workspace or not workspace.startswith("/") or host is None:
        return None
    if viewer_id is None or host.user_id != viewer_id:
        return None
    path = quote(workspace, safe="/")
    if host.ssh_target is not None:
        location, local_only = f"vscode-remote/ssh-remote+{host.ssh_target}", False
    elif host.sandbox_provider:
        # A server-managed sandbox has no path the viewer's machine can open.
        return None
    else:
        location, local_only = "file", True
    return [
        EditorLink(
            editor=scheme, label=label, url=f"{scheme}://{location}{path}", local_only=local_only
        )
        for scheme, label in CODE_EDITORS
    ]
