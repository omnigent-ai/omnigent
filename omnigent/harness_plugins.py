"""Dynamic harness contribution registry.

Core Omnigent contributes the built-in harnesses directly. Optional community
packages contribute additional harnesses through the
``omnigent.community.harness`` entry point group.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import TypeVar, cast

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
from omnigent.acp_cli_harnesses import ACP_CLI_HARNESSES
from omnigent.harness_capabilities import (
    AuthModel,
    EffortFamily,
    Elicitation,
    ForkHistory,
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
class BackgroundTitleGeneratorSpec:
    """Lazy background-title generator registration for one harness."""

    generator: str
    resolver_harness: str | None = None


@dataclass(frozen=True)
class NativeHarnessProvider:
    """Import paths for a native harness's lifecycle hooks.

    ``NativeCodingAgent`` is pure identity data; behavior lives here as a
    sibling row keyed by the same ``key``. Every value is a dotted import path
    (``module:attr`` or ``module.attr``) resolved lazily at dispatch time via
    :mod:`omnigent.native_dispatch`, so building the registry never imports the
    runner / CLI / native-harness stack. Optional hooks are ``None`` when the
    behavior is not yet a module-level function the resolver can reach (e.g.
    interrupt/stop handlers that are still runner closures, or the inline
    spawn-env dispatch); those hubs migrate onto the seam in later phases.
    """

    key: str  # matches NativeCodingAgent.key
    run_native: str  # CLI + resume launch entry point
    auto_create_terminal: str  # runner terminal builder
    spawn_env_builder: str | None = None
    # Session-label key carrying this harness's bridge id, when its spawn-env
    # builder takes a ``bridge_id=`` kwarg resolved from session labels
    # (codex/opencode/antigravity). ``None`` for bare builders and for harnesses
    # whose bridge id resolves through a different path (e.g. claude).
    bridge_id_label_key: str | None = None
    interrupt_handler: str | None = None
    stop_handler: str | None = None
    materialize_agent_spec: str | None = None  # built-in agent seeding
    bridge_dir: str | None = None  # cost-popup bridge-dir lookup


@dataclass(frozen=True)
class HarnessContribution:
    """One package's harness registry contribution."""

    name: str
    valid_harnesses: frozenset[str] = frozenset()
    harness_modules: dict[str, str] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    native_harnesses: frozenset[str] = frozenset()
    native_agents: tuple[NativeCodingAgent, ...] = ()
    native_providers: tuple[NativeHarnessProvider, ...] = ()
    install_specs: dict[str, HarnessInstallSpec] = field(default_factory=dict)
    harness_install_keys: dict[str, str] = field(default_factory=dict)
    model_env_keys: dict[str, str] = field(default_factory=dict)
    spawn_env_builders: dict[str, str] = field(default_factory=dict)
    background_title_generators: dict[str, BackgroundTitleGeneratorSpec] = field(
        default_factory=dict
    )
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


# Native harnesses whose spawn-env builder takes a ``bridge_id=`` resolved from
# a session label. Their label key follows the uniform
# ``omnigent.<key>_native.bridge_id`` pattern (pinned against the real bridge
# constants in tests/test_harness_plugins.py). Claude also carries a bridge id
# but resolves it through a runner helper with a server-side fallback, so it is
# handled as a spawn-env special case rather than a plain label read.
_BRIDGE_ID_LABEL_HARNESSES: frozenset[str] = frozenset({"codex", "opencode", "antigravity"})


def _builtin_native_provider(key: str) -> NativeHarnessProvider:
    """Build a built-in provider row from the ``omnigent.<key>_native`` module.

    The built-in native harnesses follow a uniform module layout: each exports
    ``run_<key>_native`` (CLI + resume launch) and ``_materialize_<key>_agent_spec``
    (agent seeding), exposes a ``_launch_<key>`` terminal adapter in
    ``omnigent.runner.native``, and exposes ``build_<key>_native_spawn_env`` in
    ``omnigent.<key>_native_bridge``. The remaining hooks (interrupt, stop,
    bridge-dir) are still runner-local closures / inline dispatch, so they stay
    ``None`` until those hubs migrate onto the seam.
    """
    module = f"omnigent.{key}_native"
    return NativeHarnessProvider(
        key=key,
        run_native=f"{module}:run_{key}_native",
        auto_create_terminal=f"omnigent.runner.native:_launch_{key}",
        spawn_env_builder=f"{module}_bridge:build_{key}_native_spawn_env",
        bridge_id_label_key=(f"{module}.bridge_id" if key in _BRIDGE_ID_LABEL_HARNESSES else None),
        materialize_agent_spec=f"{module}:_materialize_{key}_agent_spec",
    )


