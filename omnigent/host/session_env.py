"""Per-session environment forwarded from a launching CLI to its runner.

The host daemon is shared by every session on the machine, so its own
environment cannot carry values that belong to one terminal, such as a
terminal multiplexer's pane socket or a short-lived capability token. A
launching CLI names those variables in :data:`SESSION_ENV_PASSTHROUGH_ENV_VAR`;
their values travel to the local daemon through an owner-only file that only
the launch request naming its handoff ID consumes. The server relays that
opaque ID, never the values, which stay out of the session database and the
host's own environment.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import secrets
import stat
import tempfile
import time
from collections.abc import Iterator, Mapping
from pathlib import Path

import click

from omnigent._platform import IS_WINDOWS

_logger = logging.getLogger(__name__)

# Comma-separated variable names the launching shell forwards to this
# session's runner, e.g. ``ARCATERM_CAPABILITY,MACTERM_SOCKET``.
SESSION_ENV_PASSTHROUGH_ENV_VAR = "OMNIGENT_SESSION_ENV_PASSTHROUGH"

_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Runner wiring that a forwarded value must never replace.
_RESERVED_EXACT = frozenset({"RUNNER_SERVER_URL"})
_RESERVED_PREFIX = "OMNIGENT"
# A launch reaches the daemon within seconds; an older file outlived its CLI.
_HANDOFF_MAX_AGE_S = 300.0


def _handoff_dir() -> Path:
    from omnigent.process_logging import data_dir

    return data_dir() / "session-env"


def _handoff_path(session_id: str, handoff_id: str) -> Path:
    key = hashlib.sha256(f"{session_id}\0{handoff_id}".encode()).hexdigest()
    return _handoff_dir() / f"{key}.json"


def is_forwardable_name(name: str) -> bool:
    """Return whether *name* may be forwarded into a runner environment.

    :param name: Candidate variable name, e.g. ``"ARCATERM_CAPABILITY"``.
    :returns: ``True`` for a valid name outside Omnigent's runner wiring.
    """
    return (
        _NAME_PATTERN.fullmatch(name) is not None
        and not name.upper().startswith(_RESERVED_PREFIX)
        and name.upper() not in _RESERVED_EXACT
    )


def requested_session_env(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Collect the variables the launching shell asked to forward.

    Names listed in :data:`SESSION_ENV_PASSTHROUGH_ENV_VAR` but unset in the
    shell are skipped, so one setting works inside and outside the terminal
    that defines them.

    :param source: Environment to read. Defaults to ``os.environ``.
    :returns: Mapping of each requested, present name to its value.
    :raises click.ClickException: If a listed name is malformed or reserved,
        or if there are values to forward on Windows.
    """
    env = os.environ if source is None else source
    names = [
        name.strip()
        for name in env.get(SESSION_ENV_PASSTHROUGH_ENV_VAR, "").split(",")
        if name.strip()
    ]
    rejected = sorted({name for name in names if not is_forwardable_name(name)})
    if rejected:
        raise click.ClickException(
            f"{SESSION_ENV_PASSTHROUGH_ENV_VAR} lists names that cannot be forwarded "
            f"to a runner: {', '.join(rejected)}. Use variable names that do not "
            f"start with {_RESERVED_PREFIX} and are not RUNNER_SERVER_URL."
        )
    values = {name: env[name] for name in names if name in env}
    if values and IS_WINDOWS:
        raise click.ClickException(
            f"{SESSION_ENV_PASSTHROUGH_ENV_VAR} is not supported on Windows, where the "
            "file that hands the values to the host daemon cannot be restricted to your "
            "account. Unset it to launch without forwarding."
        )
    return values


@contextlib.contextmanager
def session_env_handoff(session_id: str, values: Mapping[str, str]) -> Iterator[str | None]:
    """Offer *values* to the daemon's launch of *session_id* for this request.

    The file is keyed by the session and a fresh, non-secret handoff ID that
    the launch request carries to the daemon, so only that launch's runner can
    consume it. The file exists only while the body runs.

    :param session_id: Session whose runner should receive *values*.
    :param values: Forwarded variables from :func:`requested_session_env`.
    :returns: The handoff ID to send with the launch request, or ``None`` when
        there is nothing to forward.
    """
    if not values:
        yield None
        return
    directory = _handoff_dir()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    _remove_expired_handoffs(directory)
    handoff_id = secrets.token_urlsafe(16)
    path = _handoff_path(session_id, handoff_id)
    try:
        _write_handoff(directory, path, values)
        yield handoff_id
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)


def session_env_handoff_pending(session_id: str, handoff_id: str) -> bool:
    """Return whether the daemon has not consumed this launch's handoff.

    :param session_id: Session the handoff was written for.
    :param handoff_id: ID yielded by :func:`session_env_handoff`.
    :returns: ``True`` while the file is still waiting.
    """
    return os.path.lexists(_handoff_path(session_id, handoff_id))


def _write_handoff(directory: Path, path: Path, values: Mapping[str, str]) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=".session-env-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(dict(values), handle)
        os.replace(temp_name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_name)


def consume_session_env_handoff(session_id: str, handoff_id: str | None) -> dict[str, str]:
    """Read and remove the forwarded variables waiting for one launch.

    :param session_id: Session the daemon is launching a runner for.
    :param handoff_id: Handoff ID the launch request carried, or ``None`` for
        a launch no CLI handed values to, such as a server-driven restart.
    :returns: Forwarded variables, or ``{}`` when none are waiting or the file
        is not a fresh, owner-only regular file.
    """
    _remove_expired_handoffs(_handoff_dir())
    if not handoff_id:
        return {}
    path = _handoff_path(session_id, handoff_id)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return {}
    except OSError as exc:
        _logger.warning("Ignoring unreadable session env handoff %s: %s", path, exc)
        with contextlib.suppress(OSError):
            os.unlink(path)
        return {}
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            info = os.fstat(handle.fileno())
            problem = _handoff_problem(info)
            payload = None if problem else json.load(handle)
    except (OSError, ValueError) as exc:
        problem, payload = f"unreadable ({type(exc).__name__})", None
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)
    if problem:
        _logger.warning("Ignoring session env handoff %s: %s", path, problem)
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        name: value
        for name, value in payload.items()
        if isinstance(name, str) and isinstance(value, str) and is_forwardable_name(name)
    }


def _handoff_problem(info: os.stat_result) -> str | None:
    if not stat.S_ISREG(info.st_mode):
        return "not a regular file"
    if IS_WINDOWS:
        return "owner-only access cannot be verified on Windows"
    if info.st_uid != os.getuid():
        return "owned by another user"
    if info.st_mode & 0o077:
        return "readable by other users"
    if time.time() - info.st_mtime > _HANDOFF_MAX_AGE_S:
        return "stale"
    return None


def _remove_expired_handoffs(directory: Path) -> None:
    cutoff = time.time() - _HANDOFF_MAX_AGE_S
    try:
        entries = list(os.scandir(directory))
    except FileNotFoundError:
        return
    except OSError as exc:
        _logger.warning("Cannot scan session env handoffs in %s: %s", directory, exc)
        return
    for entry in entries:
        with contextlib.suppress(OSError):
            if entry.stat(follow_symlinks=False).st_mtime < cutoff:
                os.unlink(entry.path)
