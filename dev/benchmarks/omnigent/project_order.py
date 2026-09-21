"""Project-order latency journeys with fixed, isolated per-user project counts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

from .environment import BenchEnvironment

if TYPE_CHECKING:
    from .journeys import Journey

PROJECT_COUNT = 1000
Operation = Literal["get", "save", "projects", "session_projects"]


@dataclass
class OrderContext:
    headers: dict[str, str]
    alphabetical: list[str]
    custom: list[str]
    expected: list[str]


async def _put(env: BenchEnvironment, ctx: OrderContext, ids: list[str]) -> None:
    response = await env.client.put(
        "/v1/projects/order", headers=ctx.headers, json={"ordered_project_ids": ids}
    )
    response.raise_for_status()
    if response.json() != {
        "ordered_project_ids": ids,
        "sort_mode": "manual",
    }:
        raise RuntimeError("Project-order save returned the wrong order")
    ctx.expected = ids


async def _verify_saved(env: BenchEnvironment, context: object) -> None:
    ctx = cast(OrderContext, context)
    response = await env.client.get("/v1/projects/order", headers=ctx.headers)
    response.raise_for_status()
    if response.json() != {
        "ordered_project_ids": ctx.expected,
        "sort_mode": "manual",
    }:
        raise RuntimeError("Project-order read differs from the saved order")


async def _setup(env: BenchEnvironment) -> OrderContext:
    count = PROJECT_COUNT
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
    ctx = OrderContext(headers, alphabetical, alphabetical[::-1], alphabetical[::-1])
    await _put(env, ctx, ctx.custom)
    await _verify_saved(env, ctx)
    return ctx


async def _save(env: BenchEnvironment, context: object) -> None:
    ctx = cast(OrderContext, context)
    # Change the value every time, including after warmup.
    ids = ctx.alphabetical if ctx.expected == ctx.custom else ctx.custom
    await _put(env, ctx, ids)


async def _list(env: BenchEnvironment, context: object, path: str) -> None:
    ctx = cast(OrderContext, context)
    response = await env.client.get(path, headers=ctx.headers)
    response.raise_for_status()
    projects = response.json()
    if path == "/v1/projects":
        projects = projects["data"]
    if [p["id"] for p in projects] != ctx.expected:
        raise RuntimeError("Project list returned the wrong count or order")


def _journey(operation: Operation) -> Journey:
    from .journeys import Journey

    path = "/v1/sessions/projects" if operation == "session_projects" else "/v1/projects"

    async def list_projects(env: BenchEnvironment, ctx: object) -> None:
        await _list(env, ctx, path)

    if operation == "save":
        return Journey(
            name=f"project_order_save_{PROJECT_COUNT}",
            kind="latency",
            setup=_setup,
            measure=_save,
            validate=_verify_saved,
            description=f"PUT /v1/projects/order — manual save, {PROJECT_COUNT} projects.",
        )
    return Journey(
        name=f"project_order_{operation}_custom_{PROJECT_COUNT}",
        kind="latency",
        setup=_setup,
        measure=_verify_saved if operation == "get" else list_projects,
        concurrency_safe=True,
        description=(
            f"GET {'/v1/projects/order' if operation == 'get' else path}"
            f" — manual order, {PROJECT_COUNT} projects."
        ),
    )


def project_order_journeys() -> list[Journey]:
    """One representative large-project case per endpoint keeps CI reports compact."""
    return [_journey(operation) for operation in ("save", "get", "projects", "session_projects")]
