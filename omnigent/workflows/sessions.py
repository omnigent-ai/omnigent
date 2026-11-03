"""Execute team turns using the existing Omnigent Sessions SDK."""

from __future__ import annotations

import asyncio
import logging

from omnigent_client import SessionsChat, SessionsNamespace
from omnigent_client._sessions import Session
from omnigent_client._tool_handler import ResponseEndCtx, StreamHooks

from omnigent.chat import _persisted_session_turn_text
from omnigent.harness_aliases import is_native_harness
from omnigent.session_host import set_session_host
from omnigent.workflows.engine import MemberRequest
from omnigent.workflows.models import ConnectedHost

logger = logging.getLogger(__name__)


class SessionExecutor:
    """Create one session per node/member, reusing the lead's session for synthesis.

    The caller owns the SDK transport, authentication, and any routing headers.
    Known sessions are interrupted on timeout/cancellation; completed sessions remain
    available in Omnigent and managed hosts follow their normal idle lifecycle.
    """

    def __init__(
        self, sessions: SessionsNamespace, *, sandbox_provider: str | None = None
    ) -> None:
        self._sessions = sessions
        self._sandbox_provider = sandbox_provider
        self._created: dict[tuple[str, str, str], Session] = {}

    async def __call__(self, request: MemberRequest) -> str:
        key = (request.run_id, request.node.id, request.member.id)
        session = self._created.get(key)
        if session is None and request.record.session_id:
            session = await self._sessions.get(request.record.session_id)
            self._created[key] = session
        if session is None:
            placement = request.member.placement
            session = await self._sessions.create_from_agent_id(
                request.member.agent_id,
                title=f"{request.node.id} / {request.member.id}",
                labels={
                    "omnigent.workflow.run": request.run_id,
                    "omnigent.workflow.node": request.node.id,
                    "omnigent.workflow.team": request.node.team,
                    "omnigent.workflow.member": request.member.id,
                },
                host_id=placement.host_id if isinstance(placement, ConnectedHost) else None,
                workspace=placement.workspace,
                host_type="external" if isinstance(placement, ConnectedHost) else "managed",
                sandbox_provider=None
                if isinstance(placement, ConnectedHost)
                else self._sandbox_provider or placement.provider,
            )
            self._created[key] = session
        request.record.session_id = session.id
        managed = not isinstance(request.member.placement, ConnectedHost)
        # Creates without a requested host ID launch on the default replica.
        set_session_host(session.id, session.host_id, keyless=managed)
        request.checkpoint()
        completed = False

        def check_completion(context: ResponseEndCtx) -> None:
            nonlocal completed
            if context.status != "completed":
                raise RuntimeError(f"Agent turn ended with status {context.status!r}")
            completed = True

        try:
            # Managed creates return before provisioning binds a host and runner.
            if managed:
                while not (session.host_id and session.runner_id):
                    launch = session.sandbox_status or {}
                    if launch.get("stage") == "failed":
                        raise RuntimeError(f"Managed session launch failed: {launch.get('error')}")
                    if (
                        session.status == "failed"
                        or session.last_task_error
                        or session.labels.get("omnigent.startup") == "failed"
                    ):
                        raise RuntimeError(
                            f"Managed session launch failed: {session.last_task_error}"
                        )
                    await asyncio.sleep(1)
                    session = await self._sessions.get(session.id)
                    set_session_host(session.id, session.host_id, keyless=True)
                self._created[key] = session
            if is_native_harness(session.harness):
                return await self._run_native_turn(session.id, request.prompt)
            chat = SessionsChat(
                namespace=self._sessions,
                session=session,
                files_uploader=None,
                files_getter=None,
                hooks=StreamHooks(on_response_end=check_completion),
            )
            result = await chat.query(request.prompt)
            if not completed:
                raise RuntimeError("Agent stream ended without a completed turn")
            output = result.text or await _persisted_session_turn_text(self._sessions, session.id)
            if not output:
                raise RuntimeError("Completed agent turn produced no text in stream or transcript")
            return output
        except (asyncio.CancelledError, Exception):
            try:
                async with asyncio.timeout(10):
                    await self._sessions.interrupt(session.id)
            except Exception as exc:
                request.record.cleanup_error = f"Could not interrupt session {session.id}: {exc}"
                logger.warning(
                    "Could not interrupt workflow session %s", session.id, exc_info=True
                )
            raise

    async def _run_native_turn(self, session_id: str, prompt: str) -> str:
        # Native bootstrap can emit response.completed before consuming the input.
        # Follow durable output and session status; the workflow owns the timeout.
        recent = await self._sessions.list_items(session_id, limit=1, order="desc")
        boundary = str(recent.data[0]["id"]) if recent.data else None
        await self._sessions.post_event(
            session_id,
            {
                "type": "message",
                "data": {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
            },
        )
        while True:
            output = await _persisted_session_turn_text(
                self._sessions, session_id, after_item_id=boundary
            )
            session = await self._sessions.get(session_id)
            if session.status == "failed" or session.last_task_error:
                raise RuntimeError(f"Native agent turn failed: {session.last_task_error}")
            if session.status == "idle" and output:
                return output
            await asyncio.sleep(1)
