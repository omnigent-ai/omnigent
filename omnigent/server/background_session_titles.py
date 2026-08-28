"""Non-blocking semantic titles for newly started sessions."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from omnigent.entities.conversation import synthesize_conversation_title
from omnigent.harness_aliases import canonicalize_harness
from omnigent.harness_plugins import background_title_generators
from omnigent.stores.conversation_store import ConversationStore

if TYPE_CHECKING:
    from omnigent.entities.conversation import Conversation
    from omnigent.runner.routing import RunnerRouter
    from omnigent.server.schemas import SessionEventInput

_logger = logging.getLogger(__name__)


def _background_session_title_harness_supported(harness: str | None) -> bool:
    """Return whether a known session harness may run automatic title inference."""
    if harness is None:
        return True
    canonical = canonicalize_harness(harness)
    return canonical is not None and canonical in background_title_generators()


@dataclass(frozen=True)
class BackgroundTitleRequest:
    """Immutable session inputs captured after the first prompt is forwarded."""

    session_id: str
    prompt: str
    agent_id: str | None = None
    harness_override: str | None = None
    model_override: str | None = None
    sub_agent_name: str | None = None
    additional_instructions: str | None = None


BackgroundTitleGenerator = Callable[[BackgroundTitleRequest], Awaitable[str | None]]

_TITLE_WRAPPERS = "'\"`“”‘’"
_TRAILING_PUNCTUATION = re.compile(r"[.!?;:,]+$")


def normalize_background_title(value: str | None) -> str | None:
    """Return a compact title or ``None`` when model output is unusable."""
    if not value:
        return None
    first_line = next((line.strip() for line in value.splitlines() if line.strip()), "")
    title = " ".join(first_line.strip(_TITLE_WRAPPERS).split())
    title = _TRAILING_PUNCTUATION.sub("", title).strip()
    if len(title) < 2 or len(title) > 60:
        return None
    return title


class RunnerBackgroundTitleGenerator:
    """Request isolated title inference from the session's bound runner."""

    def __init__(self, runner_router: RunnerRouter, *, timeout_seconds: float = 65.0) -> None:
        self._runner_router = runner_router
        self._timeout_seconds = timeout_seconds

    async def __call__(self, request: BackgroundTitleRequest) -> str | None:
        routed = self._runner_router.client_for_existing_conversation(request.session_id)
        if routed is None:
            return None
        body = {
            "prompt": request.prompt,
            "agent_id": request.agent_id,
            "harness_override": request.harness_override,
            "model_override": request.model_override,
            "sub_agent_name": request.sub_agent_name,
        }
        if request.additional_instructions is not None:
            body["additional_instructions"] = request.additional_instructions
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
    """Run one guarded title attempt outside the user turn's critical path."""

    def __init__(
        self,
        conversation_store: ConversationStore,
        generator: BackgroundTitleGenerator,
        *,
        timeout_seconds: float = 70.0,
        seed_wait_seconds: float = 15.0,
        max_concurrency: int = 4,
        additional_instructions: str | None = None,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be at least 1")
        self._conversation_store = conversation_store
        self._generator = generator
        self._timeout_seconds = timeout_seconds
        self._seed_wait_seconds = seed_wait_seconds
        self._additional_instructions = additional_instructions
        self._generation_slots = asyncio.Semaphore(max_concurrency)
        self._pending: set[asyncio.Task[None]] = set()
        self._scheduled_session_ids: set[str] = set()
        self._scheduled_task_summary_ids: set[str] = set()

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
        """Schedule at most one title attempt and return without awaiting it."""
        if session_id in self._scheduled_session_ids:
            return
        self._scheduled_session_ids.add(session_id)
        task = asyncio.create_task(
            self._run(
                request=BackgroundTitleRequest(
                    session_id=session_id,
                    prompt=prompt,
                    agent_id=agent_id,
                    harness_override=harness_override,
                    model_override=model_override,
                    sub_agent_name=sub_agent_name,
                    additional_instructions=self._additional_instructions,
                ),
                expected_seed_title=expected_seed_title,
            ),
            name=f"background-session-title-{session_id}",
        )
        self._pending.add(task)

        def _discard(completed: asyncio.Task[None]) -> None:
            self._pending.discard(completed)
            self._scheduled_session_ids.discard(session_id)

        task.add_done_callback(_discard)

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
    ) -> None:
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
                    )
                    return
                generated = await asyncio.wait_for(
                    self._generator(request),
                    timeout=self._timeout_seconds,
                )
            title = normalize_background_title(generated)
            if title is None:
                _logger.info(
                    "background session title skipped session=%s "
                    "reason=invalid_title elapsed_ms=%.1f",
                    request.session_id,
                    (time.perf_counter() - started) * 1000,
                )
                return
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
            )
        except TimeoutError:
            _logger.info(
                "background session title timed out session=%s elapsed_ms=%.1f",
                request.session_id,
                (time.perf_counter() - started) * 1000,
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - background metadata must never fail the user turn
            _logger.warning(
                "background session title failed session=%s elapsed_ms=%.1f",
                request.session_id,
                (time.perf_counter() - started) * 1000,
                exc_info=True,
            )

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
        except Exception:  # noqa: BLE001
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
    expected_seed_title: str

    def schedule(self) -> None:
        """Start the prepared title attempt without blocking the caller."""
        self.coordinator.schedule(
            session_id=self.request.session_id,
            prompt=self.request.prompt,
            expected_seed_title=self.expected_seed_title,
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
) -> PendingBackgroundSessionTitle | None:
    """Prepare a guarded first-turn title attempt for a top-level session."""
    if (
        coordinator is None
        or conversation.title is not None
        or conversation.parent_conversation_id is not None
        or not _background_session_title_harness_supported(conversation.harness_override)
    ):
        return None

    prompt = background_title_prompt(event)
    if not prompt:
        return None

    expected_seed_title = synthesize_conversation_title([{"type": "input_text", "text": prompt}])
    if expected_seed_title is None:
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
        expected_seed_title=expected_seed_title,
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
