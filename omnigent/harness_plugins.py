"""Dynamic harness contribution registry.

Core Omnigent contributes the built-in harnesses directly. Optional community
packages contribute additional harnesses through the
``omnigent.community.harness`` entry point group.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import logging
from dataclasses import dataclass, field
from typing import Any

from omnigent._wrapper_labels import (
    ANTIGRAVITY_NATIVE_WRAPPER_VALUE,
    CLAUDE_NATIVE_WRAPPER_VALUE,
    CODEX_NATIVE_WRAPPER_VALUE,
    CURSOR_NATIVE_WRAPPER_VALUE,
    GOOSE_NATIVE_WRAPPER_VALUE,
    HERMES_NATIVE_WRAPPER_VALUE,
    KIMI_NATIVE_WRAPPER_VALUE,
    KIRO_NATIVE_WRAPPER_VALUE,
    OPENCODE_NATIVE_WRAPPER_VALUE,
    PI_NATIVE_WRAPPER_VALUE,
    QWEN_NATIVE_WRAPPER_VALUE,
    UI_MODE_LABEL_KEY,
    UI_MODE_TERMINAL_VALUE,
    WRAPPER_LABEL_KEY,
)
from omnigent.harness_capabilities import (
    AuthModel,
    EffortFamily,
    Elicitation,
    HarnessCapabilities,
    IntegrationMode,
    ModelFamily,
    Resume,
)
from omnigent.harness_install_spec import HarnessInstallSpec

_logger = logging.getLogger(__name__)

COMMUNITY_ENTRY_POINT_GROUP = "omnigent.community.harness"
COMMUNITY_MODULE_PREFIX = "omnigent.community.harness."


@dataclass(frozen=True)
class NativeCodingAgent:
    """Stable wire metadata for a native coding-agent TUI."""

    key: str
    display_name: str
    agent_name: str
    harness: str
    wrapper_label: str
    terminal_name: str
    subagent_wrapper_label: str | None = None

    @property
    def presentation_labels(self) -> dict[str, str]:
        """Return labels that make sessions render terminal-first."""
        return {
            UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
            WRAPPER_LABEL_KEY: self.wrapper_label,
        }


@dataclass(frozen=True)
class HarnessContribution:
    """One package's harness registry contribution."""

    name: str
    valid_harnesses: frozenset[str] = frozenset()
    harness_modules: dict[str, str] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    native_harnesses: frozenset[str] = frozenset()
    native_agents: tuple[NativeCodingAgent, ...] = ()
    install_specs: dict[str, HarnessInstallSpec] = field(default_factory=dict)
    harness_install_keys: dict[str, str] = field(default_factory=dict)
    model_env_keys: dict[str, str] = field(default_factory=dict)
    spawn_env_builders: dict[str, str] = field(default_factory=dict)
    missing_install_package: dict[str, str] = field(default_factory=dict)
    harness_labels: dict[str, str] = field(default_factory=dict)
    # Declared feature set per harness id ("what can this harness do?"). Sparse
    # is allowed — a harness with no entry simply has no declared capabilities.
    capabilities: dict[str, HarnessCapabilities] = field(default_factory=dict)


@dataclass(frozen=True)
class HarnessPluginState:
    """Merged harness registry plus non-fatal plugin load errors."""

    contributions: tuple[HarnessContribution, ...]
    load_errors: dict[str, str]


CLAUDE_NATIVE_CODING_AGENT = NativeCodingAgent(
    key="claude",
    display_name="Claude",
    agent_name="claude-native-ui",
    harness="claude-native",
    wrapper_label=CLAUDE_NATIVE_WRAPPER_VALUE,
    terminal_name="claude",
    subagent_wrapper_label="claude-code-native-ui-subagent",
)

CODEX_NATIVE_CODING_AGENT = NativeCodingAgent(
    key="codex",
    display_name="Codex",
    agent_name="codex-native-ui",
    harness="codex-native",
    wrapper_label=CODEX_NATIVE_WRAPPER_VALUE,
    terminal_name="codex",
    subagent_wrapper_label="codex-native-ui-subagent",
)

PI_NATIVE_CODING_AGENT = NativeCodingAgent(
    key="pi",
    display_name="Pi",
    agent_name="pi-native-ui",
    harness="pi-native",
    wrapper_label=PI_NATIVE_WRAPPER_VALUE,
    terminal_name="pi",
)

