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

    NONE = "none"  # prior conversations cannot be resumed
    WARM_REATTACH = "warm-reattach"  # reattach to a live vendor session / terminal
    COLD_ONLY = "cold-only"  # rebuild from Omnigent transcript / history replay


class PauseSemantics(str, Enum):
    """What a Pause (interrupt) actually does to the vendor agent.

    Published so a client can tell, *before* pressing Pause, whether the work
    in flight survives. No harness freezes a turn mid-step today, so nothing
    declares ``SUSPENDS``; the value exists because the wire contract is
    public and a future harness may earn it.
    """

    SUSPENDS = "suspends"  # the turn freezes in place and can continue later
    ENDS_RUN = "ends-run"  # the turn is stopped and abandoned; the work is lost
    UNSUPPORTED = "unsupported"  # Pause does not reach the vendor agent at all


class PauseResume(str, Enum):
    """What a turn sent *after* a Pause continues.

    ``SAME_THREAD`` means the vendor keeps the conversation it already had, so
    the next turn lands in it. ``NEW_TURN`` means the vendor session was
    dropped and the next turn rebuilds it from the Omnigent transcript — the
    conversation is preserved, the vendor's own thread is not.
    """

    SAME_THREAD = "same-thread"
    NEW_TURN = "new-turn"
    UNSUPPORTED = "unsupported"


class EffortFamily(str, Enum):
    """Which reasoning-effort value set applies (see reasoning_effort.py)."""

    NONE = "none"
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GEMINI = "gemini"
    COPILOT = "copilot"
    PI = "pi"
    # The real codex process is the per-model authority on reasoning levels
    # (Sol reaches ``ultra``); the SDK codex wire caps at ``xhigh``.
    CODEX_NATIVE = "codex-native"


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


class ForkHistory(str, Enum):
    """How a fork (or in-place agent switch) carries prior history into the harness."""

    NONE = "none"  # fork launches fresh; no prior turns are carried
    REBUILD = "rebuild"  # rebuild the vendor's resumable session file from copied items
    PREAMBLE = "preamble"  # replay prior turns as a text preamble (server-backed vendors)


class InstructionDelivery(str, Enum):
    """Whether and how ``AgentSpec.instructions`` reach the vendor agent.

    See ``docs/AGENT_YAML_SPEC.md`` for the full per-harness matrix and the
    lifecycle meaning of each value.
    """

    COMPOSED_PER_TURN = "composed-per-turn"
    COMPOSED_SESSION_SNAPSHOT = "composed-session-snapshot"
    AGENT_STARTUP_ADDITIVE = "agent-startup-additive"
    FIRST_USER_PREFIX = "first-user-prefix"
    NOT_DELIVERED = "not-delivered"
    UNKNOWN = "unknown"


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
    :param pause: What Pause does to the vendor agent — ``ends-run`` for every
        harness that honors an interrupt today, ``unsupported`` where the
        interrupt never reaches the vendor. Always ``unsupported`` when
        *interrupt* is ``False``.
    :param resume_after_pause: What the turn after a Pause continues —
        ``same-thread`` when the vendor keeps its own conversation across the
        interrupt, ``new-turn`` when the vendor session is dropped and rebuilt
        from the Omnigent transcript.
    :param cancel: Whether Cancel is supported. ``True`` for every harness:
        Omnigent's own turn teardown records the cancellation and its cause
        even when the vendor ignores the interrupt.
    :param streaming: Whether the harness forwards token-level deltas (vs a
        single complete blob). Declared claim; verified by the bench's
        streaming probe.
    :param steering: Whether input can be added to an active turn.
    :param live_queue: Whether follow-up input can be queued during an active
        turn.
    :param images: Whether the harness accepts image input.
    :param compaction: Whether the harness can compact conversation history.
        Optional capability fields use ``None`` when the harness makes no claim;
        the bench reports those declarations as ``UNKNOWN`` rather than assuming
        the capability is unsupported.
    :param fork_history: How a fork / in-place agent switch carries prior history
        into the harness — ``none`` (fresh), ``rebuild`` (rebuild the vendor's
        resumable session file from copied items), or ``preamble`` (replay prior
        turns as a text preamble). Drives the server's fork-history gating.
    :param shell_tool_name: The harness's shell/exec tool name the harness bench
        provokes to verify tool-calling (e.g. ``"Bash"``, ``"shell"``). ``None``
        skips the bench's tool/policy probe for this harness.
    :param shell_tool_prompt: The prompt the bench sends to provoke that tool.
        Must contain the ``omnigent-bench-ok`` placeholder the probe token-swaps.
        ``None`` skips the probe.
    :param instruction_delivery: Whether and how ``AgentSpec.instructions``
        reach the vendor agent. Defaults to ``UNKNOWN`` for undeclared/
        third-party harnesses.
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
    pause: PauseSemantics = PauseSemantics.ENDS_RUN
    resume_after_pause: PauseResume = PauseResume.NEW_TURN
    cancel: bool = True
    steering: bool | None = None
    live_queue: bool | None = None
    images: bool | None = None
    compaction: bool | None = None
    fork_history: ForkHistory = ForkHistory.NONE
    shell_tool_name: str | None = None
    shell_tool_prompt: str | None = None
    instruction_delivery: InstructionDelivery = InstructionDelivery.UNKNOWN

    def as_dict(self) -> dict[str, str | bool | None]:
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
            "pause": self.pause.value,
            "resume_after_pause": self.resume_after_pause.value,
            "cancel": self.cancel,
            "steering": self.steering,
            "live_queue": self.live_queue,
            "images": self.images,
            "compaction": self.compaction,
            "fork_history": self.fork_history.value,
            "shell_tool_name": self.shell_tool_name,
            "shell_tool_prompt": self.shell_tool_prompt,
            "instruction_delivery": self.instruction_delivery.value,
        }
