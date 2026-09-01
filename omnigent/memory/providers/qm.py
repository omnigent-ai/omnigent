from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Sequence
from typing import TypeGuard
from urllib.parse import urlparse

import httpx

from omnigent.memory.capture_models import (
    MemoryCaptureReceipt,
    MemoryCaptureRequest,
    MemoryEraseReceipt,
    MemoryEraseRequest,
)
from omnigent.memory.models import MemoryRecallRequest, MemoryScope, RetrievalResult
from omnigent.memory.providers.qm_auth import QmRequestSigner
from omnigent.memory.router import MemoryProviderError
from omnigent.spec.types import MemoryProviderName


class QmMemoryProvider:
    name: MemoryProviderName = "qm-notebook"

    def __init__(
        self,
        base_url: str,
        signing_key_material: str,
        *,
        client: httpx.AsyncClient | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        parsed = urlparse(base_url)
        loopback = parsed.scheme == "http" and parsed.hostname in {
            "127.0.0.1",
            "localhost",
            "::1",
        }
        if (parsed.scheme != "https" and not loopback) or not parsed.netloc:
            raise ValueError("qm memory URL must use HTTPS or loopback HTTP")
        if parsed.username or parsed.password or parsed.fragment:
            raise ValueError("qm memory URL must not contain credentials or a fragment")
        self._base_url = base_url.rstrip("/")
        self._signer = QmRequestSigner(signing_key_material)
        self._client = client or httpx.AsyncClient()
        self._owns_client = client is None
        self._now = now

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def recall(self, request: MemoryRecallRequest) -> Sequence[RetrievalResult]:
        results: list[RetrievalResult] = []
        for index, scope in enumerate(request.scopes):
            qm_scope = _qm_scope_id(scope)
            operation_id = f"{request.context.operation_id}:qm-read:{index}"
            body = _canonical_json({"operationId": operation_id, "scopeId": qm_scope})
            response = await self._post("/v1/memory/read", body)
            payload = _response_object(response)
            if payload.get("operationId") != operation_id or payload.get("scopeId") != qm_scope:
                raise MemoryProviderError(
                    "qm memory response correlation did not match the request"
                )
            content = payload.get("content")
            revision = payload.get("revision")
            if not isinstance(content, str) or not isinstance(revision, str):
                raise MemoryProviderError("qm memory response is missing content or revision")
            if not content.strip():
                continue
            updated_at = payload.get("updatedAt")
            results.append(
                RetrievalResult(
                    provider=self.name,
                    scope=scope,
                    text=content,
                    score=1.0,
                    record_id=qm_scope,
                    version=revision,
                    source_title="Curated memory",
                    sensitivity="personal" if scope.kind == "personal" else "internal",
                    snapshot_sha=(
                        hashlib.sha256(content.encode()).hexdigest()
                        if not isinstance(updated_at, int)
                        else hashlib.sha256(
                            f"{revision}:{updated_at}:{content}".encode()
                        ).hexdigest()
                    ),
                )
            )
        return results

    async def capture(self, request: MemoryCaptureRequest) -> MemoryCaptureReceipt:
        qm_scope = _qm_scope_id(request.scope)
        body = _canonical_json(
            {
                "capturedAt": request.captured_at,
                "facts": list(request.facts),
                "operationId": request.operation_id,
                "scopeId": qm_scope,
            }
        )
        response = await self._post("/v1/memory/capture", body)
        payload = _response_object(response)
        if (
            payload.get("operationId") != request.operation_id
            or payload.get("scopeId") != qm_scope
        ):
            raise MemoryProviderError(
                "qm memory capture response correlation did not match the request"
            )
        added = payload.get("added")
        revision = payload.get("revision")
        updated_at = payload.get("updatedAt")
        if (
            not isinstance(added, int)
            or isinstance(added, bool)
            or added < 0
            or not isinstance(revision, str)
            or not revision
            or not isinstance(updated_at, int)
            or isinstance(updated_at, bool)
            or updated_at < 0
        ):
            raise MemoryProviderError("qm memory capture response is missing its receipt")
        return MemoryCaptureReceipt(
            provider=self.name,
            operation_id=request.operation_id,
            scope=request.scope,
            added=added,
            revision=revision,
            updated_at=updated_at,
        )

    async def erase(self, request: MemoryEraseRequest) -> MemoryEraseReceipt:
        qm_scope = _qm_scope_id(request.scope)
        body = _canonical_json(
            {
                "erasedAt": request.erased_at,
                "operationId": request.operation_id,
                "scopeId": qm_scope,
            }
        )
        response = await self._post("/v1/memory/erase", body)
        payload = _response_object(response)
        if payload.get("operationId") != request.operation_id:
            raise MemoryProviderError(
                "qm memory erase response correlation did not match the request"
            )
        scope_hash = payload.get("scopeHash")
        erased_revisions = payload.get("erasedRevisions")
        tombstoned_operations = payload.get("tombstonedOperations")
        completed_at = payload.get("completedAt")
        if (
            not isinstance(scope_hash, str)
            or len(scope_hash) != 64
            or any(character not in "0123456789abcdef" for character in scope_hash)
            or not _non_negative_int(erased_revisions)
            or not _non_negative_int(tombstoned_operations)
            or not _non_negative_int(completed_at)
        ):
            raise MemoryProviderError("qm memory erase response is missing its receipt")
        return MemoryEraseReceipt(
            provider=self.name,
            operation_id=request.operation_id,
            scope_hash=scope_hash,
            erased_revisions=erased_revisions,
            tombstoned_operations=tombstoned_operations,
            completed_at=completed_at,
        )

    async def verify_erased(self, request: MemoryEraseRequest) -> bool:
        qm_scope = _qm_scope_id(request.scope)
        operation_id = f"{request.operation_id}:verify"
        body = _canonical_json({"operationId": operation_id, "scopeId": qm_scope})
        response = await self._post("/v1/memory/read", body)
        payload = _response_object(response)
        if payload.get("operationId") != operation_id or payload.get("scopeId") != qm_scope:
            raise MemoryProviderError(
                "qm memory erase verification correlation did not match the request"
            )
        content = payload.get("content")
        if not isinstance(content, str):
            raise MemoryProviderError("qm memory erase verification is missing content")
        return not content.strip()

    async def _post(self, path: str, body: str) -> httpx.Response:
        timestamp = int(self._now())
        signature = self._signer.sign(
            timestamp=timestamp,
            method="POST",
            path=path,
            body=body,
        )
        try:
            response = await self._client.post(
                f"{self._base_url}{path}",
                content=body,
                headers={
                    "content-type": "application/json",
                    "x-timestamp": str(timestamp),
                    "x-signature": f"v0={signature}",
                },
            )
        except httpx.HTTPError as exc:
            raise MemoryProviderError(f"qm memory request failed: {exc}") from exc
        if response.status_code >= 400:
            raise MemoryProviderError(
                f"qm memory returned HTTP {response.status_code}: {_safe_error(response)}"
            )
        return response


def _qm_scope_id(scope: MemoryScope) -> str:
    if scope.kind not in {"personal", "org"}:
        raise MemoryProviderError(f"qm notebook does not support {scope.kind} scope")
    return f"{scope.kind}:{scope.key}"


def _canonical_json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True)


def _non_negative_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _response_object(response: httpx.Response) -> dict[str, object]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise MemoryProviderError("qm memory returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise MemoryProviderError("qm memory returned a non-object response")
    return payload


def _safe_error(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return "request failed"
    if isinstance(payload, dict) and isinstance(payload.get("error"), str):
        return payload["error"][:100]
    return "request failed"