OPENCODE_NATIVE_CODING_AGENT = NativeCodingAgent(
    key="opencode",
    display_name="OpenCode",
    agent_name="opencode-native-ui",
    harness="opencode-native",
    wrapper_label=OPENCODE_NATIVE_WRAPPER_VALUE,
    terminal_name="opencode",
    subagent_wrapper_label="opencode-native-ui-subagent",
)

CURSOR_NATIVE_CODING_AGENT = NativeCodingAgent(
    key="cursor",
    display_name="Cursor",
    agent_name="cursor-native-ui",
    harness="cursor-native",
    wrapper_label=CURSOR_NATIVE_WRAPPER_VALUE,
    terminal_name="cursor",
)

KIRO_NATIVE_CODING_AGENT = NativeCodingAgent(
    key="kiro",
    display_name="Kiro",
    agent_name="kiro-native-ui",
    harness="kiro-native",
    wrapper_label=KIRO_NATIVE_WRAPPER_VALUE,
    terminal_name="kiro",
)

GOOSE_NATIVE_CODING_AGENT = NativeCodingAgent(
    key="goose",
    display_name="Goose",
    agent_name="goose-native-ui",
    harness="goose-native",
    wrapper_label=GOOSE_NATIVE_WRAPPER_VALUE,
    terminal_name="goose",
)

ANTIGRAVITY_NATIVE_CODING_AGENT = NativeCodingAgent(
    key="antigravity",
    display_name="Antigravity",
    agent_name="antigravity-native-ui",
    harness="antigravity-native",
    wrapper_label=ANTIGRAVITY_NATIVE_WRAPPER_VALUE,
    terminal_name="antigravity",
)

QWEN_NATIVE_CODING_AGENT = NativeCodingAgent(
    key="qwen",
    display_name="Qwen Code",
    agent_name="qwen-native-ui",
    harness="qwen-native",
    wrapper_label=QWEN_NATIVE_WRAPPER_VALUE,
    terminal_name="qwen",
)

KIMI_NATIVE_CODING_AGENT = NativeCodingAgent(
    key="kimi",
    display_name="Kimi",
    agent_name="kimi-native-ui",
    harness="kimi-native",
    wrapper_label=KIMI_NATIVE_WRAPPER_VALUE,
    terminal_name="kimi",
)

HERMES_NATIVE_CODING_AGENT = NativeCodingAgent(
    key="hermes",
    display_name="Hermes",
    agent_name="hermes-native-ui",
    harness="hermes-native",
    wrapper_label=HERMES_NATIVE_WRAPPER_VALUE,
    terminal_name="hermes",
)


# Declared capabilities for the built-in harnesses. Each value is backed by the
# module that implements it; the derivable axes (model_family, subagents) are
# asserted against their source in tests/test_harness_capabilities.py so the
# table cannot silently drift. See designs/harness-modular-registry-proposal.md.
_C = HarnessCapabilities
_IM = IntegrationMode
_EL = Elicitation
_RS = Resume
_EF = EffortFamily
_MF = ModelFamily
_AU = AuthModel

