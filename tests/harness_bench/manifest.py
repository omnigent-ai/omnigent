"""The registry of official harness bench profiles.

Each profile's descriptive columns and *declared* verdicts derive from the
canonical capability model (:func:`omnigent.harness_plugins.harness_capabilities`),
so there is a single source of truth for "what each harness supports". The
base fields (model, env_prefix, marker, cli_binary) are reused from
``tests.e2e._harness_probes.HARNESS_PROBES`` — a harness added to the e2e
parametrize matrix flows into the bench without a second copy.

The declared matrix is the harness's *published capability*; the bench's
probes measure live behavior. When they disagree,
:func:`tests.harness_bench.verdict.reconcile` flags ``DRIFT`` — which means a
harness's capability declaration is false. That makes the capability table
self-enforcing.

Axis mapping (see ``designs/harness-capabilities-bench-seam.md``):

- **Group A — descriptive columns** derive from capabilities:
  ``implementation`` from ``integration_mode``, ``auth`` from ``auth``.
- **Group B — declared verdicts** derive where a capability backs the probe:
  ``interrupt`` from ``capabilities.interrupt``, ``streaming`` from
  ``capabilities.streaming``, ``model_override`` from membership in
  ``model_env_keys()`` (the SDK model-override registry).
- **Group C — probe-only** dimensions have no backing capability axis and
  stay explicit: ``basic_turn`` (every harness completes a turn),
  ``tool_calling`` (not a modeled axis), and ``policy_deny`` (enforcement,
  distinct from the elicitation ASK surface — deliberately NOT derived from
  ``elicitation``).

Non-P0 harnesses' ``interrupt``/``streaming`` are declared best-effort by
integration mode and not yet probe-verified; the bench's live probes confirm
or correct them as transport coverage lands.
"""

from __future__ import annotations

from omnigent.harness_capabilities import AuthModel, HarnessCapabilities, IntegrationMode
from omnigent.harness_plugins import harness_capabilities, model_env_keys
from tests.e2e._harness_probes import HARNESS_PROBES, HarnessProbe
from tests.harness_bench.profile import BenchProfile
from tests.harness_bench.verdict import Verdict

# ── Group A: enum → prose for the descriptive columns ────────────

_INTEGRATION_MODE_PROSE: dict[IntegrationMode, str] = {
    IntegrationMode.SDK_IN_PROCESS: "SDK in-process",
    IntegrationMode.CLI_SUBPROCESS: "CLI subprocess",
    IntegrationMode.ACP_SUBPROCESS: "ACP subprocess",
    IntegrationMode.NATIVE_TUI: "Native TUI",
    IntegrationMode.NATIVE_SERVER: "Native server",
}

_AUTH_PROSE: dict[AuthModel, str] = {
    AuthModel.OMNIGENT_CREDENTIAL: "Omnigent credential (gateway / provider config)",
    AuthModel.OWN_AUTH: "Own auth (vendor login / API key)",
    AuthModel.SESSION_SCOPED_CONFIG: "Session-scoped vendor config",
}


# ── Group C: probe-only dimensions with no backing capability ────
#
# These stay explicitly SUPPORTED for the official (P0) harnesses: every one
# completes a turn, calls tools, and enforces a policy DENY. They are NOT
# derived from any capability axis (see the module docstring / seam brief).
_PROBE_ONLY_DECLARED: dict[str, Verdict] = {
    "basic_turn": Verdict.SUPPORTED,
    "tool_calling": Verdict.SUPPORTED,
    "policy_deny": Verdict.SUPPORTED,
}


def _implementation_prose(caps: HarnessCapabilities | None) -> str:
    """Group A: the ``implementation`` column from ``integration_mode``."""
    if caps is None:
        return ""
    return _INTEGRATION_MODE_PROSE.get(caps.integration_mode, caps.integration_mode.value)


def _auth_prose(caps: HarnessCapabilities | None) -> str:
    """Group A: the ``auth`` column from ``auth``."""
    if caps is None:
        return ""
    return _AUTH_PROSE.get(caps.auth, caps.auth.value)


def _declared_from_capabilities(harness: str) -> dict[str, Verdict]:
    """Build a harness's declared verdicts from the capability model.

    Group B (capability-backed) plus group C (probe-only, explicit).
    Tolerant of a harness with no declared capabilities (a sparse
    ``harness_capabilities()`` — e.g. a community plugin): the
    capability-backed dimensions are simply omitted (left ``UNKNOWN`` by
    :meth:`BenchProfile.declared_for`) rather than raising.

    :param harness: Harness id, e.g. ``"codex"``.
    :returns: A ``{dimension: Verdict}`` map for this harness.
    """
    declared: dict[str, Verdict] = dict(_PROBE_ONLY_DECLARED)

    caps = harness_capabilities().get(harness)
    if caps is not None:
        # streaming: True → deltas (SUPPORTED); False → complete-only (PARTIAL).
        declared["streaming"] = Verdict.SUPPORTED if caps.streaming else Verdict.PARTIAL
        # interrupt: True → SUPPORTED; False → UNSUPPORTED.
        declared["interrupt"] = Verdict.SUPPORTED if caps.interrupt else Verdict.UNSUPPORTED

    # model_override is backed by the model-env-key registry (the SDK
    # model-override set), not a capability field: a harness with a
    # HARNESS_<H>_MODEL env key accepts a caller-specified model.
    if harness in model_env_keys():
        declared["model_override"] = Verdict.SUPPORTED

    return declared


def _profile_from_probe(probe: HarnessProbe) -> BenchProfile:
    """Build an official :class:`BenchProfile` from an e2e ``HarnessProbe``.

    Descriptive columns and declared verdicts derive from the capability
    model; only the transport and the e2e base fields are bench-local.
    """
    caps = harness_capabilities().get(probe.harness)
    return BenchProfile(
        harness=probe.harness,
        model=probe.model,
        env_prefix=probe.env_prefix,
        marker=probe.marker,
        cli_binary=probe.cli_binary,
        transport="sdk-inproc",
        owner="",
        auth=_auth_prose(caps),
        implementation=_implementation_prose(caps),
        declared=_declared_from_capabilities(probe.harness),
    )


# Official harnesses the bench ships with: the P0 SDK harnesses the
# sdk-inproc driver covers today. Built from HARNESS_PROBES so the e2e and
# bench matrices never diverge.
_OFFICIAL_HARNESSES = frozenset({"claude-sdk", "codex", "pi", "openai-agents"})

OFFICIAL_PROFILES: dict[str, BenchProfile] = {
    probe.harness: _profile_from_probe(probe)
    for probe in HARNESS_PROBES
    if probe.harness in _OFFICIAL_HARNESSES
}


__all__ = ["OFFICIAL_PROFILES"]
