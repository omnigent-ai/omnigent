"""Stable directory identities for multi-directory sessions."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePath

DEFAULT_DIRECTORY_ID = "default"
DIRECTORY_ID_PREFIX = "dir_"
MAX_SESSION_DIRECTORIES = 16
MAX_DIRECTORY_NICKNAME_LENGTH = 80


@dataclass(frozen=True)
class SessionDirectory:
    """One project directory attached to a session.

    ``default`` is the session's primary cwd (the legacy ``workspace``
    field). Additional roots receive opaque ``dir_<uuid>`` identifiers that
    remain stable when the directory set is inherited by child sessions.
    """

    id: str
    path: str
    nickname: str | None = None

    @property
    def basename(self) -> str:
        """Return the physical directory basename."""
        return PurePath(self.path).name or self.path

    @property
    def name(self) -> str:
        """Return the persisted nickname or physical directory basename."""
        return self.nickname or self.basename

    @property
    def environment_name(self) -> str:
        """Return the environment label shown by filesystem clients."""
        if self.nickname:
            return self.nickname
        if self.id == DEFAULT_DIRECTORY_ID:
            return "Working folder"
        return self.basename

    def as_dict(self) -> dict[str, str]:
        """Return the public JSON representation."""
        return {"id": self.id, "path": self.path, "name": self.name}


def generate_directory_id() -> str:
    """Mint an opaque stable id for an additional directory."""
    return f"{DIRECTORY_ID_PREFIX}{uuid.uuid4().hex}"


def validate_directory_id(directory_id: str) -> str:
    """Validate and return a public session-directory identifier."""
    if directory_id == DEFAULT_DIRECTORY_ID:
        return directory_id
    suffix = directory_id.removeprefix(DIRECTORY_ID_PREFIX)
    if (
        not directory_id.startswith(DIRECTORY_ID_PREFIX)
        or len(suffix) != 32
        or any(ch not in "0123456789abcdef" for ch in suffix)
    ):
        raise ValueError(f"invalid session directory id: {directory_id!r}")
    return directory_id


def normalize_directory_nickname(nickname: str | None) -> str | None:
    """Validate and normalize an optional user-facing directory nickname."""
    if nickname is None:
        return None
    normalized = nickname.strip()
    if not normalized:
        raise ValueError("directory nickname must not be blank")
    if "\n" in normalized or "\r" in normalized:
        raise ValueError("directory nickname must be a single line")
    if len(normalized) > MAX_DIRECTORY_NICKNAME_LENGTH:
        raise ValueError(
            f"directory nickname must be at most {MAX_DIRECTORY_NICKNAME_LENGTH} characters"
        )
    return normalized


def build_session_directories(
    workspace: str | None,
    additional_paths: Iterable[str] = (),
    *,
    requested_additional_paths: Iterable[str] | None = None,
) -> tuple[SessionDirectory, ...]:
    """Build a new session directory set from canonical paths.

    When a host resolves a picked symlink, ``additional_paths`` contains the
    canonical target while ``requested_additional_paths`` retains the path the
    user selected. If their basenames differ, preserve the selected basename as
    the initial nickname so the UI does not replace the link name with its
    target's name.
    """
    canonical_paths = tuple(additional_paths)
    requested_paths = (
        canonical_paths
        if requested_additional_paths is None
        else tuple(requested_additional_paths)
    )
    if len(requested_paths) != len(canonical_paths):
        raise ValueError("requested and canonical additional directory counts must match")

    directories: list[SessionDirectory] = []
    if workspace is not None and workspace.strip():
        directories.append(SessionDirectory(DEFAULT_DIRECTORY_ID, workspace))
    directories.extend(
        SessionDirectory(
            generate_directory_id(),
            canonical_path,
            _requested_directory_nickname(requested_path, canonical_path),
        )
        for requested_path, canonical_path in zip(
            requested_paths,
            canonical_paths,
            strict=True,
        )
    )
    validate_session_directories(directories)
    return tuple(directories)


def _requested_directory_nickname(requested_path: str, canonical_path: str) -> str | None:
    """Return a safe picked-path basename when canonicalization changes it."""
    requested_name = PurePath(requested_path).name or requested_path
    canonical_name = PurePath(canonical_path).name or canonical_path
    if requested_name == canonical_name:
        return None
    try:
        return normalize_directory_nickname(requested_name)
    except ValueError:
        # A filesystem basename can exceed the editable nickname limit. Keep
        # creation working and fall back to the canonical basename in that case.
        return None


def validate_session_directories(
    directories: Iterable[SessionDirectory],
) -> tuple[SessionDirectory, ...]:
    """Validate size, identifier, and canonical-path uniqueness invariants."""
    values = tuple(directories)
    if len(values) > MAX_SESSION_DIRECTORIES:
        raise ValueError(f"a session supports at most {MAX_SESSION_DIRECTORIES} directories")
    ids = [directory.id for directory in values]
    if len(ids) != len(set(ids)):
        raise ValueError("session directory ids must be unique")
    paths = [directory.path for directory in values]
    if len(paths) != len(set(paths)):
        raise ValueError("session directory paths must be unique")
    for directory in values:
        if not directory.path:
            raise ValueError("session directory paths must be non-empty")
        validate_directory_id(directory.id)
        if directory.nickname is not None:
            normalized = normalize_directory_nickname(directory.nickname)
            if normalized != directory.nickname:
                raise ValueError("directory nickname must not have surrounding whitespace")
    return values


def validate_workspace_directory_consistency(
    directories: Iterable[SessionDirectory],
    workspace: str | None,
) -> tuple[SessionDirectory, ...]:
    """Ensure ``workspace`` and the stable ``default`` root agree.

    An empty directory tuple is the legacy representation and remains valid
    with a non-null workspace. A non-empty set must include exactly the same
    default path when workspace is set; additional-only child scopes require
    workspace to be null.
    """
    values = validate_session_directories(directories)
    if not values:
        return values
    default = next(
        (directory for directory in values if directory.id == DEFAULT_DIRECTORY_ID),
        None,
    )
    if workspace is None and default is not None:
        raise ValueError("a default session directory requires workspace")
    if workspace is not None and (default is None or default.path != workspace):
        raise ValueError("the default session directory must match workspace")
    return values


def select_session_directories(
    parent_directories: Iterable[SessionDirectory],
    directory_ids: Iterable[str] | None,
) -> tuple[SessionDirectory, ...]:
    """Return an inherited child scope, rejecting attempts to widen it.

    ``None`` inherits all parent roots. An explicit empty list creates a
    private scratch child with no project roots. Order always follows the
    parent's order rather than caller input.
    """
    parent = validate_session_directories(parent_directories)
    if directory_ids is None:
        return parent
    selected_ids = tuple(directory_ids)
    if len(selected_ids) != len(set(selected_ids)):
        raise ValueError("directory_ids must not contain duplicates")
    parent_ids = {directory.id for directory in parent}
    unknown = sorted(set(selected_ids) - parent_ids)
    if unknown:
        raise ValueError(f"directory_ids are outside the parent scope: {unknown}")
    wanted = set(selected_ids)
    return tuple(directory for directory in parent if directory.id in wanted)


def encode_session_directories(directories: Iterable[SessionDirectory]) -> str | None:
    """Encode a directory set for the session metadata table."""
    values = validate_session_directories(directories)
    if not values:
        return None
    return json.dumps(
        [
            {
                "id": directory.id,
                "path": directory.path,
                **({"nickname": directory.nickname} if directory.nickname is not None else {}),
            }
            for directory in values
        ],
        separators=(",", ":"),
    )


def replace_directory_nickname(
    directories: Iterable[SessionDirectory],
    directory_id: str,
    nickname: str | None,
) -> tuple[SessionDirectory, ...]:
    """Replace one attached directory's nickname while preserving its identity."""
    values = validate_session_directories(directories)
    normalized = normalize_directory_nickname(nickname)
    found = False
    updated: list[SessionDirectory] = []
    for directory in values:
        if directory.id != directory_id:
            updated.append(directory)
            continue
        found = True
        updated.append(SessionDirectory(directory.id, directory.path, normalized))
    if not found:
        raise ValueError(f"directory {directory_id!r} is not attached to the session")
    return validate_session_directories(updated)


