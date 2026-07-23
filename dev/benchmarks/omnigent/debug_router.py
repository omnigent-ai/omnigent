"""CI-only debug router exposing the server's request counter over HTTP.

The benchmark harness runs the server as a subprocess, so it cannot read the
in-process ``ServerPerformanceMetrics`` counter directly. This router surfaces
that counter on ``GET /debug/server-metrics`` so the harness can diff it around
each journey's timed region and report requests-per-op — including the
cross-process traffic (runner → server callbacks, host → server) that a
client-side hook in the benchmark process can't see.

**This never reaches the production server.** Three independent reasons:

1. The module lives under ``dev/``, which ``pyproject.toml`` excludes from the
   wheel (``include = ["omnigent*"]``), so a production install cannot import it.
2. It is loaded only via the ``debug_router_modules`` config key, which the
   benchmark's generated ``server.yaml`` sets and production config never does.
3. ``create_app`` loads the module tolerantly — an ``ImportError`` logs a
   warning and skips, so even a stray config key is a no-op where ``dev/`` is
   absent.

``create_app`` reads the module-level ``DEBUG_ROUTERS`` list (mirroring the
``POLICY_REGISTRY`` convention) and mounts each ``(router, prefix, tags)`` entry.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/server-metrics")
async def server_metrics(request: Request) -> dict[str, object]:
    """Return the server's cumulative HTTP request counters.

    Reads the process-local :class:`ServerPerformanceMetrics` tracker stashed on
    ``app.state.server_metrics``. The counters are monotonic since process
    start; the harness diffs two reads to get a journey's request volume.

    ``route_counts`` additionally breaks the total down by low-cardinality
    route template (``"METHOD /v1/sessions/{session_id}"`` → count), letting the
    harness attribute a journey's requests to specific endpoints — including the
    cross-process runner → server / host → server calls a client-side hook can't
    see.

    :param request: Incoming request, used to reach ``app.state``.
    :returns: ``total_started`` / ``total_completed`` / ``total_failed`` /
        ``in_flight`` plus a ``route_counts`` map from the current metrics.
    """
    metrics = request.app.state.server_metrics
    snapshot = metrics.snapshot()
    return {
        "total_started": snapshot.total_started,
        "total_completed": snapshot.total_completed,
        "total_failed": snapshot.total_failed,
        "in_flight": snapshot.in_flight,
        "route_counts": metrics.route_counts(),
    }


# Consumed by ``create_app(debug_router_modules=...)``: each entry is mounted as
# ``app.include_router(router, prefix=prefix, tags=tags)``.
DEBUG_ROUTERS: list[tuple[APIRouter, str, list[str]]] = [(router, "/debug", ["debug"])]
