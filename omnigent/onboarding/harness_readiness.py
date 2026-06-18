"""Harness readiness checks used by the host daemon.

The daemon reports a per-harness readiness map in its hello frame (so the
web agent picker can warn) and re-checks the session's harness before
spawning a runner (so an unconfigured launch fails with a clear,
actionable error instead of dying inside the executor).

"Configured" here is deliberately narrow: the **only** thing the daemon
can reliably determine locally is whether a harness's wrapped CLI binary
is on ``PATH``. That gates the native CLI harnesses (Claude Code / Codex
via ``claude`` / ``codex``) and ``pi`` — the common "I picked Claude Code
but never ran ``omnigent setup`` to install it" case.

In-process SDK harnesses (``claude-sdk``, ``openai-agents``) run without
any CLI and resolve their model credentials at runtime from sources the
daemon cannot enumerate — environment API keys, a Databricks profile /
gateway, or the spec's ``executor.auth`` with ``${ENV}`` expansion. The
daemon has no way to know whether those will resolve, so it never gates
them (a genuine auth failure surfaces at the first turn via the
executor's own error). Unknown harnesses fail open for the same reason.
This keeps the check free of false negatives that would block a launch
that would actually work.
"""

from __future__ import annotations

import os

from omnigent.harness_aliases import HARNESS_ALIASES, canonicalize_harness
from omnigent.onboarding.harness_install import CURSOR_KEY, PI_KEY, harness_cli_installed
from omnigent.onboarding.provider_config import (
    _EXECUTOR_TYPE_HARNESS_ALIASES,
    _HARNESS_FAMILY,
    PI_SURFACE,
)

# In-process SDK harnesses: no CLI binary, credentials resolved at runtime
# from ambient/spec sources the daemon can't see. Never gated. Includes both
# the canonical ``openai-agents`` and the ``openai-agents-sdk`` spelling the
# workflow's ``AgentHarnessType`` uses; executor-type spellings (``claude_sdk``
# / ``agents_sdk``) and the ``claude`` alias normalize onto these first.
_SDK_HARNESSES: frozenset[str] = frozenset(
    {"claude-sdk", "openai-agents", "openai-agents-sdk", "antigravity"}
)

# CLI-wrapping pi harnesses. Both the bare ``pi`` surface and the native
# ``pi-native`` wrapper launch the same ``pi`` binary (``canonicalize_harness``
# folds ``native-pi`` → ``pi-native``). Unlike claude/codex they have no
# ``_HARNESS_FAMILY`` entry — pi uses the ``PI_SURFACE`` sentinel — so they must
# be gated explicitly or they fail open like an unknown harness.
_PI_HARNESSES: frozenset[str] = frozenset({PI_SURFACE, "pi-native"})
_CURSOR_NATIVE_HARNESSES: frozenset[str] = frozenset({"cursor-native", "native-cursor"})


def _canonical_harness(harness: str) -> str:
    """Normalize a harness id to its canonical spelling.

    Folds the user-facing alias (``claude`` → ``claude-sdk``) and the
    executor-type spellings :attr:`AgentSpec.harness_kind` returns
    (``claude_sdk`` → ``claude-sdk``, ``agents_sdk`` → ``openai-agents``)
    onto the canonical ids keyed in ``_HARNESS_FAMILY``.

    :param harness: A harness id, e.g. ``"claude"``, ``"agents_sdk"``,
        or ``"codex-native"``.
    :returns: The canonical spelling, e.g. ``"claude-sdk"`` or
        ``"codex-native"``; unknown names are returned unchanged.
    """
    canonical = canonicalize_harness(harness) or harness
    return _EXECUTOR_TYPE_HARNESS_ALIASES.get(canonical, canonical)


