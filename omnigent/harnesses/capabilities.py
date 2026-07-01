"""Declarative capability model for coding-agent harnesses.

This is the single place that answers "what can harness X do?" — the data that
was previously implicit, scattered across ``if harness == "x"`` branches and the
presence/absence of companion modules (``codex_native_elicitation.py``,
``*_native_hook.py``, ``*_native_permissions.py``, ...).

Each value here is backed by the code that implements it; where a value is
*derivable* from an existing constant (``model_family`` from
``model_override``'s family sets, ``subagents`` from ``NativeCodingAgent``'s
``subagent_wrapper_label``) the accompanying test asserts the declaration
matches that source, so the table cannot silently drift. The remaining axes
(``integration_mode``, ``elicitation``, ``resume``, ``effort``, ``auth``) are
declared from the module-level evidence cited inline.

Aligned with the axes in the ``harness-integration-guide`` skill's feature
matrix.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class IntegrationMode(str, Enum):
    """How the harness runs the vendor agent."""

    SDK_IN_PROCESS = "sdk-in-process"  # vendor SDK inside the harness subprocess
    CLI_SUBPROCESS = "cli-subprocess"  # drives a vendor CLI per turn
    ACP_SUBPROCESS = "acp-subprocess"  # vendor CLI in Agent Client Protocol mode
    NATIVE_TUI = "native-tui"  # wraps a resident vendor TUI (tmux / file-inject)
    NATIVE_SERVER = "native-server"  # runner-owned vendor server + HTTP/SSE bridge


class Elicitation(str, Enum):
    """How a policy ASK / tool-approval is surfaced to the Omnigent web UI."""

    NONE = "none"
    HOOK = "hook"  # vendor PreToolUse hook posts to Omnigent
    JSONRPC = "jsonrpc"  # app-server JSON-RPC elicitation (codex)
    APPROVAL_MIRROR = "approval-mirror"  # poll the TUI approval pane, mirror to web
    SSE_PERMISSION = "sse-permission"  # permission events over SSE / ACP elicit


class Resume(str, Enum):
    """Whether a prior conversation is reattached or rebuilt."""

    WARM_REATTACH = "warm-reattach"  # reattach to a live vendor session / terminal
    COLD_ONLY = "cold-only"  # rebuild from Omnigent transcript / history replay


class EffortFamily(str, Enum):
    """Which reasoning-effort value set applies (see reasoning_effort.py)."""

    NONE = "none"
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GEMINI = "gemini"
    COPILOT = "copilot"


class ModelFamily(str, Enum):
    """Which model vendors the harness accepts (see model_override.py)."""

    CLAUDE = "claude"
    GPT = "gpt"
    GEMINI = "gemini"
    MULTI = "multi"  # accepts any validated id (no family rejection)


class AuthModel(str, Enum):
    """Where the harness's credentials come from."""

    OMNIGENT_CREDENTIAL = "omnigent-credential"  # Omnigent gateway / provider config
    OWN_AUTH = "own-auth"  # vendor login / API key, not Omnigent-managed
    SESSION_SCOPED_CONFIG = "session-scoped-config"  # per-session synthesized vendor config


@dataclass(frozen=True)
class HarnessCapabilities:
    """The feature set one harness supports. See module docstring."""

    integration_mode: IntegrationMode
    elicitation: Elicitation
    resume: Resume
    effort: EffortFamily
    model_family: ModelFamily
    auth: AuthModel
    subagents: bool


# Convenience aliases for the dense table below.
_M = IntegrationMode
_E = Elicitation
_R = Resume
_EF = EffortFamily
_MF = ModelFamily
_A = AuthModel


