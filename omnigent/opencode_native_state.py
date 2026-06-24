"""Persistent client-side state for ``omnigent opencode`` sessions.

The native OpenCode wrapper records the cwd used to create a session so a
later ``omnigent opencode --resume <conv_id>`` can launch OpenCode from
the same workspace. This state is intentionally client-side: local
filesystem paths belong to the user's machine and should not be stored on
the shared Omnigent server. Mirrors :mod:`omnigent.codex_native_state`.

Layout (per conversation):

    ~/.omnigent/opencode-native/<sha256(conv_id)[:32]>/launch.json
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

_STATE_ROOT_ENV_VAR = "OMNIGENT_OPENCODE_NATIVE_STATE_DIR"
_logger = logging.getLogger(__name__)
_LAUNCH_FILE = "launch.json"
_ID_HASH_CHARS = 32


@dataclass(frozen=True)
class OpenCodeNativeLaunchState:
    """
    Persisted state about how an opencode-native session was launched.

    :param working_directory: Absolute filesystem path the wrapper was
        invoked from when the session was created, e.g. ``"/home/me/repo"``.
    """

    working_directory: str


def _opencode_native_state_root() -> Path:
    """
    Return the root directory for persistent opencode-native state.

    Honors :data:`_STATE_ROOT_ENV_VAR` for tests and advanced local setups.
    Production defaults to ``~/.omnigent/opencode-native``.

    :returns: Absolute path to the state root.
    """
    override = os.environ.get(_STATE_ROOT_ENV_VAR)
    if override:
        return Path(override)
    return Path.home() / ".omnigent" / "opencode-native"


def _state_dir_for_conversation_id(conversation_id: str) -> Path:
    """
    Return the per-conversation persistent state directory.

    Hashing the conversation id prevents path traversal if a server ever
    returned an attacker-controlled id such as ``"../etc"``.

    :param conversation_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :returns: Absolute directory path; not guaranteed to exist.
    """
    digest = hashlib.sha256(conversation_id.encode("utf-8")).hexdigest()[:_ID_HASH_CHARS]
    return _opencode_native_state_root() / digest


def write_launch_state(conversation_id: str, working_directory: str) -> None:
    """
    Persist a session's launch state at creation time.

    Same-value writes are idempotent. Different-value writes are refused
    and logged because changing the recorded cwd for an existing session
    would make future resume checks incorrect.

    :param conversation_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :param working_directory: Absolute launch cwd, e.g. ``"/home/me/repo"``.
    :returns: None.
    :raises ValueError: If *working_directory* is empty or relative.
    """
    if not working_directory:
        raise ValueError("working_directory must be a non-empty absolute path")
    if not Path(working_directory).is_absolute():
        raise ValueError("working_directory must be a non-empty absolute path")
    state_dir = _state_dir_for_conversation_id(conversation_id)
    existing = read_launch_state(conversation_id)
    if existing is not None and existing.working_directory != working_directory:
        _logger.warning(
            "opencode-native launch state mismatch for %s: existing=%r new=%r; "
            "keeping existing value",
            conversation_id,
            existing.working_directory,
            working_directory,
        )
        return
    state_dir.mkdir(parents=True, exist_ok=True)
    target = state_dir / _LAUNCH_FILE
    payload = {
        "conversation_id": conversation_id,
        "working_directory": working_directory,
    }
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
    os.replace(tmp, target)


def read_launch_state(conversation_id: str) -> OpenCodeNativeLaunchState | None:
    """
    Load a session's launch state, or ``None`` if not recorded.

    Missing, unreadable, or malformed state is treated as absent so legacy
    and cross-machine resumes continue to behave as before.

    :param conversation_id: Omnigent conversation id, e.g. ``"conv_abc123"``.
    :returns: Parsed state, or ``None`` if missing / malformed.
    """
    target = _state_dir_for_conversation_id(conversation_id) / _LAUNCH_FILE
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        _logger.warning(
            "opencode-native launch state read failed for %s",
            conversation_id,
            exc_info=True,
        )
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        _logger.warning(
            "opencode-native launch state JSON is malformed for %s; ignoring",
            conversation_id,
        )
        return None
    if not isinstance(payload, dict):
        return None
    working_directory = payload.get("working_directory")
    if not isinstance(working_directory, str) or not working_directory:
        return None
    return OpenCodeNativeLaunchState(working_directory=working_directory)
