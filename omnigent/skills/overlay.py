"""Carry installed skills into a session as bundle skills.

A runner fetches its agent bundle from the server and discovers
``skills/<name>/SKILL.md`` inside it, for every harness. Adding an installed
skill under that path in the served bundle therefore needs no runner or
harness change. The bundle's own skills always win a name collision.
"""

from __future__ import annotations

import gzip
import io
import tarfile
from collections.abc import Collection, Sequence
from pathlib import PurePosixPath

from omnigent.skills.limits import MAX_SKILL_BYTES, MAX_SKILL_ENTRIES


class SkillTarballError(Exception):
    """A stored skill tarball is not safe to unpack into a bundle."""


def _open(data: bytes) -> tarfile.TarFile:
    try:
        return tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
    except (tarfile.ReadError, tarfile.CompressionError) as exc:
        raise SkillTarballError(f"invalid tarball: {exc}") from exc


def _check_member(member: tarfile.TarInfo) -> None:
    if not (member.isfile() or member.isdir()):
        raise SkillTarballError(f"unsupported entry type: {member.name!r}")
    path = PurePosixPath(member.name)
    if path.is_absolute() or ".." in path.parts:
        raise SkillTarballError(f"unsafe path: {member.name!r}")


def validate_skill_tarball(data: bytes) -> None:
    """Reject links, traversal, bombs, and a tarball without a root ``SKILL.md``.

    :raises SkillTarballError: With a user-facing reason.
    """
    total = entries = 0
    has_manifest = False
    with _open(data) as tf:
        for member in tf:
            _check_member(member)
            entries += 1
            total += member.size
            if entries > MAX_SKILL_ENTRIES or total > MAX_SKILL_BYTES:
                raise SkillTarballError("skill is too large")
            if member.isfile() and PurePosixPath(member.name).as_posix() in {
                "SKILL.md",
                "./SKILL.md",
            }:
                has_manifest = True
    if not has_manifest:
        raise SkillTarballError("skill tarball has no SKILL.md at its root")


def bundle_skill_names(bundle_bytes: bytes) -> set[str]:
    """Directory names of every ``SKILL.md`` under ``skills/``, at any depth.

    The parser names a skill after its directory, so ``skills/team/triage``
    is ``triage``. Callers that have the parsed agent spec should pass its
    canonical names to :func:`overlay_skills` as well; this scan is the
    fallback for a bundle whose spec is unavailable.
    """
    names: set[str] = set()
    with _open(bundle_bytes) as tf:
        for member in tf:
            parts = [part for part in PurePosixPath(member.name).parts if part != "."]
            if len(parts) >= 3 and parts[0] == "skills" and parts[-1] == "SKILL.md":
                names.add(parts[-2])
    return names


def overlay_skills(
    bundle_bytes: bytes,
    skills: Sequence[tuple[str, bytes]],
    *,
    reserved: Collection[str] = (),
) -> bytes:
    """Return *bundle_bytes* with each ``(name, tarball)`` added as ``skills/<name>/``.

    A name the bundle already has, or one in *reserved* (the parsed spec's
    canonical skill names), is left alone. The archive is rebuilt once for
    all skills, with a fixed gzip timestamp so the same inputs give the same
    bytes and the runner's content-addressed cache hits. An unsafe tarball
    raises rather than being skipped, so a bad row is noticed instead of
    silently missing from the menu.
    """
    if not skills:
        return bundle_bytes
    present = bundle_skill_names(bundle_bytes) | set(reserved)
    out = io.BytesIO()
    with (
        _open(bundle_bytes) as src,
        gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as packed,
        tarfile.open(fileobj=packed, mode="w") as dst,
    ):
        for member in src:
            dst.addfile(member, src.extractfile(member) if member.isfile() else None)
        for name, data in skills:
            if name in present:
                continue
            validate_skill_tarball(data)
            with _open(data) as skill:
                for member in skill:
                    rel = PurePosixPath(member.name)
                    parts = [p for p in rel.parts if p != "."]
                    if not parts:
                        continue
                    copied = tarfile.TarInfo(name=str(PurePosixPath("skills", name, *parts)))
                    copied.size = member.size
                    copied.mode = member.mode
                    copied.mtime = member.mtime
                    copied.type = member.type
                    dst.addfile(copied, skill.extractfile(member) if member.isfile() else None)
            present.add(name)
    return out.getvalue()
