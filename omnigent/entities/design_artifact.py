"""Design page artifacts: slide decks and wireframes indexed across sessions."""

from __future__ import annotations

from dataclasses import dataclass

DESIGN_ARTIFACT_SUFFIXES: dict[str, str] = {
    ".slides.html": "deck",
    ".wireframe.html": "wireframe",
}
# Paths longer than this are not indexed; it keeps the primary key indexable.
DESIGN_ARTIFACT_PATH_MAX = 512


def design_artifact_kind(path: str) -> str | None:
    """Return ``"deck"`` or ``"wireframe"`` for an artifact path, else ``None``."""
    name = path.rsplit("/", 1)[-1]
    for suffix, kind in DESIGN_ARTIFACT_SUFFIXES.items():
        if name.endswith(suffix) and name != suffix:
            return kind
    return None


def is_safe_artifact_path(path: str) -> bool:
    """Whether *path* is a plain workspace-relative POSIX path the index may store."""
    if not path or len(path) > DESIGN_ARTIFACT_PATH_MAX or path.startswith("/") or "\\" in path:
        return False
    return all(segment not in ("", ".", "..") for segment in path.split("/"))


@dataclass(frozen=True)
class DesignArtifact:
    """One indexed artifact file in a session's workspace.

    :param session_id: Session the file was written through.
    :param path: Path relative to the session's workspace.
    :param kind: ``"deck"`` or ``"wireframe"``.
    :param updated_at: Unix epoch seconds of the last recorded change.
    """

    session_id: str
    path: str
    kind: str
    updated_at: int
