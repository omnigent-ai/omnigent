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
from collections.abc import Callable

import omnigent.onboarding.gemini_auth as _gemini_auth
from omnigent.harness_aliases import HARNESS_ALIASES, canonicalize_harness
from omnigent.onboarding.harness_install import (
    COPILOT_KEY,
    CURSOR_KEY,
    GOOSE_KEY,
    HERMES_KEY,
    KIMI_KEY,
    KIRO_KEY,
    OPENCODE_KEY,
    PI_KEY,
    QWEN_KEY,
    harness_cli_installed,
)
from omnigent.onboarding.provider_config import (
    _EXECUTOR_TYPE_HARNESS_ALIASES,
    _HARNESS_FAMILY,
    GEMINI_FAMILY,
    PI_SURFACE,
)

# In-process SDK harnesses: no CLI binary, credentials resolved at runtime
# from ambient/spec sources the daemon can't see. Never gated. Includes both
# the canonical ``openai-agents`` and the ``openai-agents-sdk`` spelling the
# workflow's ``AgentHarnessType`` uses; executor-type spellings (``claude_sdk``
# / ``agents_sdk``) and the ``claude`` alias normalize onto these first.
# ``antigravity`` is the in-process Gemini SDK harness (its key resolves at
# runtime), distinct from the CLI-wrapping ``antigravity-native`` (``agy``)
# harness gated below on its binary plus a file-based OAuth credential.
_SDK_HARNESSES: frozenset[str] = frozenset(
    {"claude-sdk", "openai-agents", "openai-agents-sdk", "antigravity"}
)

# Families whose CLIs authenticate via file-based credentials rather than a CLI
# login command. For these, ``harness_is_configured`` checks BOTH the binary
# (via ``harness_cli_installed``) AND the credential (via the callable here).
# The ``anthropic`` / ``openai`` families authenticate via subscription provider
# config and do not appear here. The lambda resolves through the module at call
# time so a test can monkeypatch
# ``omnigent.onboarding.gemini_auth.gemini_login_detected`` and have the patch
# take effect without this dict caching the old function object.
_FAMILY_CREDENTIAL_CHECK: dict[str, Callable[[], bool]] = {
    GEMINI_FAMILY: lambda: _gemini_auth.gemini_login_detected(),
}

# CLI-wrapping pi harnesses. Both the bare ``pi`` surface and the native
# ``pi-native`` wrapper launch the same ``pi`` binary (``canonicalize_harness``
# folds ``native-pi`` → ``pi-native``). Unlike claude/codex they have no
# ``_HARNESS_FAMILY`` entry — pi uses the ``PI_SURFACE`` sentinel — so they must
# be gated explicitly or they fail open like an unknown harness.
_PI_HARNESSES: frozenset[str] = frozenset({PI_SURFACE, "pi-native"})

# Surface name for Kimi Code in the readiness map. Mirrors :data:`PI_SURFACE`
# — kimi is a CLI-backed harness with its own backend (Moonshot AI's), not a
# member of the anthropic/openai families that :data:`_HARNESS_FAMILY` keys.
KIMI_SURFACE = "kimi"
# Vibe surface
VIBE_SURFACE = "vibe"

# Native OpenCode harness. Like pi, it wraps a CLI (``opencode``) with no
# ``_HARNESS_FAMILY`` entry, so it must be gated explicitly or it would fail
# open like an unknown harness.
_OPENCODE_HARNESSES: frozenset[str] = frozenset({"opencode-native"})

# Native Cursor harnesses. These boot the ``cursor-agent`` TUI (``omni cursor``)
# and so, like the other native CLI harnesses, can't launch without that binary
# on ``PATH`` — gate them on it. Distinct from the SDK ``cursor`` harness
# (``CURSOR_KEY`` below), which runs in-process via ``cursor-sdk`` and gates on
# a ``CURSOR_API_KEY`` instead. Without these entries they'd fail open like an
# unknown harness, letting a binary-less launch die inside the executor.
_CURSOR_NATIVE_HARNESSES: frozenset[str] = frozenset({"cursor-native", "native-cursor"})

# Native Kiro harnesses boot the standalone ``kiro-cli`` TUI. Kiro has its own
# auth backend and no Omnigent provider family, so readiness is binary presence.
_KIRO_NATIVE_HARNESSES: frozenset[str] = frozenset({"kiro-native", "native-kiro"})

