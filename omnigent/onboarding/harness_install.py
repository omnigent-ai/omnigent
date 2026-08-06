"""Harness CLI install + auth operations — shared by ``run`` and ``configure``.

A coding harness is "ready" along two independent axes:

- **configured** — a usable model credential serves its family (resolved via
  :func:`omnigent.onboarding.provider_config.default_provider_for_harness`
  over the ambient-merged config). That lives in the provider layer.
- **installed** — the harness's CLI binary is on ``PATH``. This module owns
  that axis, mirroring how ``ucode`` checks (``shutil.which(binary)``) and the
  npm packages it installs.

``omni setup --no-internal-beta`` uses this to mark an uninstalled harness and
offer to ``npm install`` it; the first-run ``omnigent run`` flow uses the
same map so the two surfaces never disagree about what the machine can launch.

This module also owns the per-harness **CLI binary name**, so it is the natural
home for driving each harness's own *subscription login/logout* commands
(:func:`harness_login` / :func:`harness_logout`) — letting ``configure
harnesses`` be the single place a user signs in or out of Claude / Codex rather
than running ``codex login`` / ``claude auth login`` by hand.

The "is the CLI logged in?" verdict (:func:`harness_cli_logged_in`) asks the
CLI itself (``claude auth status`` / ``codex login status`` / ``agy models``)
rather than reading a credential file, because the file location is
**platform-specific**
— Claude Code stores its OAuth tokens in the macOS Keychain (not
``~/.claude/.credentials.json``) on macOS, so a file check would falsely report
"not logged in" right after a successful ``claude auth login``. The CLI's own
status command reads wherever it actually stored the credential, so login
verification is correct on every platform. (Ambient detection in
:mod:`omnigent.onboarding.ambient` is file-based and subprocess-free on
Linux; on macOS it reuses :func:`harness_cli_logged_in` as a Keychain fallback
when the credentials file is absent — see ``ambient._claude_login_detected``.)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

from packaging.version import InvalidVersion, Version

from omnigent._platform import resolve_cli_binary
from omnigent.acp_cli_harnesses import ACP_CLI_HARNESSES
from omnigent.harness_install_spec import HarnessInstallSpec, SetupStep
from omnigent.onboarding.provider_config import ANTHROPIC_FAMILY, GEMINI_FAMILY, OPENAI_FAMILY
from omnigent.opencode_native_client import (
    OPENCODE_MAX_VERSION_EXCLUSIVE,
    OPENCODE_MIN_VERSION,
)

# Pi is not a configure-menu family (the menu is Claude + Codex), but the
# first-run ``run`` flow falls back to it, so it has install metadata too.
PI_KEY = "pi"

# Qwen Code uses npm installation and has login/logout commands similar to
# other coding CLIs. The binary name is ``qwen``.
QWEN_KEY = "qwen"

# Cursor authenticates against its own backend (``cursor-agent login`` /
# ``CURSOR_API_KEY``) with no provider/gateway credential, and ships via a curl
# installer rather than npm — so it carries an ``install_hint``, not a ``package``.
CURSOR_KEY = "cursor"

# Kimi authenticates against Moonshot AI's backend (``kimi login`` OAuth or a
# Moonshot API key), not via the ambient provider config; like Cursor it ships
# via a curl installer rather than npm, so it carries an ``install_hint``.
KIMI_KEY = "kimi"

# Kiro authenticates against its own backend and ships as a standalone native
# installer, not an npm package managed by ``omni setup``.
KIRO_KEY = "kiro"

# Minimum CLI versions for native harnesses where the runtime has a known
# feature floor. These are intentionally conservative: the runtime may
# gracefully degrade on older CLIs, but setup enforces the floor so a user
# isn't surprised by missing behaviour (e.g. policy hooks, non-interactive
# approval, forwarder schema) after launching.
# Sources:
# - codex: native policy hook requires >= 0.129.0
#   (`omnigent/codex_native_app_server.py`).
# - pi: non-interactive ``--approve`` override requires >= 0.79.0
#   (``omnigent/pi_native.py``).
# - qwen: ``--input-file`` / ``--json-file`` bridge verified on v0.18.1
#   (``omnigent/qwen_native_forwarder.py`` / ``docs/QWEN_NATIVE_DESIGN.md``).
# - goose: SQLite forwarder schema verified on Goose 1.38.0
#   (``omnigent/goose_native_forwarder.py``).
# - hermes: parent_session_id schema introduced in v0.17.0
#   (``omnigent/hermes_native_forwarder.py``).
# - kiro: MCP config schema (``{"mcpServers": ...}``) verified on kiro-cli 2.10.0
#   (``omnigent/kiro_native_bridge.py``).
# - claude: `--mcp-config` (required by the native bridge) introduced long
#   before 2026-06-01. The first Claude Code release after the cutoff is
#   2.1.161, so use that as the supported floor.
# - codex: native policy hook requires >= 0.129.0, but that shipped before
#   2026-06-01. The first Codex release after the cutoff is 0.137.0. The
#   subagent-router ``PreToolUse`` hook needs 0.145.0, but that is enforced
#   where the hook is registered
#   (``codex_native_app_server._CODEX_ROUTING_HOOK_MIN_VERSION``) so an older
#   CLI loses only smart-routing spawn gating, not the ability to launch.
# - cursor: Cursor's CLI uses ``YYYY.MM.DD[-build]`` date versions. Default
#   to the day after 2026-06-01 so we don't support stale pre-June builds.
# - kimi: first ``kimi-cli`` release after 2026-06-01 is 1.47.0
#   (https://github.com/MoonshotAI/kimi-cli/blob/main/CHANGELOG.md).
# - hermes: parent_session_id schema was introduced in v0.17.0, but Hermes now
#   ships date-tagged releases; the first one after 2026-06-01 is 2026.06.05.
_CODEX_MIN_VERSION = "0.137.0"
_PI_MIN_VERSION = "0.79.0"
_QWEN_MIN_VERSION = "0.18.1"
_GOOSE_MIN_VERSION = "1.38.0"
_HERMES_MIN_VERSION = "2026.06.05"
_KIRO_MIN_VERSION = "2.10.0"
_CLAUDE_MIN_VERSION = "2.1.161"
_CURSOR_MIN_VERSION = "2026.06.02"
_KIMI_MIN_VERSION = "1.47.0"

# OpenCode native harness CLI (``opencode serve`` / ``opencode attach``),
# installed via the ``opencode-ai`` npm package. No login/logout/status argv
# is wired yet — readiness is binary-only until an auth check exists.
OPENCODE_KEY = "opencode"

# Goose authenticates against its own config (``goose configure`` → keyring /
# ``~/.config/goose/config.yaml``) with no Omnigent-managed credential, and ships
# via Homebrew / a curl installer rather than npm — so it carries an
# ``install_hint``, not a ``package``.
GOOSE_KEY = "goose"

# Copilot runs in-process via the ``github-copilot-sdk`` package, which bundles
# the Copilot CLI binary it drives — so, like cursor, there is no separately
# installed CLI to gate on; readiness is whether a GitHub token resolves (see
# :func:`omnigent.onboarding.harness_readiness.harness_is_configured`). The key
# is kept here purely as the canonical harness id the readiness layer shares.
COPILOT_KEY = "copilot"

# Hermes Agent is installed via a curl installer from Nous Research and
# authenticates through its own ``hermes model`` interactive flow (no
# Omnigent-managed credentials). The ``hermes`` binary must be on PATH.
HERMES_KEY = "hermes"

_HERMES_INSTALL_HINT = "curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash"


# Keyed by harness family (Claude=anthropic, Codex=openai) plus the pi
# fallback. Binaries/packages mirror ucode's ``TOOL_SPECS`` so the two tools
# install the same thing. Login/logout argv use each CLI's first-class auth
# subcommands (``claude auth login --claudeai`` / ``codex login``), so the user
# can sign in to a subscription from ``configure harnesses`` directly.
_HARNESS_INSTALL: dict[str, HarnessInstallSpec] = {
    ANTHROPIC_FAMILY: HarnessInstallSpec(
        "Claude",
        "claude",
        "@anthropic-ai/claude-code",
        login_args=("auth", "login", "--claudeai"),
        logout_args=("auth", "logout"),
        status_args=("auth", "status"),
        login_status_key="loggedIn",
        # The native bridge injects Omnigent's MCP relay via `--mcp-config`;
        # that flag first shipped in Claude Code 0.2.75.
        min_version=_CLAUDE_MIN_VERSION,
    ),
    OPENAI_FAMILY: HarnessInstallSpec(
        "Codex",
        "codex",
        "@openai/codex",
        login_args=("login",),
        logout_args=("logout",),
        status_args=("login", "status"),
        # The native Codex policy hook requires ``codex >= 0.129.0``;
        # anything older silently disables tool-call enforcement. Setup
        # enforces the same floor up-front. Smart Routing's spawn hook wants
        # 0.145.0, but it is gated at its own registration site so it degrades
        # to "no spawn gate" instead of blocking every codex launch.
        min_version=_CODEX_MIN_VERSION,
    ),
    PI_KEY: HarnessInstallSpec(
        "Pi",
        "pi",
        "@earendil-works/pi-coding-agent",
        # The ``--approve`` / non-interactive trust override requires
        # ``pi >= 0.79.0``; older CLIs would prompt mid-session.
        min_version=_PI_MIN_VERSION,
    ),
    # Pin the install to the supported 1.17.x range: opencode-ai's npm ``latest``
    # is a ``0.0.0-beta-*`` pre-release, so a bare ``opencode-ai`` would install a
    # version the runtime version-check (``check_opencode_version``,
    # >=1.17.7,<1.18.0) then rejects. ``~1.17.7`` mirrors that exact range.
    # The same version bounds are enforced in setup via ``min_version`` /
    # ``max_version_exclusive`` so the install/upgrade prompt fires before
    # the runtime gate does.
    OPENCODE_KEY: HarnessInstallSpec(
        "OpenCode",
        "opencode",
        "opencode-ai@~1.17.7",
        min_version=OPENCODE_MIN_VERSION,
        max_version_exclusive=OPENCODE_MAX_VERSION_EXCLUSIVE,
    ),
    QWEN_KEY: HarnessInstallSpec(
        "Qwen Code",
        "qwen",
        "@qwen-code/qwen-code",
        # NB: deliberately no login/logout/status args. Qwen *removed* its
        # ``auth`` subcommand and has no CLI login — ``qwen login`` doesn't
        # exist and ``qwen auth status`` prints "auth has been removed" and
        # exits 0 (which would make harness_cli_logged_in falsely report a
        # login via its exit-code fallback). Auth is via OpenAI-compatible env
        # vars or the interactive ``/auth`` command; the setup wizard handles
        # that in ``_manage_qwen_harness``. Leaving these None keeps
        # harness_login/logout/cli_logged_in no-ops for qwen.
        min_version=_QWEN_MIN_VERSION,
    ),
    CURSOR_KEY: HarnessInstallSpec(
        "Cursor",
        "cursor-agent",
        package=None,
        login_args=("login",),
        logout_args=("logout",),
        status_args=("status", "--format", "json"),
        install_hint="curl https://cursor.com/install -fsS | bash",
        login_status_key="isAuthenticated",
        # Cursor CLI versions are calendar dates; only support builds from
        # after 2026-06-01 for the native harness path.
        min_version=_CURSOR_MIN_VERSION,
    ),
    # Kimi Code CLI ships a single-binary ``kimi`` via a curl installer (no
    # npm). ``kimi login`` is the interactive provider login (OAuth or a
    # Moonshot API key). ``status_args`` is intentionally ``None``: kimi has
    # no first-class "am I logged in?" exit-code probe — login state is
    # inspected file-based via ``kimi_auth.kimi_login_detected`` instead. With
    # ``None`` the login path runs every time the operator asks for it
    # (interactive, so they can cancel if already authenticated).
    # ``logout_args`` is ``None`` because kimi has no ``kimi logout`` subcommand
    # (verified against kimi CLI v0.29.1 — ``kimi logout`` errors "unknown
    # command"), so ``harness_logout`` is a no-op for it (same as Qwen / agy).
    KIMI_KEY: HarnessInstallSpec(
        "Kimi",
        "kimi",
        package=None,
        login_args=("login",),
        install_hint="curl -fsSL https://code.kimi.com/kimi-code/install.sh | bash",
        # First kimi-cli release after 2026-06-01. Older builds may lack
        # newer TUI/session wiring needed by the native harness.
        min_version=_KIMI_MIN_VERSION,
    ),
    KIRO_KEY: HarnessInstallSpec(
        "Kiro",
        "kiro-cli",
        package=None,
        install_hint="curl -fsSL https://cli.kiro.dev/install | bash",
        min_version=_KIRO_MIN_VERSION,
    ),
    # The native Antigravity (agy) TUI bridge wraps the ``agy`` CLI. agy's auth
    # service is reached by launching ``agy`` itself (no login subcommand); after
    # the browser flow completes, ``agy models`` exits 0 and provides the same
    # revocation-aware status probe Codex gets from ``codex login status``. An
    # empty ``login_args`` tuple intentionally means “run the binary with no
    # arguments” in ``harness_login``. ``agy`` ships via a shell installer rather
    # than npm, so ``package`` is ``None`` and the manual command lives in
    # ``install_hint`` (shown as guidance; ``install_harness_cli`` refuses to
    # auto-run it).
    GEMINI_FAMILY: HarnessInstallSpec(
        "Antigravity",
        "agy",
        package=None,
        login_args=(),
        status_args=("models",),
        install_hint="curl -fsSL https://antigravity.google/cli/install.sh | bash",
        auth_hint="run `agy` and complete the browser sign-in",
    ),
    GOOSE_KEY: HarnessInstallSpec(
        "Goose",
        "goose",
        package=None,
        install_hint="brew install block-goose-cli",
        min_version=_GOOSE_MIN_VERSION,
    ),
    HERMES_KEY: HarnessInstallSpec(
        "Hermes",
        "hermes",
        package=None,
        install_hint=_HERMES_INSTALL_HINT,
        install_command=("bash", "-c", _HERMES_INSTALL_HINT),
        min_version=_HERMES_MIN_VERSION,
    ),
}


# Maps an executor *harness identifier* (the value the runtime resolves from a
# spec's ``executor.config["harness"]`` / ``executor.type``) to its
# :data:`_HARNESS_INSTALL` family key. Only the CLI-backed harnesses appear
# here — the ones that cannot launch without a binary on ``PATH``:
# ``claude-native`` wraps the ``claude`` CLI, ``codex-native`` the ``codex``
# CLI, ``pi`` / ``pi-native`` the ``pi`` CLI, ``opencode-native`` the
# ``opencode`` CLI, ``qwen`` / ``qwen-code`` the ``qwen`` CLI,
# ``cursor-native`` / ``native-cursor`` the ``cursor-agent`` CLI, and
# ``kiro-native`` / ``native-kiro`` the ``kiro-cli`` CLI. Cursor and Kiro
# install out-of-band rather than through npm — see their ``install_hint``
# values.
# SDK-based harnesses run in-process and are deliberately absent, so they
# resolve to "no CLI required": ``claude-sdk``, ``codex``, ``openai-agents-sdk``,
# the in-process ``antigravity`` Gemini SDK harness, and the SDK ``cursor``
# harness (which drives the ``cursor-sdk`` Python package over its own bundled
# bridge, NOT the ``cursor-agent`` CLI).
_HARNESS_NAME_TO_KEY: dict[str, str] = {
    "claude-native": ANTHROPIC_FAMILY,
    "codex-native": OPENAI_FAMILY,
    PI_KEY: PI_KEY,
    "pi-native": PI_KEY,
    # Kimi is multi-provider but binary-gated: cannot launch without the
    # ``kimi`` CLI on PATH. Listed here so ``required_cli_for_harness``
    # returns its install spec and ``missing_harness_cli`` fails loud
    # before a subagent spawn.
    KIMI_KEY: KIMI_KEY,
    "cursor-native": CURSOR_KEY,
    "native-cursor": CURSOR_KEY,
    "kiro-native": KIRO_KEY,
    "native-kiro": KIRO_KEY,
    # The native agy TUI bridge wraps the ``agy`` CLI; both spellings map to
    # the Gemini family's install spec. (The in-process ``antigravity`` SDK
    # harness is deliberately absent — like the other SDK harnesses it needs no
    # CLI binary.)
    "antigravity-native": GEMINI_FAMILY,
    "native-antigravity": GEMINI_FAMILY,
    "goose-native": GOOSE_KEY,
    "native-goose": GOOSE_KEY,
    # Headless Goose (``harness: goose``, drives ``goose acp``) wraps the same
    # ``goose`` CLI as the native TUI, so it gates on the same binary.
    GOOSE_KEY: GOOSE_KEY,
    # Native Kimi TUI harness — same binary gate as the bare ``kimi`` surface.
    "kimi-native": KIMI_KEY,
    "native-kimi": KIMI_KEY,
    QWEN_KEY: QWEN_KEY,
    "qwen-code": QWEN_KEY,
    # Native qwen TUI (``qwen-native``) wraps the same ``qwen`` CLI as the ACP
    # harness; the ``native-qwen`` reversed spelling gates on the same binary.
    "qwen-native": QWEN_KEY,
    "native-qwen": QWEN_KEY,
    # Native OpenCode (``opencode-native``) wraps the ``opencode`` CLI; its
    # ``native-opencode`` reversed spelling gates on the same binary.
    "opencode-native": OPENCODE_KEY,
    "native-opencode": OPENCODE_KEY,
    # Hermes Agent (``harness: hermes``) wraps the ``hermes`` CLI.
    HERMES_KEY: HERMES_KEY,
    # Native Hermes TUI (``hermes-native``, via ``omni hermes``) wraps the same
    # ``hermes`` CLI as the headless harness; ``native-hermes`` reversed spelling
    # gates on the same binary.
    "hermes-native": HERMES_KEY,
    "native-hermes": HERMES_KEY,
}


# UI-installable harnesses: the identifiers the web UI's New Chat dialog may
# request an install for, mapped to their :data:`_HARNESS_INSTALL` key. Single
# source of truth for both the host install handler (which runs the installer)
# and the server route (which allowlists the request). Scope is deliberately
# narrow — npm-installable, key/env-auth harnesses only; curl/brew/shell
# installers (cursor, kimi, hermes, …) are absent, so an install request for
# them is rejected before any installer runs.
_UI_INSTALLABLE_HARNESS_TO_KEY: dict[str, str] = {
    "claude": ANTHROPIC_FAMILY,
    "codex": OPENAI_FAMILY,
    PI_KEY: PI_KEY,
    OPENCODE_KEY: OPENCODE_KEY,
    QWEN_KEY: QWEN_KEY,
}

# Builtin ACP CLI harnesses (omnigent/acp_cli_harnesses.py) with an npm package
# are one-click installable; rows shipping via curl/shell installers stay out,
# like cursor/kimi above.
for _acp_name, _acp_row in ACP_CLI_HARNESSES.items():
    if _acp_row.install.package is not None:
        for _acp_spelling in (_acp_name, *_acp_row.aliases):
            _UI_INSTALLABLE_HARNESS_TO_KEY[_acp_spelling] = _acp_name


# Family keys the UI may install, derived once from the allowlist so the
# executor-spelling fallback in ``ui_install_key`` can't admit a non-installable
# family (e.g. cursor) that happens to share the name map.
_UI_INSTALLABLE_KEYS: frozenset[str] = frozenset(_UI_INSTALLABLE_HARNESS_TO_KEY.values())


def ui_install_key(harness: str) -> str | None:
    """Resolve a harness identifier to its UI-installable install-spec key.

    Accepts both the bare install ids (``"claude"``, ``"codex"``, ``"pi"``,
    ``"opencode"``, ``"qwen"``) and the executor spellings a session actually
    carries — the native TUI wrappers (``"codex-native"``, ``"qwen-native"``,
    …) resolve through the shared :data:`_HARNESS_NAME_TO_KEY` map to the same
    family key. Any harness that doesn't map onto the UI-installable family set
    (SDK harnesses like ``"claude-sdk"``, or curl/OAuth harnesses like
    ``"cursor"``/``"hermes"``) returns ``None`` so the caller rejects it.

    :param harness: A harness identifier from the web UI, e.g. ``"claude"`` or
        ``"codex-native"``.
    :returns: The :data:`_HARNESS_INSTALL` key (e.g. ``"anthropic"``) when the
        harness is UI-installable; ``None`` otherwise (caller rejects it).
    """
    direct = _UI_INSTALLABLE_HARNESS_TO_KEY.get(harness)
    if direct is not None:
        return direct
    # Fall back to the executor-spelling map, but only accept keys that are
    # themselves UI-installable — this keeps curl/OAuth harnesses (cursor,
    # hermes, …) out even though they appear in _HARNESS_NAME_TO_KEY.
    key = _all_harness_name_to_key().get(harness)
    if key is not None and key in _UI_INSTALLABLE_KEYS:
        return key
    return None


def ui_installable_harnesses() -> frozenset[str]:
    """Return every harness identifier the web UI may install.

    Includes the bare install ids and all executor spellings that resolve to a
    UI-installable family (e.g. ``"codex-native"``, ``"qwen-native"``), so the
    New Chat dialog can offer setup for the harness a session actually declares
    — not just the bare ids.

    :returns: The full set of accepted harness identifiers, e.g.
        ``{"claude", "claude-native", "codex", "codex-native", "pi", ...}``.
    """
    resolvable = set(_UI_INSTALLABLE_HARNESS_TO_KEY)
    for name, mapped in _all_harness_name_to_key().items():
        if mapped in _UI_INSTALLABLE_KEYS:
            resolvable.add(name)
    return frozenset(resolvable)


# The families whose credential the UI can WRITE (Claude/Codex/Pi). A strict
# subset of the installable families: opencode/qwen are installable but env-auth
# (omnigent stores no key for them), so they are NOT credential-configurable.
# ``pi`` consumes anthropic/openai and is handled by the host store-secret
# handler, so it's included via its own key.
_UI_CREDENTIAL_FAMILIES: frozenset[str] = frozenset({ANTHROPIC_FAMILY, OPENAI_FAMILY, PI_KEY})


def ui_credential_configurable_harnesses() -> frozenset[str]:
    """Return every harness identifier the web UI may write a credential for.

    A strict subset of :func:`ui_installable_harnesses`: only harnesses whose
    provider credential omnigent owns (Claude / Codex / Pi families). The
    env-auth harnesses (opencode, qwen) are installable but excluded — the host
    store-secret handler can't configure them — so the credential route can
    reject them with a clean 400 rather than forwarding a frame the host fails.

    :returns: The set of harness identifiers accepted by the credential route,
        e.g. ``{"claude", "claude-native", "codex", "codex-native", "pi", ...}``.
    """
    resolvable = {
        h for h, key in _UI_INSTALLABLE_HARNESS_TO_KEY.items() if key in _UI_CREDENTIAL_FAMILIES
    }
    for name, mapped in _all_harness_name_to_key().items():
        if mapped in _UI_CREDENTIAL_FAMILIES:
            resolvable.add(name)
    return frozenset(resolvable)


# The auth step per UI-installable family, for the setup checklist. These are
# display-only checklist rows (the command is shown for the user to run on the
# host, never executed server-side), so the commands are literal here rather
# than derived from ``HarnessInstallSpec.login_args`` — keep them in sync with
# that spec by hand if a harness's login command changes.
# ``command`` steps run on the host and are status-tracked; ``setup`` steps
# ``command`` steps run on the host and are status-tracked; ``auth`` steps
# (pi) open the UI credential form and are status-tracked; ``setup`` steps
# (qwen: env-auth, not UI-authable) point at ``omni setup`` and don't track.
#   claude/codex: subscription login via the CLI's own login command.
#   opencode: its own `opencode auth login`.
#   pi: a provider credential (API key / gateway / adopt) written from the UI.
#   qwen: env-auth — omnigent stores no key, so it stays a setup signpost.
_UI_AUTH_STEP_BY_KEY: dict[str, SetupStep] = {
    ANTHROPIC_FAMILY: SetupStep(
        kind="auth",
        # UI-authable: the dialog opens a form listing every way to authenticate
        # (subscription login, API key, gateway, or adopting a detected key), so
        # the row is a neutral "set up auth" rather than naming just one path —
        # the subscription ``command`` below is one option inside that form, not
        # the whole step.
        title="Set up authentication",
        detail="Sign in with your Claude subscription, an API key, or a gateway.",
        action="auth",
        command="claude auth login --claudeai",
        status_key="authed",
    ),
    OPENAI_FAMILY: SetupStep(
        kind="auth",
        title="Set up authentication",
        detail="Sign in with your ChatGPT subscription, an API key, or a gateway.",
        action="auth",
        command="codex login",
        status_key="authed",
    ),
    OPENCODE_KEY: SetupStep(
        kind="auth",
        title="Sign in to OpenCode",
        detail="OpenCode manages its own credentials — sign in on the host.",
        action="command",
        command="opencode auth login",
        status_key="authed",
    ),
    PI_KEY: SetupStep(
        kind="auth",
        # Same neutral "set up auth" framing as claude/codex — the dialog opens a
        # form listing the ways to authenticate. Pi has NO subscription CLI login
        # (``command=None``, so the form omits that option), so the detail names
        # only the applicable paths. ``status_key="authed"`` keeps the step
        # trackable so it isn't dropped as "unknown" (which would wrongly read
        # "ready").
        title="Set up authentication",
        detail="Add an API key or a gateway so Pi can run.",
        action="auth",
        command=None,
        status_key="authed",
    ),
    QWEN_KEY: SetupStep(
        kind="auth",
        title="Add a Qwen credential",
        # Qwen is env-auth (not UI-authable): omnigent stores no key for it, so
        # this stays an untrackable signpost pointing at the CLI.
        detail="Qwen needs an API key or gateway. Set it up on the host for now.",
        action="setup",
        command="omni setup",
        status_key=None,
    ),
}

# Builtin ACP CLI harnesses own their credentials (vendor CLI login), so each
# row with a login command gets a run-on-host auth step, untracked like qwen's.
for _acp_name, _acp_row in ACP_CLI_HARNESSES.items():
    _acp_login = _acp_row.login_command
    if _acp_login is not None:
        _UI_AUTH_STEP_BY_KEY[_acp_name] = SetupStep(
            kind="auth",
            title=f"Sign in to {_acp_row.label}",
            detail=f"{_acp_row.label} manages its own credentials; sign in on the host.",
            action="command",
            command=_acp_login,
            status_key=None,
        )


def ui_setup_steps(harness: str) -> list[SetupStep]:
    """Return the ordered setup checklist for a UI harness identifier.

    Mirrors what ``omni setup`` walks a user through for the harness: an
    install step, then (for the five first-class families) an auth step. The
    install step's label uses the harness's :class:`HarnessInstallSpec` display
    name; the auth step's command is a display-only literal from
    :data:`_UI_AUTH_STEP_BY_KEY` (shown for the user to run, not executed).
    Harnesses outside the UI-installable set get a single generic
    "run ``omni setup``" step (M1 scope).

    :param harness: A harness identifier the UI holds, e.g. ``"codex"`` or the
        native spelling ``"codex-native"`` (both resolve to the same steps).
    :returns: Ordered :class:`SetupStep` list; never empty.
    """
    key = ui_install_key(harness)
    if key is None:
        # Not UI-installable (curl/OAuth/SDK harness): one generic step.
        return [
            SetupStep(
                kind="install",
                title="Set up on the host",
                detail="Run omni setup on the host to configure this agent.",
                action="setup",
                command="omni setup",
                status_key=None,
            )
        ]

    spec = _all_harness_install().get(key)
    display = spec.display if spec is not None else harness
    steps = [
        SetupStep(
            kind="install",
            title=f"Install {display}",
            detail=f"We'll install {display} on the host for you.",
            action="install",
            command=None,
            status_key="installed",
        )
    ]
    auth = _UI_AUTH_STEP_BY_KEY.get(key)
    if auth is not None:
        steps.append(auth)
    return steps


def _all_harness_install() -> dict[str, HarnessInstallSpec]:
    from omnigent.harness_plugins import install_specs

    merged = dict(_HARNESS_INSTALL)
    merged.update(install_specs())
    return merged


def _all_harness_name_to_key() -> dict[str, str]:
    from omnigent.harness_plugins import harness_install_keys

    merged = dict(_HARNESS_NAME_TO_KEY)
    merged.update(harness_install_keys())
    return merged


def required_cli_for_harness(harness: str) -> HarnessInstallSpec | None:
    """Return the CLI a harness needs on ``PATH`` to launch, or ``None``.

    :param harness: An executor harness identifier, e.g. ``"pi"``,
        ``"claude-native"``, ``"codex-native"``, or an SDK harness like
        ``"claude-sdk"``.
    :returns: The :class:`HarnessInstallSpec` whose ``binary`` must be on
        ``PATH`` for *harness* to start; ``None`` for SDK-based / unknown
        harnesses that need no CLI binary.
    """
    key = _all_harness_name_to_key().get(harness)
    return _all_harness_install().get(key) if key is not None else None


def missing_harness_cli(harness: str) -> HarnessInstallSpec | None:
    """Return a harness's required CLI spec when that CLI can't be used.

    Combines :func:`required_cli_for_harness` with the same probe
    :func:`harness_cli_installed` uses, so the verdict matches what the
    harness's own launch will see (both check ``PATH`` plus the common global
    install dirs the host daemon's frozen ``PATH`` may omit, and now also the
    declared version range). Used by sub-agent dispatch to fail loud *before*
    spawning a worker whose harness can never boot here, instead of letting
    the missing or incompatible binary surface as a lazy, generic turn failure.

    :param harness: An executor harness identifier, e.g. ``"pi"`` or
        ``"claude-native"``.
    :returns: The :class:`HarnessInstallSpec` for a CLI-backed harness whose
        ``binary`` is not on ``PATH`` or is outside its supported version range;
        ``None`` when the harness needs no CLI (SDK-based / unknown) or the
        required binary is present and version-compatible.
    """
    spec = required_cli_for_harness(harness)
    if spec is None:
        return None
    install_key = _all_harness_name_to_key().get(harness)
    if install_key is not None and harness_cli_installed(install_key):
        return None
    return spec


def harness_setup_hint(harness: str | None) -> str:
    """Return actionable remediation when *harness* can't launch on a machine.

    Most CLI harnesses (``claude``/``codex``/``pi``) install via npm and a
    model credential, both of which ``omni setup`` handles — so they route
    there. But a harness whose CLI ships out-of-band (``cursor-agent``, via
    Cursor's own curl installer rather than npm — it carries an ``install_hint``
    and no ``package``) is **not** installed by ``omni setup``: pointing a
    native-Cursor user there is a dead end, since setup only configures the
    SDK-based ``cursor`` harness (``cursor-sdk`` + ``CURSOR_API_KEY``). For
    those, name the vendor installer and the CLI's own login instead.

    :param harness: An executor harness identifier, e.g. ``"cursor-native"``,
        ``"claude-native"``, or ``"codex"``; ``None`` falls back to the
        ``omni setup`` hint.
    :returns: A remediation clause for the "harness not configured" message,
        e.g. ``"install the cursor-agent CLI on that machine with `curl
        https://cursor.com/install -fsS | bash`, then run `cursor-agent
        login`"`` for native Cursor, or the ``omni setup`` hint otherwise.
    """
    spec = required_cli_for_harness(harness or "")
    if spec is not None and spec.package is None and spec.install_hint:
        login = ""
        if spec.login_args:
            login = f", then run `{spec.binary} {' '.join(spec.login_args)}`"
        elif spec.auth_hint:
            login = f", then {spec.auth_hint}"
        return f"install the {spec.binary} CLI on that machine with `{spec.install_hint}`{login}"
    return "run `omni setup` on that machine to install the CLI and set a default credential"


_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+(?:[-.][0-9A-Za-z]+)*)")


def _normalize_date_version(version: str) -> str:
    """Trim date-shaped versions (``YYYY.MM.DD[-build]``) to their date part.

    Cursor's CLI reports versions like ``2026.07.01-777f564`` or
    ``2026.06.19-20-24-33-653a7fb`` on Windows. ``packaging`` rejects those
    as PEP 440, but the leading ``YYYY.MM.DD`` is enough to compare chronology.
    Normalizing keeps semver versions intact so existing parsing is unaffected.
    """
    parts = re.split(r"[.-]", version.replace("_", "-"))
    if len(parts) < 3:
        return version
    try:
        year = int(parts[0])
        month = int(parts[1])
        day = int(parts[2])
    except ValueError:
        return version
    # Treat plausible calendar dates (year 2000-2199) as date versions.
    if 2000 <= year <= 2199 and 1 <= month <= 12 and 1 <= day <= 31:
        return f"{year}.{month:02d}.{day:02d}"
    return version


def _parse_harness_cli_version(text: str) -> str | None:
    """Extract a semver-ish string from ``<binary> --version`` output.

    Mirrors the OpenCode-specific parser in
    :func:`omnigent.opencode_native_app_server.parse_opencode_version` but is
    kept generic so any harness can declare a version range in its install spec.
    Date-shaped versions (e.g. Cursor's ``2026.06.22`` or
    ``2026.06.19-20-24-33-653a7fb``) are normalized to ``YYYY.MM.DD``.
    """
    match = _VERSION_RE.search(text or "")
    if match is None:
        return None
    return _normalize_date_version(match.group(1))


# Wall-clock cap on a harness CLI probe subprocess (``--version`` /
# ``auth status``). The default stays lenient for setup and launch gating; the
# throttled readiness refresh passes ``READINESS_CLI_PROBE_TIMEOUT_S`` so a hung
# CLI can't stall the refresh — and, through it, the host tunnel's keepalive.
# The readiness cap matches goose's status-probe budget (``_INFO_TIMEOUT_S``):
# enough for a healthy ``auth status`` keychain read / token refresh, short
# enough that a wedged CLI fails fast.
_DEFAULT_CLI_PROBE_TIMEOUT_S = 30.0
READINESS_CLI_PROBE_TIMEOUT_S = 10.0


def _harness_cli_version_satisfies(
    spec: HarnessInstallSpec,
    binary: str,
    timeout: float = _DEFAULT_CLI_PROBE_TIMEOUT_S,
) -> bool:
    """Check *binary*'s ``--version`` against *spec*'s declared range.

    A missing/unparseable version or a subprocess error is treated as not
    satisfying the range, so an installed but incompatible CLI is reported
    as not ready and the setup flow prompts for an upgrade before the
    runtime gate rejects it.

    :param timeout: Seconds to wait for the ``--version`` subprocess before
        giving up, e.g. ``10.0`` on the readiness path.
    """
    if spec.min_version is None and spec.max_version_exclusive is None:
        return True
    version = _harness_cli_version_string(spec, binary, timeout)
    if version is None:
        return False
    try:
        parsed = Version(version)
    except InvalidVersion:
        return False
    if spec.min_version is not None:
        try:
            if parsed < Version(spec.min_version):
                return False
        except InvalidVersion:
            return False
    if spec.max_version_exclusive is not None:
        try:
            if parsed >= Version(spec.max_version_exclusive):
                return False
        except InvalidVersion:
            return False
    return True


def harness_install_spec(key: str) -> HarnessInstallSpec | None:
    """Return the install spec for a family/harness key, or ``None``.

    :param key: A harness family (``"anthropic"`` / ``"openai"``) or
        :data:`PI_KEY` (``"pi"``).
    :returns: The :class:`HarnessInstallSpec`, or ``None`` for an unknown key
        (e.g. a gateway-only family with no dedicated CLI).
    """
    return _all_harness_install().get(key)


def harness_cli_version_satisfies(key: str) -> bool:
    """Return whether the installed CLI for *key* satisfies its version range.

    Only harnesses that declare ``min_version`` / ``max_version_exclusive`` in
    their install spec are probed. A missing binary, an unparsable version, or
    a subprocess error is treated as not satisfying the range — the setup flow
    will then prompt for an upgrade before the runtime gate rejects it.

    :param key: A harness family key, e.g. :data:`OPENCODE_KEY`.
    :returns: ``True`` when the binary is present and its version falls inside
        the declared range, or when the spec has no version bounds.
    """
    spec = harness_install_spec(key)
    if spec is None:
        return False
    binary = resolve_cli_binary(spec.binary)
    if binary is None:
        return False
    return _harness_cli_version_satisfies(spec, binary)


def harness_cli_installed(key: str, timeout: float = _DEFAULT_CLI_PROBE_TIMEOUT_S) -> bool:
    """Return whether the harness's CLI is present and meets its version range.

    "Installed" now means the CLI binary (:func:`resolve_cli_binary`) is
    resolvable **and**, when the harness declares ``min_version`` /
    ``max_version_exclusive`` in its install spec, the binary's
    ``--version`` output satisfies that range. This prevents an outdated
    native CLI (e.g. an OpenCode release outside the supported 1.17.x band)
    from being treated as ready during setup.

    :param key: A harness family (``"anthropic"`` / ``"openai"``) or
        :data:`PI_KEY` / :data:`KIMI_KEY`.
    :param timeout: Seconds to wait for the ``--version`` probe subprocess,
        e.g. ``10.0`` on the readiness path where a hung CLI must not stall
        the refresh.
    :returns: ``True`` when the CLI resolves and is version-compatible;
        ``False`` when it doesn't resolve, the key has no associated CLI,
        or its version falls outside the declared range.
    """
    spec = harness_install_spec(key)
    if spec is None:
        return False
    binary = resolve_cli_binary(spec.binary)
    if binary is None:
        return False
    return _harness_cli_version_satisfies(spec, binary, timeout)


def harness_cli_version(key: str) -> tuple[str | None, str | None]:
    """Return the installed CLI's version string plus the declared range.

    Useful for human-readable status messages when the CLI is present but
    outside its supported range, so the UI can say "installed vX, required >=Y"
    instead of just "not installed".

    :param key: A harness family key, e.g. :data:`OPENCODE_KEY`.
    :returns: ``(version, range_str)``. ``version`` is ``None`` when the binary
        is missing or its ``--version`` output is unparseable. ``range_str``
        is a human-readable summary of the declared ``min_version`` /
        ``max_version_exclusive`` range, or ``None`` when the spec has no
        version bounds.
    """
    spec = harness_install_spec(key)
    if spec is None:
        return None, None
    binary = resolve_cli_binary(spec.binary)
    if binary is None:
        return None, None
    version = _harness_cli_version_string(spec, binary)
    if version is None:
        return None, _version_range_str(spec)
    return version, _version_range_str(spec)


def _version_range_str(spec: HarnessInstallSpec) -> str | None:
    """Human-readable rendering of a spec's version range, or ``None``."""
    if spec.min_version is None and spec.max_version_exclusive is None:
        return None
    if spec.min_version is not None and spec.max_version_exclusive is None:
        return f">={spec.min_version}"
    if spec.min_version is None and spec.max_version_exclusive is not None:
        return f"<{spec.max_version_exclusive}"
    return f">={spec.min_version}, <{spec.max_version_exclusive}"


def _harness_cli_version_string(
    spec: HarnessInstallSpec,
    binary: str,
    timeout: float = _DEFAULT_CLI_PROBE_TIMEOUT_S,
) -> str | None:
    """Return the parsed, normalized version string from *binary* ``--version``.

    :param timeout: Seconds to wait for the ``--version`` subprocess, e.g.
        :data:`READINESS_CLI_PROBE_TIMEOUT_S` on the readiness path.
    """
    try:
        completed = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return _parse_harness_cli_version((completed.stdout or "") + "\n" + (completed.stderr or ""))


def harness_install_command(key: str) -> list[str]:
    """Return the argv that installs the harness CLI.

    :param key: A harness family or :data:`PI_KEY`.
    :returns: The install command, e.g. ``["npm", "install", "-g",
        "@anthropic-ai/claude-code"]`` or an explicitly configured vendor
        installer command.
    :raises KeyError: If *key* has no install spec (caller should gate on
        :func:`harness_install_spec`).
    :raises ValueError: If *key* has a spec but no npm ``package`` (a CLI
        installed out-of-band, e.g. cursor-agent); show its ``install_hint``.
    """
    spec = harness_install_spec(key)
    if spec is None:
        raise KeyError(key)
    if spec.install_command is not None:
        return list(spec.install_command)
    package = spec.package
    if package is None:
        raise ValueError(f"{key!r} has no npm package; show its install_hint instead")
    return ["npm", "install", "-g", package]


class HarnessInstallResult(NamedTuple):
    """Outcome of :func:`try_install_harness_cli`.

    :param installed: Whether the CLI resolves after the attempt, via the same
        :func:`resolve_cli_binary` ladder readiness uses (``PATH`` plus the
        common global install dirs), not bare ``PATH`` alone.
    :param reason: Human-readable failure reason when ``installed`` is False;
        ``None`` on success.
    """

    installed: bool
    reason: str | None


def try_install_harness_cli(key: str) -> HarnessInstallResult:
    """Install the harness CLI, returning whether it landed and why not.

    Same behavior and side effects as :func:`install_harness_cli` (the
    installer's output streams to this process, uncaptured, so failures stay
    visible in the setup terminal / host log), but returns a human-readable
    reason so a UI-driven install can surface "npm is not available on the
    host" instead of a silent boolean failure.

    :param key: A harness family or :data:`PI_KEY`.
    :returns: A :class:`HarnessInstallResult` — ``(True, None)`` once the CLI
        resolves via :func:`resolve_cli_binary` (including the no-op where it
        was already present), otherwise ``(False, reason)`` naming the failure
        (manual-only spec, missing installer, timeout, OS error, non-zero exit,
        or a post-install binary-not-found).
    :raises KeyError: If *key* has no install spec.
    """
    spec = harness_install_spec(key)
    if spec is not None and spec.package is None and spec.install_command is None:
        # Manual-only CLI (e.g. cursor-agent): caller shows install_hint.
        return HarnessInstallResult(False, f"{spec.binary!r} is not installable automatically")
    cmd = harness_install_command(key)
    if shutil.which(cmd[0]) is None:
        return HarnessInstallResult(False, f"{cmd[0]!r} is not available on the host")
    try:
        result = subprocess.run(cmd, check=False, timeout=300)
    except subprocess.TimeoutExpired:
        return HarnessInstallResult(False, "install timed out after 300s")
    except OSError as exc:
        return HarnessInstallResult(False, f"install command failed to run: {exc}")
    # harness_install_command would have raised for a spec-less key, so spec is
    # non-None past this point.
    assert spec is not None
    # Resolve the freshly-installed binary via the SAME ladder readiness uses
    # (:func:`resolve_cli_binary` — ``PATH`` plus the nvm/npm-global/homebrew
    # fallback dirs), so the install verdict and the readiness badge can never
    # disagree. A bare ``shutil.which`` here would report "not found" for a
    # binary the host daemon's frozen ``PATH`` omits but readiness still resolves
    # via the ladder — the spurious "failed" toast next to a green "ready" tick.
    resolved = resolve_cli_binary(spec.binary)
    if resolved is not None:
        # Put the resolving dir on ``PATH`` for this process so the setup
        # wizard's *later* steps — harness_login / harness_cli_logged_in /
        # harness_logout — which shell out with the bare binary name and only
        # bare ``shutil.which``, can find it too. Without this, an install that
        # succeeded via a fallback dir (nvm/homebrew/…) would be followed by a
        # login step that can't locate the very binary just installed.
        resolved_dir = str(Path(resolved).resolve().parent)
        path_entries = os.environ.get("PATH", "").split(os.pathsep)
        if resolved_dir not in path_entries:
            os.environ["PATH"] = os.pathsep.join([resolved_dir, *path_entries])
        return HarnessInstallResult(True, None)
    if result.returncode != 0:
        return HarnessInstallResult(False, f"installer exited with code {result.returncode}")
    return HarnessInstallResult(
        False, f"installer completed but {spec.binary!r} could not be found"
    )


def install_harness_cli(key: str) -> bool:
    """Install the harness CLI; return whether it landed on ``PATH``.

    Thin wrapper over :func:`try_install_harness_cli` that discards the failure
    reason, preserving the boolean contract the setup wizard relies on.

    :param key: A harness family or :data:`PI_KEY`.
    :returns: ``True`` when the CLI is on ``PATH`` after the install attempt,
        ``False`` if the installer is missing or the install failed.
    :raises KeyError: If *key* has no install spec.
    """
    return try_install_harness_cli(key).installed


def harness_cli_logged_in(key: str, timeout: float = _DEFAULT_CLI_PROBE_TIMEOUT_S) -> bool:
    """Return whether the harness CLI itself reports a usable login.

    Asks the CLI's own status command (``claude auth status`` /
    ``codex login status`` / ``agy models``) instead of reading a credential
    file, because the file location is platform-specific — Claude Code stores
    its tokens in the macOS Keychain rather than ``~/.claude/.credentials.json``
    on macOS, so a file check would falsely report "not logged in" right after a
    successful ``claude auth login``. The status command reads wherever the CLI
    actually stored the credential, so this is correct on every platform.

    Two output shapes are handled, selected explicitly by the spec's
    ``login_status_key``: a CLI that publishes a JSON status object names its
    boolean field there (Claude ``loggedIn`` / Cursor ``isAuthenticated``) and
    is read structurally; a CLI with no ``login_status_key`` has no JSON verdict
    (Codex's human line, ``agy models``' model list), so the exit code decides
    (``0`` only when logged in) and stdout is never parsed.

    :param key: A harness family, e.g. ``"anthropic"`` (Claude),
        ``"openai"`` (Codex), or ``"gemini"`` (Antigravity, via ``agy models``).
    :param timeout: Seconds to wait for the status subprocess, e.g. ``10.0`` on
        the readiness path where a hung CLI must not stall the refresh.
    :returns: ``True`` when the CLI reports a usable login; ``False`` when the
        key has no status command, the CLI binary is missing, the status
        process failed to spawn, or the CLI reports no login.
    """
    spec = harness_install_spec(key)
    if spec is None or spec.status_args is None:
        return False
    binary = resolve_cli_binary(spec.binary) if key == GEMINI_FAMILY else shutil.which(spec.binary)
    if binary is None:
        return False
    argv_binary = binary if key == GEMINI_FAMILY else spec.binary
    try:
        result = subprocess.run(
            [argv_binary, *spec.status_args],
            check=False,
            timeout=timeout,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    # Dispatch is explicit per spec: a harness that publishes a JSON status
    # object names its boolean field in ``login_status_key`` (Claude
    # ``loggedIn`` / Cursor ``isAuthenticated``). When that key is unset the
    # harness has no JSON verdict (Codex's human line, agy's model list), so the
    # exit code is authoritative and stdout is never parsed — output that merely
    # happens to be JSON can't flip the verdict.
    status_key = spec.login_status_key
    if status_key is not None:
        try:
            payload = json.loads(result.stdout)
        except (json.JSONDecodeError, ValueError):
            return result.returncode == 0
        if isinstance(payload, dict) and status_key in payload:
            return bool(payload[status_key])
    return result.returncode == 0


def harness_login(key: str) -> bool:
    """Run the harness CLI's interactive subscription login; return logged-in state.

    Lets ``configure harnesses`` be the single place to sign in: when the user
    picks a subscription or Antigravity sign-in, we drive the harness's own
    auth entrypoint (``claude auth login --claudeai`` / ``codex login`` / bare
    ``agy``) **in the foreground** (inheriting stdio so OAuth / device-code
    prompts and browser URLs reach the user), then confirm via
    :func:`harness_cli_logged_in`.
    If the CLI is already logged in this is a no-op that returns ``True``
    immediately (no redundant re-auth).

    :param key: A harness family, ``"anthropic"`` (Claude), ``"openai"``
        (Codex), or ``"gemini"`` (Antigravity).
    :returns: ``True`` when the harness CLI is logged in after the attempt
        (including the already-logged-in short-circuit); ``False`` when the key
        has no login command, the CLI binary is missing, the login process
        failed to spawn, or the user did not complete the login.
    """
    spec = harness_install_spec(key)
    if spec is None or spec.login_args is None:
        return False
    binary = resolve_cli_binary(spec.binary) if key == GEMINI_FAMILY else shutil.which(spec.binary)
    if binary is None:
        return False
    argv_binary = binary if key == GEMINI_FAMILY else spec.binary
    if harness_cli_logged_in(key):
        return True
    try:
        # Open /dev/tty explicitly so the child process sees a real TTY even
        # when the parent's stdio is piped (e.g. launched via `uv tool run` or
        # another wrapper). The Claude CLI checks isatty() and skips opening the
        # browser when it returns false, which strands the login until it times
        # out. Fall back to inherited stdio when /dev/tty can't be opened (a
        # headless run with no controlling terminal).
        tty_fd: int | None = None
        if not sys.stdin.isatty():
            try:
                tty_fd = os.open("/dev/tty", os.O_RDWR)
            except OSError:
                tty_fd = None
        argv = [argv_binary, *spec.login_args]
        try:
            if tty_fd is not None:
                subprocess.run(
                    argv, check=False, timeout=600, stdin=tty_fd, stdout=tty_fd, stderr=tty_fd
                )
            else:
                subprocess.run(argv, check=False, timeout=600)
        finally:
            if tty_fd is not None:
                os.close(tty_fd)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return harness_cli_logged_in(key)


def harness_logout(key: str) -> bool:
    """Run the harness CLI's logout; return whether it is now logged out.

    Drives the harness's own logout command (``claude auth logout`` /
    ``codex logout``) so removing a subscription from ``configure harnesses``
    actually signs the user out of the standalone CLI — otherwise the
    credential persists and ambient detection re-adopts the subscription on the
    next ``configure`` open.

    :param key: A harness family, ``"anthropic"`` (Claude) or ``"openai"``
        (Codex).
    :returns: ``True`` when the harness CLI is logged out after the attempt;
        ``False`` when the key has no logout command, the binary is missing, the
        process failed to spawn, or a login still resolves afterward.
    """
    spec = harness_install_spec(key)
    if spec is None or spec.logout_args is None:
        return False
    if shutil.which(spec.binary) is None:
        return False
    try:
        subprocess.run([spec.binary, *spec.logout_args], check=False, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return not harness_cli_logged_in(key)
