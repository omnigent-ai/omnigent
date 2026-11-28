"""User-configured MCP inheritance for Omnigent-launched Claude and Codex sessions.

``mcp: {inherit: false, allow: [names]}`` in the user config, optionally refined
per harness under ``harness.<id>.mcp``, keeps a launched session from loading the
MCP servers configured for the CLI it wraps. Omnigent's own ``omnigent`` server
is always allowed. Project configuration cannot change this setting.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, TypedDict

_logger = logging.getLogger(__name__)
OMNIGENT_SERVER: Final = "omnigent"
_KEY_INHERIT: Final = "inherit"
_KEY_ALLOW: Final = "allow"
_warned_lock = threading.Lock()
_warned: set[str] = set()


@dataclass(frozen=True, slots=True)
class McpInheritance:
    """Whether a launch keeps the wrapped CLI's MCP servers, and which survive if not."""

    inherit: bool = True
    allow: frozenset[str] = frozenset()

    def allows(self, name: str) -> bool:
        """Return whether a server named *name* may load in this launch."""
        return self.inherit or name == OMNIGENT_SERVER or name in self.allow

    def allowed_names(self) -> tuple[str, ...]:
        """Return the strict-mode allowlist, ``omnigent`` first, then sorted names."""
        return (OMNIGENT_SERVER, *sorted(self.allow - {OMNIGENT_SERVER}))


class _Fields(TypedDict, total=False):
    inherit: bool
    allow: frozenset[str]


def _warn_once(message: str) -> None:
    with _warned_lock:
        if message in _warned:
            return
        _warned.add(message)
    _logger.warning(message)


def _parse(value: object, path: str) -> _Fields:
    """Validate one ``mcp`` mapping; malformed fields warn once and are ignored."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        _warn_once(f"config `{path}` must be a mapping, got {type(value).__name__}; ignoring it")
        return {}
    fields: _Fields = {}
    inherit = value.get(_KEY_INHERIT)
    if isinstance(inherit, bool):
        fields["inherit"] = inherit
    elif inherit is not None:
        _warn_once(f"config `{path}.{_KEY_INHERIT}` must be true or false; ignoring it")
    allow = value.get(_KEY_ALLOW)
    if isinstance(allow, list) and all(isinstance(name, str) and name for name in allow):
        fields["allow"] = frozenset(allow)
    elif allow is not None:
        _warn_once(f"config `{path}.{_KEY_ALLOW}` must be a list of server names; ignoring it")
    return fields


def resolve_mcp_inheritance(
    executor: str, cfg: Mapping[str, object] | None = None
) -> McpInheritance:
    """Resolve the MCP inheritance for one launch of *executor*.

    :param executor: Canonical harness id, e.g. ``"claude-sdk"`` or ``"codex-native"``.
    :param cfg: User config; ``None`` reads the user-level ``config.yaml``.
    :returns: Per-harness fields over global ``mcp`` fields over inheriting everything.
    """
    if cfg is None:
        import yaml

        from omnigent.config import global_config_path, load_global_config

        try:
            cfg = load_global_config()
        except (OSError, ValueError, yaml.YAMLError) as error:
            _warn_once(
                f"cannot read MCP inheritance from {global_config_path()}: "
                f"{type(error).__name__}; inheriting all MCP servers"
            )
            return McpInheritance()
        if not isinstance(cfg, Mapping):
            _warn_once(
                f"cannot read MCP inheritance from {global_config_path()}: "
                "not a mapping; inheriting all MCP servers"
            )
            return McpInheritance()
    if not isinstance(cfg, Mapping):
        return McpInheritance()
    fields = _parse(cfg.get("mcp"), "mcp")
    harness = cfg.get("harness")
    if isinstance(harness, dict):
        from omnigent.harness_aliases import canonicalize_harness

        for key, entry in harness.items():
            if (
                isinstance(key, str)
                and isinstance(entry, dict)
                and (canonicalize_harness(key) or key) == executor
            ):
                fields.update(_parse(entry.get("mcp"), f"harness.{key}.mcp"))
    return McpInheritance(
        inherit=fields.get("inherit", True), allow=fields.get("allow", frozenset())
    )
