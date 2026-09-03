from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from omnigent.entities import DpiaCaseRecord, DpiaCaseRevision


class DpiaCaseConflictError(RuntimeError):
    def __init__(self, current_revision: int) -> None:
        super().__init__(f"DPIA case revision conflict; current revision is {current_revision}")
        self.current_revision = current_revision


class DpiaCaseStore(ABC):
    def __init__(self, storage_location: str) -> None:
        self.storage_location = storage_location

    @abstractmethod
    def get_case(self, case_id: str) -> DpiaCaseRecord | None: ...

    @abstractmethod
    def list_cases(self) -> list[DpiaCaseRecord]: ...

    @abstractmethod
    def list_revisions(self, case_id: str, *, limit: int = 100) -> list[DpiaCaseRevision]: ...

    @abstractmethod
    def get_revision(self, case_id: str, revision: int) -> DpiaCaseRevision | None: ...

    @abstractmethod
    def save_case(
        self,
        case_id: str,
        snapshot: dict[str, Any],
        *,
        expected_revision: int,
        actor: str,
    ) -> DpiaCaseRecord: ...
