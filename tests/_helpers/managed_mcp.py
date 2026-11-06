"""Claude Code's ``${VAR}`` expansion for entries in an enterprise ``managed-mcp.json``."""

from __future__ import annotations

import re
from collections.abc import Mapping

_REFERENCE = re.compile(r"\$\{(\w+)(?::-([^}]*))?\}")


def expand_entry_value(value: str, env: Mapping[str, str]) -> str:
    """Expand ``${VAR}`` and ``${VAR:-default}`` in one entry value as Claude Code does.

    An unset or empty ``VAR`` takes the default, or nothing without one.
    """
    return _REFERENCE.sub(lambda match: env.get(match.group(1)) or match.group(2) or "", value)
