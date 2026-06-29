"""Registry-based tool manager for agent execution.

Registers builtin, client-specified, and local-python tools.
MCP lifecycle lives on the runner — see designs/RUNNER_MCP.md.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.inner.os_env import OSEnvironment
from omnigent.spec import AgentSpec
from omnigent.spec.types import SharePolicy, ToolRuntime
from omnigent.tools._srt import is_srt_available
from omnigent.tools.base import Tool, ToolContext, is_valid_tool_name
from omnigent.tools.builtins import (
    ListCommentsTool,
    LoadSkillTool,
    ReadSkillFileTool,
    SysAgentDownloadTool,
    SysAgentGetTool,
    SysAgentListTool,
    SysCallAsyncTool,
    SysCancelAsyncTool,
    SysListModelsTool,
    SysReadInboxTool,
    SysSessionCloseTool,
    SysSessionCreateTool,
    SysSessionGetHistoryTool,
    SysSessionGetInfoTool,
    SysSessionListTool,
    SysSessionSendTool,
    SysSessionShareTool,
    SysTimerCancelTool,
    SysTimerSetTool,
    UpdateCommentTool,
    any_skill_has_resources,
    get_builtin_tool,
)
from omnigent.tools.client_specified import ClientSideTool, ClientSideToolSpec
from omnigent.tools.local import load_local_python_tools

# MCP lifecycle moved to runner; see designs/RUNNER_MCP.md.

_logger = logging.getLogger(__name__)


class _UCFunctionSchemaTool(Tool):
    """Schema-only tool entry for UC function tools.

    UC function tools are dispatched by the runner via the SQL
    Statement Execution API — the tool manager only exposes the
    schema to the LLM. This avoids the ``ClientSideTool`` path
    (which routes to ``action_required`` / client tunneling)
    and the ``LocalCallableTool`` path (which requires a
    server-side callable).

    :param tool_name: Tool name shown to the LLM, e.g.
        ``"classify_text"``.
    :param schema: OpenAI-format tool schema dict.
    """

    def __init__(self, tool_name: str, schema: dict[str, object]) -> None:
        self._name = tool_name
        self._schema = schema

    def name(self) -> str:  # type: ignore[override]
        """Return the tool name.

        :returns: The tool name, e.g. ``"classify_text"``.
        """
        return self._name

    def description(self) -> str:  # type: ignore[override]
        """Return the tool description from the schema.

        :returns: The description string, or empty string if
            none was provided in the schema.
        """
        func = self._schema.get("function", {})
        return func.get("description", "") if isinstance(func, dict) else ""

    def get_schema(self) -> dict[str, object]:
        """Return the OpenAI-format tool schema.

        :returns: A dict with ``"type": "function"`` and the
            ``"function"`` sub-dict.
        """
        return self._schema


class ToolManager:
    """Registry-based tool manager for a single workflow execution.

    Registers at init: skill tools (if any skills), builtins,
    local-python tools, and client-specified tools. MCP tools are
    runner-owned (designs/RUNNER_MCP.md) and never registered here.
    """

    def __init__(
        self,
        spec: AgentSpec,
        client_tool_specs: list[ClientSideToolSpec] | None = None,
        workdir: Path | None = None,
        sandbox_enabled: bool = True,
        os_env: OSEnvironment | None = None,
    ) -> None:
        """
        Initialize the tool manager and register built-in,
        client-specified, and local tools.

        MCP tools are not registered until ``start()`` is called.

        :param spec: The parsed AgentSpec defining which tools
            (skills, MCP servers) are available.
        :param client_tool_specs: Optional list of
            :class:`ClientSideToolSpec` objects supplied by the API
            caller at request time, e.g.
            ``[ClientSideToolSpec(name="get_weather", ...)]``.
            ``None`` and ``[]`` are equivalent (no client tools).
        :param workdir: The extracted agent image directory on disk.
            Required for local tool loading. ``None`` skips local
            tool registration, e.g. ``Path("/tmp/cache/ag_abc123")``.
        :param sandbox_enabled: Runtime policy for ``srt`` sandboxing.
            ``True`` enables sandboxing when ``srt`` is on PATH.
            This is a deployment decision from ``RuntimeCaps``, not
            an agent config setting.
        :param os_env: Pre-resolved primary OSEnvironment from the
            ``SessionResourceRegistry``. When provided, ``sys_os_*``
            tools use this shared instance instead of creating their
            own. ``None`` falls back to per-call creation via
            ``create_os_environment()``.
        """
        self._spec = spec
        self._workdir = workdir
        self._sandbox_enabled = sandbox_enabled
        self._pre_resolved_os_env = os_env
        self._started = False
        self._tools: dict[str, Tool] = {}
        self._srt_available = is_srt_available()
        self._uv_available = shutil.which("uv") is not None
        # Track the OS environment instance the spec opts into so
        # ``shutdown`` can close it cleanly. ``None`` when the spec
        # didn't declare ``os_env``.
        self._os_env: OSEnvironment | None = None
        self._register_skill_tools()
        self._register_builtin_tools()
        self._register_sub_agent_tools()
        self._register_agent_mgmt_tools()
        self._register_os_env_tools()
        self._register_terminal_tools()
        self._register_local_tools(workdir)
        self._register_client_tools(client_tool_specs or [])
        # Step 11a: register the async-dispatch builtins
        # (sys_call_async, sys_read_inbox, sys_cancel_async).
        # ``AgentSpec.async_enabled`` defaults to True (matches
        # the legacy inner stack) — agents that want to suppress
        # the surface set ``async: false`` explicitly. Lookup of
        # the dispatch target uses the runtime ContextVar, so
        # registration only cares about the gate — no
        # construction-time wiring.
        self._register_async_inbox_tools()
        # Step 10: register sys_timer_set / sys_timer_cancel when
        # the spec opts in via ``timers: true``. Defaults off
        # (matches the inner stack's ``AgentDef.timers`` default).
        # Firings ride the same ``async_work_complete`` drain path
        # as ``sys_call_async``, but with ``kind="timer"`` so they
        # don't block end-of-turn auto-collect.
        self._register_timer_tools()
        # Task lifecycle builtin (sys_cancel_task) is auto-enabled
        # at the end so any AP-created background handle can point
        # at a stable cancel tool.
        self._register_task_lifecycle_tools()
        # Comment tools are always auto-registered so agents can
        # list and update review comments without the spec opting in.
        self._register_comment_tools()
        # Work-item (#3), schedule/loop (#6), and canvas (#2) builtins are
        # likewise always auto-registered, so an agent — and the Omnigent MCP
        # surface — can create/track work items, manage loops & monitors, and
        # set the conversation canvas without the spec opting in (#12).
        # Registered directly (like the comment tools) rather than declared in
        # ``tools.builtins``, which sidesteps the function-tool callable-
        # recovery translation path.
        self._register_work_management_tools()
        # Policy tool is always auto-registered so agents can add
        # inline CEL policies at runtime without spec changes.
        self._register_policy_tools()

    def _register_policy_tools(self) -> None:
        """
        Auto-register ``sys_add_policy`` and ``sys_policy_registry``.

        Always available so the agent can browse available policy
        templates and add CEL or builtin policies to the current
        session at runtime. The runner dispatches both tools via
        the Omnigent server's REST endpoints.
        """
        from omnigent.tools.builtins.policy import SysAddPolicyTool, SysPolicyRegistryTool

        self._tools[SysAddPolicyTool.name()] = SysAddPolicyTool()
        self._tools[SysPolicyRegistryTool.name()] = SysPolicyRegistryTool()

    def _register_async_inbox_tools(self) -> None:
        """
        Register the async-dispatch builtins when the agent spec
        has ``async`` enabled (parsed onto
        :attr:`AgentSpec.async_enabled`, which defaults to
        ``True``).

        Registers (in order):

        * :class:`SysCallAsyncTool` — fire-and-forget dispatcher
          for any local Python tool (11a.i).
        * :class:`SysReadInboxTool` — pull-mode drain of completed
          async-work payloads (11a.ii).
        * :class:`SysCancelAsyncTool` — cancel a dispatched task
          by handle id; thin alias over the always-registered
          ``sys_cancel_task`` (11a.iii).

        When the spec sets ``async: false`` explicitly, none of
        the async-namespace builtins register — the flag is the
        kill-switch for agents that want a minimal-tools surface.

        See ``designs/SERVER_HARNESS_CONTRACT.md`` §Async work +
        inbox for the design rationale (including why the default
        flipped to ``True`` post-step-11a.iii).
        """
        if not self._spec.async_enabled:
            return
        self._tools[SysCallAsyncTool.name()] = SysCallAsyncTool()
        self._tools[SysReadInboxTool.name()] = SysReadInboxTool()
        self._tools[SysCancelAsyncTool.name()] = SysCancelAsyncTool()

    def _register_timer_tools(self) -> None:
        """
        Register the timer builtins when the agent spec opts in.

        Gated on :attr:`AgentSpec.timers` (defaults to ``False`` to
        match ``omnigent/inner/datamodel.py::AgentDef.timers``).
        Agents that want timer scheduling declare ``timers: true``
        at the top level of their YAML.

        Registers (in order):

        * :class:`SysTimerSetTool` — schedules a timer; returns the
          ``timer_id`` synchronously, fires later via the
          ``async_work_complete`` drain.
        * :class:`SysTimerCancelTool` — cancels a scheduled timer
          by id.

        Step 10 of the harness contract migration. See
        ``designs/SERVER_HARNESS_CONTRACT.md`` §Timers and
        the runner-side timer task implementation for the
        firing workflow.

        :raises ValueError: If a ``sys_timer_*`` name collides with
            an already-registered tool. Defensive — should not
            happen given the standard registration order.
        """
        if not self._spec.timers:
            return
        for tool in (SysTimerSetTool(), SysTimerCancelTool()):
            if tool.name() in self._tools:
                raise ValueError(
                    f"sys_timer_* tool {tool.name()!r} collides with an already-registered tool"
                )
            self._tools[tool.name()] = tool

    def _register_skill_tools(self) -> None:
        """
        Register built-in skill tools.

        Always registers ``load_skill`` — it discovers host-scope
        skills (``~/.claude/skills/``, ``.agents/skills/``, etc.)
        at init time even when the agent has no bundled skills.
        Registers ``read_skill_file`` only when at least one skill
        has bundled resource files.
        """
        load_tool = LoadSkillTool(
            self._spec.skills,
            agent_root=self._workdir,
            skills_filter=self._spec.skills_filter,
        )
        self._tools[load_tool.name()] = load_tool
        # Combine bundled + discovered skills for resource check.
        all_skills = load_tool.skills
        if any_skill_has_resources(all_skills):
            read_tool = ReadSkillFileTool(all_skills)
            self._tools[read_tool.name()] = read_tool

    def _register_builtin_tools(self) -> None:
        """
        Register built-in tools declared in ``tools.builtins``.

        Most tools are looked up in the built-in registry and
        instantiated with spec-level config. Some (``web_search``,
        ``web_fetch``, ``upload_file``) need runtime context the
        registry doesn't have and are dispatched through
        :meth:`_create_builtin` instead.
        """
        for entry in self._spec.tools.builtins:
            tool = self._create_builtin(entry.name, entry.config)
            if tool is None:
                _logger.warning(
                    "Unknown built-in tool %r — skipping. "
                    "Available: web_search, web_fetch, upload_file, "
                    "list_files, download_file, "
                    "search_conversations, export_agent. "
                    "Note: list_comments and update_comment are "
                    "framework-owned and auto-registered; they cannot "
                    "be declared in the spec. "
                    "(Interactive shells now go through the "
                    "spec-level `terminals:` block + sys_terminal_*; "
                    "one-shot shells use sys_os_shell.)",
                    entry.name,
                )
                continue
            self._tools[tool.name()] = tool

    def _create_builtin(
        self,
        name: str,
        config: dict[str, str] | None,
    ) -> Tool | None:
        """
        Instantiate a built-in tool by name.

        :param name: The builtin name from the spec.
        :param config: Optional spec-level config dict.
        :returns: A :class:`Tool` instance, or ``None``.
        """
        if name == "web_search":
            return self._create_web_search(config)
        if name == "web_fetch":
            return self._create_web_fetch()
        if name == "upload_file":
            from omnigent.tools.builtins.upload_file import UploadFileTool

            return UploadFileTool()
        return get_builtin_tool(name, config=config)

    def _create_web_search(self, config: dict[str, str] | None) -> Tool:
        """
        Build a :class:`WebSearchTool` for the parent's LLM.

        Uses ``parse_model_string`` to infer the provider, except for
        ``databricks-*`` models which don't support the native
        ``web_search_preview`` schema and fall back to function-tool mode.

        :param config: Spec-level tool config dict, e.g.
            ``{"api_key": "...", "engine_id": "..."}``.
        :returns: A configured :class:`WebSearchTool`.
        """
        from omnigent.tools.builtins.web_search import WebSearchTool

        llm_provider = None
        if self._spec.executor.model:
            model = self._spec.executor.model
            # Databricks doesn't support web_search_preview; skip
            # OpenAI provider inference for all databricks-* models.
            if not model.startswith("databricks-"):
                from omnigent.llms.routing import parse_model_string

                llm_provider = parse_model_string(model).provider
        return WebSearchTool(config=config, llm_provider=llm_provider)

    def _create_web_fetch(self) -> Tool:
        """
        Build a WebFetchTool with the parent's spec.

        :returns: A WebFetchTool that inherits the parent's LLM config.
        """
        from omnigent.tools.builtins.web_fetch import WebFetchTool

        return WebFetchTool(parent_spec=self._spec)

    def _register_sub_agent_tools(self) -> None:
        """
        Register the sub-agent tool surface.

        The read-only discovery tools — ``sys_session_list``,
        ``sys_session_get_history``, and ``sys_session_get_info`` — are
        registered for **every** agent. Any agent can be part of a
        multi-agent session (most importantly a user-added agent that
        declares no sub-agents of its own but needs to read
        ``main``/siblings for context), so listing, peeking, and
        metadata reads are always available; access is enforced
        server-side (each proxies an auth-gated ``GET`` endpoint).
        ``peek`` / ``get_info`` take a ``conversation_id`` (from
        ``sys_session_list`` or a prior ``sys_session_send`` handle),
        so they need no ``sub_specs`` map.

        ``sys_session_share`` is gated by its OWN dedicated
        ``agent_session_sharing:`` flag (:class:`SharePolicy`),
        independent of the spawn grants (and unrelated to sharing via
        the server API or CLI). Sharing MUTATES access control — it can
        expose a session to a third party or, via ``__public__``, to
        anonymous read of the full transcript — and the server can
        confirm the caller holds manage-level access but cannot
        distinguish "the owner intended this" from "the agent was
        prompt-injected into sharing". So it is off unless the spec opts
        in: ``none`` (default) leaves it unregistered; ``non-public``
        registers it for granting named users; ``public`` additionally
        lets it grant ``__public__`` (the tool advertises and enforces
        that extra tier via ``allow_public``).

        The spawn-lifecycle tools are a SEPARATE opt-in, gated behind
        ``tools.agents`` (declared sub-agents) or the top-level
        ``spawn: true`` flag:

        - ``tools.agents`` registers ``sys_session_send`` (named
          ``(agent, title)`` mode limited to the declared-type enum,
          plus ``session_id`` mode for driving existing children),
          ``sys_session_close``, and ``sys_list_models`` (per-worker
          model availability for picking a valid ``args.model``) — the
          agent may spawn THE SPECIFIED LIST of sub-agents, nothing else.
        - ``spawn: true`` additionally registers ``sys_session_create``
          — launching arbitrary children from an existing agent_id or a
          custom locally-authored bundle (``config_path``). It also
          registers send/close (an agent must be able to drive and
          tombstone the children it creates); without declared
          sub-agents, send's schema omits the named-mode parameters.

        The spawn writes are child-only and ``sys_session_share`` is
        owner-authority-bounded, both enforced at dispatch/server level;
        the opt-ins control advertisement, not authority.
        (Cancellation uses the unified ``sys_cancel_task``; inspection
        is via inbox auto-delivery.)
        """
        # Read-only discovery tools: always available.
        self._tools[SysSessionListTool.name()] = SysSessionListTool()
        self._tools[SysSessionGetHistoryTool.name()] = SysSessionGetHistoryTool()
        self._tools[SysSessionGetInfoTool.name()] = SysSessionGetInfoTool()

        # Session sharing: opt-in via the dedicated
        # ``agent_session_sharing`` flag, independent of spawn / declared
        # sub-agents. ``none`` leaves it unregistered; ``public``
        # additionally permits __public__ grants (the tool reflects that
        # in its schema and the runner enforces it). It is its own flag —
        # not folded into the spawn grant — because letting the agent
        # expose a session is a distinct authority from spawning
        # children, and the public tier warrants an explicit extra opt-in
        # given the prompt-injection exposure.
        if self._spec.agent_session_sharing is not SharePolicy.NONE:
            self._tools[SysSessionShareTool.name()] = SysSessionShareTool(
                allow_public=self._spec.agent_session_sharing is SharePolicy.PUBLIC,
            )

        # send + close: opt-in via declared sub-agents or spawn: true.
        if not (self._spec.tools.agents or self._spec.spawn):
            return

        sub_specs = {sa.name: sa for sa in self._spec.sub_agents if sa.name is not None}
        self._tools[SysSessionSendTool.name()] = SysSessionSendTool(
            sub_specs=sub_specs,
        )
        self._tools[SysSessionCloseTool.name()] = SysSessionCloseTool()
        # Model awareness pairs with the dispatch grant: the per-worker
        # listing exists to pick a valid ``args.model`` for send.
        self._tools[SysListModelsTool.name()] = SysListModelsTool(spec=self._spec)

        # create: spawning OUTSIDE the declared list (existing agents
        # by id, or custom bundles via config_path) requires the
        # explicit ``spawn: true`` grant — declaring tools.agents
        # alone only permits the specified sub-agent types.
        if self._spec.spawn:
            self._tools[SysSessionCreateTool.name()] = SysSessionCreateTool()

    def _register_agent_mgmt_tools(self) -> None:
        """
        Register the read-only ``sys_agent_*`` discovery tools.

        ``sys_agent_get``, ``sys_agent_download``, and ``sys_agent_list``
        are registered for **every** agent, mirroring the always-on
        session reads in :meth:`_register_sub_agent_tools`. All three are
        global reads bounded by the server's per-user permission model —
        they proxy auth-gated ``GET /v1/sessions/{id}/agent``,
        ``.../agent/contents``, and ``GET /v1/agents`` + ``/v1/sessions``
        endpoints (``sys_agent_list`` also scans the agent's own local
        config dir), so registration grants no access the caller didn't
        already have. They are runner-dispatched and need no
        construction-time wiring.
        """
        self._tools[SysAgentGetTool.name()] = SysAgentGetTool()
        self._tools[SysAgentDownloadTool.name()] = SysAgentDownloadTool()
        self._tools[SysAgentListTool.name()] = SysAgentListTool()

    def _register_task_lifecycle_tools(self) -> None:
        """
        Auto-register ``sys_cancel_task``.

        ``sys_cancel_task`` is registered unconditionally on every
        agent: any dispatched handle's system message references
        it — that promise only holds if it's always in the schema.

        ``list_tasks`` has been removed (the tasks table was removed;
        it always returned an empty list in production). ``check_task``
        was dropped per design step 11 in favour of the inbox-based
        pattern (``sys_call_async`` + ``sys_read_inbox`` + auto-delivery).

        Idempotent: explicit registrations elsewhere win.
        """
        from omnigent.tools.builtins.async_inbox import SysCancelTaskTool

        # Idempotent: explicit registration wins.
        if SysCancelTaskTool.name() not in self._tools:
            self._tools[SysCancelTaskTool.name()] = SysCancelTaskTool()

    def _register_comment_tools(self) -> None:
        """
        Auto-register ``list_comments`` and ``update_comment``.

        Both tools are framework-owned and always available to the
        agent so it can read and update review comments left by the
        user without the spec explicitly opting in. They are session-
        scoped at invoke time via ``ToolContext.conversation_id``
        (W1/W2 multi-user guard — the agent cannot query another
        session's comments by supplying a different id).
        """
        self._tools[ListCommentsTool.name()] = ListCommentsTool()
        self._tools[UpdateCommentTool.name()] = UpdateCommentTool()

    def _register_work_management_tools(self) -> None:
        """
        Auto-register the work-item / schedule builtins (#12).

        These (#3 work items, #6 loops & monitors) are framework-owned and
        always available so an agent — and the Omnigent MCP surface — can
        create and track work items and manage schedules without the spec
        opting in. They're instantiated straight from the builtin registry
        (like the comment tools) rather than declared in ``tools.builtins``,
        which avoids the declared-builtin function-tool callable-recovery
        path. Each tool returns a clear error at invoke time when its backing
        store isn't configured, so registering them unconditionally is safe.
        """
        for name in (
            "create_work_item",
            "list_work_items",
            "update_work_item",
            "create_loop",
            "create_monitor",
            "list_schedules",
            "delete_schedule",
        ):
            tool = get_builtin_tool(name)
            if tool is not None:
                self._tools[name] = tool

    def _register_os_env_tools(self) -> None:
        """
        Register ``sys_os_*`` tools when the spec declares ``os_env``.

        When a pre-resolved :class:`OSEnvironment` was provided to
        the constructor (from ``SessionResourceRegistry``), that
        shared instance is used directly. Otherwise, falls back to
        creating a new instance via ``create_os_environment()``.

        When the spec declares no os_env and no pre-resolved env
        was provided, this is a no-op.

        :raises ValueError: If a ``sys_os_*`` name collides with
            an already-registered tool.
        """
        from omnigent.tools.builtins.os_env import build_os_env_tools

        os_env = self._pre_resolved_os_env
        if os_env is None:
            from omnigent.inner.os_env import create_os_environment

            os_env_spec_obj = self._spec.os_env
            if os_env_spec_obj is None:
                return
            os_env = create_os_environment(os_env_spec_obj)
            if os_env is None:
                return

        self._os_env = os_env
        for tool in build_os_env_tools(os_env):
            if tool.name() in self._tools:
                raise ValueError(
                    f"sys_os_* tool {tool.name()!r} collides with an "
                    f"already-registered tool — investigate the offending "
                    f"earlier registration before re-enabling os_env."
                )
            self._tools[tool.name()] = tool

    def _register_terminal_tools(self) -> None:
        """
        Register ``sys_terminal_*`` tools when the spec declares ``terminals``.

        Per ``designs/OMNIGENT_TERMINAL_BRIDGE.md`` §4.3, the five
        ``sys_terminal_launch`` / ``send`` / ``read`` / ``list`` /
        ``close`` tools register together when the spec carries a
        non-empty ``terminals`` block. They share the AP-process
        :class:`TerminalRegistry` (looked up via
        :func:`omnigent.runtime.get_terminal_registry`) so state
        survives across turns within a conversation.

        When the spec declares no terminals, this is a no-op.

        :raises ValueError: If a ``sys_terminal_*`` name collides
            with an already-registered tool — defensive; none of
            AP's other builtins use that prefix today.
        """
        if not self._spec.terminals:
            return
        from omnigent.runtime import get_terminal_registry
        from omnigent.tools.builtins.sys_terminal import (
            SysTerminalCloseTool,
            SysTerminalLaunchTool,
            SysTerminalListTool,
            SysTerminalReadTool,
            SysTerminalSendTool,
        )

        registry = get_terminal_registry()
        for tool in (
            SysTerminalLaunchTool(spec=self._spec, registry=registry),
            SysTerminalSendTool(registry=registry),
            SysTerminalReadTool(registry=registry),
            SysTerminalListTool(registry=registry),
            SysTerminalCloseTool(registry=registry),
        ):
            if tool.name() in self._tools:
                raise ValueError(
                    f"sys_terminal_* tool {tool.name()!r} collides with an "
                    f"already-registered tool — investigate the offending "
                    f"earlier registration."
                )
            self._tools[tool.name()] = tool

    def _register_local_tools(self, workdir: Path | None) -> None:
        """
        Load and register local Python tools from the agent image.

        Each ``@tool``-decorated function in ``tools/python/*.py``
        becomes one tool. Name collisions with already-registered
        tools (built-ins or earlier local tools) fail loud at load
        time per G27. If ``workdir`` is ``None`` or the spec has
        no local tools, this is a no-op.

        :param workdir: The agent image directory, or ``None``.
        :raises LocalToolLoadError: If any tool file fails to load
            or any name collides with a built-in.
        """
        if not self._spec.local_tools:
            return
        # Split local tools by runtime. ``runtime: client`` entries
        # have no server-side callable (no ``path``, no ``callable:``),
        # so the file-based / dotted-callable loaders below would
        # crash on them. They're registered separately as
        # :class:`ClientSideTool` instances using the explicit
        # ``parameters`` block from the spec — schema visible to the
        # LLM, dispatch short-circuited to ``action_required`` via
        # :meth:`is_client_side_tool`.
        server_local_tools = [
            t
            for t in self._spec.local_tools
            if t.runtime not in (ToolRuntime.CLIENT, ToolRuntime.UC_FUNCTION)
        ]
        client_local_tools = [t for t in self._spec.local_tools if t.runtime == ToolRuntime.CLIENT]
        uc_function_tools = [
            t for t in self._spec.local_tools if t.runtime == ToolRuntime.UC_FUNCTION
        ]
        for client_info in client_local_tools:
            if not is_valid_tool_name(client_info.name):
                _logger.warning(
                    "Spec-declared client local tool %r has invalid name — skipping",
                    client_info.name,
                )
                continue
            if client_info.name in self._tools:
                # Collision with a builtin or earlier registration —
                # fail loud rather than silently override (matches
                # the server-side path's G27 collision discipline).
                raise ValueError(
                    f"spec-declared client local tool {client_info.name!r} "
                    f"collides with an already-registered tool"
                )
            # The validator (see ``omnigent/spec/validator.py``)
            # enforces that ``runtime: client`` tools carry an
            # explicit ``parameters`` block — no callable to
            # introspect, so the schema must be authoritative.
            # Fail loud here if the invariant is broken rather
            # than substituting a synthetic empty-parameters
            # default that would mask a validator regression.
            if client_info.parameters is None:
                raise ValueError(
                    f"spec-declared client local tool {client_info.name!r} "
                    f"has no ``parameters`` block — the spec validator "
                    f"is supposed to require one for runtime: client tools"
                )
            schema: dict[str, Any] = {
                "type": "function",
                "function": {
                    "name": client_info.name,
                    "parameters": client_info.parameters,
                },
            }
            self._tools[client_info.name] = ClientSideTool(
                ClientSideToolSpec(name=client_info.name, schema=schema),
            )
        # UC function tools — dispatched by the runner via the SQL
        # Statement Execution API. The tool manager only needs to
        # expose the schema to the LLM; actual execution goes
        # through ``_execute_uc_function_tool`` in tool_dispatch.
        for uc_info in uc_function_tools:
            if not is_valid_tool_name(uc_info.name):
                _logger.warning(
                    "UC function tool %r has invalid name — skipping",
                    uc_info.name,
                )
                continue
            if uc_info.name in self._tools:
                raise ValueError(
                    f"UC function tool {uc_info.name!r} collides with an already-registered tool"
                )
            uc_schema: dict[str, Any] = {
                "type": "function",
                "function": {
                    "name": uc_info.name,
                    "parameters": uc_info.parameters
                    or {
                        "type": "object",
                        "properties": {},
                    },
                },
            }
            if uc_info.description:
                uc_schema["function"]["description"] = uc_info.description
            self._tools[uc_info.name] = _UCFunctionSchemaTool(
                tool_name=uc_info.name,
                schema=uc_schema,
            )
        # Native Omnigent local tools (``language == "python"``) need a
        # workdir on disk so the subprocess loader can locate the
        # ``tools/python/*.py`` files. Omnigent-style tools
        # (``language == "omnigent-python-callable"``) come
        # from a dotted import path with no on-disk presence and
        # don't need ``workdir``.
        if workdir is not None:
            for tool in load_local_python_tools(
                server_local_tools,
                workdir,
                sandbox_config=self._spec.tools.sandbox,
                srt_available=self._srt_available,
                uv_available=self._uv_available,
                sandbox_enabled=self._sandbox_enabled,
                agent_name=self._spec.name,
                # Pass the names of already-registered tools (builtins
                # at this point) so the loader can detect collisions
                # at G27 strictness — fail loud, not silent shadowing.
                builtin_tool_names=frozenset(self._tools.keys()),
            ):
                if not is_valid_tool_name(tool.name()):
                    _logger.warning(
                        "Local tool %r has invalid name — skipping",
                        tool.name(),
                    )
                    continue
                self._tools[tool.name()] = tool
        # Omnigent-style callable tools — sibling loader for the
        # ``omnigent-python-callable`` language entries the YAML
        # translator emits. See
        # :mod:`omnigent.tools.local_callable` for the reasoning;
        # in short, these wrap dotted Python callables imported
        # in-process and don't need the subprocess sandbox the
        # file-based path uses.
        from omnigent.tools.local_callable import load_local_callable_tools

        for callable_tool in load_local_callable_tools(server_local_tools):
            if not is_valid_tool_name(callable_tool.name()):
                _logger.warning(
                    "Omnigent callable tool %r has invalid name — skipping",
                    callable_tool.name(),
                )
                continue
            if callable_tool.name() in self._tools:
                # Collision with a builtin or earlier registration —
                # fail loud rather than silently override (matches
                # the file-based path's G27 collision discipline).
                raise ValueError(
                    f"omnigent callable tool {callable_tool.name()!r} "
                    f"collides with an already-registered tool"
                )
            self._tools[callable_tool.name()] = callable_tool

    def _register_client_tools(
        self,
        specs: list[ClientSideToolSpec],
    ) -> None:
        """
        Register client-specified tools.

        Raises :class:`OmnigentError` if a tool name violates the
        OpenAI function-calling constraint
        (``^[a-zA-Z0-9_-]{1,256}$``). If a client tool name collides
        with an already-registered tool (e.g. a built-in skill tool),
        the client tool wins and a warning is logged.

        :param specs: List of :class:`ClientSideToolSpec` objects to
            register, e.g.
            ``[ClientSideToolSpec(name="get_weather", ...)]``.
        :raises OmnigentError: If any tool name is invalid.
        """
        for spec in specs:
            if not is_valid_tool_name(spec.name):
                raise OmnigentError(
                    f"Invalid client tool name {spec.name!r}: must match [a-zA-Z0-9_-]{{1,256}}",
                    code=ErrorCode.INVALID_INPUT,
                )
            if spec.name in self._tools:
                _logger.warning(
                    "Client-specified tool %r shadows existing tool — overwriting",
                    spec.name,
                )
            self._tools[spec.name] = ClientSideTool(spec)

    def start(self) -> None:
        """No-op marker; all tools registered at init. MCP lives on runner."""
        self._started = True

    def shutdown(self) -> None:
        """Idempotent teardown: close OS environment and shut down tools.

        Closes the OS environment that was created during registration
        (skipped when the environment was pre-resolved from the
        ``SessionResourceRegistry`` — the registry owns that lifecycle).
        Then calls ``shutdown()`` on every registered tool so
        subprocess-backed tools can kill lingering child processes.

        Safe to call multiple times; the second call is a no-op.
        """
        self._started = False

        os_env = self._os_env
        if os_env is not None and self._pre_resolved_os_env is None:
            self._os_env = None
            try:
                os_env.close()
            except Exception:
                _logger.warning("os_env.close() failed during shutdown", exc_info=True)

        for tool in self._tools.values():
            try:
                tool.shutdown()
            except Exception:
                _logger.warning("tool %s shutdown failed", tool.name(), exc_info=True)

    def get_tool_names(self) -> list[str]:
        """
        Return the names of all registered tools.

        :returns: Tool names, e.g. ``["sys_session_send",
            "load_skill", "web_search"]``.
        """
        return list(self._tools.keys())

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        """
        Return OpenAI-format tool schemas for all registered
        tools.

        Each tool's schema is built independently. If a single tool's
        ``get_schema()`` raises — e.g. a ``type: function`` tool whose
        dotted ``callable`` path is unimportable — that one tool is
        skipped with a warning rather than aborting the whole list. This
        keeps one bad tool from silently dropping the agent's entire tool
        surface (#378); the remaining, valid tools are still advertised.

        :returns: A list of OpenAI tool schema dicts, each
            with ``"type": "function"`` and a ``"function"``
            sub-dict describing the tool.
        """
        schemas: list[dict[str, Any]] = []
        for name, tool in self._tools.items():
            try:
                schemas.append(tool.get_schema())
            except Exception:
                _logger.warning(
                    "Skipping tool %r: schema build failed; other tools are unaffected.",
                    name,
                    exc_info=True,
                )
        return schemas

    def get_tool(self, name: str) -> Tool | None:
        """
        Look up a registered tool by name.

        :param name: The tool function name, e.g. ``"load_skill"``.
        :returns: The :class:`Tool` instance, or ``None`` if not
            registered.
        """
        return self._tools.get(name)

    def call_tool(
        self,
        name: str,
        arguments: str,
        ctx: ToolContext,
    ) -> str:
        """
        Dispatch a tool call to the registered handler.

        :param name: The tool function name, e.g.
            ``"load_skill"`` or ``"github_list_issues"``.
        :param arguments: JSON-encoded arguments string from
            the LLM, e.g. ``'{"name": "summarize"}'``.
        :param ctx: Server-side execution context with task
            and agent identity.
        :returns: The tool's string result, or an error
            message if the tool is not registered.
        """
        tool = self._tools.get(name)
        if tool is None:
            return f"Error: tool {name!r} not found. Registered tools: {list(self._tools.keys())}"
        return tool.invoke(arguments, ctx)

    def get_client_tool_schemas(self) -> list[dict[str, Any]]:
        """
        Return the raw OpenAI-format schemas for all registered
        client-side tools.

        A tool is client-side if it was registered as a
        :class:`ClientSideTool` instance. Two sources produce
        :class:`ClientSideTool` registrations:

        - Request-time: each entry in the ``client_tool_specs``
          constructor argument (from the API request body).
        - Spec-declared: each :attr:`AgentSpec.local_tools` entry
          whose ``runtime`` is :attr:`ToolRuntime.CLIENT`.

        Both paths produce indistinguishable :class:`ClientSideTool`
        registrations from the workflow's perspective.

        Used by :class:`SpawnTool` to propagate client tools to
        sub-agent workflows — the sub-agent's LLM needs the schemas
        so it knows which tools are available.

        Each client tool's schema is built independently. If a single
        tool's ``get_schema()`` raises, that one tool is skipped with a
        warning rather than aborting the whole list, mirroring
        :meth:`get_tool_schemas` (#378). This keeps one bad client tool
        from silently dropping every client tool propagated to a
        sub-agent; the remaining, valid tools are still advertised.

        :returns: List of tool schema dicts, e.g.
            ``[{"type": "function", "function": {"name": "Read", ...}}]``.
            Empty list if no client tools are registered.
        """
        schemas: list[dict[str, Any]] = []
        for name, tool in self._tools.items():
            if not isinstance(tool, ClientSideTool):
                continue
            try:
                schemas.append(tool.get_schema())
            except Exception:
                _logger.warning(
                    "Skipping client tool %r: schema build failed; other tools are unaffected.",
                    name,
                    exc_info=True,
                )
        return schemas

    def is_client_side_tool(self, name: str) -> bool:
        """
        Return ``True`` if the named tool should be dispatched as
        ``action_required`` instead of executed server-side.

        A tool is client-side iff it's registered as a
        :class:`ClientSideTool` instance — the same predicate
        :meth:`get_client_tool_schemas` uses. Both the request-time
        ``client_tool_specs`` path and the spec-declared
        ``runtime: client`` path register :class:`ClientSideTool`
        instances, so this single check covers both.

        Used by the agent loop to detect when the LLM has invoked a
        client-side tool. On detection, the workflow persists the
        ``function_call`` items, streams them to the caller, and
        completes the response without executing any tools server-side.

        :param name: The tool function name, e.g. ``"get_weather"``.
        :returns: ``True`` if the tool is registered as a
            :class:`ClientSideTool`, ``False`` if the tool is not
            registered or is server-side.
        """
        return isinstance(self._tools.get(name), ClientSideTool)
