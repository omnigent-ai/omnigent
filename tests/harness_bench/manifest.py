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

from omnigent.harness_aliases import is_native_harness
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

    # model_override is backed by the registry, not a capability field. An
    # SDK harness takes it via a HARNESS_<H>_MODEL env key (model_env_keys);
    # a native harness takes it as a launch --model argv element (see
    # omnigent/model_override.py). Either path means the harness accepts a
    # caller-specified model.
    if harness in model_env_keys() or is_native_harness(harness):
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


# ── native-tui harnesses ─────────────────────────────────────────
#
# Native harnesses are not in HARNESS_PROBES (that matrix is the SDK-wrap
# e2e set), so their profiles are built directly here.
#
# Only claude-native ships as official today. It is an OMNIGENT_CREDENTIAL
# vendor (the bench can mint its gateway credential) AND its turns surface on
# the shared session HTTP stream the driver reads (tmux-paste delivery), so
# the native-tui driver can actually observe it — live-verified end to end.
#
# codex-native is deliberately NOT shipped yet: it is also OMNIGENT_CREDENTIAL,
# but it delivers output via app-server RPC rather than tmux paste, so a turn
# runs (in_progress → completed) without ever emitting text deltas or
# persisting an assistant item on the session stream — the shared driver
# cannot observe it. Its vendor entry stays in the driver's _VENDORS so a
# `--harness codex-native --transport native-tui` opt-in resolves and
# skip-gates cleanly, but wiring RPC-delivery observation is a follow-up
# before it earns an official profile. OWN_AUTH natives (cursor-native,
# kiro-native, ...) need a vendor login the bench cannot provision, and are
# likewise left to a --harness <ref> opt-in.
#
# model: native harnesses take the model as a launch --model, not a
# HARNESS_<H>_MODEL env var, so they are absent from model_env_keys() and
# their model_override declares UNKNOWN (honest — the probe confirms it live
# once native model-override observation is wired).
_NATIVE_PROFILES: dict[str, tuple[str, str]] = {
    # harness: (model, marker)
    "claude-native": ("databricks-claude-sonnet-4-6", "CLAUDE_NATIVE_OK"),
}


def _native_profile(harness: str, model: str, marker: str) -> BenchProfile:
    """Build a native-tui :class:`BenchProfile`, columns/verdicts from capabilities."""
    caps = harness_capabilities().get(harness)
    # The vendor CLI the driver skip-gates on (claude-native -> "claude").
    cli_binary = harness.removesuffix("-native")
    env_prefix = "HARNESS_" + harness.upper().replace("-", "_") + "_"
    return BenchProfile(
        harness=harness,
        model=model,
        env_prefix=env_prefix,
        marker=marker,
        cli_binary=cli_binary,
        transport="native-tui",
        owner="",
        auth=_auth_prose(caps),
        implementation=_implementation_prose(caps),
        declared=_declared_from_capabilities(harness),
    )


for _h, (_model, _marker) in _NATIVE_PROFILES.items():
    OFFICIAL_PROFILES[_h] = _native_profile(_h, _model, _marker)


__all__ = ["OFFICIAL_PROFILES"]
