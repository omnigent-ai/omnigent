"""Transient, session-scoped requests to the file viewer."""

from fastapi import APIRouter, Request
from pydantic import BaseModel

from omnigent.runtime import session_stream
from omnigent.server.auth import LEVEL_EDIT, AuthProvider
from omnigent.server.routes._auth_helpers import get_user_id, require_access_and_level
from omnigent.server.schemas import UiOpenFileEvent
from omnigent.stores import ConversationStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.tools.builtins.ui import OpenFileRequest


class OpenFileResponse(BaseModel):
    """Delivery to live streams is not an acknowledgement from a file viewer."""

    subscribers: int


def register_ui_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> None:
    """Register the agent-facing UI endpoint."""

    @router.post(
        "/sessions/{session_id}/ui/open-file",
        status_code=202,
        response_model=OpenFileResponse,
    )
    async def open_file(
        request: Request, session_id: str, body: OpenFileRequest
    ) -> OpenFileResponse:
        """Ask current viewers to open a workspace-relative file.

        Requires session edit access. Requests are transient and never replayed.
        The subscriber count measures stream delivery, not successful display:
        background subscribers may ignore the request, and the viewer retains
        its normal filesystem permissions, error handling and unsaved-edit guard.
        """
        await require_access_and_level(
            get_user_id(request, auth_provider),
            session_id,
            LEVEL_EDIT,
            permission_store,
            conversation_store,
        )
        event = UiOpenFileEvent(type="ui.open_file", path=body.path)
        return OpenFileResponse(subscribers=session_stream.publish(session_id, event.model_dump()))
