"""Agent-management tools (``sys_agent_*``).

The orchestrator surface for inspecting and materializing agents. These
tools are **runner-dispatched**: the runner has no in-process stores, so
each proxies the Omnigent server's existing REST endpoints over
``server_client`` (same channel and security posture as the
``sys_session_*`` family in
:mod:`omnigent.tools.builtins.spawn`). They ship as schema-only
:class:`~omnigent.tools.base.Tool` subclasses — the base-class
``invoke`` fails loud if the AP-side path ever reaches them.

- ``sys_agent_get`` → ``GET /v1/sessions/{id}/agent`` (a global read of
  the agent bound to any accessible session).
- ``sys_agent_download`` → ``GET /v1/sessions/{id}/agent/contents``
  (download a session's agent bundle ``.tar.gz`` to the agent's local
  disk; returns the path).
- ``sys_agent_list`` → ``GET /v1/agents`` + ``GET /v1/sessions`` + a
  local-disk scan (built-in/template agents, agents bound to sessions,
  and locally-authored configs).

See ``designs/ORCHESTRATOR_SYS_TOOLS.md`` for the full surface.
"""

from __future__ import annotations

from typing import Any

from omnigent.tools.base import Tool


class SysAgentGetTool(Tool):
    """
    Return the agent metadata bound to a given session.

    A **global read**: resolves against any session the caller is
    permitted to access (bounded by the server's per-user permission
    model). Reports the agent's id, name, version, description, harness,
    and the safe summaries of its MCP servers and guardrail policies.
    Requires a ``session_id`` — there is no way to inspect an agent that
    isn't running in some session; to fork a built-in, create a session
    from it first, then inspect / download from that session.

    Runner-dispatched: the runner proxies ``GET
    /v1/sessions/{id}/agent`` and projects the
    :class:`~omnigent.server.schemas.AgentObject`. Returns
    ``agent_not_found`` when the session/agent is unknown and
    ``access_denied`` when the server refuses the read.
    """

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_agent_get"``."""
        return "sys_agent_get"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Return the agent metadata bound to a session: agent_id, "
            "name, version, description, harness, MCP servers, and "
            "guardrail policies. Global read — any session you can "
            "access. Requires session_id (an agent is only inspectable "
            "while running in some session). The returned agent_id "
            "launches the same agent directly via sys_session_create. "
            "Use sys_agent_download only to fetch the agent's full "
            "bundle for inspection or forking."
        )

    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI-format tool schema.

        :returns: Dict with ``"type": "function"`` and a
            ``"function"`` sub-dict; ``session_id`` is required.
        """
        return {
            "type": "function",
            "function": {
                "name": SysAgentGetTool.name(),
                "description": SysAgentGetTool.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": (
                                "The session (conversation_id) whose "
                                "bound agent to inspect, e.g. "
                                "'conv_abc123'. Get this from "
                                "sys_session_list or sys_agent_list."
                            ),
                        },
                    },
                    "required": ["session_id"],
                    "additionalProperties": False,
                },
            },
        }


class SysAgentDownloadTool(Tool):
    """
    Download a session's agent bundle ``.tar.gz`` to local disk.

    A **global read**: downloads the agent bundle bound to any session
    the caller is permitted to access (bounded by the server's per-user
    permission model). The bundle is written to the agent's local
    working directory (the same filesystem ``sys_os_read`` /
    ``sys_os_shell`` operate on) and the tool returns the path —
    path-only by design. To inspect the config, extract the archive
    (``sys_os_shell``) then read ``config.yaml`` (``sys_os_read``).
    Secret VALUES (MCP server ``headers`` / ``env`` and any ``.env``
    file) are **redacted** from the written bundle — key names are kept
    but each value becomes ``[REDACTED]`` — so the download discloses no
    more than the secret-free ``sys_agent_get``. A fork therefore needs
    its credentials re-supplied.
    The download exists for inspecting or forking a config — launching
    an already-registered agent needs no download: pass its
    ``agent_id`` (from ``sys_agent_list`` / ``sys_agent_get``) to
    ``sys_session_create``.

    Requires a ``session_id`` for the same reason as ``sys_agent_get``:
    an agent is only reachable through some session. To fork a built-in,
    create a session from it first, then download from that session.

    Runner-dispatched: the runner proxies ``GET
    /v1/sessions/{id}/agent/contents`` and writes the returned bytes.
    Returns ``agent_not_found`` when the session/agent is unknown and
    ``access_denied`` when the server refuses the read.
    """

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_agent_download"``."""
        return "sys_agent_download"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "Download a session's agent bundle (.tar.gz) to local disk "
            "and return the path. Global read — any session you can "
            "access. Requires session_id. Path-only: extract the "
            "archive with sys_os_shell, then read config.yaml with "
            "sys_os_read. Secret values (MCP headers/env, .env) are "
            "redacted to [REDACTED] in the bundle; key names are kept. "
            "Optionally pass dest_filename to control the output "
            "filename. Only needed to inspect or fork an agent's config "
            "— launching an existing agent needs no download: pass its "
            "agent_id to sys_session_create."
        )

    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI-format tool schema.

        :returns: Dict with ``"type": "function"`` and a
            ``"function"`` sub-dict; ``session_id`` is required,
            ``dest_filename`` is optional.
        """
        return {
            "type": "function",
            "function": {
                "name": SysAgentDownloadTool.name(),
                "description": SysAgentDownloadTool.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": (
                                "The session (conversation_id) whose "
                                "agent bundle to download, e.g. "
                                "'conv_abc123'."
                            ),
                        },
                        "dest_filename": {
                            "type": "string",
                            "description": (
                                "Optional output filename (relative to "
                                "the working directory), e.g. "
                                "'researcher.tar.gz'. Defaults to "
                                "'<agent_name>-v<version>.tar.gz'."
                            ),
                        },
                    },
                    "required": ["session_id"],
                    "additionalProperties": False,
                },
            },
        }


class SysAgentListTool(Tool):
    """
    List launchable agents across three sources.

    A **global read** that surfaces, in one call, every agent the caller
    could launch a session from:

    - **built-ins**: template agents registered on the server
      (``GET /v1/agents``);
    - **session-bound**: the agents bound to sessions the caller can
      access (``GET /v1/sessions``), each with its ``agent_id`` (for a
      direct ``sys_session_create`` launch) and its ``session_id`` (so
      the caller can ``sys_agent_get`` / ``sys_agent_download`` it); and
    - **local configs**: agent config YAMLs authored locally with
      ``sys_os_write`` (e.g. following the ``build-omnigent`` skill) — a
      scan of the working directory's agent-config subdir.

    Both ``builtins`` and ``session_agents`` rows carry an ``agent_id``
    that launches the agent directly via
    ``sys_session_create(agent_id=...)`` — an already-registered agent
    never needs its bundle downloaded or re-uploaded.

    Built-ins and session-bound entries are bounded by the server's
    per-user permission model. Local configs are whatever is on the
    agent's own disk. Runner-dispatched.
    """

    @classmethod
    def name(cls) -> str:
        """:returns: ``"sys_agent_list"``."""
        return "sys_agent_list"

    @classmethod
    def description(cls) -> str:
        """:returns: Human-readable description of the tool."""
        return (
            "List launchable agents across three sources: built-in / "
            "template agents, agents bound to sessions you can access, "
            "and locally-authored agent config YAMLs. Returns "
            "{builtins: [...], session_agents: [...], local_configs: "
            "[...]}. Both builtins and session_agents rows carry an "
            "agent_id that launches the agent directly via "
            "sys_session_create(agent_id=...) — an already-registered "
            "agent never needs its bundle downloaded or re-uploaded. "
            "Use sys_agent_get / sys_agent_download (with a "
            "session_agents row's session_id) only to inspect or fork "
            "an agent's config. Global read — no parameters."
        )

    def get_schema(self) -> dict[str, Any]:
        """
        Return the OpenAI-format tool schema.

        :returns: Dict with ``"type": "function"`` and a
            ``"function"`` sub-dict; no parameters.
        """
        return {
            "type": "function",
            "function": {
                "name": SysAgentListTool.name(),
                "description": SysAgentListTool.description(),
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
            },
        }
