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

# Inherited git configuration is dropped for a server-side fetch so a request
# from one user can never borrow the operator's credential helpers or config.
_INHERITED_GIT_ENV_PREFIXES = ("GIT_", "SSH_", "GPG_")


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


def isolated_git_env() -> dict[str, str]:
    """An environment for a server-side clone: no inherited git, ssh or gpg settings.

    Global and system git config are pointed at empty files and credential
    helpers are disabled on the command line, so the clone can only reach
    what an anonymous HTTPS client can.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(_INHERITED_GIT_ENV_PREFIXES)
    }
    env.update(
        {
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_LFS_SKIP_SMUDGE": "1",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_ASKPASS": "",
            "SSH_ASKPASS": "",
        }
    )
    return env


def clone_github_source(
    clone_url: str, ref: str | None, dest: Path, *, isolated: bool = False
) -> None:
    """Shallow-clone without submodules, hooks, or credential prompts.

    :param isolated: Drop the inherited git configuration and credential
        helpers (a server fetching on a user's behalf). The CLI keeps the
        developer's own git setup.
    :raises GitSourceError: When git is missing, the clone times out, or git fails.
    """
    cmd = ["git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.allow=never",
           "-c", "protocol.https.allow=always"]  # fmt: skip
    if isolated:
        cmd += ["-c", "credential.helper="]
    cmd += ["clone", "--depth", "1", "--no-recurse-submodules", "--quiet"]
    if ref:
        cmd += ["--branch", ref]
    env = (
        isolated_git_env()
        if isolated
        else {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"}
    )
    try:
        proc = subprocess.run(
            [*cmd, "--", clone_url, str(dest)],
            capture_output=True,
            text=True,
            env=env,
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


def tar_untrusted_dir(
    source: Path,
    *,
    what: str = "an agent",
    max_bytes: int | None = None,
    max_entries: int | None = None,
) -> bytes:
    """Tar a directory from a remote repo as is.

    ``${VAR}`` references are left unexpanded so a repo cannot pull this
    process's secrets into its config, symlinks are refused so it cannot pull
    in files from outside the checkout, ``.git`` is skipped, and Git LFS
    pointers (never downloaded) are refused. The limits are checked while
    walking, before a file is read, so an oversized tree stops early.

    :param source: Directory to pack; entries are stored relative to it.
    :param what: Noun for error messages, e.g. ``"an agent"`` or ``"a skill"``.
    :param max_bytes: Total size of regular files allowed, or ``None``.
    :param max_entries: Number of entries allowed, or ``None``.
    :raises GitSourceError: On a symlink, an LFS pointer, or a limit.
    """
    total = entries = 0
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
            entries += 1
            if max_entries is not None and entries > max_entries:
                raise GitSourceError(f"{what} has more than {max_entries} files; refusing it.")
            if file_path.is_file():
                total += file_path.lstat().st_size
                if max_bytes is not None and total > max_bytes:
                    raise GitSourceError(
                        f"{what} is larger than {max_bytes // (1024 * 1024)} MB; refusing it."
                    )
                with file_path.open("rb") as fh:
                    if fh.read(len(LFS_POINTER)) == LFS_POINTER:
                        raise GitSourceError(
                            f"{printable(str(rel))} is stored in Git LFS, which "
                            "`omnigent agent add` doesn't download."
                        )
                tf.add(str(file_path), arcname=str(rel))
    return buf.getvalue()