# Trailing two bools are (interrupt, streaming). Only the four P0 SDK harnesses
# (claude-sdk, codex, pi, openai-agents) have these verified live by the harness
# bench today; the rest are declared best-effort by integration mode and will be
# reconciled against the bench's interrupt/streaming probes as coverage expands.
_BUILTIN_CAPABILITIES: dict[str, HarnessCapabilities] = {
    # Native-CLI harnesses (wrap a resident vendor TUI/server).
    "claude-native": _C(
        _IM.NATIVE_TUI,
        _EL.HOOK,
        _RS.WARM_REATTACH,
        _EF.ANTHROPIC,
        _MF.CLAUDE,
        _AU.OMNIGENT_CREDENTIAL,
        subagents=True,
        interrupt=True,
        streaming=True,
    ),
    "codex-native": _C(
        _IM.NATIVE_TUI,
        _EL.JSONRPC,
        _RS.WARM_REATTACH,
        _EF.OPENAI,
        _MF.GPT,
        _AU.OMNIGENT_CREDENTIAL,
        subagents=True,
        interrupt=True,
        streaming=True,
    ),
    # pi/cursor/kiro/goose/qwen/kimi/hermes are transcript-mirror natives: their
    # forwarder posts each COMPLETE assistant message (external_conversation_item),
    # never token-level external_output_text_delta, so the web UI sees the reply
    # complete-only, not streamed. streaming=False reflects that (bench-verified
    # for kiro-native; the others share the same forwarder shape — 0 delta posts).
    # Contrast claude/codex/antigravity, whose forwarders do post deltas.
    "pi-native": _C(
        _IM.NATIVE_TUI,
        _EL.NONE,
        _RS.WARM_REATTACH,
        _EF.NONE,
        _MF.MULTI,
        _AU.SESSION_SCOPED_CONFIG,
        subagents=False,
        interrupt=True,
        streaming=False,
    ),
    "cursor-native": _C(
        _IM.NATIVE_TUI,
        _EL.APPROVAL_MIRROR,
        _RS.WARM_REATTACH,
        _EF.NONE,
        _MF.MULTI,
        _AU.OWN_AUTH,
        subagents=False,
        interrupt=True,
        streaming=False,
    ),
    # kiro_native_permissions.py: "TUI ACP recorder -> web elicitation".
    "kiro-native": _C(
        _IM.NATIVE_TUI,
        _EL.APPROVAL_MIRROR,
        _RS.WARM_REATTACH,
        _EF.NONE,
        _MF.MULTI,
        _AU.OWN_AUTH,
        subagents=False,
        interrupt=True,
        streaming=False,
    ),
    "antigravity-native": _C(
        _IM.NATIVE_TUI,
        _EL.NONE,
        _RS.WARM_REATTACH,
        _EF.GEMINI,
        _MF.GEMINI,
        _AU.OWN_AUTH,
        subagents=False,
        interrupt=True,
        streaming=True,
    ),
    "goose-native": _C(
        _IM.NATIVE_TUI,
        _EL.APPROVAL_MIRROR,
        _RS.WARM_REATTACH,
        _EF.NONE,
        _MF.MULTI,
        _AU.OWN_AUTH,
        subagents=False,
        interrupt=True,
        streaming=False,
    ),
    "qwen-native": _C(
        _IM.NATIVE_TUI,
        _EL.APPROVAL_MIRROR,
        _RS.WARM_REATTACH,
        _EF.NONE,
        _MF.MULTI,
        _AU.OWN_AUTH,
        subagents=False,
        interrupt=True,
        streaming=False,
    ),
    "kimi-native": _C(
        _IM.NATIVE_TUI,
        _EL.HOOK,
        _RS.WARM_REATTACH,
        _EF.NONE,
        _MF.MULTI,
        _AU.SESSION_SCOPED_CONFIG,
        subagents=False,
        interrupt=True,
        streaming=False,
    ),
    "opencode-native": _C(
        _IM.NATIVE_SERVER,
        _EL.SSE_PERMISSION,
        _RS.WARM_REATTACH,
        _EF.NONE,
        _MF.MULTI,
        _AU.OWN_AUTH,
        subagents=True,
        interrupt=True,
        streaming=True,
    ),
    "hermes-native": _C(
        _IM.NATIVE_TUI,
        _EL.APPROVAL_MIRROR,
        _RS.WARM_REATTACH,
        _EF.NONE,
        _MF.MULTI,
        _AU.OWN_AUTH,
        subagents=False,
        interrupt=True,
        streaming=False,
    ),
    # SDK / subprocess harnesses (run the vendor model directly). The first four
    # are bench-verified interrupt=streaming=True.
    "claude-sdk": _C(
        _IM.SDK_IN_PROCESS,
        _EL.NONE,
        _RS.COLD_ONLY,
        _EF.ANTHROPIC,
        _MF.CLAUDE,
        _AU.OMNIGENT_CREDENTIAL,
        subagents=False,
        interrupt=True,
        streaming=True,
    ),
    "codex": _C(
        _IM.CLI_SUBPROCESS,
        _EL.JSONRPC,
        _RS.WARM_REATTACH,
        _EF.OPENAI,
        _MF.GPT,
        _AU.OMNIGENT_CREDENTIAL,
        subagents=False,
        interrupt=True,
        streaming=True,
    ),
    "pi": _C(
        _IM.CLI_SUBPROCESS,
        _EL.NONE,
        _RS.COLD_ONLY,
        _EF.NONE,
        _MF.MULTI,
        _AU.OMNIGENT_CREDENTIAL,
        subagents=False,
        interrupt=True,
        streaming=True,
    ),
    "openai-agents": _C(
        _IM.SDK_IN_PROCESS,
        _EL.NONE,
        _RS.COLD_ONLY,
        _EF.OPENAI,
        _MF.MULTI,
        _AU.OMNIGENT_CREDENTIAL,
        subagents=False,
        interrupt=True,
        streaming=True,
    ),
    "cursor": _C(
        _IM.SDK_IN_PROCESS,
        _EL.NONE,
        _RS.WARM_REATTACH,
        _EF.NONE,
        _MF.MULTI,
        _AU.OWN_AUTH,
        subagents=False,
        interrupt=True,
        streaming=True,
    ),
    "antigravity": _C(
        _IM.SDK_IN_PROCESS,
        _EL.NONE,
        _RS.COLD_ONLY,
        _EF.GEMINI,
        _MF.GEMINI,
        _AU.OWN_AUTH,
        subagents=False,
        interrupt=True,
        streaming=True,
    ),
    "goose": _C(
        _IM.ACP_SUBPROCESS,
        _EL.SSE_PERMISSION,
        _RS.COLD_ONLY,
        _EF.NONE,
        _MF.MULTI,
        _AU.OWN_AUTH,
        subagents=False,
        interrupt=True,
        streaming=True,
    ),
    "qwen": _C(
        _IM.ACP_SUBPROCESS,
        _EL.SSE_PERMISSION,
        _RS.COLD_ONLY,
        _EF.NONE,
        _MF.MULTI,
        _AU.OWN_AUTH,
        subagents=False,
        interrupt=True,
        streaming=True,
    ),
    "kimi": _C(
        _IM.CLI_SUBPROCESS,
        _EL.NONE,
        _RS.WARM_REATTACH,
        _EF.NONE,
        _MF.MULTI,
        _AU.SESSION_SCOPED_CONFIG,
        subagents=False,
        interrupt=True,
        streaming=True,
    ),
    "hermes": _C(
        _IM.CLI_SUBPROCESS,
        _EL.HOOK,
        _RS.COLD_ONLY,
        _EF.NONE,
        _MF.MULTI,
        _AU.OWN_AUTH,
        subagents=False,
        interrupt=True,
        streaming=True,
    ),
    "copilot": _C(
        _IM.SDK_IN_PROCESS,
        _EL.NONE,
        _RS.COLD_ONLY,
        _EF.COPILOT,
        _MF.MULTI,
        _AU.OWN_AUTH,
        subagents=False,
        interrupt=True,
        streaming=True,
    ),
    # open-responses is resolved via an alternate path, but its executor
    # (omnigent/inner/open_responses_sdk.py) is concrete: interrupt_session()
    # closes the active stream and returns True, supports_streaming() is True,
    # and it drives an OpenAI Responses model (gpt-5.3-codex) forwarding
    # reasoning_effort via cfg.extra — so effort is OPENAI.
    "open-responses": _C(
        _IM.SDK_IN_PROCESS,
        _EL.NONE,
        _RS.COLD_ONLY,
        _EF.OPENAI,
        _MF.MULTI,
        _AU.OMNIGENT_CREDENTIAL,
        subagents=False,
        interrupt=True,
        streaming=True,
    ),
}


