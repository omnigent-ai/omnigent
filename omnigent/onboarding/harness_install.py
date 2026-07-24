"""Harness CLI install + auth operations — shared by ``run`` and ``configure``.

A coding harness is "ready" along two independent axes:

- **configured** — a usable model credential serves its family (resolved via
  :func:`omnigent.onboarding.provider_config.default_provider_for_harness`
  over the ambient-merged config). That lives in the provider layer.
- **installed** — the harness's CLI binary is on ``PATH``. This module owns
  that axis, mirroring how ``ucode`` checks (``shutil.which(binary)``) and the
  npm packages it installs.

``omnigent setup --no-internal-beta`` uses this to mark an uninstalled harness and
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
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

from omnigent._platform import resolve_cli_binary
from omnigent.harness_install_spec import HarnessInstallSpec, SetupStep
from omnigent.onboarding.provider_config import ANTHROPIC_FAMILY, GEMINI_FAMILY, OPENAI_FAMILY

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
# installer, not an npm package managed by ``omnigent setup``.
KIRO_KEY = "kiro"

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
    ),
    OPENAI_FAMILY: HarnessInstallSpec(
        "Codex",
        "codex",
        "@openai/codex",
        login_args=("login",),
        logout_args=("logout",),
        status_args=("login", "status"),
    ),
    PI_KEY: HarnessInstallSpec("Pi", "pi", "@earendil-works/pi-coding-agent"),
    # Pin the install to the supported 1.17.x range: opencode-ai's npm ``latest``
    # is a ``0.0.0-beta-*`` pre-release, so a bare ``opencode-ai`` would install a
    # version the runtime version-check (``check_opencode_version``,
    # >=1.17.7,<1.18.0) then rejects. ``~1.17.7`` mirrors that exact range.
    OPENCODE_KEY: HarnessInstallSpec("OpenCode", "opencode", "opencode-ai@~1.17.7"),
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
    ),
    # Kimi Code CLI ships a single-binary ``kimi`` via a curl installer (no
    # npm). ``kimi login`` is the interactive provider login (OAuth or a
    # Moonshot API key). ``status_args`` is intentionally ``None``: kimi has
    # no first-class "am I logged in?" exit-code probe — login state is
    # only inspected interactively. With ``None`` the login path runs every
    # time the operator asks for it (interactive, so they can cancel if
    # already authenticated).
    KIMI_KEY: HarnessInstallSpec(
        "Kimi",
        "kimi",
        package=None,
        login_args=("login",),
        logout_args=("logout",),
        install_hint="curl -fsSL https://code.kimi.com/kimi-code/install.sh | bash",
    ),
    KIRO_KEY: HarnessInstallSpec(
        "Kiro",
        "kiro-cli",
        package=None,
        install_hint="curl -fsSL https://cli.kiro.dev/install | bash",
    ),
    # The native Antigravity (agy) TUI bridge wraps the ``agy`` CLI. ``agy`` has
    # no ``login`` / ``logout`` subcommand — the user authenticates via browser
    # OAuth by launching ``agy`` with no arguments on first run — so login_args /
    # logout_args stay ``None`` (``harness_login`` / ``harness_logout`` no-op for
    # it). It DOES expose a usable status check: ``agy models`` lists models and
    # exits 0 only when signed in (else exits non-zero with "Please sign in …"),
    # so status_args wires it the way Codex's exit-code ``login status`` is — so
    # ``harness_cli_logged_in`` reads a real, revocation-aware verdict from the
    # CLI instead of guessing from a credential file. (The readiness layer still
    # uses the subprocess-free file check ``gemini_login_detected`` for its fast
    # path.) ``agy`` ships via a shell installer rather than npm, so ``package``
    # is ``None`` and the manual command lives in ``install_hint`` (shown as
    # guidance; ``install_harness_cli`` refuses to auto-run it).
    GEMINI_FAMILY: HarnessInstallSpec(
        "Antigravity",
        "agy",
        package=None,
        status_args=("models",),
        install_hint="curl -fsSL https://antigravity.google/cli/install.sh | bash",
        auth_hint="run `agy` once and complete the browser sign-in",
    ),
    GOOSE_KEY: HarnessInstallSpec(
        "Goose",
        "goose",
        package=None,
        install_hint="brew install block-goose-cli",
    ),
    HERMES_KEY: HarnessInstallSpec(
        "Hermes",
        "hermes",
        package=None,
        install_hint=_HERMES_INSTALL_HINT,
        install_command=("bash", "-c", _HERMES_INSTALL_HINT),
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


# The auth step per UI-installable family, for the setup checklist. These are
# display-only checklist rows (the command is shown for the user to run on the
# host, never executed server-side), so the commands are literal here rather
# than derived from ``HarnessInstallSpec.login_args`` — keep them in sync with
# that spec by hand if a harness's login command changes.
# ``command`` steps run on the host and are status-tracked; ``setup`` steps
# (pi/qwen: API key or gateway) can't be driven from the UI yet, so M1 points at
# ``omnigent setup`` and does not track their status.
#   claude/codex: subscription login via the CLI's own login command.
#   opencode: its own `opencode auth login`.
#   pi/qwen: a provider credential (API key or gateway) — configured by setup.
_UI_AUTH_STEP_BY_KEY: dict[str, SetupStep] = {
    ANTHROPIC_FAMILY: SetupStep(
        kind="auth",
        title="Sign in to Claude",
        detail="Uses your Claude subscription — sign in on the host.",
        action="command",
        command="claude auth login --claudeai",
        status_key="authed",
    ),
    OPENAI_FAMILY: SetupStep(
        kind="auth",
        title="Sign in to Codex",
        detail="Uses your ChatGPT subscription — sign in on the host.",
        action="command",
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
        title="Add a Pi credential",
        detail="Pi needs an API key or gateway. Set it up on the host for now.",
        action="setup",
        command="omnigent setup",
        status_key=None,
    ),
    QWEN_KEY: SetupStep(
        kind="auth",
        title="Add a Qwen credential",
        detail="Qwen needs an API key or gateway. Set it up on the host for now.",
        action="setup",
        command="omnigent setup",
        status_key=None,
    ),
}


def ui_setup_steps(harness: str) -> list[SetupStep]:
    """Return the ordered setup checklist for a UI harness identifier.

    Mirrors what ``omnigent setup`` walks a user through for the harness: an
    install step, then (for the five first-class families) an auth step. The
    install step's label uses the harness's :class:`HarnessInstallSpec` display
    name; the auth step's command is a display-only literal from
    :data:`_UI_AUTH_STEP_BY_KEY` (shown for the user to run, not executed).
    Harnesses outside the UI-installable set get a single generic
    "run ``omnigent setup``" step (M1 scope).

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
                detail="Run omnigent setup on the host to configure this agent.",
                action="setup",
                command="omnigent setup",
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
    """Return a harness's required CLI spec when that CLI can't be resolved.

    Combines :func:`required_cli_for_harness` with the same
    :func:`resolve_cli_binary` probe :func:`harness_cli_installed` uses, so the
    verdict matches what the harness's own launch will see (both check ``PATH``
    plus the common global install dirs the host daemon's frozen ``PATH`` may
    omit). Used by sub-agent dispatch to fail loud *before* spawning a worker
    whose harness can never boot here, instead of letting the missing binary
    surface as a lazy, generic turn failure.

    :param harness: An executor harness identifier, e.g. ``"pi"`` or
        ``"claude-native"``.
    :returns: The :class:`HarnessInstallSpec` for a CLI-backed harness whose
        ``binary`` is not on ``PATH``; ``None`` when the harness needs no CLI
        (SDK-based / unknown) or the required binary is present.
    """
    spec = required_cli_for_harness(harness)
    if spec is None:
        return None
    if resolve_cli_binary(spec.binary) is not None:
        return None
    return spec


