"""Resource routes: list/get/create/delete session resources, files, filesystem."""

from __future__ import annotations

import asyncio
import mimetypes
import urllib.parse
from typing import Annotated, Any

import httpx
from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import Response

from omnigent.entities import (
    Conversation,
    StoredFile,
)
from omnigent.entities.session_resources import session_resource_view_to_dict
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.native_coding_agents import (
    native_coding_agent_for_terminal_name,
)
from omnigent.runner.routing import RunnerRouter
from omnigent.runtime.policies.approval import _ELICITATION_MODE
from omnigent.server._elicitation_registry import (
    _harness_elicitation_owners,
    _harness_elicitation_registry,
    _harness_parked_elicitations,
    _harness_pre_resolved_elicitations,
    _ParkedHarnessElicitation,
    _PreResolvedHarnessElicitation,
)
from omnigent.server.auth import (
    LEVEL_EDIT,
    LEVEL_READ,
    AuthProvider,
)
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes._auth_helpers import (
    get_user_id as _get_user_id,
)
from omnigent.server.routes._auth_helpers import (
    require_access_and_level as _require_access_and_level,
)
from omnigent.server.routes._content_type import (
    require_json_content_type,
)
from omnigent.server.routes._errors import session_not_found as _session_not_found
from omnigent.server.routes._origin import require_trusted_origin
from omnigent.server.routes._sessions.common import *
from omnigent.server.routes._sessions.common import (
    get_server_runner_router,
    set_server_runner_router,
)
from omnigent.server.routes._sessions.helpers import *
from omnigent.server.routes._sessions.helpers import _load_agent_spec_for_session
from omnigent.server.routes._sessions.orchestration import *
from omnigent.server.schemas import (
    CopiedFile,
    CopyFilesRequest,
    CopyFilesResponse,
    SessionResourceObject,
    SessionResourcePaginatedList,
)
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.file_store import FileStore
from omnigent.stores.permission_store import PermissionStore