_BUILTIN_CONTRIBUTION = HarnessContribution(
    name="omnigent",
    valid_harnesses=frozenset(
        {
            "antigravity",
            "antigravity-native",
            "claude-native",
            "claude-sdk",
            "codex",
            "codex-native",
            "copilot",
            "cursor",
            "cursor-native",
            "goose",
            "goose-native",
            "hermes",
            "hermes-native",
            "kimi",
            "kimi-native",
            "kiro-native",
            "open-responses",
            "openai-agents",
            "opencode-native",
            "pi",
            "pi-native",
            "qwen",
            "qwen-native",
        }
    ),
    harness_modules={
        "antigravity": "omnigent.inner.antigravity_harness",
        "antigravity-native": "omnigent.inner.antigravity_native_harness",
        "claude-native": "omnigent.inner.claude_native_harness",
        "claude-sdk": "omnigent.inner.claude_sdk_harness",
        "codex": "omnigent.inner.codex_harness",
        "codex-native": "omnigent.inner.codex_native_harness",
        "copilot": "omnigent.inner.copilot_harness",
        "cursor": "omnigent.inner.cursor_harness",
        "cursor-native": "omnigent.inner.cursor_native_harness",
        "goose": "omnigent.inner.goose_harness",
        "goose-native": "omnigent.inner.goose_native_harness",
        "hermes": "omnigent.inner.hermes_harness",
        "hermes-native": "omnigent.inner.hermes_native_harness",
        "kimi": "omnigent.inner.kimi_harness",
        "kimi-native": "omnigent.inner.kimi_native_harness",
        "kiro-native": "omnigent.inner.kiro_native_harness",
        "openai-agents": "omnigent.inner.openai_agents_sdk_harness",
        "opencode-native": "omnigent.inner.opencode_native_harness",
        "pi": "omnigent.inner.pi_harness",
        "pi-native": "omnigent.inner.pi_native_harness",
        "qwen": "omnigent.inner.qwen_harness",
        "qwen-native": "omnigent.inner.qwen_native_harness",
    },
    aliases={
        "agy": "antigravity",
        "claude": "claude-sdk",
        "github-copilot": "copilot",
        "google-antigravity": "antigravity",
        "kimi-code": "kimi",
        "native-antigravity": "antigravity-native",
        "native-goose": "goose-native",
        "native-hermes": "hermes-native",
        "native-kimi": "kimi-native",
        "native-kiro": "kiro-native",
        "native-opencode": "opencode-native",
        "native-pi": "pi-native",
        "native-qwen": "qwen-native",
        "opencode": "opencode-native",
        "openai-agents-sdk": "openai-agents",
        "qwen-code": "qwen",
    },
    native_harnesses=frozenset(
        {
            "antigravity-native",
            "claude-native",
            "codex-native",
            "cursor-native",
            "goose-native",
            "hermes-native",
            "kimi-native",
            "kiro-native",
            "native-antigravity",
            "native-claude",
            "native-codex",
            "native-cursor",
            "native-goose",
            "native-hermes",
            "native-kimi",
            "native-kiro",
            "native-opencode",
            "native-pi",
            "native-qwen",
            "opencode-native",
            "pi-native",
            "qwen-native",
        }
    ),
    native_agents=(
        CLAUDE_NATIVE_CODING_AGENT,
        CODEX_NATIVE_CODING_AGENT,
        PI_NATIVE_CODING_AGENT,
        OPENCODE_NATIVE_CODING_AGENT,
        CURSOR_NATIVE_CODING_AGENT,
        KIRO_NATIVE_CODING_AGENT,
        GOOSE_NATIVE_CODING_AGENT,
        ANTIGRAVITY_NATIVE_CODING_AGENT,
        QWEN_NATIVE_CODING_AGENT,
        KIMI_NATIVE_CODING_AGENT,
        HERMES_NATIVE_CODING_AGENT,
    ),
    model_env_keys={
        "antigravity": "HARNESS_ANTIGRAVITY_MODEL",
        "claude-sdk": "HARNESS_CLAUDE_SDK_MODEL",
        "codex": "HARNESS_CODEX_MODEL",
        "copilot": "HARNESS_COPILOT_MODEL",
        "cursor": "HARNESS_CURSOR_MODEL",
        "goose": "HARNESS_GOOSE_MODEL",
        "kimi": "HARNESS_KIMI_MODEL",
        "openai-agents": "HARNESS_OPENAI_AGENTS_MODEL",
        "pi": "HARNESS_PI_MODEL",
        "qwen": "HARNESS_QWEN_MODEL",
    },
    harness_labels={
        "antigravity": "Antigravity",
        "claude-sdk": "Claude SDK",
        "codex": "Codex",
        "copilot": "Copilot",
        "cursor": "Cursor",
        "openai-agents": "OpenAI Agents SDK",
        "pi": "Pi",
    },
    capabilities=_BUILTIN_CAPABILITIES,
)

