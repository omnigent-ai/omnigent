"""Harness readiness checks used by the host daemon.

The daemon reports a per-harness readiness map in its hello frame, refreshes
it while connected (so the web agent picker can warn accurately), and
re-checks the session's harness before spawning a runner (so an unconfigured
launch fails clearly instead of dying inside the executor).

"Configured" here is deliberately narrow: the **only** thing the daemon
can reliably determine locally is whether a harness's wrapped CLI binary
is on ``PATH``. That gates the native CLI harnesses (Claude Code / Codex
via ``claude`` / ``codex``) and ``pi`` — the common "I picked Claude Code
but never ran ``omnigent setup`` to install it" case.

In-process SDK harnesses (``claude-sdk``, ``openai-agents``,
``antigravity``) run without any CLI and resolve their model credentials
at runtime. The **launch gate** still never blocks them: the spec's
``executor.auth`` (with ``${ENV}`` expansion) is invisible here, so a
hard gate would break launches that actually work. The **picker map**,
however, checks the credential sources the daemon *can* see locally —
locally resolvable provider entries (including ambient-detected local
providers such as a reachable Ollama), ambient env API keys, Claude Code's
own login / managed settings, an ambient Databricks workspace — and reports
``"needs-auth"`` when none is visible, so a credential-less host warns
before the first turn dies instead of after. Unknown harnesses fail
open on both axes.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import omnigent.onboarding.gemini_auth as _gemini_auth
import omnigent.onboarding.kimi_auth as _kimi_auth
from omnigent._platform import IS_WINDOWS, resolve_cli_binary
from omnigent.harness_aliases import HARNESS_ALIASES, NATIVE_HARNESSES, canonicalize_harness
from omnigent.harness_availability import (
    CODEX_CANONICAL_HARNESSES,
    HARNESS_BINARY_MISSING,
    HARNESS_NEEDS_AUTH,
    HARNESS_VERSION_TOO_LOW,
    HarnessAvailability,
)
from omnigent.harness_plugins import harness_install_keys, valid_harnesses
from omnigent.onboarding.harness_install import (
    COPILOT_KEY,
    CURSOR_KEY,
    DEVIN_KEY,
    GOOSE_KEY,
    HERMES_KEY,
    KIMI_KEY,
    KIRO_KEY,
    OPENCODE_KEY,
    PI_KEY,
    QWEN_KEY,
    READINESS_CLI_PROBE_TIMEOUT_S,
    harness_cli_installed,
    harness_install_spec,
    required_cli_for_harness,
)
from omnigent.onboarding.provider_config import (
    _EXECUTOR_TYPE_HARNESS_ALIASES,
    _HARNESS_FAMILY,
    ANTHROPIC_FAMILY,
    BEDROCK_KIND,
    CLI_CONFIG_KIND,
    GEMINI_FAMILY,
    OPENAI_FAMILY,
    PI_SURFACE,
    SUBSCRIPTION_KIND,
    ProviderEntry,
    default_provider_for_harness,
    load_config,
)

# In-process SDK harnesses: no CLI binary to gate on. The launch gate never
# blocks them (spec-level auth is invisible here), but the picker map reports
# ``needs-auth`` when no locally visible credential source could serve them
# (see :func:`_sdk_harness_availability`). Includes both
# the canonical ``openai-agents`` and the ``openai-agents-sdk`` spelling the
# workflow's ``AgentHarnessType`` uses; executor-type spellings (``claude_sdk``
# / ``agents_sdk``) and the ``claude`` alias normalize onto these first.
# ``antigravity`` is the in-process Gemini SDK harness (its key resolves at
# runtime), distinct from the CLI-wrapping ``antigravity-native`` (``agy``)
# harness gated below on its binary plus an API key or OAuth credential.
_logger = logging.getLogger(__name__)

# Bound startup and periodic readiness work: the slow checks are independent
# CLI version/auth probes, but an unbounded process burst would be unfriendly on
# smaller hosts.
_READINESS_PROBE_MAX_WORKERS = 4

_SDK_HARNESSES: frozenset[str] = frozenset(
    {"claude-sdk", "openai-agents", "openai-agents-sdk", "antigravity"}
)

# Families/harnesses whose CLIs authenticate via file-based credentials rather
# than a CLI login-status command. For these, ``harness_is_configured`` checks
# BOTH the binary (via ``harness_cli_installed``) AND the credential (via the
# callable here). ``agy`` accepts ``GEMINI_API_KEY`` or writes an OAuth token on
# its first interactive run; ``kimi login`` writes
# ``~/.kimi-code/credentials/kimi-code.json``, and pay-per-use users instead set
# an API key in ``~/.kimi-code/config.toml`` (kimi has no login-status probe) —
# ``kimi_auth_configured`` accepts either. The ``anthropic`` / ``openai``
# families authenticate via subscription provider config and do not appear here.
# Each lambda resolves through its module at call time so a test can monkeypatch
# ``…gemini_auth.gemini_login_detected`` / ``…kimi_auth.kimi_auth_configured``
# and have the patch take effect without this dict caching the old function
# object.
_FAMILY_CREDENTIAL_CHECK: dict[str, Callable[[], bool]] = {
    GEMINI_FAMILY: lambda: _gemini_auth.gemini_login_detected(),
    KIMI_KEY: lambda: _kimi_auth.kimi_auth_configured(),
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

# Native Devin harnesses boot the resident ``devin`` TUI (``omni devin``). Devin
# owns its own auth (``devin auth login`` writes a credential file it reads back
# at spawn), so there is no Omnigent-managed key to gate on and readiness is
# binary presence — like the other native CLI harnesses. Without these entries
# they'd fail open like an unknown harness, letting a binary-less launch die
# inside the executor.
_DEVIN_NATIVE_HARNESSES: frozenset[str] = frozenset({"devin-native", "native-devin"})

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
        :data:`~omnigent.onboarding.harness_install.OPENCODE_KEY` for
        opencode-native,
        :data:`~omnigent.onboarding.harness_install.QWEN_KEY` for qwen, or
        :data:`~omnigent.onboarding.harness_install.PI_KEY` for pi.
    """
    if canonical == KIMI_SURFACE or canonical in _KIMI_NATIVE_HARNESSES:
        return KIMI_KEY
    if canonical in _OPENCODE_HARNESSES:
        return OPENCODE_KEY
    if canonical in _QWEN_HARNESSES:
        return QWEN_KEY
    return _HARNESS_FAMILY.get(canonical) or PI_KEY


