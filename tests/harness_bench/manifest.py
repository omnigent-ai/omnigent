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
from omnigent.harness_plugins import (
    harness_aliases,
    harness_capabilities,
    harness_install_keys,
    harness_modules,
    install_specs,
    model_env_keys,
)
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
#
# tool_calling is now live-probed on BOTH transports: SDK harnesses via
# full-server (server-dispatched builtin) and native harnesses via native-tui
# (observing the vendor's own function_call item). policy_deny is live on
# full-server (spec-baked deny) and wired on native-tui (a session-attached CEL
# deny + the response.policy_denied stream signal), though native enforcement is
# a follow-up. An environment gap (fail-open policy, no tool-provocation mapping,
# CEL unavailable) reports SKIPPED, so SUPPORTED here only ever drifts on a
# genuine capability gap.
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
        # streaming is binary: True → SUPPORTED, False → UNSUPPORTED. PARTIAL is
        # a probe observation (coalesced single delta), never a declared value —
        # declaring False as PARTIAL would drift against a harness the probe
        # reports UNSUPPORTED (0 deltas).
        declared["streaming"] = Verdict.SUPPORTED if caps.streaming else Verdict.UNSUPPORTED
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
# e2e set), so their profiles are derived here directly from the capability
# model: every harness with integration_mode == NATIVE_TUI is registered, so
# the shipped natives and any community-plugin native (harness_capabilities()
# discovers plugins via entry points) are probeable by name with no bench edit.
#
# What the bench can actually *run* is a separate axis from what it registers.
# OMNIGENT_CREDENTIAL natives (claude, codex) route through the run's Databricks
# profile, so the bench runs them unattended. OWN_AUTH / session-scoped natives
# need a vendor login the bench cannot provision; they are still registered
# (visible, resolvable, honest declared matrix) but skip-gate at the driver's
# unavailable() on a host without that login.
#
# model: an OMNIGENT_CREDENTIAL native routes its launch --model through the
# gateway, so it takes a databricks-* model; an own-auth native's model lives
# in the vendor's namespace the bench does not control, and is unused in
# practice (the harness skip-gates before a turn). model_override still
# declares UNKNOWN for all natives (absent from model_env_keys()), confirmed
# live by the probe.
_NATIVE_CREDENTIAL_MODELS: dict[str, str] = {
    "claude-native": "databricks-claude-sonnet-4-6",
    "codex-native": "databricks-gpt-5-4-mini",
}
_NATIVE_DEFAULT_MODEL = "databricks-claude-sonnet-4-6"

# The vendor CLI the driver skip-gates on. Usually the harness id minus
# "-native" (claude-native -> "claude"), but several vendors ship a
# differently-named binary (the _DEFAULT_*_COMMAND in each omnigent/*_native.py),
# so those are listed explicitly. A missing/unlisted native falls back to the
# suffix convention.
_NATIVE_CLI_BINARY: dict[str, str] = {
    "cursor-native": "cursor-agent",
    "kiro-native": "kiro-cli",
}


def _native_profile(harness: str) -> BenchProfile:
    """Build a native-tui :class:`BenchProfile`; all fields from convention/capabilities."""
    caps = harness_capabilities().get(harness)
    cli_binary = _NATIVE_CLI_BINARY.get(harness, harness.removesuffix("-native"))
    env_prefix = "HARNESS_" + harness.upper().replace("-", "_") + "_"
    marker = harness.upper().replace("-", "_") + "_OK"
    return BenchProfile(
        harness=harness,
        model=_NATIVE_CREDENTIAL_MODELS.get(harness, _NATIVE_DEFAULT_MODEL),
        env_prefix=env_prefix,
        marker=marker,
        cli_binary=cli_binary,
        transport="native-tui",
        owner="",
        auth=_auth_prose(caps),
        implementation=_implementation_prose(caps),
        declared=_declared_from_capabilities(harness),
    )


def _native_tui_harnesses() -> list[str]:
    """Every harness the capability model marks as native-tui (plugins included)."""
    return [
        harness
        for harness, caps in harness_capabilities().items()
        if caps.integration_mode is IntegrationMode.NATIVE_TUI
    ]


for _h in _native_tui_harnesses():
    OFFICIAL_PROFILES[_h] = _native_profile(_h)


# ── registry fallback: build a profile for ANY registered harness ─
#
# resolve_profile uses this so a harness passed by name (--harness acp,
# --harness rovo) is runnable even though it is not an official profile and
# ships no BenchProfile of its own. It covers the harnesses the auto-derivation
# above misses: ACP / CLI-subprocess harnesses (in the capability model but not
# NATIVE_TUI, e.g. the in-repo `acp`), and community-plugin harnesses that
# register via entry point but declare no capabilities entry (e.g. `rovo-cli`
# from omnigent-rovo, discovered through harness_modules()).

