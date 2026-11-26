"""Non-blocking semantic titles for newly started sessions."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from omnigent.db.db_models import current_workspace_id
from omnigent.entities.conversation import (
    DEFAULT_GENERATED_TITLE_MAX_CHARS,
    USER_SESSION_TITLE_MAX_CHARS,
    MessageData,
    SlashCommandData,
)
from omnigent.entities.workflow import WORKFLOW_NAME_MAX_CHARS
from omnigent.harness_aliases import canonicalize_harness
from omnigent.harness_plugins import background_title_generators
from omnigent.runner.background_titles.service import (
    BACKGROUND_TITLE_MAX_PROMPT_CHARS,
    CAP_LONG_BACKGROUND_TITLE_INSTRUCTIONS,
    FOLLOW_USER_LANGUAGE_TITLE_INSTRUCTION,
    LEGACY_BACKGROUND_TITLE_INSTRUCTIONS_MAX_CHARS,
)
from omnigent.server.scheduled.workflow_automations import saved_workflow_run_name
from omnigent.stores.conversation_store import ConversationStore

if TYPE_CHECKING:
    from omnigent.entities.conversation import Conversation, ConversationItem
    from omnigent.runner.routing import RunnerRouter
    from omnigent.server.schemas import SessionEventInput

_logger = logging.getLogger(__name__)

BACKGROUND_SESSION_TITLES_HEADER = "x-omnigent-background-session-titles"


def background_session_titles_enabled(headers: Mapping[str, str]) -> bool:
    """Resolve the browser-local title preference from a request header."""
    return headers.get(BACKGROUND_SESSION_TITLES_HEADER, "on").lower() != "off"


def _background_session_title_harness_supported(harness: str | None) -> bool:
    """Return whether a known session harness may run automatic title inference."""
    if harness is None:
        return True
    canonical = canonicalize_harness(harness)
    return canonical is not None and canonical in background_title_generators()


@dataclass(frozen=True)
class BackgroundTitleRequest:
    """Immutable session inputs for isolated title inference."""

    session_id: str
    prompt: str
    agent_id: str | None = None
    harness_override: str | None = None
    model_override: str | None = None
    sub_agent_name: str | None = None
    additional_instructions: str | None = None
    #: The owner's guidance alone, for runners too old to accept the combined text.
    owner_instructions: str | None = None


BackgroundTitleGenerator = Callable[[BackgroundTitleRequest], Awaitable[str | None]]
#: Reads the session owner's saved title instructions from a session id.
UserTitleInstructionsResolver = Callable[[str], str | None]

_USER_TITLE_INSTRUCTIONS_PREAMBLE = (
    "The session owner's personal title requirements follow. "
    "Where they conflict with the requirements above, follow these."
)


def compose_session_title_instructions(
    server_instructions: str | None, user_instructions: str | None
) -> str | None:
    """Combine the server's default title guidance with the owner's own guidance.

    :param server_instructions: Validated server-config guidance, or ``None``.
    :param user_instructions: Validated per-user guidance, or ``None``.
    :returns: The guidance to send, or ``None`` for the default title prompt.
    """
    server = server_instructions.strip() if server_instructions else ""
    user = user_instructions.strip() if user_instructions else ""
    if not user:
        return server or None
    if not server:
        return user
    return f"{server}\n\n{_USER_TITLE_INSTRUCTIONS_PREAMBLE}\n{user}"


_TITLE_WRAPPERS = "'\"`“”‘’"
_TRAILING_PUNCTUATION = re.compile(r"[.!?;:,]+$")
BACKGROUND_TITLE_MAX_CHARS = DEFAULT_GENERATED_TITLE_MAX_CHARS
CUSTOM_BACKGROUND_TITLE_MAX_CHARS = USER_SESSION_TITLE_MAX_CHARS


def normalize_background_title(
    value: str | None,
    *,
    max_chars: int = BACKGROUND_TITLE_MAX_CHARS,
    truncate_overflow: bool = False,
) -> str | None:
    """Return a compact title or ``None`` when model output is unusable."""
    if not value:
        return None
    first_line = next((line.strip() for line in value.splitlines() if line.strip()), "")
    title = " ".join(first_line.strip(_TITLE_WRAPPERS).split())
    title = _TRAILING_PUNCTUATION.sub("", title).strip()
    if len(title) > max_chars:
        if not truncate_overflow:
            return None
        title = title[: max_chars - 1].rstrip() + "…"
    if len(title) < 2:
        return None
    return title


#: Share of the runner's title prompt kept for the first message when refining,
#: so the agent's reply still fits after a long request.
REFINED_TITLE_MESSAGE_MAX_CHARS = 1_500
#: Turn ends that may each find the first reply missing (a native pane idling
#: before its transcript mirrors, or a mid-turn lull) before refinement gives up.
_MAX_REFINEMENT_TURN_END_CHECKS = 5
#: Bounds memory for sessions whose first turn never ends.
_MAX_PENDING_REFINEMENTS = 1_000
#: Bounds the item scan for the first turn's reply (pages of 100 items).
_FIRST_TURN_SCAN_PAGES = 10


def refined_title_prompt(first_message: str, reply: str) -> str:
    """Combine the first message and the agent's reply within the runner's prompt limit.

    :param first_message: Text of the session's first message, e.g. ``"look at <link>"``.
    :param reply: The agent's reply to it, e.g. ``"The PR adds retry backoff to the uploader."``.
    """
    message = first_message.strip()[:REFINED_TITLE_MESSAGE_MAX_CHARS]
    head = f"User request:\n{message}\n\nAgent reply:\n"
    return head + reply.strip()[: BACKGROUND_TITLE_MAX_PROMPT_CHARS - len(head)]


@dataclass(frozen=True)
class _FirstTurnReply:
    """The agent's last reply to a session's first input, as persisted so far."""

    text: str | None
    #: A later user input exists, so the first turn's reply can no longer change.
    superseded: bool


def _is_user_input(item: ConversationItem) -> bool:
    if isinstance(item.data, SlashCommandData):
        return item.data.kind == "skill"
    return (
        isinstance(item.data, MessageData) and item.data.role == "user" and not item.data.is_meta
    )


def _first_turn_reply(
    conversation_store: ConversationStore, session_id: str
) -> _FirstTurnReply | None:
    """Read the last assistant text between the session's first and second user inputs.

    :returns: ``None`` when the first turn is too long to scan, since a partial
        scan could end on a progress message instead of the reply.
    """
    # Imported lazily: the sessions helpers import this module.
    from omnigent.server.routes._sessions.helpers import _message_text

    reply: str | None = None
    in_first_turn = False
    after: str | None = None
    for _ in range(_FIRST_TURN_SCAN_PAGES):
        page = conversation_store.list_items(session_id, limit=100, after=after, order="asc")
        for item in page.data:
            if _is_user_input(item):
                if in_first_turn:
                    return _FirstTurnReply(text=reply, superseded=True)
                in_first_turn = True
            elif (
                in_first_turn
                and isinstance(item.data, MessageData)
                and item.data.role == "assistant"
                and not item.data.is_meta
            ):
                text = _message_text(item.data.content)
                if text and text.strip():
                    reply = text
        if not page.has_more or page.last_id is None:
            return _FirstTurnReply(text=reply, superseded=False)
        after = page.last_id
    return None


def _session_is_running(session_id: str) -> bool:
    from omnigent.server.routes._sessions.common import _session_status_cache

    return _session_status_cache.get(session_id) == "running"


@dataclass(frozen=True)
class _PendingTitleRefinement:
    """A session waiting for its first turn to end so its title can be refined."""

    request: BackgroundTitleRequest
    #: Resolves to the title the refinement may replace, or ``None`` when a user or
    #: agent rename already took the session's title.
    initial_title: asyncio.Task[str | None]
    #: The scheduling request's context, so the store reads the session's workspace.
    context: contextvars.Context
    workspace_id: int
    turn_end_checks: int = 0
    #: ``(reply, title)`` from an earlier refinement, kept while the first turn
    #: may still change, e.g. when a paused pane looked idle mid-turn.
    refined_from: tuple[str, str] | None = None

    @property
    def key(self) -> tuple[int, str]:
        return (self.workspace_id, self.request.session_id)


def _with_language_rule(instructions: str) -> str:
    if not instructions:
        return FOLLOW_USER_LANGUAGE_TITLE_INSTRUCTION
    return f"{instructions}\n{FOLLOW_USER_LANGUAGE_TITLE_INSTRUCTION}"


def runner_title_instructions(request: BackgroundTitleRequest, *, long_supported: bool) -> str:
    """Return the title guidance to send, fitted to an older runner's limit when needed.

    Older runners get the longest form that fits: the guidance without the
    language rule, then the owner's guidance alone, since it wins conflicts.

    :param request: Title request with combined and owner-only guidance.
    :param long_supported: Whether the runner accepts guidance past the legacy limit.
    """
    custom = (request.additional_instructions or "").strip()
    full = _with_language_rule(custom)
    limit = LEGACY_BACKGROUND_TITLE_INSTRUCTIONS_MAX_CHARS
    if long_supported or len(full) <= limit:
        return full
    owner = (request.owner_instructions or "").strip()
    candidates = (custom, _with_language_rule(owner) if owner else "", owner)
    fitted = next((text for text in candidates if text and len(text) <= limit), full[:limit])
    _logger.warning(
        "runner lacks long title instructions; sending shortened guidance "
        "session=%s chars=%d sent_chars=%d",
        request.session_id,
        len(full),
        len(fitted),
        extra={"session_id": request.session_id},
    )
    return fitted


class RunnerBackgroundTitleGenerator:
    """Request isolated title inference from the session's bound runner."""

    def __init__(self, runner_router: RunnerRouter, *, timeout_seconds: float = 65.0) -> None:
        self._runner_router = runner_router
        self._timeout_seconds = timeout_seconds

    async def __call__(self, request: BackgroundTitleRequest) -> str | None:
        routed = await asyncio.to_thread(
            self._runner_router.client_for_existing_conversation, request.session_id
        )
        if routed is None:
            return None
        body = {
            "prompt": request.prompt,
            "agent_id": request.agent_id,
            "harness_override": request.harness_override,
            "model_override": request.model_override,
            "sub_agent_name": request.sub_agent_name,
        }
        body["additional_instructions"] = runner_title_instructions(
            request,
            long_supported=self._runner_router.runner_has_capability(
                routed.runner_id, CAP_LONG_BACKGROUND_TITLE_INSTRUCTIONS
            ),
        )
        response = await routed.client.post(
            f"/v1/sessions/{request.session_id}/background-title",
            json=body,
            timeout=self._timeout_seconds,
        )
        response.raise_for_status()
        payload: Any = response.json()
        if not isinstance(payload, dict) or payload.get("status") != "generated":
            return None
        title = payload.get("title")
        return title if isinstance(title, str) else None