# Per canonical harness. Every value is backed by the evidence noted in the
# section comments; derivable values (model_family, subagents) are additionally
# asserted against their source in tests/test_harness_registry.py.
_CAPABILITIES: dict[str, HarnessCapabilities] = {
    # ── Native-CLI harnesses (wrap a resident vendor TUI/server) ──────────
    "claude-native": HarnessCapabilities(
        _M.NATIVE_TUI,
        _E.HOOK,
        _R.WARM_REATTACH,
        _EF.ANTHROPIC,
        _MF.CLAUDE,
        _A.OMNIGENT_CREDENTIAL,
        subagents=True,
    ),
    "codex-native": HarnessCapabilities(
        # hook deny-gate + app-server JSON-RPC elicitation; elicitation surface
        # to the web UI is the JSON-RPC path (codex_native_elicitation.py).
        _M.NATIVE_TUI,
        _E.JSONRPC,
        _R.WARM_REATTACH,
        _EF.OPENAI,
        _MF.GPT,
        _A.OMNIGENT_CREDENTIAL,
        subagents=True,
    ),
    "pi-native": HarnessCapabilities(
        _M.NATIVE_TUI,
        _E.NONE,
        _R.WARM_REATTACH,
        _EF.NONE,
        _MF.MULTI,
        _A.SESSION_SCOPED_CONFIG,
        subagents=False,
    ),
    "cursor-native": HarnessCapabilities(
        _M.NATIVE_TUI,
        _E.APPROVAL_MIRROR,
        _R.WARM_REATTACH,
        _EF.NONE,
        _MF.MULTI,
        _A.OWN_AUTH,
        subagents=False,
    ),
    "kiro-native": HarnessCapabilities(
        # kiro_native_permissions.py: "TUI ACP recorder -> web elicitation".
        _M.NATIVE_TUI,
        _E.APPROVAL_MIRROR,
        _R.WARM_REATTACH,
        _EF.NONE,
        _MF.MULTI,
        _A.OWN_AUTH,
        subagents=False,
    ),
    "antigravity-native": HarnessCapabilities(
        _M.NATIVE_TUI,
        _E.NONE,
        _R.WARM_REATTACH,
        _EF.GEMINI,
        _MF.GEMINI,
        _A.OWN_AUTH,
        subagents=False,
    ),
    "goose-native": HarnessCapabilities(
        _M.NATIVE_TUI,
        _E.APPROVAL_MIRROR,
        _R.WARM_REATTACH,
        _EF.NONE,
        _MF.MULTI,
        _A.OWN_AUTH,
        subagents=False,
    ),
    "qwen-native": HarnessCapabilities(
        _M.NATIVE_TUI,
        _E.APPROVAL_MIRROR,
        _R.WARM_REATTACH,
        _EF.NONE,
        _MF.MULTI,
        _A.OWN_AUTH,
        subagents=False,
    ),
    "kimi-native": HarnessCapabilities(
        _M.NATIVE_TUI,
        _E.HOOK,
        _R.WARM_REATTACH,
        _EF.NONE,
        _MF.MULTI,
        _A.SESSION_SCOPED_CONFIG,
        subagents=False,
    ),
    "opencode-native": HarnessCapabilities(
        _M.NATIVE_SERVER,
        _E.SSE_PERMISSION,
        _R.WARM_REATTACH,
        _EF.NONE,
        _MF.MULTI,
        _A.OWN_AUTH,
        subagents=True,
    ),
    "hermes-native": HarnessCapabilities(
        _M.NATIVE_TUI,
        _E.APPROVAL_MIRROR,
        _R.WARM_REATTACH,
        _EF.NONE,
        _MF.MULTI,
        _A.OWN_AUTH,
        subagents=False,
    ),
    # ── SDK / subprocess harnesses (run the vendor model directly) ────────
    "claude-sdk": HarnessCapabilities(
        _M.SDK_IN_PROCESS,
        _E.NONE,
        _R.COLD_ONLY,
        _EF.ANTHROPIC,
        _MF.CLAUDE,
        _A.OMNIGENT_CREDENTIAL,
        subagents=False,
    ),
    "codex": HarnessCapabilities(
        # keeps one long-lived ``codex app-server`` per session; elicitation via
        # app-server JSON-RPC (server/routes/_codex_elicitation.py).
        _M.CLI_SUBPROCESS,
        _E.JSONRPC,
        _R.WARM_REATTACH,
        _EF.OPENAI,
        _MF.GPT,
        _A.OMNIGENT_CREDENTIAL,
        subagents=False,
    ),
    "pi": HarnessCapabilities(
        _M.CLI_SUBPROCESS,
        _E.NONE,
        _R.COLD_ONLY,
        _EF.NONE,
        _MF.MULTI,
        _A.OMNIGENT_CREDENTIAL,
        subagents=False,
    ),
    "openai-agents": HarnessCapabilities(
        _M.SDK_IN_PROCESS,
        _E.NONE,
        _R.COLD_ONLY,
        _EF.OPENAI,
        _MF.MULTI,
        _A.OMNIGENT_CREDENTIAL,
        subagents=False,
    ),
    "cursor": HarnessCapabilities(
        _M.SDK_IN_PROCESS,
        _E.NONE,
        _R.WARM_REATTACH,
        _EF.NONE,
        _MF.MULTI,
        _A.OWN_AUTH,
        subagents=False,
    ),
    "antigravity": HarnessCapabilities(
        _M.SDK_IN_PROCESS,
        _E.NONE,
        _R.COLD_ONLY,
        _EF.GEMINI,
        _MF.GEMINI,
        _A.OWN_AUTH,
        subagents=False,
    ),
    "goose": HarnessCapabilities(
        _M.ACP_SUBPROCESS,
        _E.SSE_PERMISSION,
        _R.COLD_ONLY,
        _EF.NONE,
        _MF.MULTI,
        _A.OWN_AUTH,
        subagents=False,
    ),
    "qwen": HarnessCapabilities(
        _M.ACP_SUBPROCESS,
        _E.SSE_PERMISSION,
        _R.COLD_ONLY,
        _EF.NONE,
        _MF.MULTI,
        _A.OWN_AUTH,
        subagents=False,
    ),
    "kimi": HarnessCapabilities(
        _M.CLI_SUBPROCESS,
        _E.NONE,
        _R.WARM_REATTACH,
        _EF.NONE,
        _MF.MULTI,
        _A.SESSION_SCOPED_CONFIG,
        subagents=False,
    ),
    "hermes": HarnessCapabilities(
        _M.CLI_SUBPROCESS,
        _E.HOOK,
        _R.COLD_ONLY,
        _EF.NONE,
        _MF.MULTI,
        _A.OWN_AUTH,
        subagents=False,
    ),
    "copilot": HarnessCapabilities(
        _M.SDK_IN_PROCESS,
        _E.NONE,
        _R.COLD_ONLY,
        _EF.COPILOT,
        _MF.MULTI,
        _A.OWN_AUTH,
        subagents=False,
    ),
    # ``open-responses`` is resolved through an alternate path (no
    # _HARNESS_MODULES entry). Conservative low-confidence defaults; refine when
    # the harness is folded into the registry proper.
    "open-responses": HarnessCapabilities(
        _M.SDK_IN_PROCESS,
        _E.NONE,
        _R.COLD_ONLY,
        _EF.NONE,
        _MF.MULTI,
        _A.OMNIGENT_CREDENTIAL,
        subagents=False,
    ),
}


def capabilities_for(name: str) -> HarnessCapabilities | None:
    """Return the declared capabilities for a canonical harness name."""
    return _CAPABILITIES.get(name)