# Native Goose harnesses. Boot the ``goose session`` TUI (``omni goose``) and
# can't launch without the ``goose`` binary on ``PATH`` — gate on it, like the
# other native CLI harnesses. Goose owns its own auth (``goose configure``), so
# there is no SDK variant or key to gate on.
_GOOSE_NATIVE_HARNESSES: frozenset[str] = frozenset({"goose-native", "native-goose"})

# Native Kimi TUI harnesses (``omnigent kimi``). Like the other native CLIs,
# they wrap the resident ``kimi`` binary and can't launch without it on
# ``PATH`` — gate on it. Distinct from the bare ``kimi`` SDK surface
# (:data:`KIMI_SURFACE`), which gates on the same binary but renders headlessly.
_KIMI_NATIVE_HARNESSES: frozenset[str] = frozenset({"kimi-native", "native-kimi"})

# Native Hermes harnesses. Boot the ``hermes`` TUI (``omni hermes``) and can't
# launch without the ``hermes`` binary on ``PATH`` — gate on it, like the other
# native CLI harnesses. Hermes owns its own auth (``hermes setup`` /
# ``hermes model``); the headless ``hermes`` harness gates on the same binary.
_HERMES_NATIVE_HARNESSES: frozenset[str] = frozenset({"hermes-native", "native-hermes"})

