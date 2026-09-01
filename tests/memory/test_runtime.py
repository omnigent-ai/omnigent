from __future__ import annotations

import hashlib
import logging
from collections.abc import Sequence

import pytest

from omnigent.memory import (
    MemoryCaptureReceipt,
    MemoryCaptureRequest,
    MemoryRecallProvider,
    MemoryRecallRequest,
    MemoryRuntime,
    MemoryScope,
    MemoryTurnContext,
    RetrievalResult,
    create_memory_runtime_from_env,
)
from omnigent.spec.types import (
    AgentSpec,
    MemoryProviderName,
    MemoryProviderSpec,
    MemoryRolloutMode,
    MemorySpec,
)


class Provider:
    name: MemoryProviderName = "qm-notebook"

    async def recall(self, request: MemoryRecallRequest) -> Sequence[RetrievalResult]:
        return [
            RetrievalResult(
                provider="qm-notebook",
                scope=request.scopes[0],
                text="Prefers concise answers",
            )
        ]

    async def capture(self, request: MemoryCaptureRequest) -> MemoryCaptureReceipt:
        return MemoryCaptureReceipt(
            provider=self.name,
            operation_id=request.operation_id,
            scope=request.scope,
            added=len(request.facts),
            revision="1",
            updated_at=request.captured_at,
        )


def _spec(mode: MemoryRolloutMode) -> AgentSpec:
    return AgentSpec(
        spec_version=1,
        memory=MemorySpec(
            mode=mode,
            providers=[
                MemoryProviderSpec(
                    provider="qm-notebook",
                    scopes=["personal"],
                    recall="ambient",
                )
            ],
        ),
    )


def _runtime() -> MemoryRuntime:
    provider = Provider()
    providers: dict[MemoryProviderName, MemoryRecallProvider] = {
        provider.name: provider,
    }
    return MemoryRuntime(providers)


def _context() -> MemoryTurnContext:
    return MemoryTurnContext(
        operation_id="op-1",
        workspace_id=7,
        account_subject="user-1",
        conversation_id="conv-1",
        query="How should you answer?",
    )


@pytest.mark.asyncio
async def test_shadow_retrieves_without_injecting() -> None:
    runtime = _runtime()

    prepared = await runtime.prepare(_spec("shadow"), _context(), injection_supported=True)

    assert len(prepared.recall.results) == 1
    assert prepared.framework_instruction is None


@pytest.mark.asyncio
async def test_read_only_injects_only_when_harness_supports_it() -> None:
    runtime = _runtime()

    supported = await runtime.prepare(_spec("read_only"), _context(), injection_supported=True)
    native = await runtime.prepare(_spec("read_only"), _context(), injection_supported=False)

    assert supported.framework_instruction is not None
    assert "Prefers concise answers" in supported.framework_instruction
    assert len(native.recall.results) == 1
    assert native.framework_instruction is None


@pytest.mark.asyncio
async def test_automatic_capture_derives_review_target_only_when_supported() -> None:
    runtime = _runtime()
    spec = AgentSpec(
        spec_version=1,
        memory=MemorySpec(
            mode="automatic_capture",
            providers=[
                MemoryProviderSpec(
                    provider="qm-notebook",
                    scopes=["personal"],
                    recall="ambient",
                    capture="review",
                )
            ],
        ),
    )

    supported = await runtime.prepare(
        spec,
        _context(),
        injection_supported=True,
        capture_supported=True,
    )
    native = await runtime.prepare(
        spec,
        _context(),
        injection_supported=False,
        capture_supported=False,
    )

    assert len(supported.capture_targets) == 1
    target = supported.capture_targets[0]
    assert target.provider == "qm-notebook"
    assert target.scope == MemoryScope(7, "personal", "user-1")
    assert target.capture_mode == "review"
    assert len(target.policy_hash) == 64
    assert native.capture_targets == ()


@pytest.mark.asyncio
async def test_recall_log_omits_memory_content_and_fingerprint(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runtime = _runtime()
    content_digest = hashlib.sha256(b"Prefers concise answers").hexdigest()

    with caplog.at_level(logging.INFO, logger="omnigent.memory.runtime"):
        await runtime.prepare(_spec("read_only"), _context(), injection_supported=True)

    messages = "\n".join(caplog.messages)
    assert "Prefers concise answers" not in messages
    assert content_digest not in messages
    assert "results=1" in messages
    assert "chars=23" in messages


def test_runtime_env_config_is_all_or_nothing() -> None:
    assert create_memory_runtime_from_env({}) is None
    with pytest.raises(ValueError, match="must be configured together"):
        create_memory_runtime_from_env({"OMNIGENT_QM_MEMORY_URL": "https://memory.internal"})
    runtime = create_memory_runtime_from_env(
        {
            "OMNIGENT_QM_MEMORY_URL": "https://memory.internal",
            "OMNIGENT_QM_MEMORY_SIGNING_SECRET": "s" * 32,
        }
    )
    assert runtime is not None


@pytest.mark.asyncio
async def test_runtime_closes_every_provider_when_one_close_fails() -> None:
    closed: list[str] = []

    class ClosingProvider(Provider):
        def __init__(self, name: MemoryProviderName, *, fails: bool = False) -> None:
            self.name = name
            self.fails = fails

        async def aclose(self) -> None:
            closed.append(self.name)
            if self.fails:
                raise RuntimeError("close failed")

    providers: dict[MemoryProviderName, MemoryRecallProvider] = {
        "qm-notebook": ClosingProvider("qm-notebook", fails=True),
        "hindsight": ClosingProvider("hindsight"),
    }

    await MemoryRuntime(providers).aclose()

    assert closed == ["qm-notebook", "hindsight"]
