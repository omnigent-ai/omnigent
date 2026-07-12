"""Build harness bench profiles from the canonical capability registry."""

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


# P1 probes stay UNKNOWN because their valid result varies by transport or pricing.
_PROBE_ONLY_DECLARED: dict[str, Verdict] = {
    "basic_turn": Verdict.SUPPORTED,
    "tool_calling": Verdict.SUPPORTED,
    "policy_deny": Verdict.SUPPORTED,
}


def _implementation_prose(caps: HarnessCapabilities | None) -> str:
    if caps is None:
        return ""
    return _INTEGRATION_MODE_PROSE.get(caps.integration_mode, caps.integration_mode.value)


def _auth_prose(caps: HarnessCapabilities | None) -> str:
    if caps is None:
        return ""
    return _AUTH_PROSE.get(caps.auth, caps.auth.value)


def _declared_from_capabilities(harness: str) -> dict[str, Verdict]:
    """Build declared verdicts, leaving unmodeled capabilities UNKNOWN."""
    declared: dict[str, Verdict] = dict(_PROBE_ONLY_DECLARED)

    caps = harness_capabilities().get(harness)
    if caps is not None:
        declared["streaming"] = Verdict.SUPPORTED if caps.streaming else Verdict.UNSUPPORTED
        declared["interrupt"] = Verdict.SUPPORTED if caps.interrupt else Verdict.UNSUPPORTED

    if harness in model_env_keys() or is_native_harness(harness):
        declared["model_override"] = Verdict.SUPPORTED

    return declared


def _profile_from_probe(probe: HarnessProbe) -> BenchProfile:
    """Build an official profile from the shared e2e probe metadata."""
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


_OFFICIAL_HARNESSES = frozenset({"claude-sdk", "codex", "pi", "openai-agents"})

OFFICIAL_PROFILES: dict[str, BenchProfile] = {
    probe.harness: _profile_from_probe(probe)
    for probe in HARNESS_PROBES
    if probe.harness in _OFFICIAL_HARNESSES
}


_NATIVE_CREDENTIAL_MODELS: dict[str, str] = {
    "claude-native": "databricks-claude-sonnet-4-6",
    "codex-native": "databricks-gpt-5-4-mini",
}
_NATIVE_DEFAULT_MODEL = "databricks-claude-sonnet-4-6"

_NATIVE_CLI_BINARY: dict[str, str] = {
    "cursor-native": "cursor-agent",
    "kiro-native": "kiro-cli",
}


def _native_profile(harness: str) -> BenchProfile:
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
    return [
        harness
        for harness, caps in harness_capabilities().items()
        if caps.integration_mode is IntegrationMode.NATIVE_TUI
    ]


for _h in _native_tui_harnesses():
    OFFICIAL_PROFILES[_h] = _native_profile(_h)


_INTEGRATION_MODE_TRANSPORT: dict[IntegrationMode, str] = {
    IntegrationMode.SDK_IN_PROCESS: "sdk-inproc",
    IntegrationMode.CLI_SUBPROCESS: "sdk-inproc",
    IntegrationMode.ACP_SUBPROCESS: "sdk-inproc",
    IntegrationMode.NATIVE_TUI: "native-tui",
}


def _registry_cli_binary(canonical: str) -> str | None:
    """Return the install spec's binary, if the harness has one."""
    install_key = harness_install_keys().get(canonical)
    spec = install_specs().get(install_key) if install_key else None
    return getattr(spec, "binary", None)


def _registry_profile(name: str) -> BenchProfile | None:
    """Build a profile for a registered harness, resolving aliases."""
    canonical = harness_aliases().get(name, name)
    # ACP slugs use base-harness metadata but must reach the runner unchanged.
    if canonical.startswith("acp:"):
        if not canonical[len("acp:") :]:
            return None
        registry_key = "acp"
    else:
        registry_key = canonical
    if registry_key not in harness_modules():
        return None

    caps = harness_capabilities().get(registry_key)
    mode = caps.integration_mode if caps is not None else None
    if mode is None:
        transport = "sdk-inproc"
    elif mode in _INTEGRATION_MODE_TRANSPORT:
        transport = _INTEGRATION_MODE_TRANSPORT[mode]
    else:
        return None

    if transport == "native-tui":
        return _native_profile(canonical)

    stem = canonical.upper().replace("-", "_").replace(":", "_")
    env_prefix = "HARNESS_" + stem + "_"
    marker = stem + "_OK"
    # Agent registration requires a model even when an own-auth harness ignores it.
    return BenchProfile(
        harness=canonical,
        model=_NATIVE_DEFAULT_MODEL,
        env_prefix=env_prefix,
        marker=marker,
        cli_binary=_registry_cli_binary(registry_key),
        transport=transport,
        owner="",
        auth=_auth_prose(caps),
        implementation=_implementation_prose(caps),
        declared=_declared_from_capabilities(registry_key),
    )


__all__ = ["OFFICIAL_PROFILES"]