# integration_mode -> bench transport family. SDK / CLI / ACP subprocess
# harnesses all run through the SDK-wrap drivers (registered as an omnigent
# agent, driven over the session HTTP surface); only NATIVE_TUI needs the
# native driver. A harness with no capabilities entry defaults to the SDK
# family (the common case for a plain subprocess plugin like rovo).
_INTEGRATION_MODE_TRANSPORT: dict[IntegrationMode, str] = {
    IntegrationMode.SDK_IN_PROCESS: "sdk-inproc",
    IntegrationMode.CLI_SUBPROCESS: "sdk-inproc",
    IntegrationMode.ACP_SUBPROCESS: "sdk-inproc",
    IntegrationMode.NATIVE_TUI: "native-tui",
}


def _registry_cli_binary(canonical: str) -> str | None:
    """The vendor binary to skip-gate on, from the harness's install spec.

    e.g. rovo-cli -> ``acli`` (Atlassian CLI). ``None`` when the harness has no
    install spec (e.g. the generic ``acp`` harness, whose command is supplied
    at run time via ``HARNESS_ACP_COMMAND``), in which case there is no cheap
    pre-flight gate and an unrunnable harness skips on the turn instead.
    """
    install_key = harness_install_keys().get(canonical)
    spec = install_specs().get(install_key) if install_key else None
    return getattr(spec, "binary", None)


def _registry_profile(name: str) -> BenchProfile | None:
    """Build a :class:`BenchProfile` for a harness known to the omnigent registry.

    Resolves aliases (``rovo`` -> ``rovo-cli``) and requires the canonical name
    to be a registered harness (``harness_modules()``); returns ``None`` for an
    unknown name so :func:`resolve_profile` can fall through to its error. The
    transport family comes from the capability model's ``integration_mode``
    (defaulting to the SDK family when a plugin declares no capabilities), so
    the harness runs on the existing drivers with no bench edit.
    """
    canonical = harness_aliases().get(name, name)
    # ``acp:<slug>`` is a first-class harness id: the base ``acp`` harness is
    # registered, and the slug selects a user-configured ACP agent at spawn
    # (resolved from the ~/.omnigent config ``acp:`` block, see
    # onboarding/acp_auth.py). Look up caps/module by the base ``acp`` but keep
    # the full id as the profile harness so ``config.harness=acp:<slug>`` reaches
    # the runner. Lets ``--harness acp:qwen`` bind to a specific ACP agent.
    if canonical.startswith("acp:"):
        if not canonical[len("acp:") :]:
            return None  # empty slug ("acp:") — use bare "acp" instead
        registry_key = "acp"
    else:
        registry_key = canonical
    if registry_key not in harness_modules():
        return None

    caps = harness_capabilities().get(registry_key)
    mode = caps.integration_mode if caps is not None else None
    if mode is None:
        # No capabilities entry (a plain subprocess plugin like rovo): the bench
        # has no modeled transport, so assume the SDK-wrap family — the only
        # thing a registered-but-unmodeled harness can plausibly run on.
        transport = "sdk-inproc"
    elif mode in _INTEGRATION_MODE_TRANSPORT:
        transport = _INTEGRATION_MODE_TRANSPORT[mode]
    else:
        # A MODELED mode the bench has no driver for (e.g. NATIVE_SERVER /
        # opencode-native). Refuse rather than silently degrade to the SDK
        # family: return None so resolve_profile raises a clean KeyError. A
        # bare "default to sdk-inproc" here would bind a native-server harness
        # to the wrong driver and drop its skip-gate.
        return None

    if transport == "native-tui":
        # A native-tui harness the auto-derivation would already cover; reuse
        # its builder so the two paths agree.
        return _native_profile(canonical)

    # env_prefix / marker sanitize non-word chars (an acp:<slug> id has a colon)
    # to a valid env-var stem, e.g. acp:qwen -> HARNESS_ACP_QWEN_.
    stem = canonical.upper().replace("-", "_").replace(":", "_")
    env_prefix = "HARNESS_" + stem + "_"
    marker = stem + "_OK"
    # A model is always required: the omnigent executor spec mandates one
    # (spec/omnigent.py: "executor.type='omnigent' requires a model"), so an
    # empty model fails agent registration ("llm.model must be present"). For an
    # own-auth harness (e.g. ACP/rovo) the value is inert — the runner drops any
    # databricks-* gateway model for it (ACP: workflow.py::_build_acp_spawn_env)
    # and the agent authenticates + picks its own model — but it must still be a
    # valid non-empty id to register. So stamp the databricks default in all
    # cases: real for a gateway harness, an accepted-but-ignored placeholder for
    # an own-auth one.
    return BenchProfile(
        harness=canonical,
        model=_NATIVE_DEFAULT_MODEL,
        env_prefix=env_prefix,
        marker=marker,
        # install-spec + declared caps are keyed by the base harness, not the
        # acp:<slug> id, so look them up by registry_key.
        cli_binary=_registry_cli_binary(registry_key),
        transport=transport,
        owner="",
        auth=_auth_prose(caps),
        implementation=_implementation_prose(caps),
        declared=_declared_from_capabilities(registry_key),
    )


__all__ = ["OFFICIAL_PROFILES"]
