"""Preference HTTP and durable-client journeys using the existing benchmark runner."""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast
from uuid import uuid4

import httpx

from omnigent.preference_client import PreferenceClient, read_preference_snapshot
from omnigent.preference_sync import (
    PreferenceMutation,
    PreferenceResult,
    PreferenceSnapshot,
    SharedPreferenceKey,
)

from .environment import BenchEnvironment

if TYPE_CHECKING:
    from .journeys import Journey

_PATH = "/v1/preferences/shared"
_MODEL = SharedPreferenceKey(kind="model", name="model")


def _plugin(index: int) -> SharedPreferenceKey:
    return SharedPreferenceKey(
        kind="plugin",
        name={"ecosystem": "claude", "source": "benchmark", "name": f"plugin-{index}"},
    )


async def _read(http: httpx.AsyncClient) -> PreferenceSnapshot:
    return await read_preference_snapshot(http)


async def _post(http: httpx.AsyncClient, operation: PreferenceMutation) -> PreferenceResult:
    response = await http.post(_PATH + "/operations", json=operation.model_dump(mode="json"))
    response.raise_for_status()
    return PreferenceResult.model_validate_json(response.content)


@dataclass
class _Worker:
    http: httpx.AsyncClient
    local: PreferenceClient
    snapshot: PreferenceSnapshot
    receipt: PreferenceResult
    original: PreferenceMutation
    key: SharedPreferenceKey
    enabled: bool = False


@dataclass
class _Context:
    directory: tempfile.TemporaryDirectory = field(
        default_factory=lambda: tempfile.TemporaryDirectory(prefix="preference-bench-")
    )
    workers: list[_Worker] = field(default_factory=list)
    pool: asyncio.Queue[_Worker] = field(default_factory=asyncio.Queue)


async def _teardown(_env: BenchEnvironment, context: object) -> None:
    ctx = cast(_Context, context)
    for worker in ctx.workers:
        await worker.http.aclose()
    ctx.directory.cleanup()


async def _setup(
    env: BenchEnvironment, *, clients: int = 1, separate_users: bool = False
) -> _Context:
    ctx = _Context()
    run_id = uuid4().hex
    if env.preference_keys <= clients:
        raise ValueError("Concurrent sync needs at least preference_clients + 1 keys")
    try:
        for index in range(clients):
            user = f"benchmark-preferences-{run_id}-{index if separate_users else 0}"
            http = httpx.AsyncClient(
                base_url=env.base_url,
                headers={"X-Forwarded-Email": user},
                timeout=30,
                trust_env=False,
                event_hooks=env.client.event_hooks,
            )
            try:
                snapshot = await _read(http)
                original = PreferenceMutation(
                    protocol_version=1,
                    identity=snapshot.identity,
                    operation_id=uuid4(),
                    installation_id="seed",
                    key=_MODEL,
                    expected_version=0,
                    value="benchmark-model",
                )
                if index == 0 or separate_users:
                    receipt = await _post(http, original)
                    for key_index in range(env.preference_keys - 1):
                        operation = original.model_copy(
                            update={
                                "operation_id": uuid4(),
                                "key": _plugin(key_index),
                                "value": False,
                            }
                        )
                        if (await _post(http, operation)).status != "applied":
                            raise RuntimeError("Preference seeding did not apply")
                    # Consume real operations so history has the production receipt format.
                    for _ in range(env.preference_history):
                        operation = original.model_copy(
                            update={"operation_id": uuid4(), "expected_version": 1}
                        )
                        if (await _post(http, operation)).status != "unchanged":
                            raise RuntimeError("Receipt seeding changed the value")
                else:
                    original, receipt = ctx.workers[0].original, ctx.workers[0].receipt
                local = PreferenceClient(
                    Path(ctx.directory.name) / f"{index}.db",
                    identity=snapshot.identity,
                    installation_id=f"client-{index}",
                )
                snapshot = await local.sync(http)
                if len(snapshot.entries) != env.preference_keys or snapshot.conflicts:
                    raise RuntimeError("Preference seed has the wrong key count or conflicts")
                worker = _Worker(http, local, snapshot, receipt, original, _plugin(index))
                ctx.workers.append(worker)
                ctx.pool.put_nowait(worker)
            except BaseException:
                await http.aclose()
                raise
        return ctx
    except BaseException:
        await _teardown(env, ctx)
        raise


