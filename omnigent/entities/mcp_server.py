"""MCP server entity — a reusable, owner-scoped MCP server config.

A standalone MCP server is registered once by a user and reused across
agents: instead of re-typing url/headers into every create-agent form,
the user picks from their preconfigured servers. Each row is owned by a
single user (``owner``) and carries the full connection config — including
secret-bearing ``headers``/``env`` — so it can both be verified
(connect + list tools) and baked into an agent bundle.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class McpServer:
    """A reusable MCP server connection owned by one user.

    :param id: Unique identifier, e.g. ``"mcp_0f1a2b3c..."``.
    :param created_at: Unix epoch seconds when the server was created.
    :param name: User-facing server name, unique per owner,
        e.g. ``"litellm"``.
    :param transport: Transport type — ``"http"`` or ``"stdio"``.
    :param owner: Owning user id, e.g. ``"alice@example.com"``. The
        reserved ``"local"`` sentinel in single-user mode.
    :param url: HTTP(S) endpoint for ``transport="http"`` servers.
    :param headers: HTTP headers (e.g. ``Authorization``) for http
        servers. Secret-bearing — never returned verbatim by the API.
    :param command: Executable for ``transport="stdio"`` servers.
    :param args: Command-line arguments for stdio servers.
    :param env: Environment variables for stdio servers. Secret-bearing.
    :param description: Optional free-text description.
    :param updated_at: Unix epoch seconds of the last update, or ``None``.
    """

    id: str
    created_at: int
    name: str
    transport: str
    owner: str | None = None
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict, repr=False)
    description: str | None = None
    updated_at: int | None = None
