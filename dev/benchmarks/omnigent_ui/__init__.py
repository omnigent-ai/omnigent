"""Omnigent end-to-end UI performance benchmark.

Stands up a real server + runner against a zero-latency mock LLM, builds and
serves the web SPA, opens a Playwright Chromium browser, and drives real user
journeys through the rendered UI — measuring per-journey latency and the number
of network requests each journey issues (grouped by resource type and by
method+path). Emits the same versioned JSON report as the HTTP benchmark
(``dev/benchmarks/omnigent``), with an added ``network`` block per journey.

See ``README.md`` for the workflow and the report contract.
"""
