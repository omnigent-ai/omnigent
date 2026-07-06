"""Import-safe declarative capability model for harness plugins.

A harness's feature support was previously implicit — scattered across
``if harness == "x"`` branches and the presence/absence of companion modules
(``codex_native_elicitation.py``, ``*_native_hook.py``, ``*_native_permissions.py``).
This module gives it one declared shape so the registry can answer "what can
this harness do?" directly.

Like :mod:`omnigent.harness_install_spec`, this type lives outside the
onboarding/provider stack so an optional harness plugin can declare its
capabilities during entry-point discovery without triggering import cycles.
Each :class:`~omnigent.harness_plugins.HarnessContribution` carries a
per-harness ``capabilities`` map of these records.

The axes align with the harness-integration-guide feature matrix.
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
    """The declared feature set one harness supports.

    :param integration_mode: How the harness runs the vendor agent.
    :param elicitation: How a policy ASK is surfaced to the web UI.
    :param resume: Whether a prior conversation is reattached or rebuilt.
    :param effort: Which reasoning-effort value set applies.
    :param model_family: Which model vendors the harness accepts.
    :param auth: Where the harness's credentials come from.
    :param subagents: Whether the harness can spawn Omnigent sub-agents.
    :param interrupt: Whether a running turn can be cancelled mid-stream. This
        is a *declared* claim; the harness bench's interrupt probe verifies it
        live and flags drift when a harness does not honor it.
    :param streaming: Whether the harness forwards token-level deltas (vs a
        single complete blob). Declared claim; verified by the bench's
        streaming probe.
    """

    integration_mode: IntegrationMode
    elicitation: Elicitation
    resume: Resume
    effort: EffortFamily
    model_family: ModelFamily
    auth: AuthModel
    subagents: bool
    interrupt: bool
    streaming: bool

    def as_dict(self) -> dict[str, str | bool]:
        """Return a JSON-serializable view for the ``/v1/harnesses`` catalog."""
        return {
            "integration_mode": self.integration_mode.value,
            "elicitation": self.elicitation.value,
            "resume": self.resume.value,
            "effort": self.effort.value,
            "model_family": self.model_family.value,
            "auth": self.auth.value,
            "subagents": self.subagents,
            "interrupt": self.interrupt,
            "streaming": self.streaming,
        }