class BackgroundSessionTitleCoordinator:
    """Coordinate background titles and policy-aware agent rename requests."""

    def __init__(
        self,
        conversation_store: ConversationStore,
        generator: BackgroundTitleGenerator,
        *,
        timeout_seconds: float = 70.0,
        seed_wait_seconds: float = 15.0,
        max_concurrency: int = 4,
        additional_instructions: str | None = None,
        user_instructions: UserTitleInstructionsResolver | None = None,
        turn_settle_seconds: float = 3.0,
    ) -> None:
        """
        :param additional_instructions: Server-configured title guidance.
        :param user_instructions: Optional lookup of the session owner's saved
            guidance, called off the event loop with the session id.
        :param turn_settle_seconds: Delay after the first turn ends before its
            reply is read, so a native transcript can mirror and a brief idle
            lull mid-turn is not mistaken for the turn's end.
        """
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self._conversation_store = conversation_store
        self._generator = generator
        self._timeout_seconds = timeout_seconds
        self._seed_wait_seconds = seed_wait_seconds
        self._additional_instructions = additional_instructions
        self._user_instructions = user_instructions
        self._turn_settle_seconds = turn_settle_seconds
        self._generation_slots = asyncio.Semaphore(max_concurrency)
        self._pending: set[asyncio.Task[Any]] = set()
        self._scheduled_session_ids: set[str] = set()
        self._scheduled_task_summary_ids: set[str] = set()
        self._pending_refinements: dict[tuple[int, str], _PendingTitleRefinement] = {}
        #: Set by each idle edge that arrives while a refinement is settling.
        self._settling_idle_edges: dict[tuple[int, str], asyncio.Event] = {}
        self._closed = False

    async def format_agent_title(self, request: BackgroundTitleRequest) -> str | None:
        """Apply configured title requirements without changing the stored title.

        Without custom requirements, the agent's proposed title is used as-is.
        Generation failures leave the existing title intact rather than falling
        back to a proposal that may violate the configured format.
        """
        request = await self._with_session_instructions(request)
        if request.additional_instructions is None:
            return request.prompt
        try:
            async with asyncio.timeout(self._timeout_seconds), self._generation_slots:
                return await self._generate_title(request)
        except Exception:
            _logger.warning(
                "agent session title generation failed session=%s",
                request.session_id,
                exc_info=True,
            )
            return None

    async def _with_session_instructions(
        self, request: BackgroundTitleRequest
    ) -> BackgroundTitleRequest:
        """Attach the server and session-owner title guidance for one session.

        A failed owner lookup falls back to the server guidance alone so a
        settings outage never blocks titles.
        """
        user_instructions = None
        if self._user_instructions is not None:
            try:
                user_instructions = await asyncio.to_thread(
                    self._user_instructions, request.session_id
                )
            except Exception:
                _logger.warning(
                    "user session title instructions unavailable session=%s",
                    request.session_id,
                    exc_info=True,
                )
        return replace(
            request,
            additional_instructions=compose_session_title_instructions(
                self._additional_instructions, user_instructions
            ),
            owner_instructions=(user_instructions or "").strip() or None,
        )

    async def _generate_title(self, request: BackgroundTitleRequest) -> str | None:
        generated = await asyncio.wait_for(
            self._generator(request),
            timeout=self._timeout_seconds,
        )
        has_custom_instructions = bool(
            request.additional_instructions and request.additional_instructions.strip()
        )
        return normalize_background_title(
            generated,
            max_chars=(
                CUSTOM_BACKGROUND_TITLE_MAX_CHARS
                if has_custom_instructions
                else BACKGROUND_TITLE_MAX_CHARS
            ),
            truncate_overflow=has_custom_instructions,
        )

    def schedule(
        self,
        *,
        session_id: str,
        prompt: str,
        expected_seed_title: str,
        agent_id: str | None = None,
        harness_override: str | None = None,
        model_override: str | None = None,
        sub_agent_name: str | None = None,
    ) -> None:
        """Schedule at most one title attempt and return without awaiting it.

        The session is also registered for a refinement once its first turn
        ends (see :meth:`turn_ended`).
        """
        if session_id in self._scheduled_session_ids:
            return
        self._scheduled_session_ids.add(session_id)
        request = BackgroundTitleRequest(
            session_id=session_id,
            prompt=prompt,
            agent_id=agent_id,
            harness_override=harness_override,
            model_override=model_override,
            sub_agent_name=sub_agent_name,
        )
        task = asyncio.create_task(
            self._run(request=request, expected_seed_title=expected_seed_title),
            name=f"background-session-title-{session_id}",
        )
        self._pending.add(task)

        def _discard(completed: asyncio.Task[str | None]) -> None:
            self._pending.discard(completed)
            self._scheduled_session_ids.discard(session_id)

        task.add_done_callback(_discard)
        workspace_id = current_workspace_id()
        if (workspace_id, session_id) not in self._pending_refinements:
            self._register_refinement(
                _PendingTitleRefinement(
                    request=request,
                    initial_title=task,
                    context=contextvars.copy_context(),
                    workspace_id=workspace_id,
                )
            )

    def turn_ended(self, session_id: str) -> None:
        """Refine a session's generated title from its first turn's reply.

        A refined title is refined again only if that reply changes before a
        later user input ends the first turn. Does nothing for sessions without
        a pending refinement, so later turns, child sessions, and sessions titled
        by a user or automation are untouched.
        """
        key = (current_workspace_id(), session_id)
        settling_idle_edge = self._settling_idle_edges.get(key)
        if settling_idle_edge is not None:
            settling_idle_edge.set()
            return
        refinement = self._pending_refinements.pop(key, None)
        if refinement is None or self._closed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        idle_edge = asyncio.Event()
        self._settling_idle_edges[key] = idle_edge
        task = loop.create_task(
            self._refine(refinement, idle_edge),
            name=f"background-session-title-refinement-{session_id}",
            context=refinement.context,
        )
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    def _register_refinement(self, refinement: _PendingTitleRefinement) -> None:
        if self._closed:
            return
        self._pending_refinements[refinement.key] = refinement
        while len(self._pending_refinements) > _MAX_PENDING_REFINEMENTS:
            del self._pending_refinements[next(iter(self._pending_refinements))]

    def _recheck_first_turn_later(
        self,
        refinement: _PendingTitleRefinement,
        first_turn: _FirstTurnReply,
        refined_from: tuple[str, str] | None = None,
    ) -> None:
        """Wait for another turn end while the first turn's reply may still change."""
        if (
            first_turn.superseded
            or refinement.turn_end_checks + 1 >= _MAX_REFINEMENT_TURN_END_CHECKS
        ):
            return
        self._register_refinement(
            replace(
                refinement,
                turn_end_checks=refinement.turn_end_checks + 1,
                refined_from=refined_from or refinement.refined_from,
            )
        )

    async def _wait_for_settled_turn(
        self, refinement: _PendingTitleRefinement, idle_edge: asyncio.Event
    ) -> str | None:
        """Wait for the first-message title, then for idle edges to stop for the settle delay.

        :returns: The title the refinement may replace, or ``None`` when a rename took it.
        """
        try:
            if refinement.refined_from is not None:
                owned_title: str | None = refinement.refined_from[1]
            else:
                owned_title = await asyncio.shield(refinement.initial_title)
            if owned_title is None:
                return None
            while True:
                idle_edge.clear()
                try:
                    await asyncio.wait_for(idle_edge.wait(), timeout=self._turn_settle_seconds)
                except TimeoutError:
                    return owned_title
        finally:
            if self._settling_idle_edges.get(refinement.key) is idle_edge:
                del self._settling_idle_edges[refinement.key]

    async def _refine(self, refinement: _PendingTitleRefinement, idle_edge: asyncio.Event) -> None:
        """Retitle from the first message and reply if the generated title is still current."""
        session_id = refinement.request.session_id
        started = time.perf_counter()
        try:
            owned_title = await self._wait_for_settled_turn(refinement, idle_edge)
            if owned_title is None:
                return
            first_turn = await asyncio.to_thread(
                _first_turn_reply, self._conversation_store, session_id
            )
            if first_turn is None:
                _logger.info(
                    "background session title refinement skipped session=%s "
                    "reason=first_turn_too_long elapsed_ms=%.1f",
                    session_id,
                    (time.perf_counter() - started) * 1000,
                    extra={"session_id": session_id},
                )
                return
            reply = first_turn.text
            if not first_turn.superseded and _session_is_running(session_id):
                # An idle lull mid-turn: the first reply may still change.
                reply = None
            if reply is None or (
                refinement.refined_from is not None and reply == refinement.refined_from[0]
            ):
                self._recheck_first_turn_later(refinement, first_turn)
                return
            request = replace(
                refinement.request,
                prompt=refined_title_prompt(refinement.request.prompt, reply),
            )
            async with self._generation_slots:
                # Skip the inference when a rename already won; the guarded rename covers races.
                conversation = await asyncio.to_thread(
                    self._conversation_store.get_conversation, session_id
                )
                if conversation is None or conversation.title != owned_title:
                    return
                request = await self._with_session_instructions(request)
                title = await self._generate_title(request)
            if title is None or title == owned_title:
                _logger.info(
                    "background session title refinement skipped session=%s "
                    "reason=%s elapsed_ms=%.1f",
                    session_id,
                    "invalid_title" if title is None else "unchanged",
                    (time.perf_counter() - started) * 1000,
                    extra={"session_id": session_id},
                )
                if title is not None:
                    self._recheck_first_turn_later(refinement, first_turn, (reply, title))
                return
            updated = await asyncio.to_thread(
                self._conversation_store.rename_conversation_if_title_matches,
                session_id,
                owned_title,
                title,
            )
            if updated is not None:
                self._recheck_first_turn_later(refinement, first_turn, (reply, title))
            _logger.info(
                "background session title refinement completed session=%s renamed=%s "
                "elapsed_ms=%.1f",
                session_id,
                updated is not None,
                (time.perf_counter() - started) * 1000,
                extra={"session_id": session_id},
            )
        except TimeoutError:
            _logger.info(
                "background session title refinement timed out session=%s elapsed_ms=%.1f",
                session_id,
                (time.perf_counter() - started) * 1000,
                extra={"session_id": session_id},
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # background metadata must never fail the user turn
            _logger.warning(
                "background session title refinement failed session=%s elapsed_ms=%.1f",
                session_id,
                (time.perf_counter() - started) * 1000,
                exc_info=True,
                extra={"session_id": session_id},
            )

    def schedule_task_summary(
        self,
        *,
        session_id: str,
        prompt: str,
        agent_id: str | None = None,
        harness_override: str | None = None,
        model_override: str | None = None,
        sub_agent_name: str | None = None,
    ) -> None:
        """Schedule a task-summary attempt for a child session."""
        if session_id in self._scheduled_task_summary_ids:
            return
        self._scheduled_task_summary_ids.add(session_id)
        task = asyncio.create_task(
            self._run_task_summary(
                request=BackgroundTitleRequest(
                    session_id=session_id,
                    prompt=prompt,
                    agent_id=agent_id,
                    harness_override=harness_override,
                    model_override=model_override,
                    sub_agent_name=sub_agent_name,
                ),
            ),
            name=f"background-task-summary-{session_id}",
        )
        self._pending.add(task)

        def _discard(completed: asyncio.Task[None]) -> None:
            self._pending.discard(completed)
            self._scheduled_task_summary_ids.discard(session_id)

        task.add_done_callback(_discard)

    async def wait_for_idle(self) -> None:
        """Wait for currently scheduled jobs; used by focused tests."""
        if self._pending:
            await asyncio.gather(*tuple(self._pending))

    async def shutdown(self) -> None:
        """Cancel and drain pending title jobs during server shutdown."""
        self._closed = True
        self._pending_refinements.clear()
        self._settling_idle_edges.clear()
        pending = tuple(self._pending)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _run(
        self,
        *,
        request: BackgroundTitleRequest,
        expected_seed_title: str,
    ) -> str | None:
        """Replace the seed title; return the title a refinement may replace.

        :returns: The generated title, the seed when generation produced none,
            or ``None`` when a user or agent rename already replaced the seed.
        """
        started = time.perf_counter()
        try:
            async with self._generation_slots:
                seed_ready = await self._wait_for_seed(
                    session_id=request.session_id,
                    expected_seed_title=expected_seed_title,
                )
                if not seed_ready:
                    _logger.info(
                        "background session title skipped session=%s "
                        "reason=seed_unavailable elapsed_ms=%.1f",
                        request.session_id,
                        (time.perf_counter() - started) * 1000,
                        extra={"session_id": request.session_id},
                    )
                    return None
                request = await self._with_session_instructions(request)
                title = await self._generate_title(request)
            if title is None:
                _logger.info(
                    "background session title skipped session=%s "
                    "reason=invalid_title elapsed_ms=%.1f",
                    request.session_id,
                    (time.perf_counter() - started) * 1000,
                    extra={"session_id": request.session_id},
                )
                return expected_seed_title
            updated = await asyncio.to_thread(
                self._conversation_store.rename_conversation_if_title_matches,
                request.session_id,
                expected_seed_title,
                title,
            )
            _logger.info(
                "background session title completed session=%s renamed=%s elapsed_ms=%.1f",
                request.session_id,
                updated is not None,
                (time.perf_counter() - started) * 1000,
                extra={"session_id": request.session_id},
            )
            return None if updated is None else title
        except TimeoutError:
            _logger.info(
                "background session title timed out session=%s elapsed_ms=%.1f",
                request.session_id,
                (time.perf_counter() - started) * 1000,
                extra={"session_id": request.session_id},
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # background metadata must never fail the user turn
            _logger.warning(
                "background session title failed session=%s elapsed_ms=%.1f",
                request.session_id,
                (time.perf_counter() - started) * 1000,
                exc_info=True,
                extra={"session_id": request.session_id},
            )
        return expected_seed_title

    async def _run_task_summary(
        self,
        *,
        request: BackgroundTitleRequest,
    ) -> None:
        """Generate a task summary for a child session and write it to task_summary."""
        started = time.perf_counter()
        try:
            async with self._generation_slots:
                generated = await asyncio.wait_for(
                    self._generator(request),
                    timeout=self._timeout_seconds,
                )
            title = normalize_background_title(generated)
            if title is None:
                _logger.info(
                    "background task summary skipped session=%s "
                    "reason=invalid_title elapsed_ms=%.1f",
                    request.session_id,
                    (time.perf_counter() - started) * 1000,
                )
                return
            updated = await asyncio.to_thread(
                self._conversation_store.set_task_summary,
                request.session_id,
                title,
            )
            _logger.info(
                "background task summary completed session=%s set=%s elapsed_ms=%.1f",
                request.session_id,
                updated is not None,
                (time.perf_counter() - started) * 1000,
            )
        except TimeoutError:
            _logger.info(
                "background task summary timed out session=%s elapsed_ms=%.1f",
                request.session_id,
                (time.perf_counter() - started) * 1000,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            _logger.warning(
                "background task summary failed session=%s elapsed_ms=%.1f",
                request.session_id,
                (time.perf_counter() - started) * 1000,
                exc_info=True,
            )

    async def _wait_for_seed(
        self,
        *,
        session_id: str,
        expected_seed_title: str,
    ) -> bool:
        deadline = time.monotonic() + self._seed_wait_seconds
        while True:
            conversation = await asyncio.to_thread(
                self._conversation_store.get_conversation,
                session_id,
            )
            if conversation is None:
                return False
            if conversation.title == expected_seed_title:
                return True
            if conversation.title is not None:
                return False
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(0.05)


@dataclass(frozen=True)
class PendingBackgroundSessionTitle:
    """A prepared title attempt that starts only after event forwarding succeeds."""

    coordinator: BackgroundSessionTitleCoordinator
    request: BackgroundTitleRequest

    def schedule(self, *, expected_seed_title: str | None) -> None:
        """Start the attempt using the title persisted by the active store."""
        if expected_seed_title is None:
            return
        self.coordinator.schedule(
            session_id=self.request.session_id,
            prompt=self.request.prompt,
            expected_seed_title=expected_seed_title,
            agent_id=self.request.agent_id,
            harness_override=self.request.harness_override,
            model_override=self.request.model_override,
            sub_agent_name=self.request.sub_agent_name,
        )


def prepare_background_session_title(
    *,
    coordinator: BackgroundSessionTitleCoordinator | None,
    conversation: Conversation,
    event: SessionEventInput,
    enabled: bool = True,
) -> PendingBackgroundSessionTitle | None:
    """Prepare a guarded first-turn title attempt for a top-level session."""
    if (
        not enabled
        or coordinator is None
        or conversation.title is not None
        or conversation.parent_conversation_id is not None
        or not _background_session_title_harness_supported(conversation.harness_override)
    ):
        return None

    prompt = background_title_prompt(event)
    if not prompt:
        return None
    # The deterministic seed already names the saved workflow the session runs.
    if saved_workflow_run_title(_title_content_from_event(event, prompt)) is not None:
        return None

    return PendingBackgroundSessionTitle(
        coordinator=coordinator,
        request=BackgroundTitleRequest(
            session_id=conversation.id,
            prompt=prompt,
            agent_id=conversation.agent_id,
            harness_override=conversation.harness_override,
            model_override=conversation.model_override,
            sub_agent_name=conversation.sub_agent_name,
        ),
    )


def schedule_background_child_task_summary(
    *,
    coordinator: BackgroundSessionTitleCoordinator | None,
    session_id: str,
    prompt: str,
    agent_id: str | None = None,
    sub_agent_name: str | None = None,
) -> None:
    """Schedule a background task-summary attempt for a child session."""
    if coordinator is None or not prompt:
        return
    coordinator.schedule_task_summary(
        session_id=session_id,
        prompt=prompt,
        agent_id=agent_id,
        sub_agent_name=sub_agent_name,
    )


def saved_workflow_run_title(content: list[dict[str, Any]]) -> str | None:
    """Title a session whose first message runs a saved workflow with the workflow's name.

    :param content: The message's content blocks, e.g.
        ``[{"type": "input_text", "text": '/workflow Run the saved workflow "Release" (ID …).'}]``.
    :returns: The workflow's name, e.g. ``"Release"``, or ``None`` for any other message
        or a name no saved workflow could have.
    """
    text = next(
        (
            block["text"]
            for block in content
            if block.get("type") == "input_text" and isinstance(block.get("text"), str)
        ),
        "",
    )
    # Saved names are one whitespace-collapsed line, kept literally, so read it the same way.
    name = " ".join((saved_workflow_run_name(text) or "").split())
    return name if 0 < len(name) <= WORKFLOW_NAME_MAX_CHARS else None


def _title_content_from_event(event: SessionEventInput, prompt: str) -> list[dict[str, Any]]:
    """The content blocks *event* seeds a title from; a slash command reads as its *prompt*."""
    content = event.data.get("content")
    if event.type == "slash_command" or not isinstance(content, list):
        return [{"type": "input_text", "text": prompt}]
    return [block for block in content if isinstance(block, dict)]


def background_title_prompt(event: SessionEventInput) -> str:
    if event.type == "slash_command":
        name = event.data.get("name")
        arguments = event.data.get("arguments", "")
        if not isinstance(name, str) or not name.strip() or not isinstance(arguments, str):
            return ""
        return f"/{name.strip()} {arguments}".strip()

    content = event.data.get("content")
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "input_text":
            text = block.get("text", "")
            if isinstance(text, str):
                parts.append(text)
    return " ".join(parts)[:4000]


# The app's coordinator, for turn ends published outside a request.
_installed_coordinator: BackgroundSessionTitleCoordinator | None = None


def install_coordinator(coordinator: BackgroundSessionTitleCoordinator | None) -> None:
    """Make ``coordinator`` the one :func:`notify_turn_end` uses; ``None`` uninstalls it."""
    global _installed_coordinator
    _installed_coordinator = coordinator


def notify_turn_end(session_id: str) -> None:
    """Refine a session's generated title after its first turn ended."""
    if _installed_coordinator is not None:
        _installed_coordinator.turn_ended(session_id)
