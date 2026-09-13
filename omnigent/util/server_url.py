"""The one representation of an Omnigent server URL.

An Omnigent server is addressed by two related but distinct URLs:

- the **API base** — what requests are sent to. For a Databricks
  workspace-hosted deployment this is the API proxy mount
  (``https://<ws>/api/2.0/omnigent``), an implementation detail.
- the **display URL** — what the user recognizes and what messages,
  login hints, and browser opens should show. For workspace-hosted
  deployments this is the workspace SPA mount (``https://<ws>/omnigent``),
  carrying the ``?o=<org>`` workspace selector when one is known so a
  copy-pasted URL still routes to the right workspace on a
  multi-workspace (SPOG) host.

:class:`ServerUrl` binds the two together with the org id, so callers
never rebuild either form ad hoc: wire traffic uses
:attr:`ServerUrl.api_base`, anything user-facing uses
:attr:`ServerUrl.display`.
"""

from __future__ import annotations

import urllib.parse
from dataclasses import dataclass

# A host-sharded Databricks deployment mounts the API at this path; an
# unsharded / single-replica server mounts elsewhere (usually the root).
# This is the routing-relevant shape of a server URL (see
# ``omnigent.cli_auth.databricks_request_headers``, which keys off it).
WORKSPACE_API_PATH = "/api/2.0/omnigent"

# The workspace SPA mount the web UI lives on — the URL shape users
# recognize for a workspace-hosted deployment.
WORKSPACE_UI_PATH = "/omnigent"


def is_workspace_hosted_url(base_url: str) -> bool:
    """Whether *base_url* is a host-sharded deployment mount.

    True for the host-sharded mount (``https://<host>/api/2.0/omnigent``), which
    is the only deployment fronted by the sharding layer. Used to gate behavior
    that only applies there (see
    :func:`omnigent.cli_auth.databricks_request_headers`).

    :param base_url: Omnigent server base URL, e.g.
        ``"https://example.databricks.com/api/2.0/omnigent"``.
    :returns: ``True`` when the URL path is the workspace API mount.
    """
    return urllib.parse.urlsplit(base_url.rstrip("/")).path == WORKSPACE_API_PATH


def org_id_from_url(url: str) -> str | None:
    """Extract the ``?o=<workspace-id>`` workspace selector from *url*.

    A Databricks host can front many workspaces under one hostname, where
    the bare host resolves to the account and ``?o=<workspace-id>`` picks
    the workspace. The selector is threaded into both the login (to bind
    the grant to the workspace) and every API request (to route to it).

    :param url: A user-supplied server URL, possibly carrying ``?o=``,
        e.g. ``"https://acme.databricks.com/?o=123"``.
    :returns: The workspace id, e.g. ``"123"``, or ``None`` when absent.
    """
    values = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("o")
    return values[0] if values and values[0] else None


@dataclass(frozen=True)
class ServerUrl:
    """An Omnigent server URL: canonical API base plus workspace selector.

    Construct via :meth:`from_api_base` (which resolves the org id from
    the URL itself or the stored login record), or directly when the org
    id is already in hand (e.g. parsed from raw ``--server`` input).
    """

    api_base: str
    """Canonical API base without trailing slash, query, or fragment,
    e.g. ``"https://ws.databricks.com/api/2.0/omnigent"`` or
    ``"http://127.0.0.1:6767"``. Every request targets this."""

    org_id: str | None = None
    """The ``?o=`` workspace selector when known, e.g. ``"123"``. Routes
    requests and browser links on multi-workspace (SPOG) hosts."""

    def __post_init__(self) -> None:
        # Enforce the documented ``api_base`` invariant (no trailing slash,
        # query, or fragment) on every construction path: callers append
        # request paths to it, so a stray ``?o=`` would corrupt them
        # (``…?o=123/v1/me``). A ``?o=`` selector on the URL becomes the org
        # id when none was given explicitly.
        stripped = self.api_base.rstrip("/")
        parsed = urllib.parse.urlsplit(stripped)
        if parsed.query or parsed.fragment:
            if self.org_id is None:
                object.__setattr__(self, "org_id", org_id_from_url(stripped))
            stripped = urllib.parse.urlunsplit(
                (parsed.scheme, parsed.netloc, parsed.path, "", "")
            ).rstrip("/")
        object.__setattr__(self, "api_base", stripped)

    @classmethod
    def from_api_base(cls, api_base: str) -> ServerUrl:
        """Build a :class:`ServerUrl` from an API base string.

        The org id is taken from the URL's own ``?o=`` query when present
        (and the query is stripped off the stored base), else from the
        ``omnigent login`` record for the base, else left unset.

        :param api_base: The API base URL, possibly carrying ``?o=``, e.g.
            ``"https://ws.databricks.com/api/2.0/omnigent?o=123"``.
        :returns: The parsed :class:`ServerUrl`.
        """
        # ``__post_init__`` strips the query/fragment and absorbs a ``?o=``
        # selector into ``org_id``; only the stored-record fallback lives here.
        resolved = cls(api_base=api_base)
        if resolved.org_id is not None:
            return resolved
        # Deferred: cli_auth imports this module's constants at load time.
        from omnigent.cli_auth import load_databricks_org_id

        return cls(
            api_base=resolved.api_base,
            org_id=load_databricks_org_id(resolved.api_base),
        )

    @property
    def is_workspace_hosted(self) -> bool:
        """Whether this server is a Databricks workspace-hosted mount."""
        return is_workspace_hosted_url(self.api_base)

    @property
    def workspace_host(self) -> str | None:
        """The fronting workspace origin for a workspace-hosted server.

        :returns: e.g. ``"https://ws.databricks.com"``, or ``None`` when
            this server is not workspace-hosted.
        """
        if not self.is_workspace_hosted:
            return None
        parsed = urllib.parse.urlsplit(self.api_base)
        return f"{parsed.scheme}://{parsed.netloc}"

    @property
    def display(self) -> str:
        """The user-facing form of this server URL.

        Workspace-hosted servers show the SPA mount the user recognizes
        (``https://<ws>/omnigent``) instead of the internal API proxy
        path, with ``?o=<org>`` appended when the selector is known — so
        the URL both reads right and, when copy-pasted (into a browser or
        ``omnigent login``), still routes to the right workspace. Every
        other URL is shown as-is.

        :returns: e.g. ``"https://ws.databricks.com/omnigent?o=123"`` or
            ``"http://127.0.0.1:6767"``.
        """
        if not self.is_workspace_hosted:
            return self.api_base
        parsed = urllib.parse.urlsplit(self.api_base)
        query = urllib.parse.urlencode({"o": self.org_id}) if self.org_id else ""
        return urllib.parse.urlunsplit(
            (parsed.scheme, parsed.netloc, WORKSPACE_UI_PATH, query, "")
        )


def display_server_url(base_url: str) -> str:
    """Map an Omnigent server base URL to the user-facing form to show.

    Convenience wrapper over :attr:`ServerUrl.display` for call sites that
    hold a plain API-base string: the org id is resolved from the URL's
    own ``?o=`` query or the stored ``omnigent login`` record (see
    :meth:`ServerUrl.from_api_base`).

    :param base_url: Omnigent server base URL, e.g.
        ``"https://example.databricks.com/api/2.0/omnigent"`` or
        ``"http://127.0.0.1:6767"``.
    :returns: The display URL, e.g.
        ``"https://example.databricks.com/omnigent?o=123"`` or
        ``"http://127.0.0.1:6767"``.
    """
    return ServerUrl.from_api_base(base_url).display