async def _read_snapshot(_env: BenchEnvironment, context: object) -> None:
    worker = cast(_Context, context).workers[0]
    if await _read(worker.http) != worker.snapshot:
        raise RuntimeError("Preference snapshot differs from saved state")


async def _retry(_env: BenchEnvironment, context: object) -> None:
    worker = cast(_Context, context).workers[0]
    if await _post(worker.http, worker.original) != worker.receipt:
        raise RuntimeError("Preference retry changed its original receipt")


async def _write(_env: BenchEnvironment, context: object) -> None:
    worker = cast(_Context, context).workers[0]
    operation = worker.original.model_copy(
        update={
            "operation_id": uuid4(),
            "expected_version": worker.receipt.entry.version,
            "value": f"model-{uuid4().hex}",
        }
    )
    result = await _post(worker.http, operation)
    if (
        result.status != "applied"
        or result.entry.value != operation.value
        or result.operation_id != operation.operation_id
        or result.entry.key != operation.key
    ):
        raise RuntimeError("Preference edit was not applied")
    worker.receipt = result


async def _verify_write(_env: BenchEnvironment, context: object) -> None:
    worker = cast(_Context, context).workers[0]
    snapshot = await _read(worker.http)
    if worker.receipt.entry not in snapshot.entries or snapshot.conflicts:
        raise RuntimeError("Preference edit was not persisted")


async def _refresh(_env: BenchEnvironment, context: object) -> None:
    worker = cast(_Context, context).workers[0]
    if (
        await worker.local.sync(worker.http, max_operations=0) != worker.snapshot
        or worker.local.cached() != worker.snapshot
    ):
        raise RuntimeError("Client refresh lost saved preferences")


async def _sync(_env: BenchEnvironment, context: object) -> None:
    ctx = cast(_Context, context)
    worker = await ctx.pool.get()
    try:
        worker.enabled = not worker.enabled
        await asyncio.to_thread(worker.local.enqueue, worker.key, worker.enabled)
        snapshot = await worker.local.sync(worker.http)
        current = next(entry for entry in snapshot.entries if entry.key == worker.key)
        if current.value != worker.enabled or snapshot.conflicts or worker.local.pending():
            raise RuntimeError("Durable client did not converge after sync")
    finally:
        ctx.pool.put_nowait(worker)


def preference_journeys() -> list[Journey]:
    from .journeys import Journey

    async def same_user(env: BenchEnvironment) -> _Context:
        return await _setup(env, clients=env.preference_clients)

    async def many_users(env: BenchEnvironment) -> _Context:
        return await _setup(env, clients=env.preference_clients, separate_users=True)

    specifications = [
        ("read", _read_snapshot, _setup, None, True),
        ("retry", _retry, _setup, None, True),
        ("write", _write, _setup, _verify_write, False),
        ("refresh", _refresh, _setup, None, False),
        ("sync_same_user", _sync, same_user, None, True),
        ("sync_many_users", _sync, many_users, None, True),
    ]
    return [
        Journey(
            name=f"shared_preferences_{name}",
            kind="latency",
            measure=measure,
            setup=setup,
            validate=validate,
            teardown=_teardown,
            concurrency_safe=concurrent,
            needs_shared_preferences=True,
            description=f"Shared preference {name}; real HTTP/SQL and client disk persistence.",
        )
        for name, measure, setup, validate, concurrent in specifications
    ]
