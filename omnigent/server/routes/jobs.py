"""Routes for Jobs/Workflows CRUD and execution.

A *job* is a saved AI workflow authored as a node graph in the web UI. Its
execution model is *promptgen*: the graph is rendered to an English narrative
client-side, persisted on the job, and fed as the initial prompt to a single
agent session when the job runs. A *run* records one such execution — it *is*
an agent session.

Endpoints (all under ``/v1``):

- ``POST/GET/GET{id}/PATCH{id}/DELETE{id}`` ``/jobs`` — job CRUD, scoped per user.
- ``POST /jobs/{id}/run`` — "Run now": create a session from the job's narrative
  and record a run.
- ``GET /jobs/{id}/runs`` — list a job's runs (optional ``?status=`` filter).
- ``GET /runs/{id}`` — a single run, with status reconciled from its session.

The backend never interprets the graph; it stores it as opaque JSON.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable

from fastapi import APIRouter, Request

from omnigent.db.utils import now_epoch
from omnigent.entities import (
    RUN_STATUS_FAILED,
    RUN_STATUS_FINISHED,
    RUN_STATUS_RUNNING,
    RUN_TRIGGER_ADHOC,
    Job,
    Run,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harness_aliases import canonicalize_harness
from omnigent.native_coding_agents import (
    CLAUDE_NATIVE_CODING_AGENT,
    CODEX_NATIVE_CODING_AGENT,
)
from omnigent.runner.routing import RunnerRouter
from omnigent.runtime.agent_cache import AgentCache
from omnigent.runtime.policies.approval import AUTO_APPROVE_LABEL
from omnigent.server.auth import LEVEL_OWNER, AuthProvider
from omnigent.server.routes._auth_helpers import attribution_user, require_user
from omnigent.server.routes.hosts import _proxy_list_dir
from omnigent.server.routes.sessions import (
    SessionLiveness,
    _announce_session_added,
    _create_session_from_existing_agent,
    _dispatch_session_event_to_runner,
    _ensure_runner_relay_ready,
    _get_runner_client,
    _launch_runner_on_host,
    _session_status_from_cache,
    _wait_for_runner_client,
)
from omnigent.server.schemas import (
    JobCreateRequest,
    JobResponse,
    JobUpdateRequest,
    RunResponse,
    SessionCreateRequest,
    SessionEventInput,
)
from omnigent.stores.agent_store import AgentStore
from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.file_store import FileStore
from omnigent.stores.job_store import JobStore
from omnigent.stores.permission_store import PermissionStore

_logger = logging.getLogger(__name__)

# System prompt for a job run. Injected at the harness level via the claude
# CLI's ``--append-system-prompt`` (see ``_native_launch_args``), so it steers
# the agent without appearing as a user-visible message.
_EXECUTION_SYSTEM_PROMPT = (
    "You are an execution engine for a flow chart. You should execute each step "
    "in the flow chart sequentially, without parallelizing branches. Do not ask "
    "questions to the user, you should make a best effort to execute steps with "
    "the information provided. Use installed plugins and skills that are "
    "available in order to complete tasks. Always use auto mode."
)


def _job_to_response(job: Job) -> JobResponse:
    """Convert a :class:`Job` entity to a :class:`JobResponse`.

    Parses the stored opaque ``graph`` JSON back into an object for the wire.

    :param job: The job entity to convert.
    :returns: The wire-shaped response.
    """
    try:
        graph = json.loads(job.graph) if job.graph else {}
    except json.JSONDecodeError:
        # The graph is opaque to us; if it somehow isn't valid JSON, surface
        # the raw string rather than 500ing a read.
        graph = {"raw": job.graph}
    try:
        schedule_config = json.loads(job.schedule_config) if job.schedule_config else None
    except json.JSONDecodeError:
        schedule_config = None
    return JobResponse(
        id=job.id,
        name=job.name,
        graph=graph,
        narrative=job.narrative,
        agent_id=job.agent_id,
        harness_override=job.harness_override,
        model_override=job.model_override,
        schedule_config=schedule_config,
        host_id=job.host_id,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def _run_to_response(run: Run) -> RunResponse:
    """Convert a :class:`Run` entity to a :class:`RunResponse`.

    :param run: The run entity to convert.
    :returns: The wire-shaped response.
    """
    return RunResponse(
        id=run.id,
        job_id=run.job_id,
        session_id=run.session_id,
        status=run.status,
        started_at=run.started_at,
        completed_at=run.completed_at,
        error=run.error,
        trigger=run.trigger,
    )


def _agent_has_responded(conversation_store: ConversationStore, session_id: str) -> bool:
    """Whether the session's agent has produced any output yet.

    The run seeds one ``user`` message (the narrative); an agent *turn* adds
    assistant messages, reasoning, or tool calls. Detecting those distinguishes
    "the turn ran" from "the session was just created and hasn't started" — the
    session reads ``idle`` in both cases, so the live-status cache alone can't
    tell them apart (especially for native harnesses that launch a terminal
    asynchronously).

    :param conversation_store: Store to read conversation items from.
    :param session_id: The run's session id.
    :returns: ``True`` once any agent-produced item exists.
    """
    page = conversation_store.list_items(session_id, limit=50, order="asc")
    for item in page.data:
        if item.type in ("function_call", "function_call_output", "reasoning", "native_tool"):
            return True
        # An assistant message means the agent replied (the seed is role=user).
        if item.type == "message" and getattr(item.data, "role", None) == "assistant":
            return True
    return False


def _reconcile_run(run: Run, job_store: JobStore, conversation_store: ConversationStore) -> Run:
    """Reconcile a still-``running`` run's status from its session.

    A run is recorded ``running`` at creation. Since a promptgen run is just an
    agent session (no DAG), the terminal state is derived from the session: a
    ``failed`` session fails the run; an ``idle`` session marks the run
    ``finished`` **only once the agent has actually produced output**. The
    second condition matters because a freshly-launched session (notably a
    native-Claude terminal, which starts asynchronously) reads ``idle`` before
    its first turn — without the output check the run would flip to ``finished``
    at t=0, before anything happened. The reconciled state is persisted so
    subsequent reads are cheap.

    :param run: The run to reconcile.
    :param job_store: Store used to persist a terminal transition.
    :param conversation_store: Store used to check for agent output.
    :returns: The (possibly updated) run.
    """
    if run.status != RUN_STATUS_RUNNING or run.session_id is None:
        return run
    session_status = _session_status_from_cache(run.session_id)
    if session_status == "failed":
        updated = job_store.update_run_status(
            run.id, status=RUN_STATUS_FAILED, completed_at=now_epoch()
        )
        return updated or run
    if session_status == "idle" and _agent_has_responded(conversation_store, run.session_id):
        updated = job_store.update_run_status(
            run.id, status=RUN_STATUS_FINISHED, completed_at=now_epoch()
        )
        return updated or run
    # Still running, or idle-but-not-yet-started: leave as ``running``.
    return run


def _pick_online_host(
    request: Request,
    *,
    user_id: str | None,
    preferred_host_id: str | None = None,
) -> str | None:
    """Pick an online host to launch the job-run's runner on.

    Runners are spawned on demand by a host (the web "New Chat" flow triggers
    this by creating the session with a ``host_id``). We do the same: find an
    online host the caller owns. Returns ``None`` when no host registry is wired
    (in-process tests) or none is online — the caller then seeds the narrative
    as history instead of dispatching.

    :param request: The originating request, carrying ``app.state``.
    :param user_id: Authenticated caller; ``None`` in single-user mode.
    :param preferred_host_id: The job's persisted host, if any. When it is
        online (and owned by the caller when auth is on) it is used verbatim;
        otherwise we fall back to auto-picking an online host.
    :returns: An online ``host_id``, or ``None``.
    """
    host_registry = getattr(request.app.state, "host_registry", None)
    host_store = getattr(request.app.state, "host_store", None)
    if host_registry is None:
        return None
    online = host_registry.online_host_ids()
    if not online:
        return None
    owned: set[str] | None = None
    if user_id is not None and host_store is not None:
        owned = {h.host_id for h in host_store.list_hosts(user_id)}
    # Honour the job's pinned host when it's online (and owned, if auth is on).
    if preferred_host_id is not None and preferred_host_id in online:
        if owned is None or preferred_host_id in owned:
            return preferred_host_id
    # Otherwise auto-pick: prefer an owned host when auth + a store are wired.
    if owned is not None:
        for host_id in online:
            if host_id in owned:
                return host_id
        return None
    return online[0]


async def _resolve_host_home(host_registry: object, host_conn: object) -> str | None:
    """Resolve a host's absolute home directory.

    A session bound to a host needs an absolute ``workspace``; the server can't
    expand ``~`` (only the host knows its ``HOME``). Mirroring the web client's
    ``deriveHomeDir``, we list the host's home (``~``) — whose entries carry
    absolute paths — and take the parent of the first entry. Returns ``None``
    when the listing fails or home is empty (then the run falls back to a seed).

    :param host_registry: The server-side ``HostRegistry``.
    :param host_conn: The live ``HostConnection`` for the host.
    :returns: Absolute home path, e.g. ``"/Users/alice"``, or ``None``.
    """
    try:
        result = await _proxy_list_dir(
            host_registry=host_registry,
            host_conn=host_conn,
            path="~",
            limit=1,
            after=None,
            before=None,
        )
    except Exception:  # noqa: BLE001 - any host/listing failure → seed fallback
        return None
    if result.get("status") != "ok":
        return None
    entries = result.get("entries") or []
    if not entries:
        return None
    first_path = entries[0].get("path")
    if not isinstance(first_path, str):
        return None
    slash = first_path.rfind("/")
    # Parent of the first entry is home; "/x" → "/", deeper → the dir.
    return first_path[:slash] if slash > 0 else "/"


def _native_launch_args(harness: str | None) -> list[str] | None:
    """Terminal launch args for a native-harness job run.

    Two concerns, both set at the harness/CLI level rather than as conversation
    content:

    1. **Auto-approve.** A job run is unattended — there's no human to answer an
       ApprovalCard — so a native harness in its default (prompt-on-action) mode
       would stall on the first Edit/Write/Bash. Force full bypass per harness,
       matching the headless seam polly's native workers use:

       - ``claude-native`` → ``--permission-mode bypassPermissions``
       - ``codex-native``  → ``--dangerously-bypass-approvals-and-sandbox``

    2. **Execution-engine system prompt.** The flow-execution framing
       (:data:`_EXECUTION_SYSTEM_PROMPT`) is appended to the agent's system
       prompt via the claude CLI's ``--append-system-prompt``, so it steers the
       run without showing up as a user-visible message. (Only wired for
       ``claude-native``; the codex CLI's system-prompt seam differs.)

    SDK harnesses (e.g. ``claude-sdk``) already default to ``bypassPermissions``
    at spawn, so they need nothing here. Returns ``None`` for any non-native /
    unknown harness (no terminal args set).

    :param harness: The run agent's canonical harness, or ``None``.
    :returns: A flat CLI-arg list, or ``None`` when nothing should be set.
    """
    canonical = canonicalize_harness(harness) or harness
    if canonical == CLAUDE_NATIVE_CODING_AGENT.harness:
        return [
            "--permission-mode",
            "bypassPermissions",
            "--append-system-prompt",
            _EXECUTION_SYSTEM_PROMPT,
        ]
    if canonical == CODEX_NATIVE_CODING_AGENT.harness:
        return ["--dangerously-bypass-approvals-and-sandbox"]
    return None


async def _execute_job_run(
    job: Job,
    request: Request,
    *,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    job_store: JobStore,
    runner_router: RunnerRouter | None,
    agent_cache: AgentCache | None,
    user_id: str | None,
    permission_store: PermissionStore | None,
    liveness_lookup: Callable[[list[str]], dict[str, SessionLiveness]] | None,
    file_store: FileStore | None,
    artifact_store: ArtifactStore | None,
    default_run_agent_id: str | None,
    trigger: str = RUN_TRIGGER_ADHOC,
) -> Run:
    """Run a job: create a session, bind a runner, dispatch the narrative.

    Mirrors the web "New Chat" flow so the run actually executes rather than
    just seeding history: create the session (no initial items), auto-bind an
    online runner, ready its relay, then dispatch the job's narrative as a user
    message that triggers an agent turn. When no runner is available (e.g. no
    host is registered), the narrative is persisted as a history-only seed so
    the session opens with the prompt ready to send.

    the background scheduler — both create a session, bind a runner, and
    dispatch the job's narrative, then persist a run row. The scheduler loads
    jobs with a non-null ``schedule_config`` and calls this with ``user_id`` set
    to the job owner and ``trigger=RUN_TRIGGER_SCHEDULED``.

    :param job: The job to run.
    :param request: The originating request (forwarded to session creation;
        only dereferenced for host-bound sessions, never for job runs which
        are host-less ``external`` sessions).
    :param default_run_agent_id: Fallback agent when the job has none bound.
    :param trigger: How this run was triggered — ``adhoc`` (default, manual
        "Run now") or ``scheduled`` (the time-trigger scheduler).
    :returns: The created :class:`Run`.
    :raises OmnigentError: 400 if no agent is bound and no default exists.
    """
    agent_id = job.agent_id or default_run_agent_id
    if agent_id is None:
        raise OmnigentError(
            "Job has no agent bound and no default agent is configured; "
            "set an agent on the job before running it.",
            code=ErrorCode.INVALID_INPUT,
        )

    # Pick an online host to launch the run's runner on. Runners are spawned on
    # demand by a host, so without one the narrative can only be seeded (the
    # session opens with the prompt ready to send manually).
    host_id = _pick_online_host(request, user_id=user_id, preferred_host_id=job.host_id)

    # A host-bound session needs an absolute workspace, and only the host knows
    # its HOME — resolve it via a list_dir round-trip. If that fails, drop the
    # host binding and fall back to seeding rather than failing the run.
    workspace: str | None = None
    host_registry = getattr(request.app.state, "host_registry", None)
    if host_id is not None and host_registry is not None:
        conn = host_registry.get(host_id)
        if conn is not None:
            workspace = await _resolve_host_home(host_registry, conn)
        if workspace is None:
            host_id = None

    # Auto-approve mode: a job run is unattended, so no human can answer an
    # approval prompt. Force full bypass for native harnesses (SDK harnesses
    # already default to bypass). Harness = the job's override, else the spec's.
    harness = job.harness_override
    if harness is None and agent_cache is not None:
        try:
            agent = await asyncio.to_thread(agent_store.get, agent_id)
            if agent is not None:
                loaded = await asyncio.to_thread(
                    agent_cache.load,
                    agent.id,
                    agent.bundle_location,
                    expand_env=agent.session_id is None,
                )
                harness = loaded.spec.executor.harness_kind
        except Exception:  # noqa: BLE001 - spec load is best-effort for approval mode
            _logger.warning("job-run harness resolve failed for agent %s", agent_id, exc_info=True)
            harness = None
    terminal_launch_args = _native_launch_args(harness)

    # Create the session WITHOUT initial items — we dispatch the narrative as a
    # real event below so it executes.
    body = SessionCreateRequest(
        agent_id=agent_id,
        title=f"Run: {job.name}",
        harness_override=job.harness_override,
        model_override=job.model_override,
        host_type="external",
        host_id=host_id,
        workspace=workspace,
        terminal_launch_args=terminal_launch_args,
    )
    session = await _create_session_from_existing_agent(
        conversation_store,
        agent_store,
        runner_router,
        body,
        request,
        agent_cache=agent_cache,
        user_id=user_id,
        permission_store=permission_store,
        liveness_lookup=liveness_lookup,
        file_store=file_store,
        artifact_store=artifact_store,
    )
    # Grant the creator ownership and surface the session in their open tabs,
    # mirroring the POST /v1/sessions route wrapper.
    if permission_store is not None and user_id is not None:
        await asyncio.to_thread(permission_store.ensure_user, user_id)
        await asyncio.to_thread(permission_store.grant, user_id, session.id, LEVEL_OWNER)
    _announce_session_added(user_id, session.id)

    # Unattended auto-approve: a job run has no human to answer an ApprovalCard,
    # so a policy ASK would hang the run until its timeout. Stamp the server-owned
    # label that makes the policy engine grant ASK verdicts automatically. Set
    # here (post-create) rather than via the create body, since that body rejects
    # this policy-owned namespace. This covers Omnigent policy-engine ASKs; the
    # harness's own permission prompts are handled by terminal_launch_args above.
    await asyncio.to_thread(
        conversation_store.set_labels, session.id, {AUTO_APPROVE_LABEL: "true"}
    )

    # Launch a runner on the host and wait for it to connect, then dispatch.
    runner_id: str | None = None
    if host_id is not None and host_registry is not None:
        conn = host_registry.get(host_id)
        conv = await asyncio.to_thread(conversation_store.get_conversation, session.id)
        if conn is not None and conv is not None:
            attempt = await _launch_runner_on_host(conv, conversation_store, host_registry, conn)
            runner_id = attempt.runner_id
            # Wait for the launched runner to connect before forwarding.
            await _wait_for_runner_client(
                session.id,
                runner_router,
                getattr(request.app.state, "tunnel_registry", None),
                runner_id=runner_id,
                timeout_s=30.0,
            )

    # Dispatch the narrative. With a connected runner this persists + forwards
    # the message and starts a turn; otherwise fall back to a history seed.
    # The execution-engine framing is injected at the harness level via
    # ``--append-system-prompt`` (see ``_native_launch_args``), so it
    # steers the agent without appearing as a user-visible message.
    narrative_event = SessionEventInput(
        type="message",
        data={"role": "user", "content": [{"type": "input_text", "text": job.narrative}]},
    )
    runner_client = await _get_runner_client(session.id, runner_router)
    conv = await asyncio.to_thread(conversation_store.get_conversation, session.id)
    if runner_client is not None and conv is not None:
        # Subscribe to runner output BEFORE forwarding (the stream has no
        # replay buffer), then dispatch the user message.
        await _ensure_runner_relay_ready(session.id, runner_id, runner_client, conversation_store)
        await _dispatch_session_event_to_runner(
            session.id,
            conv,
            narrative_event,
            conversation_store,
            runner_client,
            agent_name=agent_id,
            file_store=file_store,
            artifact_store=artifact_store,
            created_by=attribution_user(user_id),
            runner_router=runner_router,
        )
    else:
        # No runner available — seed the narrative as history so the session
        # opens with the prompt ready to send manually.
        from omnigent.entities import NewConversationItem, parse_item_data

        await asyncio.to_thread(
            conversation_store.append,
            session.id,
            [
                NewConversationItem(
                    type="message",
                    response_id="seed",
                    data=parse_item_data("message", narrative_event.data),
                    created_by=attribution_user(user_id),
                )
            ],
        )

    return await asyncio.to_thread(
        job_store.create_run,
        job_id=job.id,
        session_id=session.id,
        status=RUN_STATUS_RUNNING,
        created_by=attribution_user(user_id),
        trigger=trigger,
    )


def create_jobs_router(
    job_store: JobStore,
    conversation_store: ConversationStore,
    agent_store: AgentStore,
    runner_router: RunnerRouter | None = None,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    agent_cache: AgentCache | None = None,
    file_store: FileStore | None = None,
    artifact_store: ArtifactStore | None = None,
    liveness_lookup: Callable[[list[str]], dict[str, SessionLiveness]] | None = None,
    default_run_agent_id: str | None = None,
) -> APIRouter:
    """Build the jobs router.

    :param job_store: Store for job + run persistence.
    :param conversation_store: Store used when creating run sessions.
    :param agent_store: Store used to resolve a job's bound agent.
    :param runner_router: Runner router, forwarded to session creation.
    :param auth_provider: Auth provider used to identify the caller.
    :param permission_store: Permission store for ownership grants/scoping.
    :param agent_cache: Optional agent-spec cache, forwarded to session create.
    :param file_store: Optional file store, forwarded to session create.
    :param artifact_store: Optional artifact store, forwarded to session create.
    :param liveness_lookup: Optional liveness lookup, forwarded to session create.
    :param default_run_agent_id: Fallback agent id for jobs with none bound.
    :returns: A configured :class:`APIRouter`.
    """
    router = APIRouter()

    def _owner_scope(user_id: str | None) -> str | None:
        """The ``created_by`` filter for list/ownership checks.

        ``None`` in single-user mode (no scoping); the attribution actor
        otherwise.
        """
        return attribution_user(user_id) if permission_store is not None else None

    async def _load_owned_job(job_id: str, user_id: str | None) -> Job:
        """Fetch a job and enforce ownership, or raise 404.

        Returns 404 (not 403) for another user's job so existence isn't
        leaked across tenants.
        """
        job = await asyncio.to_thread(job_store.get_job, job_id)
        scope = _owner_scope(user_id)
        if job is None or (scope is not None and job.created_by != scope):
            raise OmnigentError(f"Job not found: {job_id!r}", code=ErrorCode.NOT_FOUND)
        return job

    @router.post("/jobs", status_code=201, response_model=JobResponse)
    async def create_job(request: Request, body: JobCreateRequest) -> JobResponse:
        """Create a job."""
        user_id = require_user(request, auth_provider)
        job = await asyncio.to_thread(
            job_store.create_job,
            name=body.name,
            graph=json.dumps(body.graph),
            narrative=body.narrative,
            agent_id=body.agent_id,
            harness_override=body.harness_override,
            model_override=body.model_override,
            created_by=attribution_user(user_id),
            schedule_config=(
                json.dumps(body.schedule_config) if body.schedule_config is not None else None
            ),
            host_id=body.host_id,
        )
        return _job_to_response(job)

    @router.get("/jobs", response_model=list[JobResponse])
    async def list_jobs(request: Request) -> list[JobResponse]:
        """List the caller's jobs, newest-updated first."""
        user_id = require_user(request, auth_provider)
        jobs = await asyncio.to_thread(job_store.list_jobs, created_by=_owner_scope(user_id))
        return [_job_to_response(j) for j in jobs]

    @router.get("/jobs/{job_id}", response_model=JobResponse)
    async def get_job(request: Request, job_id: str) -> JobResponse:
        """Fetch one job."""
        user_id = require_user(request, auth_provider)
        return _job_to_response(await _load_owned_job(job_id, user_id))

    @router.patch("/jobs/{job_id}", response_model=JobResponse)
    async def update_job(request: Request, job_id: str, body: JobUpdateRequest) -> JobResponse:
        """Patch a job's fields."""
        user_id = require_user(request, auth_provider)
        await _load_owned_job(job_id, user_id)
        graph = json.dumps(body.graph) if body.graph is not None else None
        schedule_config = (
            json.dumps(body.schedule_config) if body.schedule_config is not None else None
        )
        updated = await asyncio.to_thread(
            job_store.update_job,
            job_id,
            name=body.name,
            graph=graph,
            narrative=body.narrative,
            agent_id=body.agent_id,
            harness_override=body.harness_override,
            model_override=body.model_override,
            schedule_config=schedule_config,
            host_id=body.host_id,
        )
        if updated is None:
            raise OmnigentError(f"Job not found: {job_id!r}", code=ErrorCode.NOT_FOUND)
        return _job_to_response(updated)

    @router.delete("/jobs/{job_id}", status_code=204)
    async def delete_job(request: Request, job_id: str) -> None:
        """Delete a job (and, via cascade, its runs)."""
        user_id = require_user(request, auth_provider)
        await _load_owned_job(job_id, user_id)
        await asyncio.to_thread(job_store.delete_job, job_id)

    @router.post("/jobs/{job_id}/run", status_code=201, response_model=RunResponse)
    async def run_job(request: Request, job_id: str) -> RunResponse:
        """Run a job now: create a session from its narrative + record a run."""
        user_id = require_user(request, auth_provider)
        job = await _load_owned_job(job_id, user_id)
        run = await _execute_job_run(
            job,
            request,
            conversation_store=conversation_store,
            agent_store=agent_store,
            job_store=job_store,
            runner_router=runner_router,
            agent_cache=agent_cache,
            user_id=user_id,
            permission_store=permission_store,
            liveness_lookup=liveness_lookup,
            file_store=file_store,
            artifact_store=artifact_store,
            default_run_agent_id=default_run_agent_id,
        )
        return _run_to_response(run)

    @router.get("/jobs/{job_id}/runs", response_model=list[RunResponse])
    async def list_runs(
        request: Request, job_id: str, status: str | None = None
    ) -> list[RunResponse]:
        """List a job's runs, newest-started first, with status reconciled."""
        user_id = require_user(request, auth_provider)
        await _load_owned_job(job_id, user_id)
        runs = await asyncio.to_thread(job_store.list_runs, job_id=job_id, status=status)
        reconciled = [
            await asyncio.to_thread(_reconcile_run, r, job_store, conversation_store) for r in runs
        ]
        # Re-apply the status filter post-reconcile so a now-finished run
        # doesn't linger in a ``?status=running`` view.
        if status is not None:
            reconciled = [r for r in reconciled if r.status == status]
        return [_run_to_response(r) for r in reconciled]

    @router.get("/runs/{run_id}", response_model=RunResponse)
    async def get_run(request: Request, run_id: str) -> RunResponse:
        """Fetch one run, with status reconciled from its session."""
        user_id = require_user(request, auth_provider)
        run = await asyncio.to_thread(job_store.get_run, run_id)
        if run is not None:
            # Enforce ownership via the parent job (404 on miss/cross-tenant).
            await _load_owned_job(run.job_id, user_id)
            run = await asyncio.to_thread(_reconcile_run, run, job_store, conversation_store)
        if run is None:
            raise OmnigentError(f"Run not found: {run_id!r}", code=ErrorCode.NOT_FOUND)
        return _run_to_response(run)

    return router
