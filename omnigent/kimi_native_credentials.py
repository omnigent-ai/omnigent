"""Build isolated current and legacy Kimi homes with Omnigent policy hooks.

Kimi 1.49 reads ``$KIMI_SHARE_DIR`` (default ``~/.kimi``); legacy Kimi reads
``$KIMI_CODE_HOME`` (default ``~/.kimi-code``). Each has one ``config.toml``
and no project-level hook merge. To apply policy without mutating either global
home, the runner creates session-scoped equivalents that:

- link reusable login/provider files while isolating config, logs, and sessions;
- append a ``PreToolUse`` policy hook to both layouts; and
- retain the legacy ``PermissionRequest`` hook only where that event exists.

Appending as text (rather than parsing + re-emitting TOML) keeps the user's
config byte-for-byte and needs no TOML writer: a trailing ``[[hooks]]`` table
array is always valid regardless of what section preceded it.
"""

from __future__ import annotations

import contextlib
import os
import re
import shlex
import sys
from pathlib import Path

import tomllib

#: Env var Kimi CLI 1.49+ reads for config, auth, and session state.
KIMI_SHARE_DIR_ENV_VAR = "KIMI_SHARE_DIR"
#: Env var Kimi Code reads to locate its data dir (config.toml + oauth + …).
KIMI_CODE_HOME_ENV_VAR = "KIMI_CODE_HOME"
_CONFIG_FILE = "config.toml"
_CURRENT_ISOLATED_ENTRIES = frozenset(
    {_CONFIG_FILE, "imported_sessions", "kimi.json", "logs", "sessions", "telemetry"}
)
_LEGACY_ISOLATED_ENTRIES = frozenset({_CONFIG_FILE, "logs", "session_index.jsonl", "sessions"})
_EMPTY_TOP_LEVEL_HOOKS = re.compile(
    r"^[ \t]*hooks[ \t]*=[ \t]*\[[ \t]*\][ \t]*(?:#.*)?(?:\r?\n)?$"
)


class KimiConfigError(RuntimeError):
    """Raised when launch-scoped hooks cannot be merged safely."""


def resolve_user_kimi_share_dir() -> Path:
    """Return Kimi CLI 1.49+'s global share directory."""
    env = os.environ.get(KIMI_SHARE_DIR_ENV_VAR)
    if env:
        return Path(env)
    return Path.home() / ".kimi"


def resolve_user_kimi_home() -> Path:
    """Return the user's global Kimi Code home.

    Mirrors kimi's own ``resolveKimiHome``: ``$KIMI_CODE_HOME`` when set, else
    ``~/.kimi-code``.

    :returns: The resolved home path (may not exist if the user never ran kimi).
    """
    env = os.environ.get(KIMI_CODE_HOME_ENV_VAR)
    if env:
        return Path(env)
    return Path.home() / ".kimi-code"


def render_kimi_hooks_toml(
    *,
    bridge_dir: Path,
    python_executable: str | None = None,
    include_permission_request: bool = True,
) -> str:
    """Render the two Omnigent ``[[hooks]]`` entries as TOML text.

    Both hooks dispatch to :mod:`omnigent.kimi_native_hook` with the bridge
    dir baked into the command (no secrets on the command line — the hook reads
    the server URL / auth / session id from the bridge's ``hook_config.json``).

    :param bridge_dir: The kimi-native bridge dir the hook commands read.
    :param python_executable: Interpreter to run the hook module; ``None`` uses
        :data:`sys.executable` (the runner's interpreter, which has omnigent).
    :returns: TOML text starting with a leading newline, safe to append.
    """
    python = python_executable or sys.executable
    # ``-I`` (isolated mode) is REQUIRED, not cosmetic: kimi runs the hook with
    # ``cwd`` set to the session workspace, and ``python -m`` puts cwd on
    # ``sys.path[0]``. A workspace that contains its own ``omnigent/`` directory
    # (another checkout, a vendored copy) would otherwise shadow the installed
    # package and the hook dies on ``ImportError`` before it can POST — so the
    # approval card never publishes. ``-I`` drops cwd + PYTHONPATH + user-site
    # from the path, importing only the interpreter's own omnigent. Mirrors
    # claude-native's ``python -I -m omnigent.claude_native_hook``.
    base = f"{shlex.quote(python)} -I -m omnigent.kimi_native_hook"
    bridge = shlex.quote(str(bridge_dir))
    pre = f"{base} evaluate-policy --bridge-dir {bridge}"
    # No ``matcher`` → matches every tool. Commands are TOML basic strings;
    # shlex.quote yields single-quoted POSIX tokens, which contain no double
    # quotes or backslashes, so they embed in a "..." TOML string verbatim.
    #
    # ``timeout`` is required: kimi's DEFAULT_HOOK_TIMEOUT_SECONDS is 30s, which
    # would kill the permission hook while it long-polls the web verdict (so the
    # injected Approve/Deny keystroke never lands) and could sever a slow policy
    # evaluate. Pin both to kimi's 600s ceiling — the longest the human may take
    # to answer the card — after which kimi's own TUI prompt stands.
    rendered = (
        "\n"
        "# --- Omnigent native hooks (auto-generated; do not edit) ---\n"
        "[[hooks]]\n"
        'event = "PreToolUse"\n'
        f'command = "{pre}"\n'
        "timeout = 600\n"
    )
    if not include_permission_request:
        return rendered
    perm = f"{base} permission-request --bridge-dir {bridge}"
    return (
        rendered
        + "\n"
        + "[[hooks]]\n"
        + 'event = "PermissionRequest"\n'
        + f'command = "{perm}"\n'
        + "timeout = 600\n"
    )