def _harness_availability_core(harness: str) -> HarnessAvailability:
    """Return the detailed availability state for *harness*.

    Mirrors :func:`harness_is_configured` but preserves the distinction
    between "CLI missing", "CLI present but version too old", and other
    structured states so the web UI and setup dialogs can show actionable
    copy.

    :param harness: A harness id, e.g. ``"claude-native"``, ``"codex"``,
        ``"openai-agents"``, ``"agents_sdk"``, ``"kiro-native"``, ``"pi"``,
        ``"pi-native"``, ``"qwen"``, or ``"qwen-code"``.
    :returns: A :data:`HarnessAvailability` value.``True`` when launchable;
        ``False`` or a reason string otherwise.
    """
    canonical = _canonical_harness(harness)
    if IS_WINDOWS and canonical in NATIVE_HARNESSES:
        # Native harnesses require tmux/PTY, which the runner does not support on Windows.
        return False
    if canonical == "acp":
        # The generic ACP harness has no fixed binary — "configured" means at
        # least one agent is registered in the ``acp:`` config block. Each
        # agent's own binary is a soft PATH hint surfaced in setup, not a hard
        # gate. A malformed block reads as not-configured rather than raising.
        try:
            from omnigent.onboarding.acp_auth import acp_agents

            return bool(acp_agents())
        except Exception:
            return False
    if canonical in _SDK_HARNESSES:
        # Launch gate only: never block an SDK launch (spec-level auth is
        # invisible here). The picker map's credential check lives in
        # :func:`_sdk_harness_availability`.
        return True
    if canonical in _CURSOR_NATIVE_HARNESSES:
        # Native Cursor (``omni cursor``) wraps the ``cursor-agent`` CLI — gate
        # on that binary. Keep the missing-binary case as the historical bare
        # ``False`` sentinel, surfacing an outdated version only as
        # ``"version-too-low"``.
        return _installer_only_availability(CURSOR_KEY)
    if canonical in _KIRO_NATIVE_HARNESSES:
        return _installer_only_availability(KIRO_KEY)
    if canonical in _GOOSE_NATIVE_HARNESSES or canonical == GOOSE_KEY:
        return _installer_only_availability(GOOSE_KEY)
    if canonical in _HERMES_NATIVE_HARNESSES or canonical == HERMES_KEY:
        return _installer_only_availability(HERMES_KEY)
    if canonical in _DEVIN_NATIVE_HARNESSES:
        return _installer_only_availability(DEVIN_KEY)
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
            copilot_github_host,
            copilot_github_token_configured,
            gh_cli_github_token,
        )

        if copilot_github_token_configured() or any(
            os.environ.get(var) for var in COPILOT_TOKEN_ENV_VARS
        ):
            return True
        # A ``gh auth login`` session is a usable Copilot credential, so a
        # logged-in user is ready without pasting a token into setup.
        return gh_cli_github_token(copilot_github_host()) is not None
    if (
        canonical not in _HARNESS_FAMILY
        and canonical not in _PI_HARNESSES
        and canonical != KIMI_SURFACE
        and canonical not in _KIMI_NATIVE_HARNESSES
        and canonical not in _OPENCODE_HARNESSES
        and canonical not in _QWEN_HARNESSES
    ):
        required_cli = required_cli_for_harness(canonical) or required_cli_for_harness(harness)
        if required_cli is not None:
            return resolve_cli_binary(required_cli.binary) is not None
        # Unknown harness — the daemon has no install metadata for it, so
        # it can't assess readiness. Fail open (custom/newer harnesses,
        # version skew).
        return True
    install_key = _install_key(canonical)
    availability = _installer_only_availability(install_key)
    # Families that authenticate via file-based credentials (not a CLI login
    # command) require both the binary AND a stored credential. The ``agy`` CLI
    # falls into this category: it has no ``agy login`` subcommand and writes
    # OAuth creds on the first interactive browser run instead.
    if availability is not True:
        return availability
    credential_check = _FAMILY_CREDENTIAL_CHECK.get(install_key)
    if credential_check is not None:
        return credential_check()
    return True