_state: HarnessPluginState | None = None


def _entry_points() -> tuple[importlib.metadata.EntryPoint, ...]:
    discovered = importlib.metadata.entry_points()
    if hasattr(discovered, "select"):
        return tuple(discovered.select(group=COMMUNITY_ENTRY_POINT_GROUP))
    return tuple(discovered.get(COMMUNITY_ENTRY_POINT_GROUP, ()))


def _module_part(import_path: str) -> str:
    return import_path.split(":", 1)[0]


def _community_paths(contribution: HarnessContribution) -> list[str]:
    paths: list[str] = []
    paths.extend(contribution.harness_modules.values())
    paths.extend(contribution.spawn_env_builders.values())
    return paths


def _harness_spellings(contribution: HarnessContribution) -> set[str]:
    """Return every harness/alias key claimed by a contribution."""
    return (
        set(contribution.valid_harnesses)
        | set(contribution.harness_modules)
        | set(contribution.aliases)
        | set(contribution.native_harnesses)
        | set(contribution.harness_install_keys)
        | set(contribution.model_env_keys)
        | set(contribution.missing_install_package)
        | set(contribution.harness_labels)
        | set(contribution.capabilities)
    )


def _native_agent_identity_values(contribution: HarnessContribution) -> set[str]:
    """Return native-agent identifiers that must stay globally unique."""
    values: set[str] = set()
    for agent in contribution.native_agents:
        values.update(
            {
                agent.key,
                agent.agent_name,
                agent.wrapper_label,
                agent.terminal_name,
            }
        )
        if agent.subagent_wrapper_label:
            values.add(agent.subagent_wrapper_label)
    return values


