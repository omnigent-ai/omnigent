"""Policy handler the benchmark's ``policy_evaluate`` journey declares.

Uploaded agent bundles may only reference registered policy handlers, so the
benchmark server loads this module through the ``policy_modules`` config key,
which the generated ``server.yaml`` sets and production config never does. Like
``debug_router``, it lives under ``dev/``, which the wheel excludes.
"""

from __future__ import annotations

POLICY_REGISTRY: list[dict[str, object]] = [
    {
        "handler": "dev.benchmarks.omnigent.bench_policies.allow_all",
        "kind": "callable",
        "description": "Benchmark policy: always allows",
    },
]


def allow_all(_event: dict) -> dict:  # type: ignore[type-arg]
    """Always ALLOW, so the measured cost is the policy engine itself."""
    return {"result": "allow"}
