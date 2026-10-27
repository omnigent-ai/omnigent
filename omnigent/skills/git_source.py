"""Hardened fetch of a GitHub repository for untrusted content.

Shared by ``omnigent agent add`` and the skills marketplace so both fetch the
same way: a shallow clone with hooks, submodules, credential prompts and LFS
smudging disabled, and a tarball of the checkout that refuses symlinks and
LFS pointers and leaves ``${VAR}`` references unexpanded.
"""

from __future__ import annotations

import io
import os
import re
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path

_GITHUB_SOURCE_RE = re.compile(
    r"^(?:github:|https://github\.com/)?"
    r"(?P<owner>\w[\w.-]*)/(?P<repo>\w[\w.-]*?)(?:\.git)?"
    r"(?:/tree/(?P<tree_ref>[^/@]+))?(?P<subpath>(?:/\w[\w.-]*)*)/?(?:@(?P<name>[\w.-]+))?$"
)

LFS_POINTER = b"version https://git-lfs.github.com/spec/"

CLONE_TIMEOUT_S = 300


class GitSourceError(Exception):
    """A fetch or packaging step refused the repository; the message is user-facing."""


@dataclass(frozen=True)
class GitHubSource:
    """A parsed ``owner/repo[/tree/<ref>][/path][@name]`` reference."""

    clone_url: str
    ref: str | None
    subpath: str
    name: str | None


def printable(text: str) -> str:
    """Strip control characters so a repo-chosen path cannot forge terminal or log output."""
    return "".join(ch if ch.isprintable() else "?" for ch in text)


def parse_github_source(source: str) -> GitHubSource | None:
    """Parse ``owner/repo[/path][@name]`` or a github.com URL; ``None`` when it is neither.

    A ref comes from ``/tree/<ref>`` and must be a branch or tag without ``/``;
    commit SHAs cannot be shallow-cloned by name.
    """
    m = _GITHUB_SOURCE_RE.match(source)
    if m is None:
        return None
    return GitHubSource(
        clone_url=f"https://github.com/{m['owner']}/{m['repo']}.git",
        ref=m["tree_ref"],
        subpath=m["subpath"].strip("/"),
        name=m["name"],
    )


def clone_github_source(clone_url: str, ref: str | None, dest: Path) -> None:
    """Shallow-clone without submodules, hooks, or credential prompts.

    :raises GitSourceError: When git is missing, the clone times out, or git fails.
    """
    cmd = ["git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.allow=never",
           "-c", "protocol.https.allow=always", "clone", "--depth", "1",
           "--no-recurse-submodules", "--quiet"]  # fmt: skip
    if ref:
        cmd += ["--branch", ref]
    try:
        proc = subprocess.run(
            [*cmd, "--", clone_url, str(dest)],
            capture_output=True,
            text=True,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"},
            check=False,
            timeout=CLONE_TIMEOUT_S,
        )
    except FileNotFoundError:
        raise GitSourceError("Installing from GitHub needs git on your PATH.") from None
    except subprocess.TimeoutExpired:
        raise GitSourceError(f"Timed out cloning {clone_url}.") from None
    if proc.returncode != 0:
        hint = (
            "\n(Only branch and tag names work as a ref, not commit SHAs.)"
            if ref and re.fullmatch(r"[0-9a-f]{7,40}", ref)
            else ""
        )
        raise GitSourceError(f"Could not clone {clone_url}: {proc.stderr.strip()}{hint}")


def checkout_commit(checkout: Path) -> str | None:
    """The checked-out commit, or ``None`` when the directory is not a git checkout."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    sha = proc.stdout.strip()
    return sha if proc.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", sha) else None


def tar_untrusted_dir(source: Path, *, what: str = "an agent") -> bytes:
    """Tar a directory from a remote repo as is.

    ``${VAR}`` references are left unexpanded so a repo cannot pull this
    process's secrets into its config, symlinks are refused so it cannot pull
    in files from outside the checkout, ``.git`` is skipped, and Git LFS
    pointers (never downloaded) are refused.

    :param source: Directory to pack; entries are stored relative to it.
    :param what: Noun for error messages, e.g. ``"an agent"`` or ``"a skill"``.
    :raises GitSourceError: On a symlink or an LFS pointer.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for file_path in sorted(source.rglob("*")):
            rel = file_path.relative_to(source)
            if ".git" in rel.parts:
                continue
            if file_path.is_symlink():
                raise GitSourceError(
                    f"{printable(str(rel))} is a symlink; refusing to install "
                    f"{what} with symlinks from a remote repo."
                )
            if file_path.is_file():
                with file_path.open("rb") as fh:
                    if fh.read(len(LFS_POINTER)) == LFS_POINTER:
                        raise GitSourceError(
                            f"{printable(str(rel))} is stored in Git LFS, which "
                            "`omnigent agent add` doesn't download."
                        )
                tf.add(str(file_path), arcname=str(rel))
    return buf.getvalue()