# Native CLI harnesses that authenticate via their own login command and can
# report auth state locally, so the picker map can distinguish "installed but
# not signed in" (``needs-auth``) from "not installed" (``binary-missing``) —
# the same two-step signal Codex already provides. This is picker-facing ONLY;
# the launch gate (:func:`harness_is_configured`) stays binary-only, so a
# not-signed-in harness is never blocked from launching (its login surfaces at
# run time). Pi is handled separately in :func:`_harness_availability` (it has
# no CLI login, so it can't use the login-command path here — its credential is
# an omnigent-managed provider). Qwen is absent on purpose: its key lives in the
# harness's own env / interactive ``/auth``, which the daemon can't reduce to a
# provider check, so it reports binary presence only.
# Cursor native is included here too: ``cursor-agent`` has its own login command,
# so the picker can distinguish "not installed" from "installed but not signed
# in", while the launch gate stays binary-only.
_AUTH_AWARE_NATIVE_HARNESSES: dict[str, str] = {
    "claude-native": "anthropic",
    "native-claude": "anthropic",
    "opencode-native": OPENCODE_KEY,
    "cursor-native": CURSOR_KEY,
    "native-cursor": CURSOR_KEY,
}


# Provider kinds the in-process SDK executors cannot consume. Launch's
# spawn-env builder (``configure_agent_harness_with_provider`` in
# :mod:`omnigent.runtime.workflow`) rejects a ``cli-config`` entry for
# anything but the ``codex`` CLI harness (it pins a provider table inside
# ``~/.codex/config.toml`` that only that CLI reads) and accepts ``bedrock``
# only for native ``omnigent claude``; a ``subscription`` is the CLI's own
# login, unusable outside that CLI. Counting any of them for an SDK harness
# would report ready for a source the launch would reject.
_SDK_UNUSABLE_PROVIDER_KINDS: frozenset[str] = frozenset(
    {SUBSCRIPTION_KIND, CLI_CONFIG_KIND, BEDROCK_KIND}
)


def _provider_entry_locally_credentialed(
    provider: ProviderEntry,
    family: str | None,
    unusable_kinds: frozenset[str] = frozenset({SUBSCRIPTION_KIND}),
) -> bool:
    """Whether *provider* is a credential source that resolves on this host.

    A provider entry only readies a harness when launch could actually
    consume it:

    - Its kind must be usable by the consuming harness (*unusable_kinds*
      names the kinds launch rejects for it — always ``subscription``, whose
      CLI login is judged separately by :func:`harness_cli_logged_in`, plus
      :data:`_SDK_UNUSABLE_PROVIDER_KINDS` for the in-process SDK harnesses).
    - The secret it references must resolve locally: launch resolves the
      entry's family through
      :meth:`~omnigent.onboarding.provider_config.ProviderEntry.family`
      (which expands ``$VAR`` / ``api_key_ref`` via :func:`resolve_secret`)
      and fails rather than skipping the provider, so an entry pointing at an
      unset ``env:`` variable or a missing keychain secret is a first-turn
      auth failure, not a credential. Usable kinds that carry no inline
      family config (``databricks``) resolve their credential elsewhere at
      launch and keep counting as sources here.

    Local env/keychain resolution only — no network I/O — and never raises.

    :param provider: The selected
        :class:`~omnigent.onboarding.provider_config.ProviderEntry`.
    :param family: The family the harness consumes (``"anthropic"`` /
        ``"openai"``), or ``None`` for an unmapped harness (``pi``), which
        may consume either family.
    :param unusable_kinds: Provider kinds launch rejects for the consuming
        harness.
    :returns: ``True`` when the entry is launch-consumable and its credential
        resolves locally (or needs no local resolution), else ``False``.
    """
    if provider.kind in unusable_kinds:
        return False
    candidates = (family,) if family is not None else (ANTHROPIC_FAMILY, OPENAI_FAMILY)
    inline = [name for name in candidates if name in provider.families]
    if not inline:
        # Nothing to resolve locally (``databricks`` / ``cli-config`` kinds,
        # or a family served structurally rather than inline).
        return True
    for name in inline:
        try:
            provider.family(name)
            return True
        except Exception as exc:
            # Class-only: resolver errors can embed secret refs or material.
            _logger.debug(
                "readiness: provider credential for %r did not resolve (%s)",
                name,
                type(exc).__name__,
            )
    return False


def _family_provider_configured(harness: str) -> bool:
    """Whether a locally credentialed default provider ENTRY serves *harness*'s family.

    Reads the local ``providers:`` config the same way the ``omnigent setup``
    overview does (:func:`surface_default_provider` / :func:`default_provider_for_harness`,
    which resolve the harness's family and — for ``pi`` — its cross-family
    fallback). A ``subscription``-kind default is NOT counted here: it lives in
    the harness CLI's own login, judged separately by :func:`harness_cli_logged_in`,
    so counting it would double-count the CLI-login path and mask a genuine
    "installed but no key" state.

    The entry counts only when its credential actually resolves locally
    (:func:`_provider_entry_locally_credentialed`): launch resolves the
    entry's secret the same way and fails rather than skipping the provider,
    so a default whose ``api_key_ref`` points at an unset ``env:``/``$VAR``
    or a missing keychain secret is a first-turn auth failure, not a
    credential. The launch gate stays binary-only regardless.

    Local, synchronous, side-effect free (config file / env / keychain reads
    only) and never raises: any resolver/config error fails to ``False`` so a
    broken config reports "needs-auth" rather than crashing the readiness
    refresh.

    :param harness: A canonical harness spelling, e.g. ``"claude-native"`` or
        ``"pi"``.
    :returns: ``True`` when a locally credentialed non-subscription default
        provider entry is present for the harness's family, else ``False``.
    """
    try:
        provider = default_provider_for_harness(load_config(), harness)
        if provider is None:
            return False
        unusable = (
            _SDK_UNUSABLE_PROVIDER_KINDS
            if harness in _SDK_HARNESSES
            else frozenset({SUBSCRIPTION_KIND})
        )
        return _provider_entry_locally_credentialed(
            provider, _HARNESS_FAMILY.get(harness), unusable
        )
    except Exception as exc:
        # Readiness must never raise; a broken/unreadable config fails to
        # "no credential" (yellow) rather than crashing the refresh.
        # Class-only: provider config errors may embed credential material.
        _logger.debug("readiness: provider check failed for %r (%s)", harness, type(exc).__name__)
        return False


