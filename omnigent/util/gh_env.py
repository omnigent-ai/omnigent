"""Environment for running GitHub's ``gh`` CLI without leaving processes behind."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

# A session-bus address ``gh`` cannot auto-launch. Given none, ``gh`` runs
# ``dbus-launch`` for its keyring lookup, and the bus it starts outlives the
# call: one idle ``dbus-daemon`` per call on a machine with no desktop session.
GH_NO_SESSION_BUS = "disabled:"

_RUN_USER_DIR = Path("/run/user")


def gh_subprocess_env(environ: Mapping[str, str]) -> dict[str, str]:
    """Return the environment for a ``gh`` call that cannot start a session bus.

    ``gh`` reads its token from the OS keyring over D-Bus before falling back to
    its own config file. An address named by ``DBUS_SESSION_BUS_ADDRESS`` is
    kept. Otherwise the user's runtime bus is named when ``gh`` itself would
    discover it: the ``bus`` socket under ``XDG_RUNTIME_DIR`` or ``/run/user/<uid>``,
    or the address in a ``/run/user/<uid>/dbus-session`` file. When there is
    none, an address ``gh`` cannot launch, so the keyring lookup fails fast and a
    config-file token is still found.

    :param environ: The calling process's environment, e.g. ``os.environ``.
    :returns: A copy with ``DBUS_SESSION_BUS_ADDRESS`` always set.
    """
    env = dict(environ)
    if env.get("DBUS_SESSION_BUS_ADDRESS"):
        return env
    env["DBUS_SESSION_BUS_ADDRESS"] = _discover_session_bus(env) or GH_NO_SESSION_BUS
    return env


def _discover_session_bus(environ: Mapping[str, str]) -> str | None:
    """Find the user's session bus the way ``gh`` does, without launching one."""
    runtime_dirs: list[Path] = []
    if environ.get("XDG_RUNTIME_DIR"):
        runtime_dirs.append(Path(environ["XDG_RUNTIME_DIR"]))
    if hasattr(os, "getuid"):
        runtime_dirs.append(_RUN_USER_DIR / str(os.getuid()))
    for runtime_dir in runtime_dirs:
        bus = runtime_dir / "bus"
        if bus.is_socket():
            return f"unix:path={bus}"
    for runtime_dir in runtime_dirs:
        try:
            lines = (runtime_dir / "dbus-session").read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            continue
        for line in lines:
            key, sep, value = line.partition("=")
            if sep and key.strip() == "DBUS_SESSION_BUS_ADDRESS" and value.strip():
                return value.strip().strip("'\"")
    return None
