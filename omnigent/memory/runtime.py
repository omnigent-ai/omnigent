from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import cast

from omnigent.memory.capture_models import (
    MemoryCaptureMode,
    MemoryCaptureProvider,
    MemoryCaptureTarget,
    MemoryEraseProvider,
)
from omnigent.memory.models import MemoryRecall, MemoryTurnContext
from omnigent.memory.providers import QmMemoryProvider
from omnigent.memory.router import MemoryRecallProvider, MemoryRouter, format_recalled_memory
from omnigent.spec.types import AgentSpec, MemoryProviderName

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreparedTurnMemory:
    recall: MemoryRecall
    framework_instruction: str | None = None
    capture_targets: tuple[MemoryCaptureTarget, ...] = ()


class MemoryRuntime:
    def __init__(self, providers: Mapping[MemoryProviderName, MemoryRecallProvider]) -> None:
        self._providers = dict(providers)

    async def prepare(
        self,
        spec: AgentSpec,
        context: MemoryTurnContext,
        *,
        injection_supported: bool,
        capture_supported: bool = True,
    ) -> PreparedTurnMemory:
        recall = await MemoryRouter(spec.memory, self._providers).recall(context)
        framework_instruction = format_recalled_memory(recall) if injection_supported else None
        capture_targets = self._capture_targets(spec, context) if capture_supported else ()
        _logger.info(
            "memory recall operation=%s mode=%s results=%d failures=%d chars=%d "
            "injected=%s injection_supported=%s",
            context.operation_id,
            spec.memory.mode if spec.memory is not None else "off",
            len(recall.results),
            len(recall.failures),
            sum(len(result.text) for result in recall.results),
            framework_instruction is not None,
            injection_supported,
        )
        return PreparedTurnMemory(
            recall=recall,
            framework_instruction=framework_instruction,
            capture_targets=capture_targets,
        )

    def _capture_targets(
        self,
        spec: AgentSpec,
        context: MemoryTurnContext,
    ) -> tuple[MemoryCaptureTarget, ...]:
        memory = spec.memory
        if memory is None or memory.mode != "automatic_capture":
            return ()
        targets: list[MemoryCaptureTarget] = []
        for route in memory.providers:
            provider = self._providers.get(route.provider)
            if (
                route.capture != "review"
                or provider is None
                or not callable(getattr(provider, "capture", None))
            ):
                continue
            policy_hash = hashlib.sha256(
                json.dumps(
                    {
                        "capture": route.capture,
                        "mode": memory.mode,
                        "provider": route.provider,
                        "scopes": route.scopes,
                        "version": 1,
                    },
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            for scope in context.resolve_scopes(route.scopes):
                if scope.kind != "personal":
                    continue
                targets.append(
                    MemoryCaptureTarget(
                        provider=route.provider,
                        scope=scope,
                        capture_mode=cast(MemoryCaptureMode, route.capture),
                        policy_hash=policy_hash,
                    )
                )
        return tuple(targets)

    async def aclose(self) -> None:
        closers = [
            (name, close)
            for name, provider in self._providers.items()
            if (close := getattr(provider, "aclose", None)) is not None
        ]
        results = await asyncio.gather(
            *(close() for _, close in closers),
            return_exceptions=True,
        )
        for (name, _), result in zip(closers, results, strict=True):
            if isinstance(result, BaseException):
                _logger.warning(
                    "memory provider close failed provider=%s error=%s",
                    name,
                    type(result).__name__,
                )

    def capture_providers(self) -> dict[MemoryProviderName, MemoryCaptureProvider]:
        return {
            name: cast(MemoryCaptureProvider, provider)
            for name, provider in self._providers.items()
            if callable(getattr(provider, "capture", None))
        }

    def erase_providers(self) -> dict[MemoryProviderName, MemoryEraseProvider]:
        return {
            name: cast(MemoryEraseProvider, provider)
            for name, provider in self._providers.items()
            if callable(getattr(provider, "erase", None))
            and callable(getattr(provider, "verify_erased", None))
        }

    def provider_names(self) -> tuple[MemoryProviderName, ...]:
        return tuple(self._providers)


def create_memory_runtime_from_env(
    environ: Mapping[str, str] | None = None,
) -> MemoryRuntime | None:
    source = os.environ if environ is None else environ
    qm_url = source.get("OMNIGENT_QM_MEMORY_URL", "").strip()
    qm_secret = source.get("OMNIGENT_QM_MEMORY_SIGNING_SECRET", "").strip()
    if bool(qm_url) != bool(qm_secret):
        raise ValueError(
            "OMNIGENT_QM_MEMORY_URL and OMNIGENT_QM_MEMORY_SIGNING_SECRET "
            "must be configured together"
        )
    if not qm_url:
        return None
    return MemoryRuntime({"qm-notebook": QmMemoryProvider(qm_url, qm_secret)})
