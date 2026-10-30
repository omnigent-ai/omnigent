"""Surgical removal of harness wiring written by ``ucode configure``.

Adding a ``kind: databricks`` provider runs ``ucode configure`` (see
:mod:`omnigent.onboarding.ucode_setup`), which wires the Claude / Codex
CLIs to a workspace's Unity Gateway. Most of that wiring lives in
ucode-owned sidecar files, but two pieces land in files the *user* owns:

- **Codex < 0.134.0 (ucode's legacy layout):** ucode deep-merges into the
  real ``~/.codex/config.toml`` — a top-level ``profile = "ucode"`` plus
  ``[profiles.ucode]`` and ``[model_providers.ucode-databricks]`` tables —
  which makes every *bare* ``codex`` run (and Omnigent' inner codex, which
  inherits the shared config) route through the workspace gateway.
  ``ucode revert`` does not restore this file (it only restores the
  per-profile ``~/.codex/ucode.config.toml``), so the edit outlives both
  ``ucode revert`` and deleting ``~/.ucode/``.
- **Claude:** a ``web_search`` MCP server is registered into the user-scope
  ``~/.claude.json`` via ``claude mcp add-json``.

Removing the provider entry from ``~/.omnigent/config.yaml`` alone leaves
all of that in place. The helpers here strip exactly the ucode-managed keys
and files — identified by ucode's fixed names (the ``ucode`` profile, the
``ucode-databricks`` model provider, the ``UCODE_WEB_SEARCH_MODEL`` env
marker) — and leave everything user-owned untouched. Surgical key removal
is deliberately used instead of restoring ucode's backup file: ucode keeps
only the *first-ever* backup, so a whole-file restore would clobber any
config edits the user made since first configuring.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import MutableMapping
from dataclasses import dataclass
from pathlib import Path

import tomlkit
import tomlkit.exceptions

from omnigent.errors import ErrorCode, OmnigentError

# Fixed names ucode uses for its codex wiring (``CODEX_PROFILE_NAME`` /
# ``CODEX_MODEL_PROVIDER_NAME`` in ucode's ``agents/codex.py``). These are
# the ownership markers: anything under these names was written by ucode.
UCODE_CODEX_PROFILE_NAME = "ucode"
UCODE_CODEX_PROVIDER_NAME = "ucode-databricks"
# Name ucode registers its Claude MCP server under, and the env var its
# entry always carries (``WEB_SEARCH_MCP_NAME`` / the ``env`` block built in
# ucode's ``agents/claude.py``) — used to tell ucode's entry apart from a
# ``web_search`` server the user registered themselves.
UCODE_WEB_SEARCH_MCP_NAME = "web_search"
_UCODE_WEB_SEARCH_ENV_MARKER = "UCODE_WEB_SEARCH_MODEL"


def _default_codex_config_path() -> Path:
    """Return the shared Codex CLI config path, ``~/.codex/config.toml``.

    :returns: The path bare ``codex`` reads its configuration from.
    """
    return Path.home() / ".codex" / "config.toml"


def _default_ucode_sidecar_paths() -> list[Path]:
    """Return the ucode-owned sidecar config files for Claude and Codex.

    These files are created by ``ucode configure`` and only ever read via
    ucode's own launchers (``codex --profile ucode`` /
    ``claude --settings <sidecar>``), so deleting them cannot affect the
    bare CLIs.

    :returns: Sidecar paths, e.g. ``[~/.codex/ucode.config.toml,
        ~/.claude/ucode-settings.json]``.
    """
    return [
        Path.home() / ".codex" / f"{UCODE_CODEX_PROFILE_NAME}.config.toml",
        Path.home() / ".claude" / "ucode-settings.json",
    ]


def _default_claude_user_config_path() -> Path:
    """Return Claude Code's user-scope config path, ``~/.claude.json``.

    :returns: The file Claude Code stores user-scope ``mcpServers`` in.
    """
    return Path.home() / ".claude.json"


@dataclass(frozen=True)
class UcodeWiringRemoval:
    """Outcome of :func:`remove_ucode_wiring`.

    :param codex_config_stripped: Whether ucode-managed keys were removed
        from the user's shared ``~/.codex/config.toml``.
    :param removed_sidecars: Ucode-owned sidecar files that existed and were
        deleted, e.g. ``[Path("~/.codex/ucode.config.toml")]``.
    :param web_search_mcp_removed: Whether ucode's ``web_search`` MCP entry
        was unregistered from Claude Code's user scope.
    """

    codex_config_stripped: bool
    removed_sidecars: list[Path]
    web_search_mcp_removed: bool

    @property
    def any_change(self) -> bool:
        """Return whether the cleanup changed anything at all.

        :returns: ``True`` when at least one wiring artifact was removed.
        """
        return (
            self.codex_config_stripped
            or bool(self.removed_sidecars)
            or self.web_search_mcp_removed
        )


def strip_ucode_codex_config(config_path: Path | None = None) -> bool:
    """Remove ucode-managed keys from the user's shared Codex config.

    Pops exactly the keys ucode's legacy (codex < 0.134.0) layout merges
    into ``~/.codex/config.toml``: the top-level ``profile`` selector (only
    when it still points at ucode's profile), the ``[profiles.ucode]``
    table, and the ``[model_providers.ucode-databricks]`` table (the modern
    layout can also leave the latter behind). Now-empty ``profiles`` /
    ``model_providers`` parent tables are dropped too. Everything else —
    the user's own keys, tables, comments, and formatting — is preserved
    (the file is round-tripped with ``tomlkit``, the same library ucode
    writes it with).

    :param config_path: The codex config to edit; defaults to
        ``~/.codex/config.toml``. Tests pass a tmp path.
    :returns: ``True`` when the file existed, contained ucode-managed keys,
        and was rewritten without them; ``False`` when there was nothing to
        strip (missing file or no ucode keys — the file is left untouched).
    :raises OmnigentError: If the file exists but is not valid TOML —
        surfaced rather than guessed at, since rewriting a file we failed to
        parse could destroy the user's config.
    :raises OSError: If the file cannot be read or written.
    """
    path = config_path if config_path is not None else _default_codex_config_path()
    if not path.exists():
        return False
    try:
        doc = tomlkit.parse(path.read_text(encoding="utf-8"))
    except tomlkit.exceptions.ParseError as exc:
        raise OmnigentError(
            f"{path} is not valid TOML ({exc}); leaving it unchanged. "
            "Remove ucode's `profile`/`profiles.ucode`/"
            "`model_providers.ucode-databricks` entries by hand.",
            code=ErrorCode.INVALID_INPUT,
        ) from exc

    changed = False
    if doc.get("profile") == UCODE_CODEX_PROFILE_NAME:
        del doc["profile"]
        changed = True
    profiles = doc.get("profiles")
    if isinstance(profiles, MutableMapping) and UCODE_CODEX_PROFILE_NAME in profiles:
        del profiles[UCODE_CODEX_PROFILE_NAME]
        if len(profiles) == 0:  # drop the now-empty parent table
            del doc["profiles"]
        changed = True
    providers = doc.get("model_providers")
    if isinstance(providers, MutableMapping) and UCODE_CODEX_PROVIDER_NAME in providers:
        del providers[UCODE_CODEX_PROVIDER_NAME]
        if len(providers) == 0:  # drop the now-empty parent table
            del doc["model_providers"]
        changed = True

    if changed:
        path.write_text(tomlkit.dumps(doc), encoding="utf-8")
    return changed


def remove_ucode_sidecars(sidecar_paths: list[Path] | None = None) -> list[Path]:
    """Delete ucode's sidecar config files for Claude and Codex.

    Sidecars are ucode-owned (only ucode's own launchers read them), so
    deletion is unconditional — no ownership check needed. Missing files
    are skipped.

    :param sidecar_paths: The sidecar files to delete; defaults to
        ``~/.codex/ucode.config.toml`` and ``~/.claude/ucode-settings.json``.
        Tests pass tmp paths.
    :returns: The paths that existed and were deleted.
    :raises OSError: If an existing sidecar cannot be deleted.
    """
    paths = sidecar_paths if sidecar_paths is not None else _default_ucode_sidecar_paths()
    removed: list[Path] = []
    for path in paths:
        if path.exists():
            path.unlink()
            removed.append(path)
    return removed


def _remove_web_search_via_claude_cli() -> bool:
    """Run ``claude mcp remove web_search -s user``.

    Removal goes through the claude CLI — the same interface ucode used to
    register the entry — rather than editing ``~/.claude.json`` directly,
    since Claude Code owns that file's (much larger) structure.

    :returns: ``True`` when the command ran and exited 0; ``False`` when the
        claude binary is missing or the command failed / timed out.
    """
    claude = shutil.which("claude")
    if claude is None:
        return False
    try:
        result = subprocess.run(
            [claude, "mcp", "remove", UCODE_WEB_SEARCH_MCP_NAME, "-s", "user"],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def remove_ucode_web_search_mcp(claude_user_config_path: Path | None = None) -> bool:
    """Unregister ucode's ``web_search`` MCP server from Claude Code.

    Detect-then-delegate: reads ``~/.claude.json`` to confirm a user-scope
    ``web_search`` entry exists AND is ucode's — its ``env`` carries
    ``UCODE_WEB_SEARCH_MODEL``, or its command resolves to a ``ucode``
    binary — so a ``web_search`` server the user registered themselves is
    never touched. Only then runs ``claude mcp remove web_search -s user``
    (the same interface ucode registered it with).

    :param claude_user_config_path: Claude Code's user-scope config to
        inspect; defaults to ``~/.claude.json``. Tests pass a tmp path.
    :returns: ``True`` when a ucode-owned entry was found and removed;
        ``False`` otherwise — no entry, an entry the user owns, an
        unreadable / non-JSON config (Claude Code owns that file, so we
        skip rather than guess), or a missing / failing claude CLI.
    """
    path = (
        claude_user_config_path
        if claude_user_config_path is not None
        else _default_claude_user_config_path()
    )
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    servers = data.get("mcpServers") if isinstance(data, dict) else None
    if not isinstance(servers, dict):
        return False
    entry = servers.get(UCODE_WEB_SEARCH_MCP_NAME)
    if not isinstance(entry, dict):
        return False
    env = entry.get("env")
    command = entry.get("command")
    is_ucode_entry = (isinstance(env, dict) and _UCODE_WEB_SEARCH_ENV_MARKER in env) or (
        isinstance(command, str) and Path(command).name == "ucode"
    )
    if not is_ucode_entry:
        return False
    return _remove_web_search_via_claude_cli()


def remove_ucode_wiring() -> UcodeWiringRemoval:
    """Remove all harness wiring ``ucode configure`` wrote, on this machine.

    Runs the three cleanups against their real default locations: strips
    ucode-managed keys from ``~/.codex/config.toml``, deletes ucode's
    sidecar files, and unregisters ucode's ``web_search`` MCP from Claude
    Code's user scope. Every step only removes ucode-namespaced artifacts,
    so the call is safe regardless of which agents ucode actually
    configured, and is idempotent.

    :returns: What was removed, for the caller to report to the user.
    :raises OmnigentError: If ``~/.codex/config.toml`` exists but is not
        valid TOML (see :func:`strip_ucode_codex_config`).
    :raises OSError: If a file cannot be read, written, or deleted.
    """
    return UcodeWiringRemoval(
        codex_config_stripped=strip_ucode_codex_config(),
        removed_sidecars=remove_ucode_sidecars(),
        web_search_mcp_removed=remove_ucode_web_search_mcp(),
    )
