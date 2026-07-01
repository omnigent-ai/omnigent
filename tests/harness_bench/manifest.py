"""The registry of official harness bench profiles — the spreadsheet, as data.

Each entry declares the static matrix columns and the *expected* verdict
per P0 dimension for one official SDK harness. The base fields (model,
env_prefix, marker, cli_binary) are reused from
``tests.e2e._harness_probes.HARNESS_PROBES`` so a harness added to the e2e
parametrize matrix flows into the bench without a second source of truth.

Declared verdicts encode the SDK support matrix. When a live probe
observes something different, :func:`tests.harness_bench.verdict.reconcile`
flags ``DRIFT`` — the whole point of the bench.

Native harnesses and the remaining SDK harnesses (cursor, antigravity,
kimi, qwen, goose, copilot, hermes) are phase-2: they need transport
drivers and profile entries, tracked in the design doc.
"""

from __future__ import annotations

from tests.e2e._harness_probes import HARNESS_PROBES, HarnessProbe
from tests.harness_bench.profile import BenchProfile
from tests.harness_bench.verdict import Verdict

# All P0 SDK harnesses declare the same verdicts: they stream, call tools,
# interrupt, enforce policy verdicts, and accept a model override. Drift
# against this baseline is the signal we care about.
_SUPPORTED = Verdict.SUPPORTED
_P0_ALL_SUPPORTED: dict[str, Verdict] = {
    "basic_turn": _SUPPORTED,
    "streaming": _SUPPORTED,
    "tool_calling": _SUPPORTED,
    "interrupt": _SUPPORTED,
    "policy_deny": _SUPPORTED,
    "model_override": _SUPPORTED,
}


# Static matrix columns per official harness, keyed by harness name. Kept
# beside the declared verdicts so the rendered report reproduces the
# spreadsheet's descriptive columns, not just the ✓/✗ grid.
_STATIC: dict[str, dict[str, str]] = {
    "claude-sdk": {
        "owner": "",
        "auth": "Anthropic key / Databricks gateway",
        "implementation": "SDK in-process",
    },
    "codex": {
        "owner": "",
        "auth": "Databricks gateway / codex auth.json",
        "implementation": "CLI subprocess (app-server RPC)",
    },
    "pi": {
        "owner": "",
        "auth": "Databricks gateway / API keys",
        "implementation": "CLI subprocess (JSONL RPC)",
    },
    "openai-agents": {
        "owner": "",
        "auth": "Databricks gateway / OpenAI key",
        "implementation": "SDK in-process",
    },
}


def _profile_from_probe(probe: HarnessProbe) -> BenchProfile:
    """Build an official :class:`BenchProfile` from an e2e ``HarnessProbe``."""
    static = _STATIC.get(probe.harness, {})
    return BenchProfile(
        harness=probe.harness,
        model=probe.model,
        env_prefix=probe.env_prefix,
        marker=probe.marker,
        cli_binary=probe.cli_binary,
        transport="sdk-inproc",
        owner=static.get("owner", ""),
        auth=static.get("auth", ""),
        implementation=static.get("implementation", ""),
        declared=dict(_P0_ALL_SUPPORTED),
    )


# Official harnesses the bench ships with. Built from HARNESS_PROBES so the
# two matrices never diverge; restricted to the harnesses the sdk-inproc
# driver covers today.
OFFICIAL_PROFILES: dict[str, BenchProfile] = {
    probe.harness: _profile_from_probe(probe)
    for probe in HARNESS_PROBES
    if probe.harness in _STATIC
}


__all__ = ["OFFICIAL_PROFILES"]