# CLI-wrapping qwen harnesses. ``qwen`` / ``qwen-code`` (the ACP harness) and
# ``qwen-native`` / ``native-qwen`` (the native TUI via ``omni qwen``) all resolve
# to the same ``qwen`` binary (canonicalize_harness folds ``qwen-code`` → ``qwen``
# and ``native-qwen`` → ``qwen-native``). Unlike claude/codex they have no
# ``_HARNESS_FAMILY`` entry, so they must be gated explicitly or they fail open.
_QWEN_HARNESSES: frozenset[str] = frozenset({QWEN_KEY, "qwen-code", "qwen-native", "native-qwen"})


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
        ``_HARNESS_FAMILY`` (e.g. ``"codex-native"``), ``"pi"``, or
        ``"kimi"``.
    :returns: ``"anthropic"`` / ``"openai"`` for the claude/codex CLIs,
        :data:`~omnigent.onboarding.harness_install.KIMI_KEY` for kimi,
        :data:`~omnigent.onboarding.harness_install.VIBE_KEY` for vibe,
        :data:`~omnigent.onboarding.harness_install.OPENCODE_KEY` for
        opencode-native,
        :data:`~omnigent.onboarding.harness_install.QWEN_KEY` for qwen, or
        :data:`~omnigent.onboarding.harness_install.PI_KEY` for pi.
    """
    if canonical == KIMI_SURFACE or canonical in _KIMI_NATIVE_HARNESSES:
        return KIMI_KEY
    if canonical == VIBE_SURFACE:
        from omnigent.onboarding.harness_install import VIBE_KEY
        return VIBE_KEY
    if canonical in _OPENCODE_HARNESSES:
        return OPENCODE_KEY
    if canonical in _QWEN_HARNESSES:
        return QWEN_KEY
    return _HARNESS_FAMILY.get(canonical) or PI_KEY


def harness_is_configured(harness: str) -> bool:
    """Return whether *harness* can be launched on this machine.

    Only CLI-wrapping harnesses are assessed (native Claude/Codex/Kiro and
    ``pi`` / ``pi-native``): they cannot run without their binary on
    ``PATH``, and that is the one thing the daemon can check reliably and
    locally. SDK harnesses and unknown harnesses always return ``True`` —
    their readiness depends on runtime/ambient credentials the daemon
    can't enumerate, so blocking them would risk false negatives that
    break working launches.

    :param harness: A harness id, e.g. ``"claude-native"``, ``"codex"``,
        ``"openai-agents"``, ``"agents_sdk"``, ``"kiro-native"``, ``"pi"``,
        ``"pi-native"``, ``"qwen"``, or ``"qwen-code"``.
    :returns: ``True`` when launchable (CLI installed, or a harness the
        daemon doesn't gate); ``False`` only when a CLI-wrapping
        harness's binary is missing from ``PATH``.
    """
    canonical = _canonical_harness(harness)
    if canonical in _SDK_HARNESSES:
        return True
    if canonical in _CURSOR_NATIVE_HARNESSES:
        # Native Cursor (``omni cursor``) wraps the ``cursor-agent`` CLI — gate
        # on that binary, like ``claude-native`` / ``codex-native``. (Login
        # state surfaces at run time; the daemon gates only on binary presence,
        # mirroring the other native harnesses.)
        return harness_cli_installed(CURSOR_KEY)
    if canonical in _KIRO_NATIVE_HARNESSES:
        return harness_cli_installed(KIRO_KEY)
    if canonical in _GOOSE_NATIVE_HARNESSES or canonical == GOOSE_KEY:
        # Goose — both the native TUI (``goose-native`` / ``native-goose``, via
        # ``omni goose``) and the headless ACP harness (``goose``, drives
        # ``goose acp``) — wraps the ``goose`` CLI, so gate on that binary.
        # Auth/provider state surfaces at run time via Goose's own config; the
        # daemon gates only on binary presence.
        return harness_cli_installed(GOOSE_KEY)
    if canonical in _HERMES_NATIVE_HARNESSES or canonical == HERMES_KEY:
        # Hermes — both the native TUI (``hermes-native`` / ``native-hermes``,
        # via ``omni hermes``) and the headless subprocess harness (``hermes``)
        # — wraps the ``hermes`` CLI (installed via a curl script from Nous
        # Research). Auth/provider config surfaces at run time via Hermes' own
        # ``hermes model`` flow; gate only on binary presence.
        return harness_cli_installed(HERMES_KEY)
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
    if canonical == COPILOT_KEY:
        # Copilot runs in-process via the ``github-copilot-sdk`` package (the
        # SDK bundles the CLI binary it drives, so there is no separate binary to
        # gate on) and authenticates against GitHub's Copilot backend with a
        # GitHub token. So, like cursor, readiness is whether a token is
        # resolvable — one stored by ``omnigent setup`` (the ``copilot:`` config
        # block — see :mod:`omnigent.onboarding.copilot_auth`) or inherited from
        # the environment. A bad / Copilot-less token surfaces at run time.
        from omnigent.onboarding.copilot_auth import (
            COPILOT_TOKEN_ENV_VARS,
            copilot_github_token_configured,
        )

        return copilot_github_token_configured() or any(
            os.environ.get(var) for var in COPILOT_TOKEN_ENV_VARS
        )
    if (
        canonical not in _HARNESS_FAMILY
        and canonical not in _PI_HARNESSES
        and canonical != KIMI_SURFACE
        and canonical not in _KIMI_NATIVE_HARNESSES
        and canonical not in _OPENCODE_HARNESSES
        and canonical not in _QWEN_HARNESSES
    ):
        # Unknown harness — the daemon has no install metadata for it, so
        # it can't assess readiness. Fail open (custom/newer harnesses,
        # version skew).
        return True
    install_key = _install_key(canonical)
    if not harness_cli_installed(install_key):
        return False
    # Families that authenticate via file-based credentials (not a CLI login
    # command) require both the binary AND a stored credential. The ``agy`` CLI
    # falls into this category: it has no ``agy login`` subcommand and writes
    # OAuth creds on the first interactive browser run instead.
    credential_check = _FAMILY_CREDENTIAL_CHECK.get(install_key)
    if credential_check is not None:
        return credential_check()
    return True


def configured_harness_map() -> dict[str, bool]:
    """Return per-harness readiness for every accepted harness spelling.

    Built so the server/web UI can do a plain dict lookup with whatever
    spelling it holds — canonical ids, executor-type spellings, the
    ``claude`` alias, and ``pi``. SDK and unknown harnesses map to
    ``True`` (never gated); CLI-wrapping harnesses map to whether their
    binary is on ``PATH``.

    :returns: Mapping of harness spelling to readiness, e.g.
        ``{"claude-native": False, "codex-native": False,
        "claude-sdk": True, "openai-agents": True, "pi": True, "qwen": True}``.
    """
    spellings: set[str] = set(_HARNESS_FAMILY)
    spellings.update(_EXECUTOR_TYPE_HARNESS_ALIASES)
    spellings.update(HARNESS_ALIASES)
    spellings.update(_PI_HARNESSES)
    spellings.update(_OPENCODE_HARNESSES)
    spellings.update(_CURSOR_NATIVE_HARNESSES)
    spellings.update(_KIRO_NATIVE_HARNESSES)
    spellings.update(_GOOSE_NATIVE_HARNESSES)
    spellings.update(_KIMI_NATIVE_HARNESSES)
    spellings.update(_HERMES_NATIVE_HARNESSES)
    spellings.update(_QWEN_HARNESSES)
    spellings.add(CURSOR_KEY)
    spellings.add(KIMI_SURFACE)
    spellings.add(GOOSE_KEY)  # headless Goose (``goose acp``) gates on the goose binary
    spellings.add(HERMES_KEY)  # Hermes Agent wraps the ``hermes`` CLI
    spellings.add(COPILOT_KEY)
    return {spelling: harness_is_configured(spelling) for spelling in spellings}