def _family_fallback_provider_configured(harness: str) -> bool:
    """Whether ANY locally credentialed provider entry serves *harness*'s family.

    Mirrors the launch-time last resort: with no family default configured,
    the runner's spawn-env resolution still credentials the head from a
    configured provider serving the family
    (:func:`~omnigent.onboarding.provider_config.first_available_provider`,
    consumed with ``for_launch=True`` in :mod:`omnigent.runtime.workflow`), so
    such an entry is a real credential source even though it is not the
    default. Like runtime resolution, the config is read over the ambient
    detection merge
    (:func:`~omnigent.onboarding.detected.effective_config_with_detected`),
    so automatically detected local providers — a reachable keyless Ollama —
    count exactly as they do at launch. Provider kinds the SDK launch would
    reject (:data:`_SDK_UNUSABLE_PROVIDER_KINDS` — ``subscription``,
    ``cli-config``, ``bedrock``) are skipped, and an entry counts only when
    its credential resolves locally
    (:func:`_provider_entry_locally_credentialed`). Local config/env reads
    plus ambient detection's localhost-only probes; never raises.

    :param harness: A canonical SDK harness id, e.g. ``"claude-sdk"``.
    :returns: ``True`` when a locally credentialed non-subscription provider
        entry (configured or ambient-detected) serves the harness's family.
    """
    family = _HARNESS_FAMILY.get(harness)
    if family is None:
        return False
    try:
        from omnigent.onboarding.detected import effective_config_with_detected
        from omnigent.onboarding.provider_config import load_providers, provider_families

        config = effective_config_with_detected(load_config())
        for entry in load_providers(config).values():
            if family not in provider_families(entry):
                continue
            if _provider_entry_locally_credentialed(entry, family, _SDK_UNUSABLE_PROVIDER_KINDS):
                return True
    except Exception as exc:
        # Class-only: provider config errors may embed credential material.
        _logger.debug(
            "readiness: fallback provider check failed for %r (%s)",
            harness,
            type(exc).__name__,
        )
    return False


def _claude_managed_gateway_configured() -> bool:
    """Whether Claude Code's own settings chain carries a usable credential.

    The structural companion to :func:`_family_provider_configured` (which sees
    only omnigent's ``providers:`` config). An enterprise install configures
    Claude Code directly — a gateway ``ANTHROPIC_BASE_URL`` + ``apiKeyHelper`` in
    its managed settings — and Claude Code applies that at its own launch, so
    the harness is genuinely usable with nothing in ``config.yaml`` and no CLI
    subscription login. Crediting it here is what stops an enterprise host from
    reading "needs-auth" (the "Claude Code isn't configured on <host>" banner).

    Local, synchronous, side-effect free (one JSON file read) and never raises:
    any error fails to ``False`` so readiness falls through to the CLI status
    probe rather than crashing the refresh.

    :returns: ``True`` when Claude Code's managed settings deliver a credential.
    """
    try:
        from omnigent.onboarding.ambient import claude_managed_gateway

        return claude_managed_gateway()[1]
    except Exception:
        _logger.debug("readiness: claude managed-settings check failed", exc_info=True)
        return False


def _ambient_family_env_key_configured(family: str) -> bool:
    """Whether an ambient env API key serving *family* is set.

    Checks the same vendor env vars ambient detection adopts
    (:data:`~omnigent.onboarding.ambient._ENV_KEY_FAMILY` over
    :data:`~omnigent.onboarding.providers.PROVIDER_ENV_VARS`), including their
    ``OMNIGENT_``-prefixed variants — e.g. ``ANTHROPIC_API_KEY`` for the
    ``anthropic`` family, ``OPENAI_API_KEY`` / ``OPENROUTER_API_KEY`` for
    ``openai``. The daemon spawns runners with its own environment, so a key
    visible here is a key the harness will inherit.

    :param family: A model family, e.g. ``"anthropic"`` or ``"openai"``.
    :returns: ``True`` when any such variable is set and non-empty.
    """
    from omnigent.onboarding.ambient import _ENV_KEY_FAMILY
    from omnigent.onboarding.providers import PROVIDER_ENV_VARS
    from omnigent.util.env_credentials import getenv_nonempty_with_omnigent_prefix

    for provider, env_var in PROVIDER_ENV_VARS.items():
        if _ENV_KEY_FAMILY.get(provider) != family:
            continue
        if getenv_nonempty_with_omnigent_prefix(env_var) is not None:
            return True
    return False


