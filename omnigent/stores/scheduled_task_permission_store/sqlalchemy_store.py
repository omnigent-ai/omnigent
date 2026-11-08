"""SQL storage for workspace-scoped automation definition sharing."""

from sqlalchemy import delete, select

from omnigent.db.account_authority import account_generation, require_active_account
from omnigent.db.db_models import (
    SqlScheduledTask,
    SqlScheduledTaskPermission,
    current_workspace_id,
)
from omnigent.db.enum_codecs import encode_scheduled_task_state
from omnigent.db.utils import get_or_create_engine, make_named_managed_session_maker
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.stores.scheduled_task_permission_store import ScheduledTaskPermissionStore


class SqlAlchemyScheduledTaskPermissionStore(ScheduledTaskPermissionStore):
    def __init__(self, storage_location: str) -> None:
        engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.scheduled_task_permission_store"
        )
        self._write = make_named_managed_session_maker(
            engine, query_name_prefix="omnigent.scheduled_task_permission_store", immediate=True
        )

    def grant(self, task_id: str, owner: str, user_id: str) -> None:
        with self._write("share_definition") as session:
            generation = require_active_account(
                session, user_id, related_accounts={owner: account_generation(owner)}
            )
            task = session.get(
                SqlScheduledTask, (current_workspace_id(), task_id), with_for_update=True
            )
            if (
                task is None
                or task.user_id != owner
                or task.state == encode_scheduled_task_state("deleted")
            ):
                raise OmnigentError("Scheduled task not found", code=ErrorCode.NOT_FOUND)
            session.merge(
                SqlScheduledTaskPermission(
                    workspace_id=current_workspace_id(),
                    scheduled_task_id=task_id,
                    user_id=user_id,
                    account_generation=generation,
                )
            )

    def revoke(self, task_id: str, user_id: str) -> None:
        with self._write("revoke_definition_access") as session:
            require_active_account(session, None)
            session.execute(
                delete(SqlScheduledTaskPermission).where(
                    SqlScheduledTaskPermission.workspace_id == current_workspace_id(),
                    SqlScheduledTaskPermission.scheduled_task_id == task_id,
                    SqlScheduledTaskPermission.user_id == user_id,
                )
            )

    def recipients(self, task_id: str) -> list[str]:
        with self._session("list_recipients") as session:
            return list(
                session.scalars(
                    select(SqlScheduledTaskPermission.user_id)
                    .where(
                        SqlScheduledTaskPermission.workspace_id == current_workspace_id(),
                        SqlScheduledTaskPermission.scheduled_task_id == task_id,
                    )
                    .order_by(SqlScheduledTaskPermission.user_id)
                )
            )

    def can_read(self, task_id: str, user_id: str) -> bool:
        with self._session("check_definition_access") as session:
            grant = session.get(
                SqlScheduledTaskPermission, (current_workspace_id(), task_id, user_id)
            )
            return grant is not None and grant.account_generation == account_generation(user_id)

    def shared_task_ids(self, user_id: str) -> list[str]:
        with self._session("list_shared_definitions") as session:
            return list(
                session.scalars(
                    select(SqlScheduledTaskPermission.scheduled_task_id)
                    .where(
                        SqlScheduledTaskPermission.workspace_id == current_workspace_id(),
                        SqlScheduledTaskPermission.user_id == user_id,
                        SqlScheduledTaskPermission.account_generation
                        == account_generation(user_id),
                    )
                    .order_by(SqlScheduledTaskPermission.scheduled_task_id)
                )
            )

    def delete_for_task(self, task_id: str) -> None:
        with self._write("remove_task_shares") as session:
            session.execute(
                delete(SqlScheduledTaskPermission).where(
                    SqlScheduledTaskPermission.workspace_id == current_workspace_id(),
                    SqlScheduledTaskPermission.scheduled_task_id == task_id,
                )
            )
