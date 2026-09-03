from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class DpiaCaseRecord:
    workspace_id: int
    case_id: str
    revision: int
    snapshot: dict[str, Any]
    created_by: str
    updated_by: str
    created_at: int
    updated_at: int


@dataclass(frozen=True, slots=True)
class DpiaCaseRevision:
    workspace_id: int
    case_id: str
    revision: int
    snapshot: dict[str, Any]
    actor: str
    created_at: int