def _claude_token_env_configured() -> bool:
    """Whether an ambient Claude token env credential is visible.

    The host forwards these to its runners (the harness credential allowlist
    in :mod:`omnigent.host.connect`) and Claude Code resolves them directly:
    ``CLAUDE_CODE_OAUTH_TOKEN`` (``claude setup-token`` subscription auth) and
    ``ANTHROPIC_AUTH_TOKEN`` (gateway bearer, usually paired with
    ``ANTHROPIC_BASE_URL``), including their ``OMNIGENT_``-prefixed variants.
    Env reads only; never raises.

    :returns: ``True`` when either token variable is set and non-empty.
    """
    from omnigent.util.env_credentials import getenv_nonempty_with_omnigent_prefix

    return any(
        getenv_nonempty_with_omnigent_prefix(var) is not None
        for var in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_AUTH_TOKEN")
    )


def _claude_code_login_configured() -> bool:
    """Whether Claude Code's own subscription login is present on this machine.

    The claude-sdk harness drives Claude Code, so the CLI's login is a
    complete credential for it even with nothing in omnigent's config. Local
    and never raises: any detection error reads as "no login".

    :returns: ``True`` when a usable Claude Code login is detected.
    """
    try:
        from omnigent.onboarding import ambient

        return ambient._claude_login_detected()
    except Exception:
        _logger.debug("readiness: claude login check failed", exc_info=True)
        return False


def _databricks_file_has_credentialed_profile(path: str) -> bool:
    """Whether a Databricks config file declares a locally usable profile.

    Checks the locally required configuration fields rather than section or
    file existence: a profile counts only when it names a workspace ``host``
    AND its authentication method's locally stored material is present — a
    ``token`` PAT, an OAuth ``client_id``/``client_secret`` pair, or a legacy
    ``username``/``password`` pair. An explicit ``auth_type`` selects one
    method: a material-bearing method (``pat`` / ``basic`` / ``oauth-m2m`` /
    ``azure-client-secret``) counts only when its fields are actually
    present, while an externally resolved method (``databricks-cli``,
    ``external-browser``, ``metadata-service``, …) keeps its material outside
    this file and counts on declaration. A file that merely exists, or a
    section that carries neither a workspace nor authentication (``[work]``
    alone, or ``auth_type = pat`` with no token), proves nothing. Applied to
    both the default ``~/.databrickscfg`` and a ``DATABRICKS_CONFIG_FILE``
    override. Local file read only, no network requests; never raises.

    :param path: The config file path to inspect.
    :returns: ``True`` when the file parses and declares at least one profile
        carrying a workspace host plus authentication material.
    """
    import configparser

    try:
        if not os.path.exists(path):
            return False
        parser = configparser.ConfigParser()
        parser.read(path)
        section_names = list(parser.sections())
        if parser.defaults():
            section_names.append(parser.default_section)
        for name in section_names:
            section = parser[name]
            if not section.get("host", "").strip():
                continue
            has_pat = bool(section.get("token", "").strip())
            has_oauth = bool(
                section.get("client_id", "").strip() and section.get("client_secret", "").strip()
            )
            has_basic = bool(
                section.get("username", "").strip() and section.get("password", "").strip()
            )
            auth_type = section.get("auth_type", "").strip().lower()
            if not auth_type:
                if has_pat or has_oauth or has_basic:
                    return True
                continue
            # An explicit auth_type selects ONE method; require that method's
            # locally stored material rather than trusting the declaration.
            if auth_type == "pat":
                if has_pat:
                    return True
                continue
            if auth_type == "basic":
                if has_basic:
                    return True
                continue
            if auth_type in ("oauth-m2m", "oauth"):
                if has_oauth:
                    return True
                continue
            if auth_type == "azure-client-secret":
                if (
                    section.get("azure_client_id", "").strip()
                    and section.get("azure_client_secret", "").strip()
                    and section.get("azure_tenant_id", "").strip()
                ):
                    return True
                continue
            # Externally resolved methods (databricks-cli, external-browser,
            # metadata-service, github-oidc, azure-cli, …) keep their material
            # outside this file; the declaration is the local signal.
            return True
        return False
    except Exception as exc:
        # Log only the exception class: configparser errors can embed the
        # offending file's contents, which may include credential material.
        _logger.debug("readiness: databricks config parse failed (%s)", type(exc).__name__)
        return False


