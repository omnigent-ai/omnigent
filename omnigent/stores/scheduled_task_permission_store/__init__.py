"""Read-and-copy grants for automation definitions, independent of session ACLs."""

from abc import ABC, abstractmethod


class ScheduledTaskPermissionStore(ABC):
    """Deployment seam for selected-person automation sharing."""

    @abstractmethod
    def grant(self, task_id: str, owner: str, user_id: str) -> None:
        """Share an owned task's definition with one recipient."""

    @abstractmethod
    def revoke(self, task_id: str, user_id: str) -> None:
        """Remove a recipient's access."""

    @abstractmethod
    def recipients(self, task_id: str) -> list[str]:
        """List the task's recipients."""

    @abstractmethod
    def can_read(self, task_id: str, user_id: str) -> bool:
        """Check a recipient's current grant in this workspace."""

    @abstractmethod
    def shared_task_ids(self, user_id: str) -> list[str]:
        """List task IDs granted to the caller in this workspace."""

    @abstractmethod
    def delete_for_task(self, task_id: str) -> None:
        """Remove grants when their source task is deleted."""
