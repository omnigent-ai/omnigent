"""Project-order latency journeys with fixed, isolated per-user project counts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

from .environment import BenchEnvironment

if TYPE_CHECKING:
    from .journeys import Journey

PROJECT_COUNTS = (10, 100, 1000)
Operation = Literal["get", "save", "reset", "projects", "session_projects"]


@dataclass
class OrderContext:
    headers: dict[str, str]
    alphabetical: list[str]
    custom: list[str]
    expected: list[str] | None
    remembered: list[str] | None = None


async def _put(env: BenchEnvironment, ctx: OrderContext, ids: list[str] | None) -> None:
    response = await env.client.put(
        "/v1/projects/order", headers=ctx.headers, json={"ordered_project_ids": ids}
    )
    response.raise_for_status()
    remembered = ids if ids is not None else ctx.remembered
    if response.json() != {
        "ordered_project_ids": remembered,
        "sort_mode": "alphabetical" if ids is None else "manual",
    }:
        raise RuntimeError("Project-order save returned the wrong order")
    ctx.expected = ids
    ctx.remembered = remembered


async def _verify_saved(env: BenchEnvironment, context: object) -> None:
    ctx = cast(OrderContext, context)
    response = await env.client.get("/v1/projects/order", headers=ctx.headers)
    response.raise_for_status()
    if response.json() != {
        "ordered_project_ids": ctx.remembered,
        "sort_mode": "alphabetical" if ctx.expected is None else "manual",
    }:
        raise RuntimeError("Project-order read differs from the saved order")


async def _setup(env: BenchEnvironment, count: int, custom: bool) -> OrderContext:
    # These benchmark-owned accounts keep corpus sessions and local preferences intact.
    headers = {"X-Forwarded-Email": f"benchmark-project-order-{count}"}
    response = await env.client.get("/v1/projects", headers=headers)
    response.raise_for_status()
    projects = {p["name"]: p["id"] for p in response.json()["data"]}
    names = [f"Benchmark project {i:04d}" for i in range(count)]
    if set(projects) - set(names):
        raise RuntimeError("Unexpected projects in the project-order benchmark account")
    for name in names:
        if name not in projects:
            response = await env.client.post("/v1/projects", headers=headers, json={"name": name})
            response.raise_for_status()
            projects[name] = response.json()["id"]
    alphabetical = [projects[name] for name in names]
    ctx = OrderContext(headers, alphabetical, alphabetical[::-1], None)
    response = await env.client.get("/v1/projects/order", headers=headers)
    response.raise_for_status()
    ctx.remembered = response.json()["ordered_project_ids"]
    await _put(env, ctx, ctx.custom if custom else None)
    await _verify_saved(env, ctx)
    return ctx


async def _save(env: BenchEnvironment, context: object) -> None:
    ctx = cast(OrderContext, context)
    # Change the value every time, including after warmup.
    ids = ctx.alphabetical if ctx.expected == ctx.custom else ctx.custom
    await _put(env, ctx, ids)


async def _prepare_reset(env: BenchEnvironment, context: object) -> None:
    ctx = cast(OrderContext, context)
    await _put(env, ctx, ctx.custom)
    await _verify_saved(env, ctx)


async def _reset(env: BenchEnvironment, context: object) -> None:
    await _put(env, cast(OrderContext, context), None)


async def _list(env: BenchEnvironment, context: object, path: str) -> None:
    ctx = cast(OrderContext, context)
    response = await env.client.get(path, headers=ctx.headers)
    response.raise_for_status()
    projects = response.json()
    if path == "/v1/projects":
        projects = projects["data"]
    expected = ctx.alphabetical if ctx.expected is None else ctx.expected
    if [p["id"] for p in projects] != expected:
        raise RuntimeError("Project list returned the wrong count or order")


def _journey(count: int, operation: Operation, custom: bool = True) -> Journey:
    from .journeys import Journey

    async def setup(env: BenchEnvironment) -> object:
        return await _setup(env, count, custom)

    path = "/v1/sessions/projects" if operation == "session_projects" else "/v1/projects"

    async def list_projects(env: BenchEnvironment, ctx: object) -> None:
        await _list(env, ctx, path)

    mode = "custom" if custom else "alphabetical"
    if operation in ("save", "reset"):
        return Journey(
            name=f"project_order_{operation}_{count}",
            kind="latency",
            setup=setup,
            prepare=_prepare_reset if operation == "reset" else None,
            measure=_save if operation == "save" else _reset,
            validate=_verify_saved,
            description=f"PUT /v1/projects/order — {operation}, {count} projects.",
        )
    return Journey(
        name=f"project_order_{operation}_{mode}_{count}",
        kind="latency",
        setup=setup,
        measure=_verify_saved if operation == "get" else list_projects,
        concurrency_safe=True,
        description=(
            f"GET {'/v1/projects/order' if operation == 'get' else path}"
            f" — {mode}, {count} projects."
        ),
    )


def project_order_journeys() -> list[Journey]:
    """Stable report keys let nightly baselines track each size and mode separately."""
    journeys = []
    for count in PROJECT_COUNTS:
        for operation in ("save", "reset"):
            journeys.append(_journey(count, operation))
        for operation in ("get", "projects", "session_projects"):
            for custom in (False, True):
                journeys.append(_journey(count, operation, custom))
    return journeys