def replace_default_directory(
    directories: Iterable[SessionDirectory],
    workspace: str,
) -> tuple[SessionDirectory, ...]:
    """Set the primary workspace while preserving additional stable roots."""
    values = validate_session_directories(directories)
    current_default = next(
        (directory for directory in values if directory.id == DEFAULT_DIRECTORY_ID),
        None,
    )
    additional = tuple(directory for directory in values if directory.id != DEFAULT_DIRECTORY_ID)
    return validate_session_directories(
        (
            SessionDirectory(
                DEFAULT_DIRECTORY_ID,
                workspace,
                current_default.nickname if current_default is not None else None,
            ),
            *additional,
        )
    )


def decode_session_directories(
    raw: str | bytes | None,
    *,
    workspace: str | None,
) -> tuple[SessionDirectory, ...]:
    """Decode stored roots, falling back to legacy ``workspace`` rows."""
    if raw is None:
        return build_session_directories(workspace)
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    payload: object = json.loads(raw)
    if not isinstance(payload, list):
        raise ValueError("stored session directories must be a list")
    directories: list[SessionDirectory] = []
    for value in payload:
        if not isinstance(value, dict):
            raise ValueError("stored session directory entries must be objects")
        directory_id = value.get("id")
        path = value.get("path")
        nickname = value.get("nickname")
        if not isinstance(directory_id, str) or not isinstance(path, str):
            raise ValueError("stored session directories require string id and path")
        if nickname is not None and not isinstance(nickname, str):
            raise ValueError("stored session directory nickname must be a string or null")
        directories.append(SessionDirectory(directory_id, path, nickname))
    values = validate_session_directories(directories)
    return validate_workspace_directory_consistency(values, workspace)