def harness_setup_hint(harness: str | None) -> str:
    """Return actionable remediation when *harness* can't launch on a machine.

    Most CLI harnesses (``claude``/``codex``/``pi``) install via npm and a
    model credential, both of which ``omnigent setup`` handles — so they route
    there. But a harness whose CLI ships out-of-band (``cursor-agent``, via
    Cursor's own curl installer rather than npm — it carries an ``install_hint``
    and no ``package``) is **not** installed by ``omnigent setup``: pointing a
    native-Cursor user there is a dead end, since setup only configures the
    SDK-based ``cursor`` harness (``cursor-sdk`` + ``CURSOR_API_KEY``). For
    those, name the vendor installer and the CLI's own login instead.

    :param harness: An executor harness identifier, e.g. ``"cursor-native"``,
        ``"claude-native"``, or ``"codex"``; ``None`` falls back to the
        ``omnigent setup`` hint.
    :returns: A remediation clause for the "harness not configured" message,
        e.g. ``"install the cursor-agent CLI on that machine with `curl
        https://cursor.com/install -fsS | bash`, then run `cursor-agent
        login`"`` for native Cursor, or the ``omnigent setup`` hint otherwise.
    """
    spec = required_cli_for_harness(harness or "")
    if spec is not None and spec.package is None and spec.install_hint:
        login = ""
        if spec.login_args:
            login = f", then run `{spec.binary} {' '.join(spec.login_args)}`"
        elif spec.auth_hint:
            login = f", then {spec.auth_hint}"
        return f"install the {spec.binary} CLI on that machine with `{spec.install_hint}`{login}"
    return "run `omnigent setup` on that machine to install the CLI and set a default credential"