def _databricks_workspace_configured() -> bool:
    """Whether an ambient Databricks credential source is resolvable here.

    The SDK executors mint a gateway bearer from ambient Databricks
    credentials for ``databricks-*`` models even with no ``providers:``
    entry, so a resolvable credential source counts. A workspace URL alone
    is **not** a credential: env-based readiness requires ``DATABRICKS_HOST``
    plus authentication material (a ``DATABRICKS_TOKEN`` PAT, or an OAuth
    service-principal ``DATABRICKS_CLIENT_ID``/``DATABRICKS_CLIENT_SECRET``
    pair), and a config file counts only when it declares a profile carrying
    a workspace host plus authentication material
    (:func:`_databricks_file_has_credentialed_profile`). Local file/env reads
    only; never raises.

    :returns: ``True`` when an ambient Databricks credential source is
        resolvable.
    """
    if os.environ.get("DATABRICKS_HOST", "").strip():
        if os.environ.get("DATABRICKS_TOKEN", "").strip():
            return True
        if (
            os.environ.get("DATABRICKS_CLIENT_ID", "").strip()
            and os.environ.get("DATABRICKS_CLIENT_SECRET", "").strip()
        ):
            return True
    config_override = os.environ.get("DATABRICKS_CONFIG_FILE", "").strip()
    if config_override and _databricks_file_has_credentialed_profile(config_override):
        return True
    try:
        from omnigent.onboarding.databricks_config import _DATABRICKSCFG_PATH

        return _databricks_file_has_credentialed_profile(str(_DATABRICKSCFG_PATH))
    except Exception as exc:
        # Class-only: the config path/parse error may embed credential material.
        _logger.debug("readiness: databricks profile check failed (%s)", type(exc).__name__)
        return False


def _adc_file_carries_credentials(path: Path) -> bool:
    """Whether an ADC file actually carries credential material.

    Checks the locally required fields rather than file existence — an ADC
    file is only a credential when it declares its type's key material: a
    ``refresh_token`` for ``authorized_user``, a ``private_key`` +
    ``client_email`` for ``service_account``. Other declared types
    (``external_account``, impersonation, …) carry type-specific material
    this check does not model, so a declared type errs toward ready (a false
    "ready" is the pre-warning status quo); an empty or type-less JSON body
    (``{}``) proves nothing. Local file read only, no provider contact; never
    raises.

    :param path: The ADC JSON file path.
    :returns: ``True`` when the file parses and declares credential material.
    """
    import json

    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as exc:
        # Class-only: an ADC file (or its parse error) may embed secrets.
        _logger.debug("readiness: ADC file parse failed (%s)", type(exc).__name__)
        return False
    if not isinstance(data, dict):
        return False
    cred_type = str(data.get("type") or "").strip()
    if not cred_type:
        return False
    if cred_type == "authorized_user":
        return bool(data.get("refresh_token"))
    if cred_type == "service_account":
        return bool(data.get("private_key")) and bool(data.get("client_email"))
    return True


def _google_adc_configured() -> bool:
    """Whether GCP Application Default Credentials are visible.

    Antigravity's Vertex AI path authenticates via ADC; whether a spec opts
    into Vertex is invisible here. The file counts only when it actually
    carries credential material (:func:`_adc_file_carries_credentials`) — a
    merely existing or empty ADC file supplies nothing a launch could
    authenticate with.

    :returns: ``True`` when an ADC credential file with material is present.
    """
    try:
        cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
        if cred_path and os.path.exists(cred_path):
            return _adc_file_carries_credentials(Path(cred_path))
        adc = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
        return adc.exists() and _adc_file_carries_credentials(adc)
    except Exception:
        _logger.debug("readiness: ADC check failed", exc_info=True)
        return False


def _antigravity_credential_configured() -> bool:
    """Whether a credential the antigravity SDK harness can use is visible.

    Mirrors the spawn-env resolution (``_build_antigravity_spawn_env``):
    Antigravity is Gemini-native — a stored ``antigravity:`` config-block key,
    an ambient ``GEMINI_API_KEY`` / ``ANTIGRAVITY_API_KEY``, or Vertex AI via
    ADC. The ``openai``/``anthropic`` provider families are deliberately not
    consulted (the spawn env never adopts them for this harness).

    :returns: ``True`` when any such source is visible.
    """
    try:
        from omnigent.onboarding.antigravity_auth import (
            ANTIGRAVITY_ENV_VARS,
            antigravity_api_key_configured,
        )

        if antigravity_api_key_configured():
            return True
        if any(os.environ.get(var) for var in ANTIGRAVITY_ENV_VARS):
            return True
    except Exception:
        _logger.debug("readiness: antigravity credential check failed", exc_info=True)
    return _google_adc_configured()


def _global_auth_configured() -> bool:
    """Whether the user-level global ``auth:`` block can serve the SDK builders.

    Both SDK executors inherit the ``auth:`` mapping from the user's global
    ``config.yaml`` (``_load_global_auth`` in :mod:`omnigent.runtime.workflow`)
    when the agent spec declares no ``executor.auth``, so a parsed global
    api-key / Databricks auth block is a complete credential source. Local
    file read only; never raises.

    :returns: ``True`` when the global ``auth:`` block parses into a usable
        auth configuration.
    """
    try:
        from omnigent.runtime.workflow import _load_global_auth

        return _load_global_auth() is not None
    except Exception as exc:
        # Class-only: the auth block (or its parse error) may carry secrets.
        _logger.debug("readiness: global auth check failed (%s)", type(exc).__name__)
        return False


