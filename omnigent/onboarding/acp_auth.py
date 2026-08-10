"""Generic ACP-agent registry for ``omnigent setup`` and the runtime.

The generic ``acp`` harness (see :func:`omnigent.runtime.workflow._build_acp_spawn_env`
and :mod:`omnigent.inner.acp_harness`) drives *any* agent that speaks the Agent
Client Protocol. Which agents are available is pure user config: a list of named
commands in a dedicated top-level ``acp:`` block of ``~/.omnigent/config.yaml``::

    acp:
      agents:
        - {name: Gemini CLI,  command: gemini --experimental-acp}
        - {name: Claude Code, command: npx -y @zed-industries/claude-code-acp}
        - {name: Goose,       command: goose acp, model: gpt-5.3}
        - {name: Grok Build,  command: grok agent stdio, env_passthrough: [XAI_API_KEY]}

Each agent gets a stable ``slug`` derived from its name; a picked
``acp:<slug>`` (carried in the spec, resolved at spawn) looks the command back up
here. Auth is each agent's own — Omnigent stores no credential, so unlike the
``providers:`` / ``cursor:`` blocks there is no secret reference. An agent that
authenticates from an environment variable names it in ``env_passthrough``
(names only, never values): the spawn env is deny-by-default, so an undeclared
variable does not reach the agent. A dedicated block (not the shared gateway
``auth:``) keeps these commands from being mis-consumed by the SDK harnesses.

This module is pure read + settings-builder (mirroring
:mod:`omnigent.onboarding.cursor_auth`): the CLI orchestrates writes through
:func:`omnigent.cli._save_global_config` so there is no cli↔onboarding cycle.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass

from omnigent.onboarding.provider_config import load_config

# The dedicated top-level config block and the list field inside it.
ACP_CONFIG_KEY = "acp"
_AGENTS_FIELD = "agents"


@dataclass(frozen=True)
class AcpAgentEntry:
    """One configured ACP agent.

    :param slug: Stable id derived from :attr:`name` (see :func:`slugify`); the
        addressable half of the ``acp:<slug>`` harness id.
    :param name: Human display name, e.g. ``"Gemini CLI"``.
    :param command: The command to launch, e.g. ``"gemini --experimental-acp"``.
    :param model: Optional model id (only honored by agents that accept a model
        in ``session/new``; see :class:`omnigent.inner.acp_executor.AcpAgentConfig`).
    :param session_id_mode: ``"server"`` (default) or ``"client"``.
    :param send_model: Send the model in ``session/new`` (Qwen-shaped agents).
    :param omnigent_mcp: Lend Omnigent's builtin MCP relay in ``session/new``.
    :param env_passthrough: Environment variable *names* the agent may read at
        spawn, e.g. ``("XAI_API_KEY",)``. The spawn env is deny-by-default and
        the executor cannot know which variable an arbitrary agent
        authenticates with, so an agent that reads one must name it here or it
        starts unauthenticated. Names only — values are read from the host
        environment at spawn, never stored in the config file.
    """

    slug: str
    name: str
    command: str
    model: str | None = None
    session_id_mode: str = "server"
    send_model: bool = False
    omnigent_mcp: bool = True
    env_passthrough: tuple[str, ...] = ()


def slugify(name: str) -> str:
    """Derive a stable, URL-safe slug from an agent name.

    Lowercases, replaces runs of non-alphanumerics with a single ``-``, and
    trims leading/trailing ``-``. Empty results fall back to ``"agent"`` so a
    name of only punctuation still yields an addressable slug.

    :param name: The agent's display name, e.g. ``"Gemini CLI"``.
    :returns: The slug, e.g. ``"gemini-cli"``.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return slug or "agent"


def parse_env_passthrough(raw: object) -> tuple[str, ...]:
    """Parse an agent's ``env_passthrough`` into a tuple of variable names.

    Accepts a list of names, or a single name as a bare string. ``NAME=value``
    is rejected rather than accepted-and-ignored: writing a secret here would
    put it in plaintext in ``config.yaml`` and it would silently not reach the
    agent, so the mistake has to be loud.

    :param raw: The value read from the config row (any type).
    :returns: The declared names, de-duplicated in first-seen order.
    :raises ValueError: When the value isn't names, or a name carries a value.
    """
    if raw is None:
        return ()
    items = [raw] if isinstance(raw, str) else raw
    if not isinstance(items, list | tuple):
        raise ValueError("acp agent env_passthrough must be a list of variable names")
    names: list[str] = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("acp agent env_passthrough entries must be non-empty strings")
        name = item.strip()
        if "=" in name:
            raise ValueError(
                f"acp agent env_passthrough must list variable NAMES, not values: {name!r}. "
                "Export the variable in the environment and name it here."
            )
        if name not in names:
            names.append(name)
    return tuple(names)


