"""Launch-config helpers for native Antigravity (agy) TUI sessions.

This module assembles the ``agy`` command-line arguments and environment
overrides needed to start or resume a native agy session.  It contains only
pure functions and a frozen dataclass — no live agy calls are made here.

Key design points:

* **Auth inheritance** — agy shares ``~/.gemini`` with the user's interactive
  login; no credential seeding is required.  :func:`resolve_native_antigravity_launch`
  verifies (informational only) that a credential exists but always returns
  ``subscription`` mode regardless.

* **Per-session identity is discovered, not assigned** — agy mints its own UUID
  conversation and ignores the ``ANTIGRAVITY_CONVERSATION_ID`` env var
  (verified empirically; see ``docs/claude/antigravity-spike-findings.md``). A
  fresh session therefore sets nothing for identity; the forwarder discovers
  agy's real id from the newest ``brain/<uuid>`` dir and persists it. A resume
  passes ``--conversation <id>`` with that discovered id on the command line.

* **Workspace = the agy process cwd** — agy runs its tools in its own working
  directory (verified empirically: a tmux-launched agy with ``cwd`` set to a
  project dir ran ``run_command`` with that ``Cwd``, never the default
  ``scratch`` dir). The launcher pins the terminal cwd to the session working
  directory, so no ``--add-dir`` flag is required.

* **Auth inheritance** — when local Google Application Default Credentials are
  available, the launcher enables agy's ADC auth mode without overwriting an
  operator-provided ``AGY_ADC_AUTH`` value. agy ignores
  ``ANTIGRAVITY_SIDECAR_WEB_PORT`` and ``ANTIGRAVITY_EXECUTABLE_DATA_DIR`` for
  the host process, so :func:`build_agy_launch` emits only the ADC override
  when appropriate.

* **Auth is inherited** — current agy releases accept either their persisted
  Google OAuth login or ``GEMINI_API_KEY``. The isolated settings prepared by
  the bridge select the Gemini provider when the key is present.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from omnigent.onboarding.gemini_auth import gemini_auth_has_credential

_logger = logging.getLogger(__name__)

# agy's coarse permission control. agy honors exactly one pre-emptive flag,
# ``--dangerously-skip-permissions`` ("Auto-approve all tool permission requests
# without prompting"); without it agy defaults to ``toolPermission=request-review``
# and prompts in the TUI for each tool. This is the ONLY pre-emptive control agy
# exposes — its ``hooks.json`` PreToolUse hook does not fire on tool execution in
# 1.0.8 (verified; see docs/claude/antigravity-native-governance-design.md §2.3),
# so there is no per-tool / web-routed gate. Omnigent therefore maps its
# ``permission_mode`` onto this single flag.
_SKIP_PERMISSIONS_FLAG = "--dangerously-skip-permissions"
# Omnigent permission mode that maps to agy's all-or-nothing bypass.
_BYPASS_PERMISSION_MODE = "bypassPermissions"
# Fallback binary path when ``agy`` is not on PATH.
_AGY_FALLBACK_PATH = Path.home() / ".local" / "bin" / "agy"
# Install instructions surfaced in the RuntimeError when agy is missing.
_AGY_INSTALL_HINT = (
    "Install agy via the Antigravity installer script:\n"
    "  curl -fsSL https://antigravity.google/cli/install.sh | bash\n"
    "Then restart your shell so ~/.local/bin is on PATH."
)


def _adc_credentials_path() -> Path:
    """Return the ADC file path selected by the ambient Google auth setup."""
    explicit_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if explicit_path:
        return Path(explicit_path).expanduser()
    config_dir = os.environ.get("CLOUDSDK_CONFIG")
    if config_dir:
        return Path(config_dir).expanduser() / "application_default_credentials.json"
    return Path.home() / ".config" / "gcloud" / "application_default_credentials.json"


def _adc_credentials_available() -> bool:
    """Return whether a readable, non-empty ADC JSON object is available."""
    path = _adc_credentials_path()
    try:
        if not path.is_file():
            return False
        with path.open(encoding="utf-8") as credentials_file:
            credentials = json.load(credentials_file)
    except (OSError, ValueError):
        return False
    return isinstance(credentials, dict) and bool(credentials)


def agy_binary_path() -> str:
    """Return the absolute path to the ``agy`` CLI executable.

    Searches PATH first via :func:`shutil.which`, then falls back to
    ``~/.local/bin/agy`` (the installer's default location).

    :returns: Absolute path string for the agy executable, e.g.
        ``"/home/user/.local/bin/agy"``.
    :raises RuntimeError: When agy is not found on PATH and
        ``~/.local/bin/agy`` does not exist.  The error message includes
        the curl installer command.
    """
    on_path = shutil.which("agy")
    if on_path is not None:
        return on_path
    if _AGY_FALLBACK_PATH.is_file():
        return str(_AGY_FALLBACK_PATH)
    raise RuntimeError(
        f"agy CLI not found on PATH and not at {_AGY_FALLBACK_PATH}.\n{_AGY_INSTALL_HINT}"
    )


@dataclass(frozen=True)
class NativeAntigravityLaunch:
    """How a native Antigravity (agy) session should be launched.

    Resolved by :func:`resolve_native_antigravity_launch`. ``auth_mode`` remains
    ``"subscription"`` as an internal native-harness marker; agy itself chooses
    OAuth or the ambient Gemini API key.

    :param auth_mode: Authentication mode.  Always ``"subscription"`` for the
        native harness; agy resolves either the ambient API key or the user's
        existing login and no credential seeding is performed here.
    :param model: Optional model label to pass to agy via ``--model``, e.g.
        ``"gemini-2.5-pro"``.  ``None`` lets agy use its own default.
    :param extra_env: Additional environment variables to inject into the
        agy process, beyond those set by :func:`build_agy_launch`.  Empty
        by default.
    """

    auth_mode: str
    model: str | None
    extra_env: dict[str, str] = field(default_factory=dict)


def resolve_native_antigravity_launch(
    *,
    model: str | None = None,
) -> NativeAntigravityLaunch:
    """Resolve the native agy launch config for Phase 1 (subscription only).

    Phase 1 always uses ``"subscription"`` auth mode: agy inherits the
    user's Google login from ``~/.gemini`` automatically — no seeding is
    needed.  :func:`gemini_auth_has_credential` is called only for an
    informational check; the result never changes the returned mode.

    If no credential is detected a warning is logged (agy will drive its
    own OAuth flow on first run), but ``"subscription"`` mode is still
    returned unconditionally.

    :param model: Optional model label override, e.g. ``"gemini-2.5-pro"``.
        ``None`` lets agy use its own default.
    :returns: A :class:`NativeAntigravityLaunch` with
        ``auth_mode="subscription"``.
    """
    if not gemini_auth_has_credential():
        _logger.warning(
            "No agy credential found (checked GEMINI_API_KEY, ~/.gemini/oauth_creds.json "
            "and ~/.gemini/antigravity-cli/antigravity-oauth-token, plus "
            "`agy models` on macOS for a Keychain-stored login); "
            "agy will prompt for login on first run."
        )
    return NativeAntigravityLaunch(auth_mode="subscription", model=model)


def should_skip_permissions(
    *,
    permission_mode: str | None,
    headless: bool,
) -> bool:
    """Decide whether ``--dangerously-skip-permissions`` should be appended.

    agy's only pre-emptive permission control is the all-or-nothing bypass flag
    (see :data:`_SKIP_PERMISSIONS_FLAG`). The flag is appended when **either**:

    * ``permission_mode == "bypassPermissions"`` — the user/worker explicitly
      asked for full auto-approval (the truthful analogue of how
      ``claude-native`` maps ``--permission-mode bypassPermissions`` and
      ``codex-native`` maps ``--dangerously-bypass-approvals-and-sandbox``); or
    * the launch is **headless** — no interactive client will attach to answer
      agy's ``request-review`` TUI prompt, so leaving it on would hang the turn
      forever. Headless/unattended antigravity-native launches (sandbox,
      autonomous, server-spawned sub-agents) therefore auto-bypass, matching how
      the SDK/headless flavors run unattended.

    For a non-bypass, interactive (attended) launch the flag is omitted so agy's
    default ``request-review`` prompts the user per tool — answered by the
    attached TTY (CLI) or, on the host-spawned web path, surfaced as a real-time
    Omnigent elicitation by the RPC read driver's interaction bridge (see
    :mod:`omnigent.antigravity_native_reader` /
    :mod:`omnigent.antigravity_native_interactions`). agy exposes no firing
    pre-tool hook, so Omnigent cannot pre-empt a tool before it runs; the
    elicitation card is the honest gate.

    .. warning:: The ``headless`` argument is currently derived from the
       launching process's TTY (``_launch_is_headless`` in
       :mod:`omnigent.antigravity_native`). That is sound only while
       antigravity-native is CLI-launched. If a Phase-5 server-spawn / web-attach
       launch path is added, "headless" must be redefined as "no web client
       attached" — otherwise a web-attended session would auto-bypass here. See
       the matching note on ``_launch_is_headless``.

    :param permission_mode: The session's effective Omnigent permission mode,
        e.g. ``"bypassPermissions"`` / ``"default"`` / ``"acceptEdits"``.
        ``None`` is treated as a non-bypass mode.
    :param headless: ``True`` when no interactive client will attach (sandbox /
        autonomous / server-spawned / detached launch).
    :returns: ``True`` when the bypass flag should be appended to the agy argv.
    """
    if permission_mode == _BYPASS_PERMISSION_MODE:
        return True
    return headless


def build_agy_launch(
    *,
    conversation_id: str | None,
    model: str | None,
    resume: bool,
    permission_mode: str | None = None,
    headless: bool = False,
    extra_args: tuple[str, ...] = (),
) -> tuple[list[str], dict[str, str]]:
    """Build the argv and environment overrides for an agy launch.

    Two launch modes are supported:

    * **Fresh session** (``resume=False``) — nothing is set for identity. agy
      mints its own UUID conversation and ignores any
      ``ANTIGRAVITY_CONVERSATION_ID`` we set (verified empirically), so
      *conversation_id* is not used and the forwarder discovers agy's real id
      afterward. The ``--conversation`` flag is NOT added to argv.

    * **Resume session** (``resume=True``) — ``--conversation <id>`` is
      appended to argv so agy resumes the existing conversation. Here
      *conversation_id* MUST be agy's real (discovered) UUID, persisted from a
      prior launch; agy cannot resume the launcher's minted placeholder.

    **Permission mode → agy flag.** ``--dangerously-skip-permissions`` is
    appended when :func:`should_skip_permissions` says so — i.e. when
    ``permission_mode == "bypassPermissions"`` OR the launch is *headless*
    (no interactive client will attach to answer agy's ``request-review``
    prompt, which would otherwise hang an unattended turn). Otherwise it is
    omitted so agy's default ``request-review`` prompts the attached user. This
    is the *only* pre-emptive control agy honors; it is all-or-nothing and
    cannot be routed to the web UI or made per-tool (the genuine Omnigent policy
    gate for this harness is post-hoc/audit-only). The flag is not duplicated
    when *extra_args* already carries it.

    In both modes the workspace is the agy process cwd (set by the terminal
    spec), so no ``--add-dir`` is emitted. ADC auth is enabled through the
    returned environment overrides when local credentials are available;
    agy ignores ``ANTIGRAVITY_SIDECAR_WEB_PORT`` /
    ``ANTIGRAVITY_CONVERSATION_ID`` / ``ANTIGRAVITY_EXECUTABLE_DATA_DIR`` for
    the host process.

    :param conversation_id: agy's real conversation id to resume, e.g.
        ``"68caaeac-..."``. Required (non-``None``) when ``resume=True``;
        ignored when ``resume=False``.
    :param model: Optional model label, e.g. ``"gemini-2.5-pro"``.
        ``None`` omits ``--model`` so agy uses its default.
    :param resume: ``True`` to resume an existing conversation; ``False``
        to start a fresh one.
    :param permission_mode: The session's effective Omnigent permission mode,
        e.g. ``"bypassPermissions"``. ``None`` (the default) is a non-bypass
        mode. See :func:`should_skip_permissions`.
    :param headless: ``True`` when no interactive client will attach to the agy
        TUI (sandbox / autonomous / server-spawned / detached). Forces the
        bypass flag so agy does not hang waiting for a ``request-review``
        answer. See :func:`should_skip_permissions`.
    :param extra_args: Additional raw CLI args appended after all generated
        flags, e.g. ``("--print-timeout", "30")``.
    :returns: A ``(argv, env_overrides)`` tuple where *argv* is the full
        command list starting with the agy binary path and *env_overrides*
        is a dict of env variables to layer on top of the process
        environment, including the ADC auth override when local credentials
        are available.
    :raises ValueError: When ``resume=True`` but *conversation_id* is ``None``
        or empty (agy needs a real id to resume).
    """
    argv: list[str] = [agy_binary_path()]
    if resume:
        if not conversation_id:
            raise ValueError("Resuming an agy conversation requires a conversation id.")
        argv.extend(["--conversation", conversation_id])
    if model is not None:
        argv.extend(["--model", model])
    # Append the coarse bypass flag iff permission mode / headless calls for it,
    # and only when the caller's pass-through args have not already added it
    # (a user can pass it explicitly; avoid a confusing duplicate).
    if should_skip_permissions(permission_mode=permission_mode, headless=headless) and (
        _SKIP_PERMISSIONS_FLAG not in extra_args
    ):
        argv.append(_SKIP_PERMISSIONS_FLAG)
    argv.extend(extra_args)

    env_overrides: dict[str, str] = {}
    if "AGY_ADC_AUTH" not in os.environ and _adc_credentials_available():
        env_overrides["AGY_ADC_AUTH"] = "1"
    return argv, env_overrides
