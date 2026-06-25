"""Per-session ``KIMI_CODE_HOME`` builder that injects Omnigent hooks.

Kimi Code reads a single ``config.toml`` at ``$KIMI_CODE_HOME/config.toml``
(default ``~/.kimi-code``) and stores its auth (``oauth/`` + ``credentials/``)
relative to the same home — there is no project-level merge for the ``hooks``
array. To gate a session's tools without mutating the user's global config, the
runner points the launched ``kimi`` process at a session-scoped home that:

- symlinks every entry of the user's global home (oauth, credentials,
  sessions, …) so login / providers / history keep working, and
- carries a ``config.toml`` that is the user's config text with two Omnigent
  ``[[hooks]]`` appended — a ``PreToolUse`` deny-gate and a ``PermissionRequest``
  read-only surface, both dispatched to :mod:`omnigent.kimi_native_hook`.

Appending as text (rather than parsing + re-emitting TOML) keeps the user's
config byte-for-byte and needs no TOML writer: a trailing ``[[hooks]]`` table
array is always valid regardless of what section preceded it.
"""

from __future__ import annotations

import contextlib
import os
import shlex
import sys
from pathlib import Path

#: Env var Kimi Code reads to locate its data dir (config.toml + oauth + …).
KIMI_CODE_HOME_ENV_VAR = "KIMI_CODE_HOME"
_CONFIG_FILE = "config.toml"


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


def render_kimi_hooks_toml(*, bridge_dir: Path, python_executable: str | None = None) -> str:
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
    perm = f"{base} permission-request --bridge-dir {bridge}"
    # No ``matcher`` → matches every tool. Commands are TOML basic strings;
    # shlex.quote yields single-quoted POSIX tokens, which contain no double
    # quotes or backslashes, so they embed in a "..." TOML string verbatim.
    #
    # ``timeout`` is required: kimi's DEFAULT_HOOK_TIMEOUT_SECONDS is 30s, which
    # would kill the permission hook while it long-polls the web verdict (so the
    # injected Approve/Deny keystroke never lands) and could sever a slow policy
    # evaluate. Pin both to kimi's 600s ceiling — the longest the human may take
    # to answer the card — after which kimi's own TUI prompt stands.
    return (
        "\n"
        "# --- Omnigent native hooks (auto-generated; do not edit) ---\n"
        "[[hooks]]\n"
        'event = "PreToolUse"\n'
        f'command = "{pre}"\n'
        "timeout = 600\n"
        "\n"
        "[[hooks]]\n"
        'event = "PermissionRequest"\n'
        f'command = "{perm}"\n'
        "timeout = 600\n"
    )


def build_kimi_session_home(
    session_home: Path,
    *,
    bridge_dir: Path,
    python_executable: str | None = None,
) -> dict[str, str]:
    """Materialize a session-scoped ``KIMI_CODE_HOME`` with Omnigent hooks.

    Symlinks every entry of the user's global kimi home (except
    ``config.toml``) into *session_home*, then writes a ``config.toml`` that is
    the user's config plus the Omnigent hooks. Best-effort and idempotent:
    re-running rewrites ``config.toml`` and leaves existing symlinks in place.

    :param session_home: Directory to use as the session's ``KIMI_CODE_HOME``.
    :param bridge_dir: The kimi-native bridge dir the hook commands read.
    :param python_executable: Interpreter for the hook commands (see
        :func:`render_kimi_hooks_toml`).
    :returns: ``{"KIMI_CODE_HOME": str(session_home)}`` to merge into the
        launched kimi process env.
    """
    session_home.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(session_home, 0o700)

    user_home = resolve_user_kimi_home()
    base_config = ""
    if user_home.is_dir():
        for entry in user_home.iterdir():
            if entry.name == _CONFIG_FILE:
                # config.toml is materialized fresh below (user content + hooks).
                continue
            link = session_home / entry.name
            if link.exists() or link.is_symlink():
                continue
            with contextlib.suppress(OSError):
                link.symlink_to(entry)
        with contextlib.suppress(OSError):
            base_config = (user_home / _CONFIG_FILE).read_text(encoding="utf-8")

    hooks = render_kimi_hooks_toml(bridge_dir=bridge_dir, python_executable=python_executable)
    # Ensure a clean separation if the user's config has no trailing newline.
    if base_config and not base_config.endswith("\n"):
        base_config += "\n"
    (session_home / _CONFIG_FILE).write_text(base_config + hooks, encoding="utf-8")

    return {KIMI_CODE_HOME_ENV_VAR: str(session_home)}