def _install_key(canonical: str) -> str:
    """Return the install-spec key whose CLI binary *canonical* requires.

    :param canonical: A canonical CLI-wrapping harness id keyed in
        ``_HARNESS_FAMILY`` (e.g. ``"codex-native"``), or ``"pi"``.
    :returns: ``"anthropic"`` / ``"openai"`` for the claude/codex CLIs,
        or :data:`~omnigent.onboarding.harness_install.PI_KEY` for pi.
    """
    return _HARNESS_FAMILY.get(canonical) or PI_KEY


def harness_is_configured(harness: str) -> bool:
    """Return whether *harness* can be launched on this machine.

    Only CLI-wrapping harnesses are assessed (native Claude/Codex and
    ``pi`` / ``pi-native``): they cannot run without their binary on
    ``PATH``, and that is the one thing the daemon can check reliably and
    locally. SDK harnesses and unknown harnesses always return ``True`` —
    their readiness depends on runtime/ambient credentials the daemon
    can't enumerate, so blocking them would risk false negatives that
    break working launches.

    :param harness: A harness id, e.g. ``"claude-native"``, ``"codex"``,
        ``"openai-agents"``, ``"agents_sdk"``, ``"pi"``, or
        ``"pi-native"``.
    :returns: ``True`` when launchable (CLI installed, or a harness the
        daemon doesn't gate); ``False`` only when a CLI-wrapping
        harness's binary is missing from ``PATH``.
    """
    canonical = _canonical_harness(harness)
    if canonical in _SDK_HARNESSES:
        return True
    if canonical in _CURSOR_NATIVE_HARNESSES:
        return harness_cli_installed(CURSOR_KEY)
    if canonical == CURSOR_KEY:
        # Cursor runs in-process via ``cursor-sdk`` and authenticates with a
        # ``CURSOR_API_KEY`` (a ``cursor-agent login`` does not apply). So,
        # unlike the CLI-wrapping harnesses, there is no binary to gate on:
        # readiness is whether a key is resolvable — stored by ``omnigent setup``
        # (the ``cursor:`` block — see :mod:`omnigent.onboarding.cursor_auth`)
        # or inherited from the env. A bad key surfaces at run time.
        #
        # ``cursor-sdk`` is now an OPTIONAL extra, but we deliberately do NOT
        # also gate on SDK presence: this mirrors ``antigravity`` (also SDK-only
        # and now-optional, never gated on the SDK). A missing SDK surfaces as
        # the executor's import error on the first turn
        # (:mod:`omnigent.inner.cursor_executor`); gating here would only
        # duplicate that, less actionably. So cursor keeps its single key check.
        from omnigent.onboarding.cursor_auth import cursor_api_key_configured

        return cursor_api_key_configured() or bool(os.environ.get("CURSOR_API_KEY"))
    if canonical not in _HARNESS_FAMILY and canonical not in _PI_HARNESSES:
        # Unknown harness — the daemon has no install metadata for it, so
        # it can't assess readiness. Fail open (custom/newer harnesses,
        # version skew).
        return True
    return harness_cli_installed(_install_key(canonical))


def configured_harness_map() -> dict[str, bool]:
    """Return per-harness readiness for every accepted harness spelling.

    Built so the server/web UI can do a plain dict lookup with whatever
    spelling it holds — canonical ids, executor-type spellings, the
    ``claude`` alias, and ``pi``. SDK and unknown harnesses map to
    ``True`` (never gated); CLI-wrapping harnesses map to whether their
    binary is on ``PATH``.

    :returns: Mapping of harness spelling to readiness, e.g.
        ``{"claude-native": False, "codex-native": False,
        "claude-sdk": True, "openai-agents": True, "pi": True}``.
    """
    spellings: set[str] = set(_HARNESS_FAMILY)
    spellings.update(_EXECUTOR_TYPE_HARNESS_ALIASES)
    spellings.update(HARNESS_ALIASES)
    spellings.update(_PI_HARNESSES)
    spellings.update(_CURSOR_NATIVE_HARNESSES)
    spellings.add(CURSOR_KEY)
    return {spelling: harness_is_configured(spelling) for spelling in spellings}