def _sdk_harness_availability(canonical: str) -> HarnessAvailability:
    """Picker-facing readiness for an in-process SDK harness.

    ``True`` when any locally visible credential source could serve the
    harness at run time, else ``"needs-auth"``. The check errs toward ready
    and detects credential *sources*, not guaranteed-usable credentials:
    spec-level ``executor.auth`` and other runtime-only sources are invisible
    here, and a configured source may still fail to resolve at launch. Only
    a host where *nothing* local could authenticate reads ``"needs-auth"``
    — and that signal warns in the picker, it never blocks the launch
    (:func:`harness_is_configured` stays ungated for SDK harnesses).

    :param canonical: A canonical SDK harness id from :data:`_SDK_HARNESSES`.
    :returns: ``True`` or ``"needs-auth"``.
    """
    if canonical == "antigravity":
        return True if _antigravity_credential_configured() else HARNESS_NEEDS_AUTH
    if _family_provider_configured(canonical):
        return True
    if _family_fallback_provider_configured(canonical):
        return True
    if _global_auth_configured():
        return True
    family = _HARNESS_FAMILY.get(canonical)
    if family is not None and _ambient_family_env_key_configured(family):
        return True
    if family == ANTHROPIC_FAMILY and (
        _claude_token_env_configured()
        or _claude_managed_gateway_configured()
        or _claude_code_login_configured()
    ):
        return True
    if _databricks_workspace_configured():
        return True
    return HARNESS_NEEDS_AUTH


def _installer_only_availability(install_key: str) -> HarnessAvailability:
    """Return availability for a binary-gated harness without login commands.

    Mirrors :func:`_binary_availability_reason` but keeps the historical bare
    ``False`` shape for a missing binary, so existing web/clients that expect a
    simple boolean get that and only learn about structured reasons when the
    binary is present but on an unsupported version.
    """
    state = _binary_availability_reason(install_key)
    if state == HARNESS_BINARY_MISSING:
        return False
    return state


def _binary_availability_reason(install_key: str) -> HarnessAvailability:
    """Return the readiness reason when a CLI-backed harness can't be used.

    Distinguishes a genuinely missing CLI from one that is on ``PATH`` but
    outside the version range the native harness requires. The latter is
    exposed to the web UI as ``"version-too-low"`` so the user sees a prompt
    to upgrade rather than "binary-missing".
    """
    if harness_cli_installed(install_key, timeout=READINESS_CLI_PROBE_TIMEOUT_S):
        return True
    spec = harness_install_spec(install_key)
    if spec is not None and resolve_cli_binary(spec.binary) is not None:
        return HARNESS_VERSION_TOO_LOW
    return HARNESS_BINARY_MISSING


def _cli_family_availability(canonical: str, install_key: str) -> HarnessAvailability:
    """Two-step availability for a login-command CLI harness.

    :returns: ``"binary-missing"`` when the CLI isn't installed,
        ``"version-too-low"`` when the CLI is present but too old,
        ``"needs-auth"`` when installed but neither a configured provider
        credential nor a CLI login is present, else ``True``.
    """
    binary_state = _binary_availability_reason(install_key)
    if binary_state is not True:
        return binary_state
    if install_key == OPENCODE_KEY:
        from omnigent.onboarding.opencode_auth import opencode_auth_summary

        return True if opencode_auth_summary().has_provider else "needs-auth"
    # claude: ready when EITHER an omnigent-managed provider serves the family
    # (an API key / gateway the user set, incl. from the UI) OR the harness's
    # own subscription login is present (`claude auth status`, a subprocess —
    # the same probe the setup wizard uses; runs off the event loop on the
    # throttled readiness refresh). Checking the config first avoids the
    # subprocess on the common key-configured path.
    from omnigent.onboarding.harness_install import harness_cli_logged_in

    if _family_provider_configured(canonical):
        return True
    # Claude Code's own managed settings can carry a complete gateway credential
    # (enterprise ``ANTHROPIC_BASE_URL`` + ``apiKeyHelper``) that omnigent's
    # config knows nothing about, and that is what a claude-native launch
    # actually routes through. Credit it structurally (one local file read, no
    # subprocess) before the status probe, so an enterprise host reads ready
    # without depending on the probe resolving the CLI on PATH.
    if install_key == ANTHROPIC_FAMILY and _claude_managed_gateway_configured():
        return True
    return (
        True
        if harness_cli_logged_in(install_key, timeout=READINESS_CLI_PROBE_TIMEOUT_S)
        else "needs-auth"
    )


def _harness_availability(canonical: str) -> HarnessAvailability:
    """Return picker-facing availability for one canonical harness spelling."""
    if IS_WINDOWS and canonical in NATIVE_HARNESSES:
        return False
    if _is_codex_family_harness(canonical):
        from omnigent.harnesses.codex_native.main import _codex_auth_unavailable_reason

        return _codex_auth_unavailable_reason() or True
    install_key = _AUTH_AWARE_NATIVE_HARNESSES.get(canonical)
    if install_key is not None:
        # Cursor is auth-aware like the other native CLI harnesses, so a missing
        # binary surfaces as the structured ``"binary-missing"`` reason — not the
        # bare ``False`` it historically reported. That keeps the picker badge /
        # warning copy uniform across every CLI-backed native harness.
        return _cli_family_availability(canonical, install_key)
    if canonical in _PI_HARNESSES:
        # pi has no CLI login — its only credential is either an omnigent-managed
        # provider (an API key / gateway) or a pi-subscription ("Pi original auth",
        # which signals "use Pi's own ~/.pi/agent as-is"). So the two-step signal
        # is binary + provider: installed-but-no-provider is the yellow "needs-auth"
        # state the setup dialog acts on.
        binary_state = _binary_availability_reason(PI_KEY)
        if binary_state is not True:
            return binary_state
        if _family_provider_configured(PI_SURFACE):
            return True
        # A pi subscription (original auth) is also a valid configured state —
        # it means Pi will use its own ~/.pi/agent credentials, no omnigent
        # provider needed.
        try:
            provider = default_provider_for_harness(load_config(), PI_SURFACE)
            if (
                provider is not None
                and provider.kind == SUBSCRIPTION_KIND
                and provider.cli == "pi"
            ):
                return True
        except Exception:
            pass
        return "needs-auth"
    if canonical in _SDK_HARNESSES:
        return _sdk_harness_availability(canonical)
    return _harness_availability_core(canonical)


