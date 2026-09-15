"""Check that order benchmarks reject fast but incorrect endpoint behavior."""

from itertools import pairwise
from types import SimpleNamespace
from typing import cast

import httpx
import pytest

from dev.benchmarks.omnigent.environment import BenchEnvironment
from dev.benchmarks.omnigent.journeys import ALL_JOURNEYS, run_latency
from dev.benchmarks.omnigent.project_order import OrderContext, _list


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["save", "reset"])
@pytest.mark.parametrize("persist", [True, False])
@pytest.mark.parametrize("iterations,warmup", [(2, 1), (1, 0)])
async def test_writes_require_persistence_and_real_changes(
    operation: str, persist: bool, iterations: int, warmup: int
) -> None:
    import json

    saved = None
    mode = "alphabetical"
    writes: list[list[str] | None] = []

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal saved, mode
        if request.url.path == "/v1/projects":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": str(i), "name": f"Benchmark project {i:04d}"} for i in range(10)
                    ]
                },
            )
        if request.method == "PUT":
            ids = json.loads(request.content)["ordered_project_ids"]
            writes.append(ids)
            # Let setup/preconditioning succeed; break only the timed writes.
            if persist or ids == [str(i) for i in reversed(range(10))]:
                if ids is not None:
                    saved = ids
                mode = "alphabetical" if ids is None else "manual"
            return httpx.Response(
                200,
                json={
                    "ordered_project_ids": saved if ids is None else ids,
                    "sort_mode": "alphabetical" if ids is None else "manual",
                },
            )
        return httpx.Response(200, json={"ordered_project_ids": saved, "sort_mode": mode})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://benchmark"
    ) as client:
        env = cast(BenchEnvironment, SimpleNamespace(client=client))
        result = await run_latency(
            ALL_JOURNEYS[f"project_order_{operation}_10"],
            env,
            iterations=iterations,
            warmup=warmup,
        )
    if persist:
        assert result.n_failures == 0
        assert result.n_success == iterations
        if operation == "save":
            assert all(a != b for a, b in pairwise(writes))
        else:
            # Setup plus one precondition/reset pair for each warmup and sample.
            count = iterations + warmup
            assert len(writes) == 1 + 2 * count
            assert writes[2::2] == [None] * count
            assert all(ids is not None for ids in writes[1::2])
    else:
        assert result.n_failures > 0
        assert any("saved order" in reason for reason in result.failures)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/v1/projects", "/v1/sessions/projects"])
@pytest.mark.parametrize("ids", [[], ["a", "b"], ["b", "a", "c"]])
async def test_list_benchmarks_reject_wrong_count_or_order(path: str, ids: list[str]) -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        rows = [{"id": value} for value in ids]
        return httpx.Response(200, json={"data": rows} if path == "/v1/projects" else rows)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://benchmark"
    ) as client:
        env = cast(BenchEnvironment, SimpleNamespace(client=client))
        ctx = OrderContext({}, ["a", "b"], ["b", "a"], ["b", "a"])
        with pytest.raises(RuntimeError, match="wrong count or order"):
            await _list(env, ctx, path)


@pytest.mark.asyncio
async def test_validation_is_excluded_from_latency(monkeypatch: pytest.MonkeyPatch) -> None:
    from dev.benchmarks.omnigent import journeys

    elapsed = 0.0

    async def measure(_env: BenchEnvironment, _ctx: object) -> None:
        nonlocal elapsed
        elapsed += 0.01

    async def validate(_env: BenchEnvironment, _ctx: object) -> None:
        nonlocal elapsed
        elapsed += 1

    monkeypatch.setattr(journeys, "time", SimpleNamespace(perf_counter=lambda: elapsed))
    result = await run_latency(
        journeys.Journey(name="validation", kind="latency", measure=measure, validate=validate),
        cast(BenchEnvironment, object()),
        iterations=1,
        warmup=0,
    )
    assert result.latencies_ms == pytest.approx([10])
    assert result.wall_time == pytest.approx(1.01)