def register_resources_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    file_store: FileStore | None = None,
    artifact_store: ArtifactStore | None = None,
    runner_router: RunnerRouter | None = None,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    host_registry: HostRegistry | None = None,
) -> None:
    """Register the resources routes on router."""

    @router.get(
        "/sessions/{session_id}/resources",
        response_model=SessionResourcePaginatedList,
        response_model_exclude_none=True,
    )
    async def list_session_resources(
        request: Request,
        session_id: str,
        # Shadows the ``type`` builtin deliberately: FastAPI maps the
        # parameter name to the wire query param, which is ``?type=``.
        type: str | None = Query(default=None),
    ) -> SessionResourcePaginatedList:
        """
        Return the runner-authoritative resource inventory for a session.

        Requires the session to be bound to a runner via
        ``PATCH /v1/sessions/{id}``; raises ``conflict`` otherwise.
        The server validates the session exists, then proxies to the
        runner's ``GET /v1/sessions/{id}/resources`` endpoint. In
        unit-test / in-process setups with no runner router/client, the
        route falls back to adapting the local terminal registry.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param type: Optional resource-type filter, e.g.
            ``"environment"`` / ``"terminal"`` / ``"file"``. Forwarded
            to the runner (its registry applies it) and honored by the
            local-registry fallback and the file-store merge below.
        """
        user_id = _get_user_id(request, auth_provider)
        access = await _require_access_and_level(
            user_id, session_id, LEVEL_READ, permission_store, conversation_store
        )
        if access.conversation is None:
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
            if conv is None:
                raise _session_not_found()
        runner_client = await _get_runner_client_for_resource_access(session_id)
        if runner_client is not None:
            page = await _proxy_get_session_resources_to_runner(
                runner_client, session_id, resource_type=type
            )
        else:
            from omnigent.entities.session_resources import (
                list_session_resources_from_terminal_registry,
            )
            from omnigent.runtime import get_terminal_registry

            try:
                local_registry = get_terminal_registry()
            except RuntimeError:
                local_registry = None
            resource_page = list_session_resources_from_terminal_registry(
                session_id,
                local_registry,
            )
            # Mirror the runner's ``?type=`` semantics on the fallback so
            # both paths return the same shape for filtered queries.
            local_data = [
                SessionResourceObject.model_validate(
                    session_resource_view_to_dict(resource),
                )
                for resource in resource_page.data
                if type is None or resource.type == type
            ]
            page = SessionResourcePaginatedList(
                data=local_data,
                first_id=local_data[0].id if local_data else None,
                last_id=local_data[-1].id if local_data else None,
                has_more=resource_page.has_more,
            )

        # Files live in the server's file store, not on the runner, so a
        # ``type`` filter for non-file resources must skip the merge.
        if file_store is not None and type in (None, "file"):
            file_page = await asyncio.to_thread(
                file_store.list,
                session_id=session_id,
                limit=1000,
            )
            for stored in file_page.data:
                resource_dict = _stored_file_to_resource(
                    session_id,
                    stored,
                )
                page.data.append(
                    SessionResourceObject.model_validate(resource_dict),
                )
            if page.data:
                page.last_id = page.data[-1].id
                if not page.first_id:
                    page.first_id = page.data[0].id

        return page

    # ── Phase 1b: typed resource collections & terminal lifecycle ──

    async def _validate_session(
        session_id: str,
        request: Request | None = None,
        required_level: int = LEVEL_READ,
    ) -> Conversation:
        """Validate session existence and enforce permission checks.

        :param session_id: Session/conversation identifier.
        :param request: The incoming FastAPI request (for auth).
            When ``None``, permission checks are skipped (internal
            calls only).
        :param required_level: Minimum permission level needed.
        :returns: The matching conversation.
        :raises OmnigentError: 401/403/404 on auth or access failure.
        """
        if request is not None:
            user_id = _get_user_id(request, auth_provider)
            access = await _require_access_and_level(
                user_id,
                session_id,
                required_level,
                permission_store,
                conversation_store,
            )
            # _require_access_and_level already fetched the conversation for
            # non-admin callers — reuse it to avoid a second DB round-trip.
            if access.conversation is not None:
                return access.conversation
        # Fallback: no-auth path, admin caller, or permissions disabled.
        conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if conv is None:
            raise _session_not_found()
        return conv

    async def _proxy_get_to_runner(
        session_id: str,
        path: str,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Proxy a GET request to the runner and return parsed JSON.

        :param session_id: Session/conversation identifier.
        :param path: Runner-relative URL path.
        :param params: Optional query params forwarded to the runner,
            e.g. ``{"order": "asc"}``. ``None`` sends no query string.
        :returns: Parsed JSON response body.
        :raises HTTPException: 502 on runner failure.
        """
        runner_client = await _get_runner_client_for_resource_access(
            session_id,
        )
        if runner_client is None:
            raise HTTPException(
                status_code=502,
                detail="no runner available for resource access",
            )
        try:
            resp = await runner_client.get(path, params=params, timeout=10.0)
        except (httpx.HTTPError, ConnectionError) as exc:
            raise HTTPException(
                status_code=502,
                detail="runner resource endpoint unavailable",
            ) from exc
        if resp.status_code == 404:
            raise OmnigentError(
                resp.json().get("error", {}).get("message", "Resource not found"),
                code=ErrorCode.NOT_FOUND,
            )
        if resp.status_code != 200:
            try:
                body = resp.json()
                error = body.get("error", {})
                msg = error.get("message") or "runner resource endpoint failed"
            except Exception:
                msg = "runner resource endpoint failed"
            raise HTTPException(status_code=502, detail=msg)
        return resp.json()

    async def _fs_get_with_host_fallback(
        session_id: str,
        *,
        op: str,
        host_params: dict[str, Any],
        runner_path: str,
        runner_params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Serve a filesystem read, falling back to the host when offline.

        Proxies the read to the session's runner as usual. When the
        runner is offline (``RUNNER_UNAVAILABLE``) but the session's host
        is still connected, the read is served from the workspace over
        the host tunnel instead — the file panel stays live without
        waking the agent. The host runs
        :class:`omnigent.workspace_fs.WorkspaceReader` and returns the
        same JSON the runner would, so the response shape is identical.

        :param session_id: Session/conversation identifier.
        :param op: Host-side op name — ``"list_or_read"`` / ``"changes"``
            / ``"diff"`` / ``"search"``.
        :param host_params: Op-specific args for the host reader.
        :param runner_path: Runner-relative URL for the live path.
        :param runner_params: Optional query params for the runner path.
        :returns: The runner-shaped filesystem result.
        :raises OmnigentError: Re-raised runner-offline error when the
            host cannot serve the read either.
        :raises HTTPException: On host-reported filesystem failures.
        """
        try:
            return await _proxy_get_to_runner(session_id, runner_path, params=runner_params)
        except OmnigentError as exc:
            # Only the runner-offline case is a candidate for the host
            # fallback; a real 404 / git error from a live runner must
            # surface unchanged.
            if exc.code != ErrorCode.RUNNER_UNAVAILABLE:
                raise
            runner_offline = exc

        payload = await _read_workspace_via_host(session_id, op, host_params)
        if payload is None:
            # No reachable host either — surface the original offline
            # error (503) so the client shows its reconnect affordance.
            raise runner_offline
        return payload

    async def _read_workspace_via_host(
        session_id: str,
        op: str,
        host_params: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Read the session's workspace over its host tunnel.

        :param session_id: Session/conversation identifier.
        :param op: Host-side op name.
        :param host_params: Op-specific args for the host reader.
        :returns: The runner-shaped result, or ``None`` when no host is
            bound / connected / reachable (caller falls back to 503).
        :raises HTTPException: On host-reported filesystem failures,
            reproducing the runner's status.
        """
        from omnigent.server.routes._host_filesystem import (
            HostFsError,
            HostFsUnavailableError,
            read_workspace_from_host,
        )

        if host_registry is None:
            return None
        conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if conv is None or not conv.host_id or not conv.workspace:
            return None
        host_conn = host_registry.get(conv.host_id)
        if host_conn is None:
            return None
        try:
            return await read_workspace_from_host(
                host_registry=host_registry,
                host_conn=host_conn,
                op=op,
                workspace=conv.workspace,
                session_id=session_id,
                params=host_params,
            )
        except HostFsUnavailableError:
            return None
        except HostFsError as exc:
            if exc.status == 404:
                raise OmnigentError(exc.message, code=ErrorCode.NOT_FOUND) from exc
            if exc.status == 400:
                # Invalid path is a client error; surface it verbatim like the
                # runner's 400 rather than collapsing it to a 502.
                raise HTTPException(status_code=400, detail=exc.message) from exc
            # Any other host FS failure (e.g. git_status_failed 500) mirrors the
            # runner proxy, which wraps non-200/404 responses as a 502.
            raise HTTPException(status_code=502, detail=exc.message) from exc

    async def _proxy_post_to_runner(
        session_id: str,
        path: str,
        body: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        """Proxy a POST request to the runner and return status + JSON.

        :param session_id: Session/conversation identifier.
        :param path: Runner-relative URL path.
        :param body: JSON body to forward.
        :returns: Tuple of (status_code, parsed_json_body).
        :raises HTTPException: 502 on transport failure.
        """
        runner_client = await _get_runner_client_for_resource_access(
            session_id,
        )
        if runner_client is None:
            raise HTTPException(
                status_code=502,
                detail="no runner available for resource access",
            )
        try:
            resp = await runner_client.post(
                path,
                json=body,
                timeout=10.0,
            )
        except (httpx.HTTPError, ConnectionError) as exc:
            raise HTTPException(
                status_code=502,
                detail="runner resource endpoint unavailable",
            ) from exc
        return resp.status_code, resp.json()

    async def _proxy_delete_to_runner(
        session_id: str,
        path: str,
    ) -> tuple[int, dict[str, Any]]:
        """Proxy a DELETE request to the runner and return status + JSON.

        :param session_id: Session/conversation identifier.
        :param path: Runner-relative URL path.
        :returns: Tuple of (status_code, parsed_json_body).
        :raises HTTPException: 502 on transport failure.
        """
        runner_client = await _get_runner_client_for_resource_access(
            session_id,
        )
        if runner_client is None:
            raise HTTPException(
                status_code=502,
                detail="no runner available for resource access",
            )
        try:
            resp = await runner_client.delete(path, timeout=10.0)
        except (httpx.HTTPError, ConnectionError) as exc:
            raise HTTPException(
                status_code=502,
                detail="runner resource endpoint unavailable",
            ) from exc
        return resp.status_code, resp.json()

    async def _proxy_put_to_runner(
        session_id: str,
        path: str,
        body: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        """Proxy a PUT request to the runner.

        :param session_id: Session/conversation identifier.
        :param path: Runner-relative URL path.
        :param body: JSON body to forward.
        :returns: Tuple of (status_code, parsed_json_body).
        :raises HTTPException: 502 on transport failure.
        """
        runner_client = await _get_runner_client_for_resource_access(
            session_id,
        )
        if runner_client is None:
            raise HTTPException(
                status_code=502,
                detail="no runner available for resource access",
            )
        try:
            resp = await runner_client.put(
                path,
                json=body,
                timeout=10.0,
            )
        except (httpx.HTTPError, ConnectionError) as exc:
            raise HTTPException(
                status_code=502,
                detail="runner resource endpoint unavailable",
            ) from exc
        return resp.status_code, resp.json()

    async def _proxy_patch_to_runner(
        session_id: str,
        path: str,
        body: dict[str, Any],
    ) -> tuple[int, dict[str, Any]]:
        """Proxy a PATCH request to the runner.

        :param session_id: Session/conversation identifier.
        :param path: Runner-relative URL path.
        :param body: JSON body to forward.
        :returns: Tuple of (status_code, parsed_json_body).
        :raises HTTPException: 502 on transport failure.
        """
        runner_client = await _get_runner_client_for_resource_access(
            session_id,
        )
        if runner_client is None:
            raise HTTPException(
                status_code=502,
                detail="no runner available for resource access",
            )
        try:
            resp = await runner_client.patch(
                path,
                json=body,
                timeout=10.0,
            )
        except (httpx.HTTPError, ConnectionError) as exc:
            raise HTTPException(
                status_code=502,
                detail="runner resource endpoint unavailable",
            ) from exc
        return resp.status_code, resp.json()

    # Typed collection routes registered BEFORE /{resource_id} so
    # "environments", "terminals", "files" are not captured as ids.

    @router.get(
        "/sessions/{session_id}/resources/environments",
        response_model=None,
    )
    async def list_session_environments(
        request: Request,
        session_id: str,
    ) -> dict[str, Any]:
        """
        Return only environment resources for a session.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session/conversation identifier.
        :returns: ``PaginatedList`` of environment resources.
        """
        await _validate_session(session_id, request, LEVEL_READ)
        path = f"/v1/sessions/{session_id}/resources/environments"
        return await _proxy_get_to_runner(session_id, path)

    @router.get(
        "/sessions/{session_id}/resources/environments/{environment_id}",
        response_model=None,
    )
    async def get_session_environment(
        request: Request,
        session_id: str,
        environment_id: str,
    ) -> dict[str, Any]:
        """
        Return a single environment resource by id.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session/conversation identifier.
        :param environment_id: Opaque environment resource id,
            e.g. ``"default"``.
        :returns: The environment resource object.
        """
        await _validate_session(session_id, request, LEVEL_READ)
        path = f"/v1/sessions/{session_id}/resources/environments/{environment_id}"
        try:
            return await _proxy_get_to_runner(session_id, path)
        except OmnigentError as exc:
            if exc.code != ErrorCode.RUNNER_UNAVAILABLE:
                raise
            # Runner offline but host-bound: synthesize the default
            # environment so the file panel (which gates on this metadata)
            # keeps browsing the host-served workspace at ``conv.workspace``.
            synthesized = await _synthesize_offline_environment(session_id, environment_id)
            if synthesized is None:
                raise
            return synthesized

    async def _synthesize_offline_environment(
        session_id: str,
        environment_id: str,
    ) -> dict[str, Any] | None:
        """Build a default-environment resource from the bound workspace.

        Used when the runner is offline but the session is host-bound, so
        the file panel's environment probe resolves and browsing can
        proceed against the host-served workspace.

        :param session_id: Session/conversation identifier.
        :param environment_id: Requested environment id; only the default
            environment is synthesized.
        :returns: A minimal environment resource dict with
            ``metadata.root`` set to the workspace path, or ``None`` when
            not applicable (non-default env, no host, no workspace).
        """
        if environment_id != "default" or host_registry is None:
            return None
        conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if conv is None or not conv.host_id or not conv.workspace:
            return None
        if host_registry.get(conv.host_id) is None:
            return None
        return {
            "id": environment_id,
            "object": "session.resource",
            "type": "environment",
            "metadata": {"root": conv.workspace},
        }

    @router.get(
        "/sessions/{session_id}/resources/terminals",
        response_model=None,
    )
    async def list_session_terminals(
        request: Request,
        session_id: str,
    ) -> dict[str, Any]:
        """
        Return only terminal resources for a session.

        The runner endpoint's pagination params (``limit`` / ``after`` /
        ``before`` / ``order``) are forwarded from the incoming query
        string — without this, a client-requested ``order=asc`` (the web
        terminal tabs rely on creation order to keep the session's own
        terminal first) would be silently dropped and the runner's
        ``desc`` default would apply.

        :param request: The incoming FastAPI request (for auth and the
            forwarded query params).
        :param session_id: Session/conversation identifier.
        :returns: ``PaginatedList`` of terminal resources.
        """
        await _validate_session(session_id, request, LEVEL_READ)
        path = f"/v1/sessions/{session_id}/resources/terminals"
        forwarded = {
            key: value
            for key, value in request.query_params.items()
            if key in ("limit", "after", "before", "order")
        }
        return await _proxy_get_to_runner(session_id, path, params=forwarded or None)

    @router.post(
        "/sessions/{session_id}/resources/terminals",
        response_model=None,
        # CSRF hardening: body is parsed via request.json(); require a JSON
        # Content-Type so a cross-site text/plain request can't reach it.
        dependencies=[Depends(require_json_content_type)],
    )
    async def create_session_terminal(
        session_id: str,
        request: Request,
    ) -> Any:
        """
        Launch or return an existing terminal resource.

        Preserves ``sys_terminal_launch`` idempotency: an
        already-running ``(terminal, session_key)`` returns the
        existing resource.

        User-initiated creates are gated on the agent's terminal
        access: the requested ``terminal`` must be one of the names
        declared in the agent spec's ``terminals:`` block. Native
        harness bootstrap requests (marked ``ensure_native_terminal``
        or ``bridge_inject_dir`` — the ``omnigent claude`` / ``codex``
        wrappers launching the session's own CLI terminal) are exempt:
        they launch undeclared names via the runner's
        synthesize-from-body path and predate the gate. The markers
        are client-controlled, so the exemption is narrowed to the
        exact shape those wrappers send — a registered native terminal
        name with ``session_key`` ``"main"`` — anything else carrying a
        marker still goes through the declared-name gate (it would
        otherwise be an arbitrary-terminal bypass).

        :param session_id: Session/conversation identifier.
        :param request: JSON body with ``terminal`` and
            ``session_key``.
        :returns: The terminal resource object.
        :raises OmnigentError: 400 when the requested terminal is not
            declared by the agent spec (or the agent has no
            ``terminals:`` block at all).
        """
        conv = await _validate_session(session_id, request, LEVEL_EDIT)
        body = await request.json()
        is_native_bootstrap = (
            bool(body.get("ensure_native_terminal") or body.get("bridge_inject_dir"))
            and native_coding_agent_for_terminal_name(body.get("terminal")) is not None
            and body.get("session_key") == "main"
        )
        if not is_native_bootstrap:
            spec = await asyncio.to_thread(_load_agent_spec_for_session, conv, agent_store)
            declared = list(spec.terminals or {}) if spec is not None else []
            if body.get("terminal") not in declared:
                raise OmnigentError(
                    (
                        f"Terminal {body.get('terminal')!r} is not declared by this "
                        f"agent. Terminals can only be created for agents whose spec "
                        f"declares them; this agent declares: {declared or 'none'}."
                    ),
                    code=ErrorCode.INVALID_INPUT,
                )
        path = f"/v1/sessions/{session_id}/resources/terminals"
        status, payload = await _proxy_post_to_runner(
            session_id,
            path,
            body,
        )
        if status >= 400:
            error = payload.get("error", {})
            # OmnigentError derives http_status from code; pass the runner's code, not a status.
            raise OmnigentError(
                error.get("message", f"Terminal launch failed (runner returned HTTP {status})"),
                code=error.get("code", ErrorCode.INTERNAL_ERROR),
            )
        _publish_and_persist_resource_event(
            session_id,
            "session.resource.created",
            resource_id=payload.get("id", ""),
            resource_type="terminal",
            conversation_store=conversation_store,
            resource=payload,
        )
        return payload

    @router.get(
        "/sessions/{session_id}/resources/terminals/{terminal_id}",
        response_model=None,
    )
    async def get_session_terminal(
        request: Request,
        session_id: str,
        terminal_id: str,
    ) -> dict[str, Any]:
        """
        Return a single terminal resource by id.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session/conversation identifier.
        :param terminal_id: Opaque terminal resource id.
        :returns: The terminal resource object.
        """
        await _validate_session(session_id, request, LEVEL_READ)
        path = f"/v1/sessions/{session_id}/resources/terminals/{terminal_id}"
        return await _proxy_get_to_runner(session_id, path)

    @router.post(
        "/sessions/{session_id}/resources/terminals/{terminal_id}/transfer",
        # Internal terminal transfer — hidden from the public API reference.
        include_in_schema=False,
        response_model=None,
        # CSRF hardening: body is parsed via request.json(); require a JSON
        # Content-Type so a cross-site text/plain request can't reach it.
        dependencies=[Depends(require_json_content_type)],
    )
    async def transfer_session_terminal(
        request: Request,
        session_id: str,
        terminal_id: str,
    ) -> Any:
        """
        Move a terminal resource to another session without closing it.

        Used by native Claude ``/clear`` rotation: ownership changes
        from the previous conversation to the fresh one while the tmux
        pane keeps running.

        :param request: The incoming FastAPI request (for auth) with
            JSON body ``{"target_session_id": "conv_new"}``.
        :param session_id: Current owning session/conversation id,
            e.g. ``"conv_old"``.
        :param terminal_id: Opaque terminal resource id,
            e.g. ``"terminal_claude_main"``.
        :returns: The terminal resource object under the target session.
        """
        await _validate_session(session_id, request, LEVEL_EDIT)
        body = await request.json()
        target_session_id = body.get("target_session_id") if isinstance(body, dict) else None
        if not isinstance(target_session_id, str) or not target_session_id:
            raise OmnigentError(
                "'target_session_id' is required",
                code=ErrorCode.INVALID_INPUT,
            )
        await _validate_session(target_session_id, request, LEVEL_EDIT)

        path = f"/v1/sessions/{session_id}/resources/terminals/{terminal_id}/transfer"
        status, payload = await _proxy_post_to_runner(
            session_id,
            path,
            {"target_session_id": target_session_id},
        )
        if status == 404:
            error = payload.get("error", {})
            raise OmnigentError(
                error.get("message", "Terminal not found"),
                code=ErrorCode.NOT_FOUND,
            )
        if status == 409:
            error = payload.get("error", {})
            raise OmnigentError(
                error.get("message", "Terminal transfer conflict"),
                code=ErrorCode.INVALID_INPUT,
            )
        if status >= 400:
            error = payload.get("error", {})
            # OmnigentError derives http_status from code; pass the runner's code, not a status.
            raise OmnigentError(
                error.get("message", "Terminal transfer failed"),
                code=error.get("code", ErrorCode.INTERNAL_ERROR),
            )

        _publish_and_persist_resource_event(
            session_id,
            "session.resource.deleted",
            resource_id=terminal_id,
            resource_type="terminal",
            conversation_store=conversation_store,
        )
        _publish_and_persist_resource_event(
            target_session_id,
            "session.resource.created",
            resource_id=payload.get("id", ""),
            resource_type="terminal",
            conversation_store=conversation_store,
            resource=payload,
        )
        return payload

    @router.delete(
        "/sessions/{session_id}/resources/terminals/{terminal_id}",
        response_model=None,
    )
    async def delete_session_terminal(
        request: Request,
        session_id: str,
        terminal_id: str,
    ) -> Any:
        """
        Close a terminal resource.

        Delegates to ``TerminalRegistry.close()`` on the runner.
        Returns 404 for unknown terminals.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session/conversation identifier.
        :param terminal_id: Opaque terminal resource id.
        :returns: Deletion confirmation object.
        """
        await _validate_session(session_id, request, LEVEL_EDIT)
        path = f"/v1/sessions/{session_id}/resources/terminals/{terminal_id}"
        status, payload = await _proxy_delete_to_runner(
            session_id,
            path,
        )
        if status == 404:
            error = payload.get("error", {})
            raise OmnigentError(
                error.get("message", "Terminal not found"),
                code=ErrorCode.NOT_FOUND,
            )
        if status >= 400:
            raise HTTPException(
                status_code=502,
                detail="runner terminal delete failed",
            )
        _publish_and_persist_resource_event(
            session_id,
            "session.resource.deleted",
            resource_id=terminal_id,
            resource_type="terminal",
            conversation_store=conversation_store,
        )
        return payload

    # ── Phase 1c: session-scoped file endpoints ────────────────────

    @router.get(
        "/sessions/{session_id}/resources/files",
        response_model=None,
    )
    async def list_session_files(
        request: Request,
        session_id: str,
        limit: int = Query(default=20, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
    ) -> dict[str, Any]:
        """
        List files owned by a session.

        :param session_id: Session/conversation identifier.
        :param limit: Maximum number of files to return.
        :param after: Cursor file ID for forward pagination.
        :param before: Cursor file ID for backward pagination.
        :param order: Sort direction, ``"desc"`` or ``"asc"``.
        :returns: ``PaginatedList`` of session file resources.
        """
        await _validate_session(session_id, request, LEVEL_READ)
        if file_store is None:
            raise HTTPException(
                status_code=501,
                detail="file store not configured",
            )
        page = file_store.list(
            session_id=session_id,
            limit=limit,
            after=after,
            before=before,
            order=order,
        )
        data = [_stored_file_to_resource(session_id, f) for f in page.data]
        return {
            "object": "list",
            "data": data,
            "first_id": page.first_id,
            "last_id": page.last_id,
            "has_more": page.has_more,
        }

    @router.post(
        "/sessions/{session_id}/resources/files",
        status_code=201,
        response_model=None,
        # CSRF hardening: this route only accepts multipart/form-data, which
        # is CORS-safelisted, so a content-type guard can't stop a cross-site
        # upload. require_trusted_origin closes the gap (allows absent Origin
        # for the non-browser SDK/runner clients; in local mode a present
        # Origin must be loopback).
        dependencies=[Depends(require_trusted_origin)],
    )
    async def upload_session_file(
        request: Request,
        session_id: str,
        file: Annotated[UploadFile, File(...)],
    ) -> dict[str, Any]:
        """
        Upload a file into the session file namespace.

        Accepts the multipart upload shape used by session file resources.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session/conversation identifier.
        :param file: The uploaded file (multipart form data).
        :returns: The session file resource object.
        """
        await _validate_session(session_id, request, LEVEL_EDIT)
        if file_store is None or artifact_store is None:
            raise HTTPException(
                status_code=501,
                detail="file store not configured",
            )
        if not file.filename:
            raise OmnigentError(
                "filename is required",
                code=ErrorCode.INVALID_INPUT,
            )
        from omnigent.runtime.content_resolver import (
            MAX_ATTACHMENT_UPLOAD_BYTES,
            _resolve_content_type,
            attachment_text_type_for_extension,
            attachment_upload_limit,
        )

        # Resolve the type from the declared MIME + filename BEFORE reading
        # the body, so an unsupported or oversized upload is rejected without
        # buffering it. Attachments are inlined into the model context as
        # base64 (see content_resolver.resolve_content_references); only
        # images, PDF, and text/code files are usable — others (pptx, docx,
        # zip, …) would be garbled or blow the request size, so reject them.
        content_type = _resolve_content_type(
            file.content_type,
            file.filename,
        )
        type_limit = attachment_upload_limit(content_type)
        if type_limit is None:
            # The browser/OS can mislabel a text/code file as binary (e.g. a
            # .csv reported as application/vnd.ms-excel on Windows). Fall back
            # to the extension — matching the web client's allowlist — and
            # normalize the type so the resolver inlines it as text.
            ext_type = attachment_text_type_for_extension(file.filename)
            if ext_type is not None:
                content_type = ext_type
                type_limit = attachment_upload_limit(content_type)
        if type_limit is None:
            raise HTTPException(
                status_code=415,
                detail=(
                    f"Unsupported attachment type '{content_type}'. Only images, "
                    "PDF, and text/code files can be attached."
                ),
            )
        content = await _read_upload_capped(
            file,
            min(type_limit, MAX_ATTACHMENT_UPLOAD_BYTES),
        )
        stored = file_store.create(
            session_id=session_id,
            filename=file.filename,
            bytes=len(content),
            content_type=content_type,
        )
        artifact_store.put(stored.id, content)
        resource = _stored_file_to_resource(session_id, stored)
        _publish_and_persist_resource_event(
            session_id,
            "session.resource.created",
            resource_id=stored.id,
            resource_type="file",
            conversation_store=conversation_store,
            resource=resource,
        )
        return resource

    @router.get(
        "/sessions/{session_id}/resources/files/{file_id}",
        response_model=None,
    )
    async def get_session_file(
        request: Request,
        session_id: str,
        file_id: str,
    ) -> dict[str, Any]:
        """
        Retrieve metadata for a session file resource.

        Verifies that ``file_id`` belongs to ``session_id``.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session/conversation identifier.
        :param file_id: Unique file identifier.
        :returns: The session file resource object.
        """
        await _validate_session(session_id, request, LEVEL_READ)
        if file_store is None:
            raise HTTPException(
                status_code=501,
                detail="file store not configured",
            )
        stored = file_store.get(file_id, session_id=session_id)
        if stored is None:
            raise OmnigentError(
                "File not found",
                code=ErrorCode.NOT_FOUND,
            )
        return _stored_file_to_resource(session_id, stored)

    @router.get(
        "/sessions/{session_id}/resources/files/{file_id}/content",
        response_model=None,
    )
    async def get_session_file_content(
        request: Request,
        session_id: str,
        file_id: str,
    ) -> Response:
        """
        Download raw content of a session file resource.

        :param session_id: Session/conversation identifier.
        :param file_id: Unique file identifier.
        :returns: Response with file bytes and Content-Type.
        """

        await _validate_session(session_id, request, LEVEL_READ)
        if file_store is None or artifact_store is None:
            raise HTTPException(
                status_code=501,
                detail="file store not configured",
            )
        stored = file_store.get(file_id, session_id=session_id)
        if stored is None:
            raise OmnigentError(
                "File not found",
                code=ErrorCode.NOT_FOUND,
            )
        content = artifact_store.get(stored.id)
        media_type = mimetypes.guess_type(stored.filename)[0] or "application/octet-stream"
        # The filename and bytes are fully user-controlled. Serving the
        # content inline lets a browser navigating directly to this URL
        # render an uploaded ``evil.html`` as ``text/html`` and execute
        # its script in the server's own origin (stored XSS — acute on
        # the OSS/local server, which has no CSRF/apiproxy boundary).
        # Force a download with ``Content-Disposition: attachment`` and
        # disable MIME sniffing so the response cannot be reinterpreted
        # as an active type.
        return Response(
            content=content,
            media_type=media_type,
            headers={
                "Content-Disposition": _attachment_disposition(stored.filename),
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.delete(
        "/sessions/{session_id}/resources/files/{file_id}",
        response_model=None,
    )
    async def delete_session_file(
        request: Request,
        session_id: str,
        file_id: str,
    ) -> dict[str, Any]:
        """
        Delete a session file resource and its artifact bytes.

        :param session_id: Session/conversation identifier.
        :param file_id: Unique file identifier.
        :returns: Deletion confirmation object.
        """
        await _validate_session(session_id, request, LEVEL_EDIT)
        if file_store is None or artifact_store is None:
            raise HTTPException(
                status_code=501,
                detail="file store not configured",
            )
        if not file_store.delete(file_id, session_id=session_id):
            raise OmnigentError(
                "File not found",
                code=ErrorCode.NOT_FOUND,
            )
        artifact_store.delete(file_id)
        _publish_and_persist_resource_event(
            session_id,
            "session.resource.deleted",
            resource_id=file_id,
            resource_type="file",
            conversation_store=conversation_store,
        )
        return {
            "id": file_id,
            "object": "session.resource.deleted",
            "deleted": True,
        }

    @router.post(
        "/sessions/{session_id}/resources/files:copy",
        response_model=None,
    )
    async def copy_session_files(
        request: Request,
        session_id: str,
        body: CopyFilesRequest,
    ) -> dict[str, Any]:
        """
        Copy lineage-owned files into this (destination) session.

        Authorizes by spawn lineage: ``body.source_session_id`` must be a
        STRICT ancestor of this session up the ``parent_conversation_id``
        chain — the session may not name itself as the source. Each source
        file is read and re-stored as a new child-scoped row owned by
        ``session_id`` — this preserves the session-scoping invariant (the
        child reads its OWN copy; no cross-session read grant is created).
        Validation is all-or-nothing: an unauthorized source, a missing
        file, or a request past the copy limits copies nothing.

        The request is bounded before any blob is read: the file count and
        the summed ``StoredFile.bytes`` are checked against the copy limits
        during metadata validation, so an over-limit request is rejected
        without buffering a single blob. Within the limits, files are copied
        one at a time (read → create → put) so peak memory is a single blob,
        not the whole batch.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Destination (child) session/conversation id.
        :param body: Source session id plus the file ids to copy.
        :returns: A ``session.files.copied`` object carrying the
            ``{source_file_id: new_file_id}`` mapping.
        """
        from omnigent.server.server_config import (
            copy_file_count_limit,
            copy_total_bytes_limit,
        )

        await _validate_session(session_id, request, LEVEL_EDIT)
        if file_store is None or artifact_store is None:
            raise HTTPException(
                status_code=501,
                detail="file store not configured",
            )

        # Lineage authorization: the source must be a STRICT ancestor up
        # the parent_conversation_id chain. A session may not name itself
        # as the source — the contract is "copy files down from a parent",
        # and a top-level session has no lineage to copy from.
        if body.source_session_id not in set(
            _ancestor_session_ids(conversation_store, session_id)
        ):
            raise OmnigentError(
                "Source session is not an ancestor of this session",
                code=ErrorCode.FORBIDDEN,
            )

        # Validate every source file WITHOUT reading a blob, enforcing the copy
        # limits before any blob is read. Summing StoredFile.bytes here means
        # an over-count or over-size request is rejected without buffering a
        # single blob — a rejected request never spikes memory. artifact_store
        # .exists() is a cheap metadata probe (S3 HEAD / local stat / DB row),
        # NOT a blob read, so checking it here preserves the original
        # "missing blob surfaces before any child row is created" guarantee
        # without reintroducing the batch prefetch. The blobs themselves are
        # fetched one at a time in the write loop below.
        max_files = copy_file_count_limit()
        max_total_bytes = copy_total_bytes_limit()
        if len(body.file_ids) > max_files:
            raise OmnigentError(
                f"Cannot copy {len(body.file_ids)} files: limit is {max_files}",
                code=ErrorCode.INVALID_INPUT,
            )
        if len(set(body.file_ids)) != len(body.file_ids):
            raise OmnigentError(
                "file_ids must not contain duplicates",
                code=ErrorCode.INVALID_INPUT,
            )
        sources: list[StoredFile] = []
        total_bytes = 0
        for file_id in body.file_ids:
            stored = file_store.get(file_id, session_id=body.source_session_id)
            if stored is None or not artifact_store.exists(stored.id):
                raise OmnigentError(
                    f"File '{file_id}' not found in source session",
                    code=ErrorCode.NOT_FOUND,
                )
            total_bytes += stored.bytes
            if total_bytes > max_total_bytes:
                raise OmnigentError(
                    f"Cannot copy files: total size exceeds limit of {max_total_bytes} bytes",
                    code=ErrorCode.INVALID_INPUT,
                )
            sources.append(stored)

        # Commit the copies one file at a time (read → create → put) so peak
        # memory is a single blob, not the whole batch. If any step fails
        # mid-batch, roll back the rows/blobs already created.
        mapping: dict[str, CopiedFile] = {}
        created: list[str] = []
        copied: list[StoredFile] = []
        try:
            for stored in sources:
                content = artifact_store.get(stored.id)
                new = file_store.create(
                    session_id=session_id,
                    filename=stored.filename,
                    bytes=stored.bytes,
                    content_type=stored.content_type,
                )
                created.append(new.id)
                artifact_store.put(new.id, content)
                # Carry the preserved filename + content_type back so the
                # caller can attach the copy without a follow-up metadata GET.
                mapping[stored.id] = CopiedFile(
                    new_id=new.id,
                    filename=new.filename,
                    content_type=new.content_type,
                )
                copied.append(new)
        except Exception as exc:
            for new_id in created:
                try:
                    file_store.delete(new_id, session_id=session_id)
                except Exception:
                    _logger.warning(
                        "Failed to delete copied file row during rollback: session=%s file_id=%s",
                        session_id,
                        new_id,
                        exc_info=True,
                    )
                try:
                    artifact_store.delete(new_id)
                except Exception:
                    _logger.warning(
                        "Failed to delete copied file blob during rollback: session=%s file_id=%s",
                        session_id,
                        new_id,
                        exc_info=True,
                    )
            raise OmnigentError(
                "Failed to copy files into destination session",
                code=ErrorCode.INTERNAL_ERROR,
            ) from exc

        # Resource events fire only after every write lands. Publishing them
        # inside the copy loop would emit (and persist as transcript items)
        # ``session.resource.created`` for early files, then a later write
        # failure would roll back the file rows/blobs without compensating
        # those events — clients would see phantom files that no longer
        # exist. Keep the create + event all-or-nothing together.
        for new in copied:
            _publish_and_persist_resource_event(
                session_id,
                "session.resource.created",
                resource_id=new.id,
                resource_type="file",
                conversation_store=conversation_store,
                resource=_stored_file_to_resource(session_id, new),
            )

        return CopyFilesResponse(
            session_id=session_id,
            mapping=mapping,
        ).model_dump()

    # ── Phase 3: environment filesystem proxy endpoints ──────────

    async def _proxy_fs_response(
        session_id: str,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        request: Request | None = None,
        required_level: int = LEVEL_EDIT,
        environment_id: str = "default",
        publish_invalidation: bool = True,
    ) -> Any:
        """Proxy a filesystem request to the runner.

        Translates runner error status codes into appropriate
        API-level exceptions.

        :param session_id: Session/conversation identifier.
        :param method: HTTP method.
        :param path: Runner-relative URL path.
        :param body: Optional JSON body.
        :param request: The incoming FastAPI request (for auth).
        :param required_level: Minimum permission level needed.
        :param environment_id: Environment resource id,
            e.g. ``"default"``. Used for the live invalidation event
            after successful mutating filesystem operations.
        :param publish_invalidation: Whether a successful proxied
            mutation should publish ``session.changed_files.invalidated``.
            False for generic shell commands because read-only commands
            are common and cannot be distinguished cheaply here.
        :returns: Parsed JSON response.
        """
        await _validate_session(session_id, request, required_level)
        if method == "GET":
            return await _proxy_get_to_runner(session_id, path)
        if method == "PUT":
            status, payload = await _proxy_put_to_runner(
                session_id,
                path,
                body or {},
            )
        elif method == "PATCH":
            status, payload = await _proxy_patch_to_runner(
                session_id,
                path,
                body or {},
            )
        elif method == "POST":
            status, payload = await _proxy_post_to_runner(
                session_id,
                path,
                body or {},
            )
        elif method == "DELETE":
            status, payload = await _proxy_delete_to_runner(
                session_id,
                path,
            )
        else:
            raise HTTPException(status_code=405)

        if status >= 400:
            error = payload.get("error", {})
            message = error.get("message", "filesystem operation failed")
            if status == 404:
                raise OmnigentError(message, code=ErrorCode.NOT_FOUND)
            raise HTTPException(status_code=status, detail=message)
        if publish_invalidation:
            _publish_changed_files_invalidated(session_id, environment_id)
        return payload

    @router.get(
        "/sessions/{session_id}/resources/environments/{environment_id}/filesystem",
        response_model=None,
    )
    async def list_environment_root(
        request: Request,
        session_id: str,
        environment_id: str,
        limit: int = Query(default=20, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
    ) -> Any:
        """
        List root directory of an environment.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session/conversation identifier.
        :param environment_id: Environment resource id.
        :param limit: Maximum number of entries to return (1-1000, default 20).
        :param after: Cursor entry id for forward pagination.
        :param before: Cursor entry id for backward pagination.
        :param order: Sort order, ``"asc"`` or ``"desc"``.
        :returns: PaginatedList of filesystem entries.
        """
        params: dict[str, str] = {"limit": str(limit), "order": order}
        if after is not None:
            params["after"] = after
        if before is not None:
            params["before"] = before
        qs = urllib.parse.urlencode(params)
        path = f"/v1/sessions/{session_id}/resources/environments/{environment_id}/filesystem?{qs}"
        await _validate_session(session_id, request, LEVEL_READ)
        return await _fs_get_with_host_fallback(
            session_id,
            op="list_or_read",
            host_params={
                "path": "",
                "limit": limit,
                "after": after,
                "before": before,
                "order": order,
            },
            runner_path=path,
        )

    @router.get(
        "/sessions/{session_id}/resources/environments/{environment_id}/search",
        response_model=None,
    )
    async def search_environment_files(
        request: Request,
        session_id: str,
        environment_id: str,
        q: str = Query(min_length=1, pattern=r".*\S.*"),
        include: str | None = Query(default=None),
        exclude: str | None = Query(default=None),
        limit: int = Query(default=500, ge=1, le=500),
    ) -> Any:
        """
        Search for files recursively by name/path substring and glob filters.

        Proxies to the runner's search endpoint.  Returns a flat list of
        matching file entries (not directories) whose name or relative path
        contains ``q`` (case-insensitive), optionally scoped by ``include`` /
        ``exclude`` globs.  Requires at least one non-whitespace character in
        ``q`` to prevent accidental full-tree scans.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        :param environment_id: Environment resource id,
            e.g. ``"default"``.
        :param q: Case-insensitive search substring, e.g. ``"test.md"``.
            Must contain at least one non-whitespace character.
        :param include: Comma-separated glob patterns scoping which files are
            returned, e.g. ``"*.ts,src/**"``.
        :param exclude: Comma-separated glob patterns for files to drop,
            e.g. ``"**/node_modules,*.test.ts"``.
        :param limit: Maximum number of results (1-500, default 500).
        :returns: JSON list response with matching filesystem entries.
        """
        params: dict[str, str] = {"q": q, "limit": str(limit)}
        if include is not None:
            params["include"] = include
        if exclude is not None:
            params["exclude"] = exclude
        qs = urllib.parse.urlencode(params)
        path = f"/v1/sessions/{session_id}/resources/environments/{environment_id}/search?{qs}"
        await _validate_session(session_id, request, LEVEL_READ)
        return await _fs_get_with_host_fallback(
            session_id,
            op="search",
            host_params={"q": q, "include": include, "exclude": exclude, "limit": limit},
            runner_path=path,
        )

    @router.get(
        "/sessions/{session_id}/resources/environments/{environment_id}/changes",
        response_model=None,
    )
    async def list_environment_filesystem_changes(
        request: Request,
        session_id: str,
        environment_id: str,
    ) -> Any:
        """
        List all files changed since session start (flat, registry-backed).

        Returns the watchdog change set for the session — every file
        created, modified, or deleted since the session began, regardless
        of directory depth.  Use for the flat "changed files" view.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session/conversation identifier.
        :param environment_id: Environment resource id.
        :returns: Flat list of changed filesystem entries with ``status``.
        """
        path = f"/v1/sessions/{session_id}/resources/environments/{environment_id}/changes"
        await _validate_session(session_id, request, LEVEL_READ)
        return await _fs_get_with_host_fallback(
            session_id,
            op="changes",
            host_params={},
            runner_path=path,
        )

    @router.get(
        "/sessions/{session_id}/resources/environments/{environment_id}/diff/{relative_path:path}",
        # Internal (UI diff view) — hidden from the public API reference.
        include_in_schema=False,
        response_model=None,
    )
    async def read_environment_file_diff(
        request: Request,
        session_id: str,
        environment_id: str,
        relative_path: str,
    ) -> Any:
        """
        Return before/after diff content for a changed file.

        Proxies to the runner's diff endpoint and returns before/after
        content strings so the UI can render a diff view.  Returns 404 when
        the file has not been modified this session.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session/conversation identifier.
        :param environment_id: Environment resource id.
        :param relative_path: Path relative to environment root.
        :returns: JSON with ``before`` and ``after`` content strings.
        """
        path = (
            f"/v1/sessions/{session_id}/resources/environments"
            f"/{environment_id}/diff/{relative_path}"
        )
        await _validate_session(session_id, request, LEVEL_READ)
        return await _fs_get_with_host_fallback(
            session_id,
            op="diff",
            host_params={"path": relative_path},
            runner_path=path,
        )

    @router.get(
        "/sessions/{session_id}/resources/environments"
        "/{environment_id}/filesystem/{relative_path:path}",
        response_model=None,
    )
    async def read_or_list_environment_path(
        request: Request,
        session_id: str,
        environment_id: str,
        relative_path: str,
        limit: int = Query(default=20, ge=1, le=1000),
        after: str | None = Query(default=None),
        before: str | None = Query(default=None),
        order: str = Query(default="desc", pattern="^(asc|desc)$"),
    ) -> Any:
        """
        Read a file or list a directory in an environment.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session/conversation identifier.
        :param environment_id: Environment resource id.
        :param relative_path: Path relative to environment root.
        :param limit: Maximum number of entries to return for directory
            listings (1-1000, default 20). Ignored for file reads.
        :param after: Cursor entry id for forward pagination.
        :param before: Cursor entry id for backward pagination.
        :param order: Sort order, ``"asc"`` or ``"desc"``.
        :returns: File content or directory listing.
        """
        params: dict[str, str] = {"limit": str(limit), "order": order}
        if after is not None:
            params["after"] = after
        if before is not None:
            params["before"] = before
        qs = urllib.parse.urlencode(params)
        path = (
            f"/v1/sessions/{session_id}/resources/environments"
            f"/{environment_id}/filesystem/{relative_path}?{qs}"
        )
        await _validate_session(session_id, request, LEVEL_READ)
        return await _fs_get_with_host_fallback(
            session_id,
            op="list_or_read",
            host_params={
                "path": relative_path,
                "limit": limit,
                "after": after,
                "before": before,
                "order": order,
            },
            runner_path=path,
        )

    @router.put(
        "/sessions/{session_id}/resources/environments"
        "/{environment_id}/filesystem/{relative_path:path}",
        response_model=None,
    )
    async def write_environment_file(
        session_id: str,
        environment_id: str,
        relative_path: str,
        request: Request,
    ) -> Any:
        """
        Write/replace a file in an environment.

        :param session_id: Session/conversation identifier.
        :param environment_id: Environment resource id.
        :param relative_path: Path relative to environment root.
        :param request: JSON body with ``content``.
        :returns: Write result.
        """
        body = await request.json()
        path = (
            f"/v1/sessions/{session_id}/resources/environments"
            f"/{environment_id}/filesystem/{relative_path}"
        )
        return await _proxy_fs_response(
            session_id,
            "PUT",
            path,
            body,
            request=request,
            environment_id=environment_id,
        )

    @router.patch(
        "/sessions/{session_id}/resources/environments"
        "/{environment_id}/filesystem/{relative_path:path}",
        response_model=None,
    )
    async def edit_environment_file(
        session_id: str,
        environment_id: str,
        relative_path: str,
        request: Request,
    ) -> Any:
        """
        Edit a file in an environment via text replacement.

        :param session_id: Session/conversation identifier.
        :param environment_id: Environment resource id.
        :param relative_path: Path relative to environment root.
        :param request: JSON body with ``old_text`` and ``new_text``.
        :returns: Edit result.
        """
        body = await request.json()
        path = (
            f"/v1/sessions/{session_id}/resources/environments"
            f"/{environment_id}/filesystem/{relative_path}"
        )
        return await _proxy_fs_response(
            session_id,
            "PATCH",
            path,
            body,
            request=request,
            environment_id=environment_id,
        )

    @router.delete(
        "/sessions/{session_id}/resources/environments"
        "/{environment_id}/filesystem/{relative_path:path}",
        response_model=None,
    )
    async def delete_environment_path(
        request: Request,
        session_id: str,
        environment_id: str,
        relative_path: str,
    ) -> Any:
        """
        Delete a file or directory in an environment.

        :param request: The incoming FastAPI request (for auth).
        :param session_id: Session/conversation identifier.
        :param environment_id: Environment resource id.
        :param relative_path: Path relative to environment root.
        :returns: Delete result.
        """
        path = (
            f"/v1/sessions/{session_id}/resources/environments"
            f"/{environment_id}/filesystem/{relative_path}"
        )
        return await _proxy_fs_response(
            session_id,
            "DELETE",
            path,
            request=request,
            environment_id=environment_id,
        )

    # ── Phase 5: environment shell proxy ─────────────────────────

    @router.post(
        "/sessions/{session_id}/resources/environments/{environment_id}/shell",
        response_model=None,
        # CSRF hardening: body is parsed via request.json(); require a JSON
        # Content-Type so a cross-site text/plain request can't reach it.
        dependencies=[Depends(require_json_content_type)],
    )
    async def run_environment_shell(
        session_id: str,
        environment_id: str,
        request: Request,
    ) -> Any:
        """
        Execute a shell command in an environment.

        :param session_id: Session/conversation identifier.
        :param environment_id: Environment resource id.
        :param request: JSON body with ``command`` and optional
            ``timeout``.
        :returns: Shell result.
        """
        body = await request.json()
        path = f"/v1/sessions/{session_id}/resources/environments/{environment_id}/shell"
        return await _proxy_fs_response(
            session_id,
            "POST",
            path,
            body,
            request=request,
            environment_id=environment_id,
            publish_invalidation=False,
        )

    # Generic single-resource lookup — registered AFTER typed
    # collections so "environments", "terminals", "files" are not
    # captured as resource_id.

    @router.get(
        "/sessions/{session_id}/resources/{resource_id}",
        response_model=None,
    )
    async def get_session_resource(
        request: Request,
        session_id: str,
        resource_id: str,
    ) -> dict[str, Any]:
        """
        Return a single resource by id from the unified inventory.

        :param session_id: Session/conversation identifier.
        :param resource_id: Opaque resource id.
        :returns: The resource object regardless of type.
        """
        await _validate_session(session_id, request, LEVEL_READ)
        path = f"/v1/sessions/{session_id}/resources/{resource_id}"
        return await _proxy_get_to_runner(session_id, path)
