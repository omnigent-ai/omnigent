"""Typed descriptor for one Omnigent coding-agent harness.

Phase 0 of ``designs/harness-modular-registry-proposal.md``. The descriptor
holds the *registration identity* of a harness — the facts that today live
spread across ``OMNIGENT_HARNESSES``, ``_HARNESS_MODULES``,
``NATIVE_CODING_AGENTS``, ``HARNESS_ALIASES`` and ``_HARNESS_NAME_TO_KEY``.

Only fields that are provable against those existing constants live here in
Phase 0; declarative *capabilities* (elicitation, resume, effort family,
permission model, ...) arrive in the next step so each value can be reviewed
against the code that implements it.

The native-UI metadata is intentionally reused as-is from
:class:`omnigent.native_coding_agents.NativeCodingAgent` rather than copied, so
the two cannot disagree while the registry is still a derived view.
"""

from __future__ import annotations

from dataclasses import dataclass

from omnigent.harnesses.capabilities import HarnessCapabilities
from omnigent.native_coding_agents import NativeCodingAgent


@dataclass(frozen=True)
class HarnessDescriptor:
    """Registration identity for one canonical harness.

    :param name: The canonical harness id, e.g. ``"claude-sdk"`` or
        ``"kiro-native"``. This is a member of ``OMNIGENT_HARNESSES`` and the
        spelling ``AgentSpec.harness_kind`` returns.
    :param aliases: Every accepted alternative spelling that resolves to
        *name*, e.g. ``("claude",)`` for ``claude-sdk`` or
        ``("native-kiro",)`` for ``kiro-native``. Sorted, deduplicated, and
        never contains *name* itself.
    :param display_name: Human label for menus/UI, e.g. ``"Claude"``.
    :param harness_module: The fully-qualified module exporting
        ``create_app() -> FastAPI`` (the value in ``_HARNESS_MODULES``), or
        ``None`` for a harness resolved through an alternate path (currently
        only ``open-responses``).
    :param native: The native terminal-UI metadata when *name* is a native-CLI
        harness, else ``None``. Presence is equivalent to
        :attr:`is_native`.
    :param install_family_key: The ``_HARNESS_INSTALL`` family key whose CLI
        binary must be on ``PATH`` for this harness to launch (e.g.
        ``"anthropic"``, ``"pi"``), or ``None`` for SDK-only harnesses that
        need no separately-installed CLI. Multiple harnesses share a key
        (``claude-sdk`` and ``claude-native`` both need the ``claude`` CLI's
        ``anthropic`` family), so this is a reference, not an owned spec.
    :param supports_model_override: Whether a per-session ``/model`` override
        reaches the harness process (native CLIs via ``--model`` at launch, SDK
        harnesses via ``HARNESS_<H>_MODEL`` in the spawn env).
    :param capabilities: The declarative feature set this harness supports —
        the single source of truth for "what can this harness do?".
    """

    name: str
    aliases: tuple[str, ...]
    display_name: str
    harness_module: str | None
    native: NativeCodingAgent | None
    install_family_key: str | None
    supports_model_override: bool
    capabilities: HarnessCapabilities

    @property
    def is_native(self) -> bool:
        """Whether this is a native-CLI harness (wraps a vendor TUI/server)."""
        return self.native is not None

    @property
    def all_names(self) -> tuple[str, ...]:
        """The canonical name followed by every alias."""
        return (self.name, *self.aliases)
