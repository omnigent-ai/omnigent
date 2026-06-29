"""MCP server store — manages reusable, owner-scoped MCP servers."""

from __future__ import annotations

from abc import ABC, abstractmethod

from omnigent.entities import McpServer


class McpServerStore(ABC):
    """
    Abstract base for standalone MCP server persistence.

    Manages the lifecycle of reusable MCP server connections: creation
    with per-owner name uniqueness, lookup by id or name, owner-scoped
    listing, update, and deletion.
    """

    def __init__(self, storage_location: str) -> None:
        """
        Initialize the MCP server store.

        :param storage_location: Backend-specific storage URI,
            e.g. ``"sqlite:///omnigent.db"``.
        """
        self.storage_location = storage_location

    @abstractmethod
    def create(
        self,
        server_id: str,
        owner: str | None,
        name: str,
        transport: str,
        *,
        url: str | None = None,
        headers: dict[str, str] | None = None,
        command: str | None = None,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
        description: str | None = None,
    ) -> McpServer:
        """
        Register a new MCP server. Name must be unique for the owner.

        :param server_id: Pre-generated unique id, e.g. ``"mcp_ab12..."``.
        :param owner: Owning user id, or ``None`` for a global row.
        :param name: Server name; unique among the owner's servers.
        :param transport: ``"http"`` or ``"stdio"``.
        :param url: HTTP endpoint (http transport).
        :param headers: HTTP headers (http transport).
        :param command: Executable (stdio transport).
        :param args: Command-line args (stdio transport).
        :param env: Environment variables (stdio transport).
        :param description: Optional free-text description.
        :returns: The newly created :class:`McpServer`.
        """
        ...

    @abstractmethod
    def get(self, server_id: str) -> McpServer | None:
        """
        Return the server, or ``None`` if it does not exist.

        :param server_id: Unique server id, e.g. ``"mcp_ab12..."``.
        :returns: The :class:`McpServer` if found, otherwise ``None``.
        """
        ...

    @abstractmethod
    def get_by_name(self, owner: str | None, name: str) -> McpServer | None:
        """
        Look up one of the owner's servers by name.

        :param owner: Owning user id.
        :param name: Server name.
        :returns: The :class:`McpServer` if found, otherwise ``None``.
        """
        ...

    @abstractmethod
    def list_for_owner(self, owner: str | None) -> list[McpServer]:
        """
        List all of the owner's servers, newest first.

        :param owner: Owning user id.
        :returns: The owner's :class:`McpServer` rows.
        """
        ...

    @abstractmethod
    def update(
        self,
        server_id: str,
        *,
        name: str,
        transport: str,
        url: str | None,
        headers: dict[str, str],
        command: str | None,
        args: list[str],
        env: dict[str, str],
        description: str | None,
    ) -> McpServer | None:
        """
        Replace a server's config and bump ``updated_at``.

        :param server_id: Unique server id.
        :returns: The updated :class:`McpServer`, or ``None`` if absent.
        """
        ...

    @abstractmethod
    def delete(self, server_id: str) -> bool:
        """
        Delete a server. Returns ``True`` if it existed.

        :param server_id: Unique server id.
        :returns: ``True`` if deleted, ``False`` if it did not exist.
        """
        ...
