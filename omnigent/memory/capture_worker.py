from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Protocol

import httpx
from sqlalchemy.exc import SQLAlchemyError

from omnigent.db.db_models import workspace_scope
from omnigent.entities import (
    Conversation,
    ConversationItem,
    FunctionCallData,
    FunctionCallOutputData,
    MessageData,
)
from omnigent.memory.capture_models import (
    MemoryCandidate,
    MemoryCaptureJob,
    MemoryCaptureProvider,
    MemoryCaptureRequest,
)
from omnigent.memory.router import MemoryProviderError
from omnigent.spec.types import MemoryProviderName
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.memory_capture_store import (
    MemoryCaptureLeaseLostError,
    MemoryCaptureStateError,
    SqlAlchemyMemoryCaptureStore,
)

_logger = logging.getLogger(__name__)
_USER_TEXT_LIMIT = 8_000
_ASSISTANT_TEXT_LIMIT = 16_000
_TOOL_TEXT_LIMIT = 8_000
_CANDIDATE_LIMIT = 20
_CANDIDATE_TEXT_LIMIT = 1_000


class MemoryExtractionError(RuntimeError):
    pass


@dataclass(frozen=True)
class MemoryExtractionEpisode:
    job: MemoryCaptureJob
    conversation: Conversation
    user_text: str
    assistant_text: str
    tool_outcomes: tuple[str, ...]
    source_item_ids: tuple[str, ...]


class MemoryExtractor(Protocol):
    async def extract(
        self,
        episode: MemoryExtractionEpisode,
    ) -> Sequence[MemoryCandidate]: ...


class MemoryCaptureWorker:
    def __init__(
        self,
        *,
        store: SqlAlchemyMemoryCaptureStore,
        conversation_store: ConversationStore,
        extractor: MemoryExtractor,
        providers: Mapping[MemoryProviderName, MemoryCaptureProvider],
        poll_seconds: float = 1.0,
        lease_seconds: int = 120,
        worker_id: str | None = None,
    ) -> None:
        self._store = store
        self._conversation_store = conversation_store
        self._extractor = extractor
        self._providers = dict(providers)
        self._poll_seconds = poll_seconds
        self._lease_seconds = lease_seconds
        self._worker_id = worker_id or f"memory-worker-{uuid.uuid4().hex}"
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name=self._worker_id)

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def wake(self) -> None:
        self._wake.set()

    async def _run(self) -> None:
        while True:
            try:
                worked = await self.run_once()
            except (OSError, RuntimeError, SQLAlchemyError) as exc:
                _logger.warning("memory capture worker poll failed: %s", exc, exc_info=True)
                worked = False
            if worked:
                continue
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_seconds)

    async def run_once(self, *, now: int | None = None) -> bool:
        claim_time = int(time.time()) if now is None else now
        await asyncio.to_thread(self._store.expire_intents, claim_time)
        job = await asyncio.to_thread(
            self._store.claim_next,
            worker_id=self._worker_id,
            now=claim_time,
            lease_seconds=self._lease_seconds,
        )
        if job is None:
            return False
        try:
            if job.phase == "extraction":
                await self._extract(job, claim_time)
            else:
                await self._write(job, claim_time)
        except MemoryCaptureLeaseLostError:
            _logger.info(
                "memory capture job lease lost job=%s attempt=%d",
                job.id,
                job.attempt_count,
            )
        except MemoryCaptureStateError:
            _logger.info(
                "memory capture job no longer mutable job=%s attempt=%d",
                job.id,
                job.attempt_count,
            )
        except (
            KeyError,
            MemoryExtractionError,
            MemoryProviderError,
            OSError,
            RuntimeError,
            SQLAlchemyError,
            TimeoutError,
            ValueError,
            httpx.HTTPError,
        ) as exc:
            failed = await asyncio.to_thread(
                self._store.fail_job,
                workspace_id=job.workspace_id,
                job_id=job.id,
                worker_id=self._worker_id,
                attempt_number=job.attempt_count,
                error=type(exc).__name__,
                now=claim_time,
            )
            _logger.warning(
                "memory capture job failed job=%s phase=%s status=%s attempts=%d",
                job.id,
                job.phase,
                failed.status,
                failed.attempt_count,
            )
        return True

    async def _extract(self, job: MemoryCaptureJob, now: int) -> None:
        with workspace_scope(job.workspace_id):
            conversation, source, response_items = await asyncio.gather(
                asyncio.to_thread(
                    self._conversation_store.get_conversation,
                    job.conversation_id,
                ),
                asyncio.to_thread(
                    self._conversation_store.get_item,
                    job.conversation_id,
                    job.source_item_id,
                ),
                asyncio.to_thread(
                    self._conversation_store.list_items_by_response_id,
                    job.conversation_id,
                    job.response_id,
                ),
            )
        if conversation is None or source is None:
            raise MemoryExtractionError("capture episode no longer exists")
        episode = _build_episode(job, conversation, source, response_items)
        extracted = await self._extractor.extract(episode)
        candidates = _normalize_candidates(extracted, episode.source_item_ids)
        await asyncio.to_thread(
            self._store.complete_extraction,
            workspace_id=job.workspace_id,
            job_id=job.id,
            worker_id=self._worker_id,
            attempt_number=job.attempt_count,
            candidates=candidates,
            now=now,
        )

    async def _write(self, job: MemoryCaptureJob, now: int) -> None:
        review = await asyncio.to_thread(
            self._store.get_review_for_job,
            job.workspace_id,
            job.id,
        )
        if review is None or review.status != "approved":
            raise MemoryExtractionError("capture job has no approved candidates")
        facts = tuple(
            candidate.text
            for candidate in review.candidates
            if candidate.sensitivity != "sensitive"
        )
        if not facts:
            await asyncio.to_thread(
                self._store.complete_write,
                workspace_id=job.workspace_id,
                job_id=job.id,
                worker_id=self._worker_id,
                attempt_number=job.attempt_count,
                receipt={"added": 0, "status": "no_eligible_candidates"},
                now=now,
            )
            return
        provider = self._providers.get(job.provider)
        if provider is None:
            raise MemoryProviderError(f"capture provider {job.provider!r} is not configured")
        receipt = await provider.capture(
            MemoryCaptureRequest(
                operation_id=job.operation_id,
                scope=job.scope,
                facts=facts,
                captured_at=now * 1000,
            )
        )
        await asyncio.to_thread(
            self._store.complete_write,
            workspace_id=job.workspace_id,
            job_id=job.id,
            worker_id=self._worker_id,
            attempt_number=job.attempt_count,
            receipt={
                "added": receipt.added,
                "operation_id": receipt.operation_id,
                "provider": receipt.provider,
                "revision": receipt.revision,
                "scope": receipt.scope.kind,
                "updated_at": receipt.updated_at,
            },
            now=now,
        )