def harness_install_spec(key: str) -> HarnessInstallSpec | None:
    """Return the install spec for a family/harness key, or ``None``.

    :param key: A harness family (``"anthropic"`` / ``"openai"``) or
        :data:`PI_KEY` (``"pi"``).
    :returns: The :class:`HarnessInstallSpec`, or ``None`` for an unknown key
        (e.g. a gateway-only family with no dedicated CLI).
    """
    return _all_harness_install().get(key)


def harness_cli_installed(key: str) -> bool:
    """Return whether the harness's CLI binary can be resolved.

    "Installed" is deliberately the CLI binary (:func:`resolve_cli_binary` —
    ``PATH`` plus the common global install dirs the host daemon's frozen
    ``PATH`` may omit), matching ucode and the npm install-prompt UX — even
    though the SDK-based ``claude-sdk`` harness can run without the ``claude``
    CLI.

    :param key: A harness family (``"anthropic"`` / ``"openai"``) or
        :data:`PI_KEY` / :data:`KIMI_KEY`.
    :returns: ``True`` when the CLI resolves; ``False`` when it doesn't or
        the key has no associated CLI.
    """
    spec = harness_install_spec(key)
    if spec is None:
        return False
    return resolve_cli_binary(spec.binary) is not None


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


def harness_cli_logged_in(key: str) -> bool:
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
    :returns: ``True`` when the CLI reports a usable login; ``False`` when the
        key has no status command, the CLI binary is missing, the status
        process failed to spawn, or the CLI reports no login.
    """
    spec = harness_install_spec(key)
    if spec is None or spec.status_args is None:
        return False
    if shutil.which(spec.binary) is None:
        return False
    try:
        result = subprocess.run(
            [spec.binary, *spec.status_args],
            check=False,
            timeout=30,
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
    picks "Claude / Codex — subscription" we drive the harness's own login
    command (``claude auth login --claudeai`` / ``codex login``) **in the
    foreground** (inheriting stdio so the OAuth / device-code prompts and any
    browser URL reach the user), then confirm via :func:`harness_cli_logged_in`.
    If the CLI is already logged in this is a no-op that returns ``True``
    immediately (no redundant re-auth).

    :param key: A harness family, ``"anthropic"`` (Claude) or ``"openai"``
        (Codex).
    :returns: ``True`` when the harness CLI is logged in after the attempt
        (including the already-logged-in short-circuit); ``False`` when the key
        has no login command, the CLI binary is missing, the login process
        failed to spawn, or the user did not complete the login.
    """
    spec = harness_install_spec(key)
    if spec is None or spec.login_args is None:
        return False
    if shutil.which(spec.binary) is None:
        return False
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
        argv = [spec.binary, *spec.login_args]
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