def _merge_config_and_hooks(base_config: str, hooks: str) -> str:
    """Append hook tables without rewriting compatible user configuration."""
    try:
        parsed = tomllib.loads(base_config) if base_config.strip() else {}
    except tomllib.TOMLDecodeError as exc:
        raise KimiConfigError("user Kimi config is invalid TOML") from exc
    configured_hooks = parsed.get("hooks")
    if configured_hooks is not None and not isinstance(configured_hooks, list):
        raise KimiConfigError("user Kimi hooks value is not a list")

    prepared = base_config
    if configured_hooks == []:
        lines = prepared.splitlines(keepends=True)
        kept: list[str] = []
        at_top_level = True
        removed = False
        for line in lines:
            stripped = line.lstrip()
            if at_top_level and _EMPTY_TOP_LEVEL_HOOKS.fullmatch(line):
                removed = True
                continue
            if stripped.startswith("[") and not stripped.startswith("#"):
                at_top_level = False
            kept.append(line)
        if not removed:
            raise KimiConfigError(
                "empty Kimi hooks declaration is not a supported top-level assignment"
            )
        prepared = "".join(kept)
    elif configured_hooks:
        raise KimiConfigError(
            "non-empty inline Kimi hooks cannot be extended without rewriting user config"
        )

    if prepared and not prepared.endswith("\n"):
        prepared += "\n"
    merged = prepared + hooks
    try:
        tomllib.loads(merged)
    except tomllib.TOMLDecodeError as exc:
        raise KimiConfigError("generated Kimi hook config is invalid TOML") from exc
    return merged


def _materialize_scoped_home(
    scoped_home: Path,
    *,
    user_home: Path,
    isolated_entries: frozenset[str],
    hooks: str,
) -> None:
    """Copy config and link immutable support files into one scoped home."""
    scoped_home.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(scoped_home, 0o700)

    base_config = ""
    if user_home.is_dir():
        for entry in user_home.iterdir():
            if entry.name in isolated_entries:
                continue
            link = scoped_home / entry.name
            if link.exists() or link.is_symlink():
                continue
            with contextlib.suppress(OSError):
                link.symlink_to(entry)
        with contextlib.suppress(OSError):
            base_config = (user_home / _CONFIG_FILE).read_text(encoding="utf-8")

    config_file = scoped_home / _CONFIG_FILE
    config_file.write_text(_merge_config_and_hooks(base_config, hooks), encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(config_file, 0o600)


def build_kimi_session_home(
    session_home: Path,
    *,
    bridge_dir: Path,
    python_executable: str | None = None,
    include_current: bool = False,
) -> dict[str, str]:
    """Materialize session-scoped Kimi homes with Omnigent policy hooks.

    The legacy home is always built. With ``include_current=True``, a sibling
    ``kimi-share`` directory is also built for Kimi 1.49+. Best-effort and
    idempotent: re-running rewrites config and keeps existing support links.

    :param session_home: Directory to use as the session's ``KIMI_CODE_HOME``.
    :param bridge_dir: The kimi-native bridge dir the hook commands read.
    :param python_executable: Interpreter for the hook commands (see
        :func:`render_kimi_hooks_toml`).
    :param include_current: Also build and export Kimi 1.49's share directory.
    :returns: Environment variables to merge into the launched process.
    """
    legacy_hooks = render_kimi_hooks_toml(
        bridge_dir=bridge_dir,
        python_executable=python_executable,
        include_permission_request=True,
    )
    _materialize_scoped_home(
        session_home,
        user_home=resolve_user_kimi_home(),
        isolated_entries=(
            _LEGACY_ISOLATED_ENTRIES if include_current else frozenset({_CONFIG_FILE})
        ),
        hooks=legacy_hooks,
    )
    env = {KIMI_CODE_HOME_ENV_VAR: str(session_home)}
    if not include_current:
        return env

    current_share_dir = session_home.parent / "kimi-share"
    current_hooks = render_kimi_hooks_toml(
        bridge_dir=bridge_dir,
        python_executable=python_executable,
        include_permission_request=False,
    )
    _materialize_scoped_home(
        current_share_dir,
        user_home=resolve_user_kimi_share_dir(),
        isolated_entries=_CURRENT_ISOLATED_ENTRIES,
        hooks=current_hooks,
    )
    return {KIMI_SHARE_DIR_ENV_VAR: str(current_share_dir), **env}