def harness_is_configured(harness: str) -> bool:
    """Return whether *harness* can be launched on this machine.

    Only CLI-wrapping harnesses are assessed (native Claude/Codex/Kiro and
    ``pi`` / ``pi-native``): they cannot run without their binary on
    ``PATH``, and that is the one thing the daemon can check reliably and
    locally. SDK harnesses and unknown harnesses always return ``True`` —
    spec-level credentials are invisible here, so blocking them would risk
    false negatives that break working launches. (The picker map separately
    reports ``"needs-auth"`` for an SDK harness with no locally visible
    credential — see :func:`_sdk_harness_availability` — but that signal
    warns, it never gates a launch.)

    The check is binary-only: an installed-but-not-logged-in CLI still
    returns ``True`` because auth failures surface at run time rather than
    blocking dispatch.

    :param harness: A harness id, e.g. ``"claude-native"``, ``"codex"``,
        ``"openai-agents"``, ``"agents_sdk"``, ``"kiro-native"``, ``"pi"``,
        ``"pi-native"``, ``"qwen"``, or ``"qwen-code"``.
    :returns: ``True`` when launchable (CLI installed, or a harness the
        daemon doesn't gate); ``False`` when the binary is missing or on
        an unsupported version.
    """
    return _harness_availability_core(harness) is True


def _is_codex_family_harness(canonical: str) -> bool:
    """Return whether a canonical harness uses Codex readiness semantics."""
    return (
        canonical in CODEX_CANONICAL_HARNESSES and _HARNESS_FAMILY.get(canonical) == OPENAI_FAMILY
    )


def configured_harness_map() -> dict[str, HarnessAvailability]:
    """Return per-harness readiness for every accepted harness spelling.

    Built so the server/web UI can do a plain dict lookup with whatever
    spelling it holds — canonical ids, executor-type spellings, the
    ``claude`` alias, and ``pi``. Unknown harnesses map to ``True`` (never
    gated); CLI-wrapping harnesses map to whether their binary is on
    ``PATH``; SDK harnesses map to ``True`` when a locally visible credential
    source could serve them, else ``"needs-auth"``. Codex entries use a
    structured string reason when unavailable: ``"binary-missing"`` or
    ``"needs-auth"``.

    :returns: Mapping of harness spelling to readiness, e.g.
        ``{"claude-native": False, "codex-native": "needs-auth",
        "claude-sdk": "needs-auth", "pi": True, "qwen": True}``.
    """
    spellings: set[str] = set(_HARNESS_FAMILY)
    spellings.update(valid_harnesses())
    spellings.update(harness_install_keys())
    spellings.update(_EXECUTOR_TYPE_HARNESS_ALIASES)
    spellings.update(HARNESS_ALIASES)
    spellings.update(_PI_HARNESSES)
    spellings.update(_OPENCODE_HARNESSES)
    spellings.update(_CURSOR_NATIVE_HARNESSES)
    spellings.update(_KIRO_NATIVE_HARNESSES)
    spellings.update(_GOOSE_NATIVE_HARNESSES)
    spellings.update(_KIMI_NATIVE_HARNESSES)
    spellings.update(_HERMES_NATIVE_HARNESSES)
    spellings.update(_DEVIN_NATIVE_HARNESSES)
    spellings.update(_QWEN_HARNESSES)
    spellings.add(CURSOR_KEY)
    spellings.add(KIMI_SURFACE)
    spellings.add(GOOSE_KEY)  # headless Goose (``goose acp``) gates on the goose binary
    spellings.add(HERMES_KEY)  # Hermes Agent wraps the ``hermes`` CLI
    spellings.add(COPILOT_KEY)
    canonical_by_cache_key: dict[tuple[str, ...], str] = {}
    cache_key_by_spelling: dict[str, tuple[str, ...]] = {}
    for spelling in spellings:
        canonical = _canonical_harness(spelling)
        # Windows-native Codex must not share plain Codex's readiness cache entry.
        if _is_codex_family_harness(canonical) and not (
            IS_WINDOWS and canonical in NATIVE_HARNESSES
        ):
            cache_key: tuple[str, ...] = ("codex",)
        else:
            cache_key = ("harness", canonical)
        canonical_by_cache_key.setdefault(cache_key, canonical)
        cache_key_by_spelling[spelling] = cache_key

    with ThreadPoolExecutor(
        max_workers=min(_READINESS_PROBE_MAX_WORKERS, len(canonical_by_cache_key)),
        thread_name_prefix="harness-readiness",
    ) as executor:
        futures = {
            cache_key: executor.submit(_harness_availability, canonical)
            for cache_key, canonical in canonical_by_cache_key.items()
        }
        availability_cache = {cache_key: future.result() for cache_key, future in futures.items()}

    return {
        spelling: availability_cache[cache_key]
        for spelling, cache_key in cache_key_by_spelling.items()
    }
