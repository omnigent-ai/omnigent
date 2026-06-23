"""Single-registration harness descriptors.

Adding a harness historically meant editing ~6 scattered registries
(runtime module map, spec allowlist, aliases, native set, model-override
set, install metadata, web registry). :class:`HarnessDescriptor` is the
one place a harness is declared; the scattered registries become *derived
views* of :data:`HARNESS_DESCRIPTORS`, and the conformance suite
(``tests/harness_conformance``) asserts every view agrees with the
descriptors so they can never drift again.

This module is pure data — it imports nothing from the rest of Omnigent,
so any registry module can import it without a cycle.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

HarnessFamily = Literal[
    "sdk",
    "native-server",
    "native-terminal",
    "headless-native",
    "legacy",
]

ModelIdFormat = Literal["omnigent", "provider-slash-model", "native"]

# Families that count as "native CLI" harnesses for is_native_harness().
_NATIVE_FAMILIES: frozenset[str] = frozenset({"native-server", "native-terminal"})


@dataclass(frozen=True)
class HarnessDescriptor:
    """
    Declarative description of one Omnigent harness.

    Fields cover identity, runtime registration, capabilities, model
    behavior, CLI install/readiness, and native-UI/web metadata so every
    scattered registry can be derived. See module docstring.

    :param id: Canonical harness id, e.g. ``"opencode-native"``.
    :param display_name: Human label, e.g. ``"OpenCode"``.
    :param module: Module exporting ``create_app()`` for the runtime
        harness process.
    :param family: Harness family.
    :param aliases: User-facing alias spellings that canonicalize to *id*.
    :param runtime_registered: Whether *id* appears in ``_HARNESS_MODULES``
        (the per-conversation FastAPI registry). ``open-responses`` is
        spec-valid but routed through the openai-agents adapter, so it is
        ``False``.
    :param runtime_aliases: Aliases that ALSO appear as keys in
        ``_HARNESS_MODULES`` (only ``claude`` today).
    :param native_aliases: Reversed ``native-<x>`` spellings recognized by
        :func:`is_native_harness` (e.g. ``native-codex``).
    :param supports_model_override: Whether a per-session model override
        reaches the harness process.
    :param supports_interrupt: Whether the harness supports abort.
    :param supports_enqueue: Whether mid-turn enqueue works.
    :param supports_terminal_takeover: Whether a TUI can attach.
    :param supports_resume: Whether resume by external session id works.
    :param supports_fork: Whether fork is supported.
    :param supports_permissions: Whether the harness has a permission API.
    :param model_id_format: Expected model-id format.
    :param cli_binary: CLI executable name required on PATH (CLI-backed
        harnesses only).
    :param npm_package: npm package providing *cli_binary*.
    :param install_hint: Non-npm install command, when applicable.
    :param install_family_key: Key into ``_HARNESS_NAME_TO_KEY`` /
        ``_HARNESS_INSTALL`` for CLI-backed harnesses.
    :param wrapper_agent_name: Built-in native-UI wrapper agent name.
    :param wrapper_label: ``omnigent.wrapper`` label value.
    :param terminal_name: Runner terminal name for the native TUI.
    :param web_icon_kind: ap-web native icon kind.
    :param web_sort_rank: ap-web native picker sort rank.
    :param web_capabilities: ap-web native capabilities.
    :param terminal_first: Whether the web UI treats this as terminal-first.
    :param transport_kind: Native transport tag (``ws-jsonrpc`` /
        ``http-sse``) for native-server harnesses.
    :param openapi_schema: Vendored OpenAPI fixture name, when applicable.
    :param min_cli_version: Inclusive minimum supported CLI version.
    :param max_cli_version_exclusive: Exclusive maximum CLI version.
    :param description: One-line description.
    :param metadata: Free-form extensibility metadata.
    """

    # Identity
    id: str
    display_name: str
    module: str
    family: HarnessFamily = "sdk"
    aliases: tuple[str, ...] = ()
    description: str | None = None

    # Runtime registration
    runtime_registered: bool = True
    runtime_aliases: tuple[str, ...] = ()
    native_aliases: tuple[str, ...] = ()

    # Capabilities
    supports_streaming: bool = True
    supports_tool_calling: bool = True
    handles_tools_internally: bool = False
    supports_interrupt: bool = False
    supports_enqueue: bool = False
    supports_terminal_takeover: bool = False
    supports_resume: bool = False
    supports_fork: bool = False
    supports_permissions: bool = False

    # Model behavior
    supports_model_override: bool = False
    model_id_format: ModelIdFormat = "omnigent"
    supported_model_families: tuple[str, ...] = ()
    default_model: str | None = None

    # CLI install / readiness
    cli_binary: str | None = None
    npm_package: str | None = None
    install_hint: str | None = None
    install_family_key: str | None = None
    min_cli_version: str | None = None
    max_cli_version_exclusive: str | None = None

    # Native UI / web
    wrapper_agent_name: str | None = None
    wrapper_label: str | None = None
    terminal_name: str | None = None
    web_icon_kind: str | None = None
    web_sort_rank: int | None = None
    web_capabilities: tuple[str, ...] = ()
    terminal_first: bool = False

    # Native server transport
    transport_kind: str | None = None
    openapi_schema: str | None = None

    metadata: Mapping[str, object] = field(default_factory=dict)

    @property
    def is_native(self) -> bool:
        """:returns: Whether this is a native CLI harness."""
        return self.family in _NATIVE_FAMILIES


HARNESS_DESCRIPTORS: dict[str, HarnessDescriptor] = {
    "claude-sdk": HarnessDescriptor(
        id="claude-sdk",
        display_name="Claude",
        module="omnigent.inner.claude_sdk_harness",
        family="sdk",
        aliases=("claude",),
        runtime_aliases=("claude",),
        supports_model_override=True,
        supported_model_families=("claude",),
        description="In-process Claude SDK harness.",
    ),
    "claude-native": HarnessDescriptor(
        id="claude-native",
        display_name="Claude",
        module="omnigent.inner.claude_native_harness",
        family="native-terminal",
        native_aliases=("native-claude",),
        handles_tools_internally=True,
        supports_interrupt=True,
        supports_enqueue=True,
        supports_terminal_takeover=True,
        supports_resume=True,
        supports_permissions=True,
        supports_model_override=True,
        supported_model_families=("claude",),
        cli_binary="claude",
        npm_package="@anthropic-ai/claude-code",
        install_family_key="anthropic",
        wrapper_agent_name="claude-native-ui",
        wrapper_label="claude-code-native-ui",
        terminal_name="claude",
        web_icon_kind="claude",
        web_sort_rank=10,
        web_capabilities=("approvalMode",),
        terminal_first=True,
        description="Native Claude Code TUI bridge.",
    ),
    "codex": HarnessDescriptor(
        id="codex",
        display_name="Codex",
        module="omnigent.inner.codex_harness",
        family="sdk",
        supports_model_override=True,
        supported_model_families=("codex",),
        description="In-process Codex harness.",
    ),
    "codex-native": HarnessDescriptor(
        id="codex-native",
        display_name="Codex",
        module="omnigent.inner.codex_native_harness",
        family="native-server",
        native_aliases=("native-codex",),
        handles_tools_internally=True,
        supports_interrupt=True,
        supports_enqueue=True,
        supports_terminal_takeover=True,
        supports_resume=True,
        supports_fork=True,
        supports_permissions=True,
        supports_model_override=True,
        supported_model_families=("codex",),
        cli_binary="codex",
        npm_package="@openai/codex",
        install_family_key="openai",
        wrapper_agent_name="codex-native-ui",
        wrapper_label="codex-native-ui",
        terminal_name="codex",
        web_icon_kind="codex",
        web_sort_rank=20,
        web_capabilities=("approvalMode",),
        terminal_first=True,
        transport_kind="ws-jsonrpc",
        description="Native Codex app-server bridge (WS JSON-RPC).",
    ),
    "opencode-native": HarnessDescriptor(
        id="opencode-native",
        display_name="OpenCode",
        module="omnigent.inner.opencode_native_harness",
        family="native-server",
        # ``opencode`` is accepted as a friendly alias for the canonical
        # ``opencode-native`` (there is no separate SDK ``opencode`` harness, so
        # the bare name is free); ``native-opencode`` is the reversed spelling.
        aliases=("native-opencode", "opencode"),
        runtime_aliases=("opencode",),
        native_aliases=("native-opencode",),
        handles_tools_internally=True,
        supports_interrupt=True,
        supports_enqueue=True,
        supports_terminal_takeover=True,
        supports_resume=True,
        supports_fork=True,
        supports_permissions=True,
        supports_model_override=True,
        model_id_format="provider-slash-model",
        cli_binary="opencode",
        npm_package="opencode-ai",
        install_family_key="opencode",
        min_cli_version="1.17.7",
        max_cli_version_exclusive="1.18.0",
        wrapper_agent_name="opencode-native-ui",
        wrapper_label="opencode-native-ui",
        terminal_name="opencode",
        web_icon_kind="opencode",
        web_sort_rank=25,
        web_capabilities=("approvalMode",),
        terminal_first=True,
        transport_kind="http-sse",
        # No vendored OpenAPI fixture: the typed client is hand-maintained and
        # the live wire-contract e2e (test_opencode_native_wire_contract_e2e)
        # validates it against a real `opencode serve` — a far better drift
        # guard than a 35k-line checked-in schema dump.
        description="Native OpenCode server bridge (HTTP + SSE).",
    ),
    "pi": HarnessDescriptor(
        id="pi",
        display_name="Pi",
        module="omnigent.inner.pi_harness",
        family="headless-native",
        supports_model_override=True,
        cli_binary="pi",
        npm_package="@earendil-works/pi-coding-agent",
        install_family_key="pi",
        description="Pi coding agent harness.",
    ),
    "pi-native": HarnessDescriptor(
        id="pi-native",
        display_name="Pi",
        module="omnigent.inner.pi_native_harness",
        family="native-terminal",
        aliases=("native-pi",),
        native_aliases=("native-pi",),
        handles_tools_internally=True,
        supports_terminal_takeover=True,
        supports_resume=True,
        supports_model_override=True,
        cli_binary="pi",
        npm_package="@earendil-works/pi-coding-agent",
        install_family_key="pi",
        wrapper_agent_name="pi-native-ui",
        wrapper_label="pi-native-ui",
        terminal_name="pi",
        web_icon_kind="pi",
        web_sort_rank=30,
        terminal_first=True,
        description="Native Pi TUI bridge.",
    ),
    "openai-agents": HarnessDescriptor(
        id="openai-agents",
        display_name="OpenAI Agents",
        module="omnigent.inner.openai_agents_sdk_harness",
        family="sdk",
        aliases=("openai-agents-sdk",),
        supports_model_override=True,
        description="In-process OpenAI Agents SDK harness.",
    ),
    "open-responses": HarnessDescriptor(
        id="open-responses",
        display_name="OpenResponses",
        module="omnigent.inner.open_responses_sdk",
        family="sdk",
        runtime_registered=False,
        supports_model_override=False,
        description="OpenAI Responses-API harness (routed via the agents adapter).",
    ),
    "cursor": HarnessDescriptor(
        id="cursor",
        display_name="Cursor",
        module="omnigent.inner.cursor_harness",
        family="sdk",
        supports_model_override=True,
        description="In-process Cursor SDK harness.",
    ),
    "cursor-native": HarnessDescriptor(
        id="cursor-native",
        display_name="Cursor",
        module="omnigent.inner.cursor_native_harness",
        family="native-terminal",
        native_aliases=("native-cursor",),
        handles_tools_internally=True,
        supports_enqueue=True,
        supports_terminal_takeover=True,
        # Native CLI override reaches the TUI at launch (is_native_harness →
        # harness_supports_model_override is True for any native harness).
        supports_model_override=True,
        # Native Cursor wraps the ``cursor-agent`` CLI, so it IS cli-backed and
        # is gated on that binary like claude-native / codex-native (matches the
        # readiness fix in #774). cursor-agent ships via a curl installer rather
        # than npm, hence ``install_hint`` not ``npm_package``. (Auth/login live
        # under the ``cursor`` family; cursor-agent still gates its own tools
        # inside the TUI, so Omnigent intercepts no permissions — no permission
        # API. See omnigent/inner/cursor_native_harness.py.)
        cli_binary="cursor-agent",
        install_family_key="cursor",
        install_hint="curl https://cursor.com/install -fsS | bash",
        wrapper_agent_name="cursor-native-ui",
        wrapper_label="cursor-native-ui",
        terminal_name="cursor",
        web_icon_kind="cursor",
        web_sort_rank=40,
        terminal_first=True,
        description="Native Cursor TUI bridge (tmux).",
    ),
    "antigravity": HarnessDescriptor(
        id="antigravity",
        display_name="Antigravity",
        module="omnigent.inner.antigravity_harness",
        family="sdk",
        aliases=("agy", "google-antigravity"),
        supports_model_override=True,
        description="In-process Google Antigravity SDK harness.",
    ),
    "qwen": HarnessDescriptor(
        id="qwen",
        display_name="Qwen Code",
        module="omnigent.inner.qwen_harness",
        family="sdk",
        aliases=("qwen-code",),
        supports_model_override=True,
        # CLI-backed (drives the ``qwen`` binary in ACP mode), gated on the
        # binary like the other CLI harnesses. Auth is via OpenAI-compatible
        # env vars / the interactive ``/auth`` command — no CLI login argv (see
        # the install spec note in onboarding/harness_install.py).
        cli_binary="qwen",
        npm_package="@qwen-code/qwen-code",
        install_family_key="qwen",
        description="Qwen Code CLI harness (ACP mode).",
    ),
    "goose": HarnessDescriptor(
        id="goose",
        display_name="Goose",
        module="omnigent.inner.goose_harness",
        family="sdk",
        supports_model_override=True,
        # Block's Goose CLI in headless ACP mode (``goose acp``). CLI-backed —
        # gated on the ``goose`` binary; Goose owns its own auth/provider config
        # (``goose configure``) and ships via brew, not npm.
        cli_binary="goose",
        install_hint="brew install block-goose-cli",
        install_family_key="goose",
        description="Headless Goose ACP harness (drives `goose acp`).",
    ),
    "goose-native": HarnessDescriptor(
        id="goose-native",
        display_name="Goose",
        module="omnigent.inner.goose_native_harness",
        family="native-terminal",
        aliases=("native-goose",),
        native_aliases=("native-goose",),
        handles_tools_internally=True,
        supports_enqueue=True,
        supports_terminal_takeover=True,
        supports_model_override=True,
        # Native ``goose session`` TUI bridge — wraps the same ``goose`` binary
        # as the headless harness, gated on it (brew install, no npm package).
        cli_binary="goose",
        install_hint="brew install block-goose-cli",
        install_family_key="goose",
        wrapper_agent_name="goose-native-ui",
        wrapper_label="goose-native-ui",
        terminal_name="goose",
        web_icon_kind="goose",
        web_sort_rank=50,
        terminal_first=True,
        description="Native Goose TUI bridge (goose session).",
    ),
}


def descriptor_for(harness: str | None) -> HarnessDescriptor | None:
    """
    Resolve a descriptor by canonical id or alias.

    :param harness: A harness id or alias, e.g. ``"native-opencode"``.
    :returns: The matching descriptor, or ``None``.
    """
    if harness is None:
        return None
    if harness in HARNESS_DESCRIPTORS:
        return HARNESS_DESCRIPTORS[harness]
    for descriptor in HARNESS_DESCRIPTORS.values():
        if harness in descriptor.aliases or harness in descriptor.native_aliases:
            return descriptor
    return None


def canonical_harness_ids() -> frozenset[str]:
    """:returns: The canonical harness id set (== spec allowlist)."""
    return frozenset(HARNESS_DESCRIPTORS)


def harness_alias_map() -> dict[str, str]:
    """:returns: ``{alias: canonical_id}`` for all user-facing aliases."""
    mapping: dict[str, str] = {}
    for descriptor in HARNESS_DESCRIPTORS.values():
        for alias in descriptor.aliases:
            mapping[alias] = descriptor.id
    return mapping


def all_harness_aliases() -> frozenset[str]:
    """:returns: The set of all user-facing alias spellings."""
    return frozenset(harness_alias_map())


def runtime_module_map() -> dict[str, str]:
    """
    Build the runtime harness→module map (``_HARNESS_MODULES``).

    Includes runtime-registered canonical ids plus the runtime aliases
    that historically appear as keys (``claude``).

    :returns: ``{harness_or_alias: module_path}``.
    """
    mapping: dict[str, str] = {}
    for descriptor in HARNESS_DESCRIPTORS.values():
        if not descriptor.runtime_registered:
            continue
        mapping[descriptor.id] = descriptor.module
        for alias in descriptor.runtime_aliases:
            mapping[alias] = descriptor.module
    return mapping


def native_harness_ids() -> frozenset[str]:
    """
    Build the native-harness recognition set (``NATIVE_HARNESSES``).

    Includes native canonical ids plus their reversed ``native-<x>``
    spellings.

    :returns: The set of native harness spellings.
    """
    ids: set[str] = set()
    for descriptor in HARNESS_DESCRIPTORS.values():
        if descriptor.is_native:
            ids.add(descriptor.id)
            ids.update(descriptor.native_aliases)
    return frozenset(ids)


def model_override_harness_ids() -> frozenset[str]:
    """:returns: Canonical ids whose harness supports model override."""
    return frozenset(d.id for d in HARNESS_DESCRIPTORS.values() if d.supports_model_override)


def cli_backed_descriptors() -> list[HarnessDescriptor]:
    """:returns: Descriptors that require a CLI binary on PATH."""
    return [d for d in HARNESS_DESCRIPTORS.values() if d.install_family_key is not None]


def native_ui_descriptors() -> list[HarnessDescriptor]:
    """:returns: Descriptors that ship a native-UI wrapper agent."""
    return [d for d in HARNESS_DESCRIPTORS.values() if d.wrapper_agent_name is not None]