# Behavior side-channel for the built-in native agents. One row per
# NativeCodingAgent above, keyed by the same ``key``; resolved lazily so this
# module stays import-light. See designs/harness-modular-registry-proposal.md
# (Phase 1). Populated uniformly because every built-in native harness shares
# the omnigent.<key>_native module layout.
_BUILTIN_NATIVE_PROVIDERS: tuple[NativeHarnessProvider, ...] = tuple(
    _builtin_native_provider(agent.key)
    for agent in (
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
    )
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
_FH = ForkHistory

# Bench shell-tool provocation prompts (moved off the bench's hardcoded
# _NATIVE_TOOL_PROVOCATION table): the generic variant, and a Bash-specific one
# for harnesses whose exec tool is literally "Bash". Both keep the
# "omnigent-bench-ok" placeholder the bench token-swaps per allow/deny probe.
_SHELL_PROMPT = "Use your shell/terminal tool to run this exact command: echo omnigent-bench-ok"
_BASH_PROMPT = "Use the Bash tool to run this exact command: echo omnigent-bench-ok"

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
        fork_history=_FH.REBUILD,
        shell_tool_name="Bash",
        shell_tool_prompt=_BASH_PROMPT,
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
        fork_history=_FH.REBUILD,
        shell_tool_name="shell",
        shell_tool_prompt=_SHELL_PROMPT,
    ),
    # streaming is declared True unless a live bench run proves a harness does
    # NOT emit token-level deltas. Only kiro-native is so proven (0 deltas over
    # a full SSE capture); a static "forwarder posts no external_output_text_delta"
    # grep is NOT sufficient — pi-native has no such delta-posting forwarder yet
    # streams 7 deltas live (by what path was not traced), so the grep-based
    # flip was wrong for it. The rest stay True until live-verified.
    "pi-native": _C(
        _IM.NATIVE_TUI,
        _EL.NONE,
        _RS.WARM_REATTACH,
        _EF.PI,
        _MF.MULTI,
        _AU.SESSION_SCOPED_CONFIG,
        subagents=False,
        interrupt=True,
        streaming=True,
        fork_history=_FH.REBUILD,
        shell_tool_name="Bash",
        shell_tool_prompt=_BASH_PROMPT,
    ),
    # streaming=False is LIVE-VERIFIED: a bench run observed 0 text deltas.
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
        fork_history=_FH.PREAMBLE,
        # No shell-tool provocation: cursor-native was intentionally absent from
        # the bench's table (its tool probe is skipped), so leave shell_tool_* None.
    ),
    # kiro_native_permissions.py: "TUI ACP recorder -> web elicitation".
    # streaming=False is LIVE-VERIFIED: a full SSE capture recorded 0 text
    # deltas; the whole reply arrives as one response.output_item.done.
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
        fork_history=_FH.NONE,
        shell_tool_name="shell",
        shell_tool_prompt=_SHELL_PROMPT,
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
        fork_history=_FH.NONE,
        shell_tool_name="run_command",
        shell_tool_prompt=_SHELL_PROMPT,
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
        streaming=True,
        fork_history=_FH.NONE,
        shell_tool_name="developer__shell",
        shell_tool_prompt=_SHELL_PROMPT,
    ),
    # streaming=False is LIVE-VERIFIED: a bench run observed 0 text deltas.
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
        fork_history=_FH.REBUILD,
        shell_tool_name="run_shell_command",
        shell_tool_prompt=_SHELL_PROMPT,
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
        streaming=True,
        fork_history=_FH.NONE,
        shell_tool_name="Bash",
        shell_tool_prompt=_BASH_PROMPT,
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
        fork_history=_FH.PREAMBLE,
        # NATIVE_SERVER, not driven by the bench's native-tui tool probe, so
        # shell_tool_* stay None.
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
        streaming=True,
        fork_history=_FH.REBUILD,
        shell_tool_name="terminal",
        shell_tool_prompt=_SHELL_PROMPT,
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
        _EF.PI,
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
    # Generic ACP harness — drives any user-configured ACP agent command. Same
    # profile as goose/qwen (own-auth, cold resume, SSE permission), but its
    # interrupt IS implemented (ACP ``session/cancel``), not just declared.
    "acp": _C(
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

# Builtin ACP CLI harnesses (omnigent/acp_cli_harnesses.py) run through the
# same generic wrap as the "acp" harness, so they share its declared profile.
for _acp_cli_name in ACP_CLI_HARNESSES:
    _BUILTIN_CAPABILITIES[_acp_cli_name] = _BUILTIN_CAPABILITIES["acp"]


_BUILTIN_CONTRIBUTION = HarnessContribution(
    name="omnigent",
    valid_harnesses=frozenset(
        {
            "acp",
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
        # Builtin ACP CLI harnesses derive from the declarative catalog; a new
        # vendor CLI is one row there, not another entry in each set below.
        | set(ACP_CLI_HARNESSES)
    ),
    harness_modules={
        # Every catalog row runs the shared generic ACP wrap.
        **dict.fromkeys(ACP_CLI_HARNESSES, "omnigent.inner.acp_harness"),
        "acp": "omnigent.inner.acp_harness",
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
        **{alias: name for name, row in ACP_CLI_HARNESSES.items() for alias in row.aliases},
        "agy": "antigravity",
        "agy-native": "antigravity-native",
        "claude": "claude-sdk",
        "github-copilot": "copilot",
        "google-antigravity": "antigravity",
        "kimi-code": "kimi",
        "native-agy": "antigravity-native",
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
            "agy-native",
            "antigravity-native",
            "claude-native",
            "codex-native",
            "cursor-native",
            "goose-native",
            "hermes-native",
            "kimi-native",
            "kiro-native",
            "native-agy",
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
    native_providers=_BUILTIN_NATIVE_PROVIDERS,
    # Catalog rows gate readiness on their vendor binary; the install spec also
    # feeds setup steps and (for npm rows) the one-click install path.
    install_specs={name: row.install for name, row in ACP_CLI_HARNESSES.items()},
    harness_install_keys={
        spelling: name
        for name, row in ACP_CLI_HARNESSES.items()
        for spelling in (name, *row.aliases)
    },
    model_env_keys={
        "acp": "HARNESS_ACP_MODEL",
        "antigravity": "HARNESS_ANTIGRAVITY_MODEL",
        "claude-sdk": "HARNESS_CLAUDE_SDK_MODEL",
        "codex": "HARNESS_CODEX_MODEL",
        "copilot": "HARNESS_COPILOT_MODEL",
        "cursor": "HARNESS_CURSOR_MODEL",
        "goose": "HARNESS_GOOSE_MODEL",
        "hermes": "HARNESS_HERMES_MODEL",
        "kimi": "HARNESS_KIMI_MODEL",
        "openai-agents": "HARNESS_OPENAI_AGENTS_MODEL",
        "pi": "HARNESS_PI_MODEL",
        "qwen": "HARNESS_QWEN_MODEL",
    },
    background_title_generators={
        "claude-sdk": BackgroundTitleGeneratorSpec(
            "omnigent.runner.background_titles.sdk:generate_background_title"
        ),
        "claude-native": BackgroundTitleGeneratorSpec(
            "omnigent.runner.background_titles.claude_native:generate_background_title",
            resolver_harness="claude-sdk",
        ),
        "codex": BackgroundTitleGeneratorSpec(
            "omnigent.runner.background_titles.sdk:generate_background_title"
        ),
        "codex-native": BackgroundTitleGeneratorSpec(
            "omnigent.runner.background_titles.codex_native:generate_background_title"
        ),
    },
    harness_labels={
        "antigravity": "Antigravity",
        "claude-sdk": "Claude SDK",
        "codex": "Codex",
        "copilot": "Copilot",
        "cursor": "Cursor",
        "hermes": "Hermes",
        # openai-agents is intentionally omitted from the picker catalog: it
        # stays a valid harness for YAML specs (and the credential-free
        # integration mock LLM), but is no longer offered as a UI pick.
        "pi": "Pi",
        **{name: row.label for name, row in ACP_CLI_HARNESSES.items()},
    },
    capabilities=_BUILTIN_CAPABILITIES,
)

_state: HarnessPluginState | None = None


def _entry_points() -> tuple[importlib.metadata.EntryPoint, ...]:
    discovered = importlib.metadata.entry_points()
    if hasattr(discovered, "select"):
        return tuple(discovered.select(group=COMMUNITY_ENTRY_POINT_GROUP))
    legacy = cast(
        Mapping[str, Iterable[importlib.metadata.EntryPoint]],
        discovered,
    )
    return tuple(legacy.get(COMMUNITY_ENTRY_POINT_GROUP, ()))


def _module_part(import_path: str) -> str:
    return import_path.split(":", 1)[0]


def _community_paths(contribution: HarnessContribution) -> list[str]:
    paths: list[str] = []
    paths.extend(contribution.harness_modules.values())
    paths.extend(contribution.spawn_env_builders.values())
    paths.extend(spec.generator for spec in contribution.background_title_generators.values())
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
        | set(contribution.background_title_generators)
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
    for harness, spec in contribution.background_title_generators.items():
        if harness not in allowed_targets:
            return (
                f"community harness plugin {entry_point_name!r} registers background "
                f"titles for undeclared harness {harness!r}"
            )
        if spec.resolver_harness is not None and spec.resolver_harness not in allowed_targets:
            return (
                f"community harness plugin {entry_point_name!r} background title resolver "
                f"{spec.resolver_harness!r} is not contributed by the plugin"
            )
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


_Value = TypeVar("_Value")


def _merge_dict(
    getter: Callable[[HarnessContribution], Mapping[str, _Value]],
) -> dict[str, _Value]:
    merged: dict[str, _Value] = {}
    for contribution in plugin_state().contributions:
        merged.update(getter(contribution))
    return merged


def _merge_set(
    getter: Callable[[HarnessContribution], Iterable[str]],
) -> frozenset[str]:
    merged: set[str] = set()
    for contribution in plugin_state().contributions:
        merged.update(getter(contribution))
    return frozenset(merged)


def valid_harnesses() -> frozenset[str]:
    """Return canonical harness ids accepted by installed contributions."""
    return _merge_set(lambda contribution: contribution.valid_harnesses)


def harness_aliases() -> dict[str, str]:
    """Return alias-to-canonical harness ids."""
    return _merge_dict(lambda contribution: contribution.aliases)


def accepted_harnesses() -> frozenset[str]:
    """Return canonical harness ids plus accepted aliases."""
    return valid_harnesses() | frozenset(harness_aliases())


def native_harnesses() -> frozenset[str]:
    """Return native CLI harness ids and native aliases."""
    return _merge_set(lambda contribution: contribution.native_harnesses)


def native_agents() -> tuple[NativeCodingAgent, ...]:
    """Return native coding-agent metadata rows."""
    agents: list[NativeCodingAgent] = []
    for contribution in plugin_state().contributions:
        agents.extend(contribution.native_agents)
    return tuple(agents)


def native_providers() -> tuple[NativeHarnessProvider, ...]:
    """Return native-harness behavior provider rows, merged across contributions."""
    providers: list[NativeHarnessProvider] = []
    for contribution in plugin_state().contributions:
        providers.extend(contribution.native_providers)
    return tuple(providers)


def native_provider_for_key(key: str) -> NativeHarnessProvider | None:
    """Return the provider row whose ``key`` matches, or ``None``."""
    for provider in native_providers():
        if provider.key == key:
            return provider
    return None


def harness_modules() -> dict[str, str]:
    """Return runtime harness module mapping, aliases included."""
    modules = _merge_dict(lambda contribution: contribution.harness_modules)
    for alias, canonical in harness_aliases().items():
        module = modules.get(canonical)
        if module is not None:
            modules.setdefault(alias, module)
    return modules


def model_env_keys() -> dict[str, str]:
    """Return harness-to-model-env-var mapping."""
    return _merge_dict(lambda contribution: contribution.model_env_keys)


def spawn_env_builders() -> dict[str, str]:
    """Return harness-to-spawn-env-builder import paths."""
    return _merge_dict(lambda contribution: contribution.spawn_env_builders)


def background_title_generators() -> dict[str, BackgroundTitleGeneratorSpec]:
    """Return harness-to-background-title-generator registrations."""
    return _merge_dict(lambda contribution: contribution.background_title_generators)


def install_specs() -> dict[str, HarnessInstallSpec]:
    """Return plugin-provided install specs."""
    return _merge_dict(lambda contribution: contribution.install_specs)


def harness_install_keys() -> dict[str, str]:
    """Return harness/alias to install-spec key mappings."""
    return _merge_dict(lambda contribution: contribution.harness_install_keys)


def missing_install_packages() -> dict[str, str]:
    """Return optional harness spellings to package names."""
    return _merge_dict(lambda contribution: contribution.missing_install_package)


def harness_labels() -> dict[str, str]:
    """Return labels for non-native harness picker/catalog rows."""
    return _merge_dict(lambda contribution: contribution.harness_labels)


def harness_capabilities() -> dict[str, HarnessCapabilities]:
    """Return the declared capability record per harness id.

    Merged across contributions; sparse (a harness need not declare
    capabilities). This is the single source of truth for "what can this
    harness do?".
    """
    return _merge_dict(lambda contribution: contribution.capabilities)


def harness_catalog() -> list[dict[str, object]]:
    """Return stable JSON-serializable harness catalog rows.

    Each row carries ``id`` and ``label``; rows for harnesses with declared
    capabilities also carry a ``capabilities`` object (see
    :meth:`HarnessCapabilities.as_dict`). ``setup_steps`` lists the ordered
    requirements to get the harness ready on a host (install + auth), so the
    web UI can render a "set up this agent" checklist that mirrors
    ``omnigent setup``; the host reports each step's status in its readiness map.
    """
    labels = harness_labels()
    capabilities = harness_capabilities()
    # Lazy import for the same reason as the acp rows below: keep this registry
    # importable without pulling in the onboarding/config stack at module load.
    try:
        from omnigent.onboarding.harness_install import ui_setup_steps
    except Exception:  # noqa: BLE001 — a broken onboarding import must not break the catalog
        _logger.debug("setup-step metadata unavailable", exc_info=True)
        ui_setup_steps = None  # type: ignore[assignment]
    rows: list[dict[str, object]] = []
    for harness in sorted(labels, key=lambda key: labels[key].lower()):
        if harness not in valid_harnesses():
            continue
        row: dict[str, object] = {"id": harness, "label": labels[harness]}
        capability = capabilities.get(harness)
        if capability is not None:
            row["capabilities"] = capability.as_dict()
        if ui_setup_steps is not None:
            row["setup_steps"] = [step.as_dict() for step in ui_setup_steps(harness)]
        rows.append(row)

    # Dynamic rows: one per user-configured generic-ACP agent, id ``acp:<slug>``.
    # The base ``acp`` harness deliberately has no ``harness_labels`` entry, so it
    # is not a standalone picker row — only the configured agents surface. Read
    # lazily so importing this registry never pulls in the onboarding/config
    # stack, and never let a malformed ``acp:`` block break the whole catalog.
    acp_capability = capabilities.get("acp")
    try:
        from omnigent.onboarding.acp_auth import acp_agents

        for agent in acp_agents():
            acp_row: dict[str, object] = {"id": f"acp:{agent.slug}", "label": agent.name}
            if acp_capability is not None:
                acp_row["capabilities"] = acp_capability.as_dict()
            rows.append(acp_row)
    except Exception:  # noqa: BLE001 — a malformed acp: block must never break the catalog
        _logger.debug("acp catalog rows skipped", exc_info=True)
    return rows


def harness_setup_steps_by_spelling() -> dict[str, list[dict[str, str | None]]]:
    """Map every harness spelling to its ordered UI setup steps.

    The web setup dialog looks steps up by the harness a *session* declares —
    which is often a native wrapper (``codex-native``) or an installable id
    that is not a picker row (``opencode``/``qwen``), neither of which appears
    in :func:`harness_catalog`. Keying by spelling here lets the dialog resolve
    steps for whatever id it holds. Values mirror ``harness_catalog``'s
    ``setup_steps`` (same :func:`ui_setup_steps` source), so the two can't
    drift.

    :returns: ``{spelling: [step.as_dict(), ...]}`` for every accepted spelling;
        empty when the onboarding stack can't be imported (fail-open).
    """
    try:
        from omnigent.onboarding.harness_install import ui_installable_harnesses, ui_setup_steps
    except Exception:  # noqa: BLE001 — a broken onboarding import must not break the catalog
        _logger.debug("setup-step metadata unavailable", exc_info=True)
        return {}
    # Cover the picker ids (catalog rows) plus every installable spelling
    # (bare + native), so a session's declared harness always resolves.
    spellings: set[str] = set(valid_harnesses())
    spellings.update(ui_installable_harnesses())
    return {
        spelling: [step.as_dict() for step in ui_setup_steps(spelling)] for spelling in spellings
    }


def load_object(import_path: str) -> object:
    """Load ``module:attribute`` or ``module.attribute``."""
    if ":" in import_path:
        module_name, attr = import_path.split(":", 1)
    else:
        module_name, attr = import_path.rsplit(".", 1)
    module = importlib.import_module(module_name)
    return getattr(module, attr)
