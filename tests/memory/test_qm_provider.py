from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest

from omnigent.memory import (
    MemoryCaptureRequest,
    MemoryEraseRequest,
    MemoryProviderError,
    MemoryRecallRequest,
    MemoryScope,
    MemoryTurnContext,
)
from omnigent.memory.providers import QmMemoryProvider

SECRET = "q" * 32


def _request(*scopes: MemoryScope) -> MemoryRecallRequest:
    return MemoryRecallRequest(
        context=MemoryTurnContext(
            operation_id="turn-123",
            workspace_id=7,
            account_subject="user/123",
            conversation_id="conv-1",
            query="What do I prefer?",
        ),
        scopes=scopes,
        max_results=8,
        max_chars=4000,
    )


@pytest.mark.asyncio
async def test_qm_provider_signs_canonical_request_and_normalizes_response() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "operationId": body["operationId"],
                "scopeId": body["scopeId"],
                "content": "# Memory\n\n- Prefers terse replies\n",
                "revision": "4",
                "updatedAt": 1_799_999_999_000,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = QmMemoryProvider(
            "https://memory.internal",
            SECRET,
            client=client,
            now=lambda: 1_800_000_000,
        )
        results = await provider.recall(_request(MemoryScope(7, "personal", "user/123")))

    assert len(results) == 1
    assert results[0].provider == "qm-notebook"
    assert results[0].scope.key == "7:user:user%2F123"
    assert results[0].record_id == "personal:7:user:user%2F123"
    assert results[0].version == "4"
    assert results[0].sensitivity == "personal"

    request = captured[0]
    body = request.content.decode()
    canonical = f"POST\n/v1/memory/read\n{body}"
    expected = hmac.new(
        SECRET.encode(),
        f"v0:1800000000:{canonical}".encode(),
        hashlib.sha256,
    ).hexdigest()
    assert request.headers["x-signature"] == f"v0={expected}"
    assert request.headers["x-timestamp"] == "1800000000"
    assert json.loads(body) == {
        "operationId": "turn-123:qm-read:0",
        "scopeId": "personal:7:user:user%2F123",
    }


@pytest.mark.asyncio
async def test_qm_provider_skips_empty_notebook() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "operationId": body["operationId"],
                "scopeId": body["scopeId"],
                "content": "",
                "revision": "0",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = QmMemoryProvider("https://memory.internal", SECRET, client=client)
        results = await provider.recall(_request(MemoryScope(7, "personal", "user-1")))

    assert results == []


@pytest.mark.asyncio
async def test_qm_provider_captures_with_stable_operation_and_receipt() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "operationId": body["operationId"],
                "scopeId": body["scopeId"],
                "added": 1,
                "revision": "5",
                "updatedAt": 1_800_000_001_000,
            },
        )

    scope = MemoryScope(7, "personal", "user/123")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = QmMemoryProvider(
            "https://memory.internal",
            SECRET,
            client=client,
            now=lambda: 1_800_000_000,
        )
        receipt = await provider.capture(
            MemoryCaptureRequest(
                operation_id="memory-capture:job-1",
                scope=scope,
                facts=("Prefers concise answers",),
                captured_at=1_800_000_000_000,
            )
        )

    assert receipt.operation_id == "memory-capture:job-1"
    assert receipt.scope == scope
    assert receipt.added == 1
    assert receipt.revision == "5"
    request_body = json.loads(captured[0].content)
    assert request_body == {
        "capturedAt": 1_800_000_000_000,
        "facts": ["Prefers concise answers"],
        "operationId": "memory-capture:job-1",
        "scopeId": "personal:7:user:user%2F123",
    }


@pytest.mark.asyncio
async def test_qm_provider_erases_then_verifies_absence() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        body = json.loads(request.content)
        if request.url.path == "/v1/memory/erase":
            return httpx.Response(
                200,
                json={
                    "operationId": body["operationId"],
                    "scopeHash": "a" * 64,
                    "erasedRevisions": 3,
                    "tombstonedOperations": 2,
                    "completedAt": 1_800_000_001_000,
                },
            )
        return httpx.Response(
            200,
            json={
                "operationId": body["operationId"],
                "scopeId": body["scopeId"],
                "content": "",
                "revision": "0",
            },
        )

    scope = MemoryScope(7, "personal", "user/123")
    request = MemoryEraseRequest(
        operation_id="memory-erasure:job-1",
        scope=scope,
        erased_at=1_800_000_001_000,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = QmMemoryProvider(
            "https://memory.internal",
            SECRET,
            client=client,
            now=lambda: 1_800_000_000,
        )
        receipt = await provider.erase(request)
        verified = await provider.verify_erased(request)

    assert receipt.operation_id == request.operation_id
    assert receipt.scope_hash == "a" * 64
    assert receipt.erased_revisions == 3
    assert receipt.tombstoned_operations == 2
    assert verified is True
    assert [http_request.url.path for http_request in captured] == [
        "/v1/memory/erase",
        "/v1/memory/read",
    ]
    assert json.loads(captured[0].content) == {
        "erasedAt": 1_800_000_001_000,
        "operationId": "memory-erasure:job-1",
        "scopeId": "personal:7:user:user%2F123",
    }
    assert json.loads(captured[1].content)["operationId"] == ("memory-erasure:job-1:verify")


@pytest.mark.asyncio
async def test_qm_provider_rejects_invalid_erasure_receipt() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "operationId": "memory-erasure:job-1",
                "scopeHash": "not-a-keyed-token",
                "erasedRevisions": 1,
                "tombstonedOperations": 1,
                "completedAt": 1,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = QmMemoryProvider("https://memory.internal", SECRET, client=client)
        with pytest.raises(MemoryProviderError, match="missing its receipt"):
            await provider.erase(
                MemoryEraseRequest(
                    operation_id="memory-erasure:job-1",
                    scope=MemoryScope(7, "personal", "user/123"),
                    erased_at=1,
                )
            )


@pytest.mark.asyncio
async def test_qm_provider_rejects_mismatched_scope_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "operationId": body["operationId"],
                "scopeId": "personal:8:user:other",
                "content": "Cross-tenant fact",
                "revision": "1",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = QmMemoryProvider("https://memory.internal", SECRET, client=client)
        with pytest.raises(MemoryProviderError, match="correlation did not match"):
            await provider.recall(_request(MemoryScope(7, "personal", "user-1")))


@pytest.mark.asyncio
async def test_qm_provider_normalizes_http_errors_without_reflecting_detail() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "forbidden", "message": "secret detail"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = QmMemoryProvider("https://memory.internal", SECRET, client=client)
        with pytest.raises(MemoryProviderError, match="HTTP 403: forbidden") as error:
            await provider.recall(_request(MemoryScope(7, "personal", "user-1")))

    assert "secret detail" not in str(error.value)


@pytest.mark.asyncio
async def test_qm_provider_rejects_conversation_scope_without_making_request() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = QmMemoryProvider("https://memory.internal", SECRET, client=client)
        with pytest.raises(MemoryProviderError, match="does not support conversation"):
            await provider.recall(_request(MemoryScope(7, "conversation", "conv-1")))

    assert calls == 0


@pytest.mark.parametrize(
    "url",
    ["http://memory.internal", "memory.internal", "https://user:pass@memory.internal"],
)
def test_qm_provider_rejects_unsafe_urls(url: str) -> None:
    with pytest.raises(ValueError, match="must"):
        QmMemoryProvider(url, SECRET)