def _validate_community_contribution(
    contribution: HarnessContribution,
    *,
    entry_point_name: str,
    existing: tuple[HarnessContribution, ...],
) -> str | None:
    if not contribution.name:
        return "plugin contribution must set name"

    for path in _community_paths(contribution):
        if not _module_part(path).startswith(COMMUNITY_MODULE_PREFIX):
            return (
                f"community harness plugin {entry_point_name!r} uses import path "
                f"{path!r}; expected {COMMUNITY_MODULE_PREFIX}*"
            )

    if contribution.native_harnesses or contribution.native_agents:
        return (
            f"community harness plugin {entry_point_name!r} registers native terminal "
            "metadata, but community native terminal harnesses are not supported yet"
        )

    existing_harness_spellings: set[str] = set()
    existing_install_keys: set[str] = set()
    existing_native_agent_values: set[str] = set()
    for accepted in existing:
        existing_harness_spellings.update(_harness_spellings(accepted))
        existing_install_keys.update(accepted.install_specs)
        existing_native_agent_values.update(_native_agent_identity_values(accepted))

    harness_collisions = existing_harness_spellings.intersection(_harness_spellings(contribution))
    if harness_collisions:
        return (
            f"community harness plugin {entry_point_name!r} attempts to override "
            f"existing harness keys: {sorted(harness_collisions)}"
        )

    install_key_collisions = existing_install_keys.intersection(contribution.install_specs)
    if install_key_collisions:
        return (
            f"community harness plugin {entry_point_name!r} attempts to override "
            f"existing install keys: {sorted(install_key_collisions)}"
        )

    native_agent_collisions = existing_native_agent_values.intersection(
        _native_agent_identity_values(contribution)
    )
    if native_agent_collisions:
        return (
            f"community harness plugin {entry_point_name!r} attempts to override "
            f"existing native-agent keys: {sorted(native_agent_collisions)}"
        )

    allowed_targets = set(contribution.valid_harnesses)
    for alias, target in contribution.aliases.items():
        if target not in allowed_targets:
            return (
                f"community harness plugin {entry_point_name!r} alias {alias!r} "
                f"targets {target!r}, which is not contributed by the plugin"
            )
    return None


def plugin_state() -> HarnessPluginState:
    """Return the merged built-in + community harness registry."""
    global _state
    if _state is not None:
        return _state

    contributions: list[HarnessContribution] = [_BUILTIN_CONTRIBUTION]
    load_errors: dict[str, str] = {}
    for entry_point in _entry_points():
        try:
            loaded = entry_point.load()
            contribution = loaded() if callable(loaded) else loaded
            if not isinstance(contribution, HarnessContribution):
                raise TypeError(
                    f"entry point returned {type(contribution).__name__}, "
                    "expected HarnessContribution"
                )
            error = _validate_community_contribution(
                contribution,
                entry_point_name=entry_point.name,
                existing=tuple(contributions),
            )
            if error is not None:
                raise ValueError(error)
            contributions.append(contribution)
        except Exception as exc:  # noqa: BLE001 - broken plugins must not break core startup.
            load_errors[entry_point.name] = str(exc)
            _logger.warning(
                "could not load harness plugin entry point %s (%s)",
                entry_point.name,
                exc,
                exc_info=True,
            )

    _state = HarnessPluginState(tuple(contributions), load_errors)
    return _state