def acp_agents(config: dict[str, object] | None = None) -> list[AcpAgentEntry]:
    """Return the configured ACP agents, each with a unique derived slug.

    Reads the ``acp:`` block's ``agents`` list. Malformed entries (not a dict,
    or missing ``name`` / ``command``) are skipped. Invalid ``omnigent_mcp``
    values raise ``ValueError`` instead of being silently coerced. Slugs are
    assigned in list order; a collision (two names slugifying the same) gets a
    ``-2`` / ``-3`` … suffix so every returned entry is uniquely addressable.

    :param config: A pre-loaded config mapping; ``None`` loads
        ``~/.omnigent/config.yaml`` via :func:`load_config`.
    :returns: The configured agents (possibly empty).
    """
    cfg = load_config() if config is None else config
    block = cfg.get(ACP_CONFIG_KEY)
    if not isinstance(block, dict):
        return []
    raw_agents = block.get(_AGENTS_FIELD)
    if not isinstance(raw_agents, list):
        return []

    entries: list[AcpAgentEntry] = []
    seen: dict[str, int] = {}
    for raw in raw_agents:
        if not isinstance(raw, dict):
            continue
        name = raw.get("name")
        command = raw.get("command")
        if not isinstance(name, str) or not name.strip():
            continue
        if not isinstance(command, str) or not command.strip():
            continue
        base = slugify(name)
        count = seen.get(base, 0) + 1
        seen[base] = count
        slug = base if count == 1 else f"{base}-{count}"
        model = raw.get("model")
        mode = raw.get("session_id_mode")
        omnigent_mcp = raw.get("omnigent_mcp", True)
        if not isinstance(omnigent_mcp, bool):
            raise ValueError("acp agent omnigent_mcp must be a boolean")
        entries.append(
            AcpAgentEntry(
                slug=slug,
                name=name.strip(),
                command=command.strip(),
                model=model.strip() if isinstance(model, str) and model.strip() else None,
                session_id_mode=mode if mode in ("server", "client") else "server",
                send_model=bool(raw.get("send_model", False)),
                omnigent_mcp=omnigent_mcp,
                env_passthrough=parse_env_passthrough(raw.get("env_passthrough")),
            )
        )
    return entries


def resolve_acp_agent(slug: str, config: dict[str, object] | None = None) -> AcpAgentEntry | None:
    """Return the configured agent for *slug*, or ``None`` if not found.

    :param slug: The slug half of an ``acp:<slug>`` harness id.
    :param config: A pre-loaded config mapping; ``None`` loads the global config.
    :returns: The matching :class:`AcpAgentEntry`, or ``None``.
    """
    for entry in acp_agents(config):
        if entry.slug == slug:
            return entry
    return None


def acp_agents_settings(entries: list[AcpAgentEntry]) -> dict[str, object]:
    """Build the ``{"acp": {"agents": [...]}}`` settings dict for persistence.

    Handed to :func:`omnigent.cli._save_global_config` (a shallow update, so it
    replaces the whole ``acp:`` block). Only the user-authored fields are
    written back — the derived ``slug`` is not persisted.

    :param entries: The full desired agent list (after an add/remove).
    :returns: The settings dict to save.
    """
    agents: list[dict[str, object]] = []
    for e in entries:
        item: dict[str, object] = {"name": e.name, "command": e.command}
        if e.model:
            item["model"] = e.model
        if e.session_id_mode != "server":
            item["session_id_mode"] = e.session_id_mode
        if e.send_model:
            item["send_model"] = True
        if not e.omnigent_mcp:
            item["omnigent_mcp"] = False
        if e.env_passthrough:
            item["env_passthrough"] = list(e.env_passthrough)
        agents.append(item)
    return {ACP_CONFIG_KEY: {_AGENTS_FIELD: agents}}


def command_binary_on_path(command: str) -> bool:
    """Return whether a command's first token resolves on ``PATH``.

    A soft check for the setup readout / readiness — the agent owns its own
    install, so a missing binary is a *hint*, never a hard gate. Absolute /
    relative paths are checked for existence directly.

    :param command: The configured command, e.g. ``"gemini --experimental-acp"``.
    :returns: ``True`` when the first token is runnable.
    """
    import shlex

    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    if not parts:
        return False
    return shutil.which(parts[0]) is not None


@dataclass(frozen=True)
class AcpConfigSummary:
    """Readiness view of the ``acp:`` block for the setup readout."""

    configured: bool
    agents: tuple[AcpAgentEntry, ...]

    @property
    def count(self) -> int:
        return len(self.agents)


def acp_config_summary(config: dict[str, object] | None = None) -> AcpConfigSummary:
    """Summarize the configured ACP agents for ``omnigent setup``.

    :param config: A pre-loaded config mapping; ``None`` loads the global config.
    :returns: An :class:`AcpConfigSummary` — ``configured`` is ``True`` iff at
        least one agent is registered.
    """
    entries = acp_agents(config)
    return AcpConfigSummary(configured=bool(entries), agents=tuple(entries))
