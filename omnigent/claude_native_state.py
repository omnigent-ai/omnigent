"""Persistent client-side state for ``omnigent claude`` sessions.

The wrapper records a small amount of per-conversation state at session
creation time and reads it back on resume. Today the only recorded fact
is the **launch cwd**: Claude Code's ``--resume <claude_sid>`` requires
the resuming invocation's cwd to match the cwd of the original session,
and resuming from a different directory makes Claude exit immediately.
The picker and the resume-helper both read the recorded cwd to detect a
mismatch and offer to ``chdir`` before spawning Claude.

Why client-side and not server-side: the launch cwd is a fact about
*this client invocation on this user's machine*. It has no meaning to
other clients or to the server. Putting it on the server would:

* leak filesystem layout across users in shared deployments (a path
  like ``/home/alice/private/project`` would be visible to anyone who
  could see the conversation),
* embed client-process state in a server-side entity (poor layering),
* require a database migration for a fact the client owns.

Why under ``~/.omnigent/`` and not the existing bridge dir at
``/tmp/omnigent-<uid>/claude-native/``: tmpfs gets cleared on
reboot (and by tmp-cleaner cron on many distros). The bridge dir is
correctly transient for hooks / tmux / token state, but the launch
cwd needs to survive across reboots so a user who resumes a session
the day after creating it still gets the chdir prompt. ``~/.omnigent/``
is where the persistent Omnigent server SQLite db and other durable
single-user state already live.

Layout (per conversation):

    ~/.omnigent/claude-native/<sha256(conv_id)[:32]>/launch.json

The directory is hashed (not the raw conv id) so a malicious server
returning an attacker-chosen conversation id like ``"../../../etc"``
cannot escape ``~/.omnigent/claude-native/``. A short hash is enough
because we never enumerate the directory by id; we only look up the
deterministic path for a known conv id.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

# Env-var override for the persistent state root. Reserved for tests
# (and for advanced users who want to put state on a non-default
# volume). When unset, the module falls back to
# ``~/.omnigent/claude-native``. Tests should set this to a per-
# test ``tmp_path`` via monkeypatch.setenv so they never touch the
# user's real home directory.
_STATE_ROOT_ENV_VAR = "OMNIGENT_CLAUDE_NATIVE_STATE_DIR"

_logger = logging.getLogger(__name__)

# Filename inside the per-conversation directory. Singular file because
# we only persist one structure today; growing this later would mean
# adding sibling files (``approvals.json`` etc.), not encoding multiple
# concerns into the same blob.
_LAUNCH_FILE = "launch.json"

# How many hex chars of the conv-id sha256 to use. 32 chars (128 bits)
# is far more than collision-safe given a single-user namespace, and
# matches the existing bridge-dir convention in
# :mod:`omnigent.claude_native_bridge`.
_ID_HASH_CHARS = 32


@dataclass(frozen=True)
class ClaudeNativeLaunchState:
    """
    Persisted state about how a claude-native session was launched.

    :param working_directory: Absolute filesystem path the wrapper
        was invoked from when the session was created, e.g.
        ``"/home/me/repo"``. Always a non-empty absolute string --
        the writer resolves the cwd through ``Path.cwd().resolve()``
        before persisting.
    """

    working_directory: str


def _claude_native_state_root() -> Path:
    """
    Return the root directory for persistent claude-native state.

    Honors the :data:`_STATE_ROOT_ENV_VAR` override so tests can
    point the state tree at a per-test ``tmp_path`` without
    clobbering the user's real home directory. Production callers
    leave the env unset and get the default
    ``~/.omnigent/claude-native``.

    Lazy: created on first write, never on read (the resume / picker
    paths read first and a stat-only check has no business creating
    directories on disk). Lives under ``~/.omnigent/claude-native/``
    so it sits next to the existing ``chat.db`` / ``logs/`` / etc.

    :returns: Absolute path to the state root.
    """
    override = os.environ.get(_STATE_ROOT_ENV_VAR)
    if override:
        return Path(override)
    return Path.home() / ".omnigent" / "claude-native"


def _state_dir_for_conversation_id(conversation_id: str) -> Path:
    """
    Return the per-conversation persistent state directory.

    The directory name is derived from a sha256 of the conversation
    id (truncated to :data:`_ID_HASH_CHARS` hex chars). Using a hash
    rather than the raw id prevents path-traversal abuse if a
    server ever returned an attacker-chosen id like ``"../etc"`` --
    every byte that lands in the path is hex, so the result is
    always a single child of the state root.

    Sessions created before ids dropped the ``conv_`` prefix hashed the
    prefixed string, so their directories live under the legacy digest; when
    the bare-digest directory is absent, the legacy one is returned (never
    renamed — files inside may embed their own absolute path).

    :param conversation_id: Omnigent conversation id, bare 32-char hex
        (a legacy ``conv_``-prefixed form is accepted and normalised).
    :returns: Absolute directory path; not guaranteed to exist.
    """
    bare = conversation_id.removeprefix("conv_")
    root = _claude_native_state_root()
    state_dir = root / hashlib.sha256(bare.encode("utf-8")).hexdigest()[:_ID_HASH_CHARS]
    if not state_dir.exists():
        legacy = root / hashlib.sha256(f"conv_{bare}".encode()).hexdigest()[:_ID_HASH_CHARS]
        if legacy.exists():
            return legacy
    return state_dir


def write_launch_state(conversation_id: str, working_directory: str) -> None:
    """
    Persist a session's launch state at creation time.

    Idempotent on same-value writes (e.g. a wrapper retry after a
    transient failure between session creation and the write here).
    Overwriting an existing different value would be a bug -- the
    launch cwd is a fact about the original session and rewriting
    it would silently invalidate the resume-time mismatch detection
    -- so we log a warning and refuse the overwrite, leaving the
    prior value intact. (The wrapper doesn't have a legitimate
    reason to call this with a different cwd for the same conv id.)

    Atomic via temp-file + ``os.replace`` so a crash mid-write never
    leaves a half-written JSON blob that a later resume would fail
    to parse.

    :param conversation_id: Omnigent conversation id, e.g.
        ``"conv_abc123"``.
    :param working_directory: Absolute filesystem path the wrapper
        was invoked from, e.g. ``"/home/me/repo"``. Should already be
        ``Path.cwd().resolve()``-canonicalized by the caller so
        symlink variants don't trigger false mismatches on resume.
    :returns: None.
    """
    if not working_directory:
        # Empty string would be a programmer error (silently
        # accepting it would later make ``read_launch_state``
        # return an entry whose comparison to a real cwd always
        # mismatches). Fail loud at the write site instead.
        raise ValueError("working_directory must be a non-empty absolute path")
    state_dir = _state_dir_for_conversation_id(conversation_id)
    existing = read_launch_state(conversation_id)
    if existing is not None and existing.working_directory != working_directory:
        # Overwrite-with-different-value is suspicious. Don't raise
        # (the calling site is on the hot path between session
        # creation and attach; the user has nothing actionable to
        # do about this), but leave the original value alone and
        # log loudly so the contradiction shows up in diagnostics.
        _logger.warning(
            "claude-native launch state mismatch for %s: existing=%r new=%r; "
            "keeping existing value",
            conversation_id,
            existing.working_directory,
            working_directory,
        )
        return
    state_dir.mkdir(parents=True, exist_ok=True)
    target = state_dir / _LAUNCH_FILE
    # Atomic-rename write: serialize, write to a sibling temp,
    # rename. ``os.replace`` is POSIX-atomic within the same
    # filesystem, which holds because we created the temp in the
    # target's parent directory. We deliberately do NOT ``fsync``
    # before the rename -- this state is a UX nicety (the next
    # resume's chdir prompt), not a durability primitive, and a
    # power-loss between write and fsync would just leave the
    # session looking like a legacy unrecorded session on resume.
    payload = {
        "conversation_id": conversation_id,
        "working_directory": working_directory,
    }
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
    os.replace(tmp, target)


def redirect_launch_state(conversation_id: str, working_directory: str) -> None:
    """
    Explicitly replace a session's launch cwd after user-approved redirect.

    Unlike :func:`write_launch_state`, this function is intentionally
    not idempotent-only: it is called after the user chooses to make a
    Claude transcript resumable from a different directory. The
    redirect action changes the launch cwd contract for future
    resumes, so the persisted cwd must follow it.

    :param conversation_id: Omnigent conversation id, e.g.
        ``"conv_abc123"``.
    :param working_directory: New absolute filesystem path for
        future resumes, e.g. ``"/home/me/new-repo"``.
    :returns: None.
    """
    if not working_directory:
        raise ValueError("working_directory must be a non-empty absolute path")
    state_dir = _state_dir_for_conversation_id(conversation_id)
    state_dir.mkdir(parents=True, exist_ok=True)
    target = state_dir / _LAUNCH_FILE
    payload = {
        "conversation_id": conversation_id,
        "working_directory": working_directory,
    }
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
    os.replace(tmp, target)


def read_launch_state(conversation_id: str) -> ClaudeNativeLaunchState | None:
    """
    Load a session's launch state, or ``None`` if not recorded.

    Used by two paths:

    * the resume helper, to decide whether to prompt for a chdir;
    * the picker, to render the Workspace column for each row.

    Never raises on a missing file -- ``None`` means "no recorded
    state for this conversation" (legacy session created before this
    tracking landed, a session created on a different machine, or a
    user who wiped ``~/.omnigent/claude-native/``). The callers
    treat ``None`` as "skip the cwd-mismatch check" and proceed.

    Returns ``None`` (with a warning log) for malformed JSON or
    missing fields rather than raising. The launch state is a UX
    nicety, not a correctness primitive; a corrupted file shouldn't
    block resume. The user can still chdir manually if Claude exits.

    :param conversation_id: Omnigent conversation id, e.g.
        ``"conv_abc123"``.
    :returns: Parsed state, or ``None`` if missing / malformed.
    """
    target = _state_dir_for_conversation_id(conversation_id) / _LAUNCH_FILE
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        # Read errors (permission denied, fs-level transient
        # failures) are best-effort: log + return None so the
        # caller treats it as "no recorded state" instead of
        # propagating the error to the user.
        _logger.warning(
            "claude-native launch state read failed for %s",
            conversation_id,
            exc_info=True,
        )
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        _logger.warning(
            "claude-native launch state JSON is malformed for %s; ignoring",
            conversation_id,
        )
        return None
    if not isinstance(payload, dict):
        return None
    working_directory = payload.get("working_directory")
    if not isinstance(working_directory, str) or not working_directory:
        return None
    return ClaudeNativeLaunchState(working_directory=working_directory)