def reset_plugin_state_for_tests() -> None:
    """Clear the cached plugin state."""
    global _state
    _state = None


def _merge_dict(attr: str) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for contribution in plugin_state().contributions:
        merged.update(getattr(contribution, attr))
    return merged


def _merge_set(attr: str) -> frozenset[str]:
    merged: set[str] = set()
    for contribution in plugin_state().contributions:
        merged.update(getattr(contribution, attr))
    return frozenset(merged)


def valid_harnesses() -> frozenset[str]:
    """Return canonical harness ids accepted by installed contributions."""
    return _merge_set("valid_harnesses")


def harness_aliases() -> dict[str, str]:
    """Return alias-to-canonical harness ids."""
    return _merge_dict("aliases")


def accepted_harnesses() -> frozenset[str]:
    """Return canonical harness ids plus accepted aliases."""
    return valid_harnesses() | frozenset(harness_aliases())


def native_harnesses() -> frozenset[str]:
    """Return native CLI harness ids and native aliases."""
    return _merge_set("native_harnesses")


def native_agents() -> tuple[NativeCodingAgent, ...]:
    """Return native coding-agent metadata rows."""
    agents: list[NativeCodingAgent] = []
    for contribution in plugin_state().contributions:
        agents.extend(contribution.native_agents)
    return tuple(agents)


def harness_modules() -> dict[str, str]:
    """Return runtime harness module mapping, aliases included."""
    modules = _merge_dict("harness_modules")
    for alias, canonical in harness_aliases().items():
        module = modules.get(canonical)
        if module is not None:
            modules.setdefault(alias, module)
    return modules


def model_env_keys() -> dict[str, str]:
    """Return harness-to-model-env-var mapping."""
    return _merge_dict("model_env_keys")


def spawn_env_builders() -> dict[str, str]:
    """Return harness-to-spawn-env-builder import paths."""
    return _merge_dict("spawn_env_builders")


def install_specs() -> dict[str, HarnessInstallSpec]:
    """Return plugin-provided install specs."""
    return _merge_dict("install_specs")


def harness_install_keys() -> dict[str, str]:
    """Return harness/alias to install-spec key mappings."""
    return _merge_dict("harness_install_keys")


def missing_install_packages() -> dict[str, str]:
    """Return optional harness spellings to package names."""
    return _merge_dict("missing_install_package")


def harness_labels() -> dict[str, str]:
    """Return labels for non-native harness picker/catalog rows."""
    return _merge_dict("harness_labels")


def harness_capabilities() -> dict[str, HarnessCapabilities]:
    """Return the declared capability record per harness id.

    Merged across contributions; sparse (a harness need not declare
    capabilities). This is the single source of truth for "what can this
    harness do?".
    """
    return _merge_dict("capabilities")


def harness_catalog() -> list[dict[str, Any]]:
    """Return stable JSON-serializable harness catalog rows.

    Each row carries ``id`` and ``label``; rows for harnesses with declared
    capabilities also carry a ``capabilities`` object (see
    :meth:`HarnessCapabilities.as_dict`), so the ``/v1/harnesses`` catalog can
    surface the feature matrix to clients.
    """
    labels = harness_labels()
    capabilities = harness_capabilities()
    rows: list[dict[str, Any]] = []
    for harness in sorted(labels, key=lambda key: labels[key].lower()):
        if harness not in valid_harnesses():
            continue
        row: dict[str, Any] = {"id": harness, "label": labels[harness]}
        capability = capabilities.get(harness)
        if capability is not None:
            row["capabilities"] = capability.as_dict()
        rows.append(row)
    return rows


def load_object(import_path: str) -> Any:
    """Load ``module:attribute`` or ``module.attribute``."""
    if ":" in import_path:
        module_name, attr = import_path.split(":", 1)
    else:
        module_name, attr = import_path.rsplit(".", 1)
    module = importlib.import_module(module_name)
    return getattr(module, attr)