def _build_episode(
    job: MemoryCaptureJob,
    conversation: Conversation,
    source: ConversationItem,
    response_items: list[ConversationItem],
) -> MemoryExtractionEpisode:
    if (
        source.type != "message"
        or not isinstance(source.data, MessageData)
        or source.data.role != "user"
    ):
        raise MemoryExtractionError("capture source is not a user message")
    user_text = _message_text(source.data, "input_text")[:_USER_TEXT_LIMIT]
    assistant_parts: list[str] = []
    tool_parts: list[str] = []
    for item in response_items:
        if item.type == "message" and isinstance(item.data, MessageData):
            if item.data.role == "assistant":
                text = _message_text(item.data, "output_text")
                if text:
                    assistant_parts.append(text)
        elif item.type == "function_call" and isinstance(item.data, FunctionCallData):
            tool_parts.append(f"{item.data.name}({item.data.arguments})")
        elif item.type == "function_call_output" and isinstance(
            item.data,
            FunctionCallOutputData,
        ):
            tool_parts.append(str(item.data.output))
    assistant_text = "\n".join(assistant_parts)[:_ASSISTANT_TEXT_LIMIT]
    if not user_text or not assistant_text:
        raise MemoryExtractionError("capture episode has no user and assistant text")
    bounded_tools: list[str] = []
    remaining = _TOOL_TEXT_LIMIT
    for value in tool_parts:
        if remaining <= 0:
            break
        bounded = value[:remaining]
        if bounded:
            bounded_tools.append(bounded)
            remaining -= len(bounded)
    return MemoryExtractionEpisode(
        job=job,
        conversation=conversation,
        user_text=user_text,
        assistant_text=assistant_text,
        tool_outcomes=tuple(bounded_tools),
        source_item_ids=(source.id, *(item.id for item in response_items)),
    )


def _message_text(message: MessageData, block_type: str) -> str:
    return "\n".join(
        block["text"].strip()
        for block in message.content
        if isinstance(block, dict)
        and block.get("type") == block_type
        and isinstance(block.get("text"), str)
        and block["text"].strip()
    )


def _normalize_candidates(
    candidates: Sequence[MemoryCandidate],
    source_item_ids: tuple[str, ...],
) -> tuple[MemoryCandidate, ...]:
    normalized: list[MemoryCandidate] = []
    seen: set[str] = set()
    for candidate in candidates[:_CANDIDATE_LIMIT]:
        text = " ".join(candidate.text.split())
        key = text.casefold()
        if (
            not text
            or len(text) > _CANDIDATE_TEXT_LIMIT
            or key in seen
            or not 0 <= candidate.confidence <= 1
            or candidate.sensitivity == "sensitive"
        ):
            continue
        seen.add(key)
        normalized.append(replace(candidate, text=text, source_item_ids=source_item_ids))
    return tuple(normalized)
