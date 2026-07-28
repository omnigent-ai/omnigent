"""OpenClaw/acpx config bridge for ``omnigent setup``.

Reads a user's acpx/OpenClaw agent registry and converts it into Omnigent's
generic ``acp:`` agent entries. The bridge stores only launch commands; each
agent keeps its own authentication.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

import json5

from omnigent.onboarding.acp_auth import AcpAgentEntry, acp_agents, slugify

SourceKind = Literal["acpx", "openclaw"]


@dataclass(frozen=True)
class OpenClawAgentEntry:
    """One ACP agent discovered in an acpx/OpenClaw config file."""

    name: str
    command: str
    args: tuple[str, ...]
    source: SourceKind
    path: Path

    @property
    def command_line(self) -> str:
        """Return the shell command Omnigent should persist."""
        return shlex.join([self.command, *self.args])


@dataclass(frozen=True)
class OpenClawConfigError:
    """A config file or agent could not be read or normalized."""

    path: Path
    message: str


@dataclass(frozen=True)
class OpenClawDiscovery:
    """Result of reading the supported OpenClaw/acpx config locations."""

    agents: tuple[OpenClawAgentEntry, ...]
    errors: tuple[OpenClawConfigError, ...] = ()


def default_config_paths(home: Path | None = None) -> tuple[Path, Path]:
    """Return the default ``(acpx, OpenClaw)`` config paths."""
    root = Path.home() if home is None else home
    return root / ".acpx" / "config.json", root / ".openclaw" / "openclaw.json"


def discover_openclaw_agents(
    *,
    acpx_path: Path | None = None,
    openclaw_path: Path | None = None,
) -> OpenClawDiscovery:
    """Read supported acpx/OpenClaw config files and return normalized agents.

    Missing files are ignored. Unreadable or malformed entries are reported as
    soft errors without blocking valid agents from either location.
    """
    default_acpx_path, default_openclaw_path = default_config_paths()
    paths: tuple[tuple[SourceKind, Path], ...] = (
        ("acpx", acpx_path or default_acpx_path),
        ("openclaw", openclaw_path or default_openclaw_path),
    )
    agents: list[OpenClawAgentEntry] = []
    errors: list[OpenClawConfigError] = []
    for source, path in paths:
        try:
            if not path.exists():
                continue
        except OSError as exc:
            errors.append(OpenClawConfigError(path=path, message=str(exc)))
            continue
        discovery = read_openclaw_config(path, source=source)
        agents.extend(discovery.agents)
        errors.extend(discovery.errors)
    return OpenClawDiscovery(agents=tuple(agents), errors=tuple(errors))


def read_openclaw_config(
    path: Path,
    *,
    source: SourceKind | None = None,
) -> OpenClawDiscovery:
    """Read one selected acpx/OpenClaw config file.

    Both shapes accept JSON5 so auto-discovered and manually selected files
    have identical parsing. When ``source`` is omitted, detects the top-level
    acpx ``agents`` shape or wrapped OpenClaw registry. Files that match neither
    are rejected instead of being treated as an empty registry.
    """
    try:
        raw = _load_config(path)
        resolved_source = source or _detect_source(raw)
        agents, errors = _extract_agents(raw, source=resolved_source, path=path)
        return OpenClawDiscovery(agents=tuple(agents), errors=tuple(errors))
    except (OSError, ValueError) as exc:
        return OpenClawDiscovery(
            agents=(),
            errors=(OpenClawConfigError(path=path, message=str(exc)),),
        )


def openclaw_agents_to_acp_entries(
    agents: tuple[OpenClawAgentEntry, ...] | list[OpenClawAgentEntry],
) -> list[AcpAgentEntry]:
    """Translate normalized OpenClaw/acpx agents into ACP config entries."""
    entries: list[AcpAgentEntry] = []
    seen_slugs: dict[str, int] = {}
    seen_agents: set[tuple[str, str]] = set()
    for agent in agents:
        command = agent.command_line
        identity = (agent.name.casefold(), command)
        if identity in seen_agents:
            continue
        seen_agents.add(identity)
        base = slugify(agent.name)
        count = seen_slugs.get(base, 0) + 1
        seen_slugs[base] = count
        slug = base if count == 1 else f"{base}-{count}"
        entries.append(AcpAgentEntry(slug=slug, name=agent.name, command=command))
    return entries


def resolve_openclaw_acp_agent(
    identifier: str,
    *,
    discovery: OpenClawDiscovery | None = None,
) -> AcpAgentEntry | None:
    """Resolve one discovered agent by display name or derived ACP slug.

    Display-name matching is case-insensitive. Slugs remain exact so collision
    suffixes such as ``agent-2`` select the intended translated entry.
    """
    resolved_discovery = discovery or discover_openclaw_agents()
    entries = openclaw_agents_to_acp_entries(resolved_discovery.agents)
    wanted = identifier.strip()
    if not wanted:
        return None
    wanted_name = wanted.casefold()
    for entry in entries:
        if entry.name.casefold() == wanted_name:
            return entry
    return next((entry for entry in entries if entry.slug == wanted), None)


def merge_imported_acp_entries(
    imported: tuple[AcpAgentEntry, ...] | list[AcpAgentEntry],
    *,
    existing: list[AcpAgentEntry] | None = None,
) -> tuple[list[AcpAgentEntry], list[AcpAgentEntry]]:
    """Append imported ACP entries, preserving distinct slug collisions.

    Returns ``(merged, added)``. Exact name/command matches are skipped for
    idempotency. A different agent whose name maps to an occupied slug receives
    the next suffix so no user-authored or imported command is lost.
    """
    current = list(acp_agents() if existing is None else existing)
    seen_slugs = {entry.slug for entry in current}
    seen_agents = {(entry.name.casefold(), entry.command) for entry in current}
    added: list[AcpAgentEntry] = []
    for entry in imported:
        identity = (entry.name.casefold(), entry.command)
        if identity in seen_agents:
            continue
        base = slugify(entry.name)
        slug = base
        suffix = 2
        while slug in seen_slugs:
            slug = f"{base}-{suffix}"
            suffix += 1
        resolved = replace(entry, slug=slug)
        current.append(resolved)
        added.append(resolved)
        seen_agents.add(identity)
        seen_slugs.add(slug)
    return current, added


def _load_config(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    try:
        return json5.loads(text)
    except RecursionError as exc:
        raise ValueError("invalid JSON5: nesting is too deep") from exc
    except ValueError as exc:
        raise ValueError(f"invalid JSON5: {exc}") from exc


def _extract_agents(
    raw: Any,
    *,
    source: SourceKind,
    path: Path,
) -> tuple[list[OpenClawAgentEntry], list[OpenClawConfigError]]:
    if not isinstance(raw, dict):
        raise ValueError("config root must be an object")

    raw_agents: Any
    if source == "acpx":
        raw_agents = raw.get("agents")
    else:
        plugins = _object(raw.get("plugins"))
        entries = _object(plugins.get("entries"))
        acpx = _object(entries.get("acpx"))
        config = _object(acpx.get("config"))
        raw_agents = config.get("agents")
    if raw_agents is None:
        return [], []
    if not isinstance(raw_agents, dict):
        raise ValueError("agents must be an object mapping name to config")

    agents: list[OpenClawAgentEntry] = []
    errors: list[OpenClawConfigError] = []
    for name, config in raw_agents.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(config, dict):
            continue
        command = config.get("command")
        if not isinstance(command, str) or not command.strip():
            continue
        try:
            args = _normalize_args(config.get("args"), agent_name=name)
        except ValueError as exc:
            errors.append(OpenClawConfigError(path=path, message=str(exc)))
            continue
        agents.append(
            OpenClawAgentEntry(
                name=name.strip(),
                command=command.strip(),
                args=args,
                source=source,
                path=path,
            )
        )
    return agents, errors


def _detect_source(raw: Any) -> SourceKind:
    if not isinstance(raw, dict):
        raise ValueError("config root must be an object")
    if "agents" in raw:
        return "acpx"
    plugins = raw.get("plugins")
    if isinstance(plugins, dict):
        entries = plugins.get("entries")
        if isinstance(entries, dict):
            acpx = entries.get("acpx")
            if isinstance(acpx, dict) and isinstance(acpx.get("config"), dict):
                return "openclaw"
    raise ValueError("file is not an OpenClaw/acpx agent registry")


def _normalize_args(raw: Any, *, agent_name: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,)
    if isinstance(raw, list):
        if all(isinstance(item, str) for item in raw):
            return tuple(raw)
    raise ValueError(f"agent {agent_name!r} args must be a string or list of strings")


def _object(raw: Any) -> dict[str, Any]:
    return raw if isinstance(raw, dict) else {}
