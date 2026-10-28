"""Hardened fetch of a GitHub repository for untrusted content.

Shared by ``omnigent agent add`` and the skills marketplace so both fetch the
same way: a shallow clone with hooks, submodules, credential prompts and LFS
smudging disabled, and a tarball of the checkout that refuses symlinks and
LFS pointers and leaves ``${VAR}`` references unexpanded.
"""

from __future__ import annotations

import base64
import io
import os
import re
import subprocess
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from omnigent.git_providers import EnvInstances, Instances
from omnigent.git_providers.github import GitHubProvider

# ``owner/repo`` and ``github:`` shorthands mean github.com; a URL names its own
# host, which must be github.com or a GitHub Enterprise host the server knows.
_GITHUB_SOURCE_RE = re.compile(
    r"^(?:github:|https://(?P<host>[A-Za-z0-9.-]+)/)?"
    r"(?P<owner>\w[\w.-]*)/(?P<repo>\w[\w.-]*?)(?:\.git)?"
    r"(?:/tree/(?P<tree_ref>[^/@]+))?(?P<subpath>(?:/\w[\w.-]*)*)/?(?:@(?P<name>[\w.-]+))?$"
)
DEFAULT_HOST = "github.com"

LFS_POINTER = b"version https://git-lfs.github.com/spec/"

CLONE_TIMEOUT_S = 300
# How often the clone watchdog measures the checkout on disk.
CHECKOUT_POLL_S = 0.25
# The git executable; tests point it at a script.
GIT = "git"

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
    host: str = DEFAULT_HOST


def printable(text: str) -> str:
    """Strip control characters so a repo-chosen path cannot forge terminal or log output."""
    return "".join(ch if ch.isprintable() else "?" for ch in text)


def parse_github_source(source: str, *, instances: Instances | None = None) -> GitHubSource | None:
    """Parse ``owner/repo[/path][@name]`` or a GitHub URL; ``None`` when it is neither.

    A URL's host is kept all the way to the clone URL and must be github.com
    or a GitHub Enterprise host the server knows (``GH_HOST``, configured
    instances, or a host gh is signed in to); an unknown host is refused rather
    than normalised onto github.com. A ref comes from ``/tree/<ref>`` and must
    be a branch or tag without ``/``; commit SHAs cannot be shallow-cloned by name.

    :param instances: Configured provider hosts; defaults to the environment.
    """
    m = _GITHUB_SOURCE_RE.match(source)
    if m is None:
        return None
    host = (m["host"] or DEFAULT_HOST).lower()
    if not GitHubProvider().matches_host(host, EnvInstances() if instances is None else instances):
        return None
    return GitHubSource(
        clone_url=f"https://{host}/{m['owner']}/{m['repo']}.git",
        ref=m["tree_ref"],
        subpath=m["subpath"].strip("/"),
        name=m["name"],
        host=host,
    )


def repository_label(clone_url: str) -> str:
    """``owner/repo`` for a github.com clone URL, ``host/owner/repo`` elsewhere."""
    parts = urlsplit(clone_url)
    path = parts.path.strip("/").removesuffix(".git")
    if parts.scheme != "https" or not parts.hostname or path.count("/") != 1:
        return clone_url
    host = parts.hostname.lower()
    return path if host == DEFAULT_HOST else f"{host}/{path}"


def auth_header_env(clone_url: str, token: str) -> dict[str, str]:
    """Git config, through the environment, that sends *token* to this URL's host only.

    ``http.<origin>/.extraheader`` is how ``actions/checkout`` passes a token:
    it never appears on the command line, and git 2.31 or newer reads
    ``GIT_CONFIG_COUNT`` after the isolated environment dropped everything else.
    """
    parts = urlsplit(clone_url)
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": f"http.{parts.scheme}://{parts.hostname}/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
    }


def isolated_git_env(home: str) -> dict[str, str]:
    """An environment for a server-side clone: no inherited git, ssh or gpg settings.

    Global and system git config are pointed at empty files, credential
    helpers are disabled on the command line, and ``HOME`` is *home*, an
    empty directory, so neither git nor libcurl finds the operator's
    ``.netrc`` or ``.config/git``. The clone can only reach what an anonymous
    HTTPS client can.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(_INHERITED_GIT_ENV_PREFIXES)
        and key not in ("XDG_CONFIG_HOME", "CURL_HOME", "NETRC")
    }
    env.update(
        {
            "HOME": home,
            "XDG_CONFIG_HOME": home,
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


def _tree_bytes(path: Path) -> int:
    """Bytes on disk under *path*, links counted by their own size, never followed."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for filename in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, filename)).st_size
            except OSError:
                continue
    return total


def _run_clone(
    cmd: list[str], env: dict[str, str], dest: Path, max_bytes: int | None
) -> subprocess.CompletedProcess[str]:
    """Run git, killing it once *dest* holds more than *max_bytes* on disk.

    A shallow clone or a blob filter does not bound what the server writes,
    since the checkout may fetch what the filter omitted; the watchdog does.

    :raises GitSourceError: When git is missing, the clone times out, or it
        exceeds the byte budget.
    """
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
    except FileNotFoundError:
        raise GitSourceError("Installing from GitHub needs git on your PATH.") from None
    deadline = time.monotonic() + CLONE_TIMEOUT_S
    over_budget = False
    while True:
        try:
            stdout, stderr = proc.communicate(timeout=CHECKOUT_POLL_S)
            break
        except subprocess.TimeoutExpired:
            if max_bytes is not None and dest.exists() and _tree_bytes(dest) > max_bytes:
                over_budget = True
            if over_budget or time.monotonic() > deadline:
                proc.kill()
                stdout, stderr = proc.communicate()
                break
    if over_budget:
        raise GitSourceError(
            f"The repository exceeds {max_bytes // (1024 * 1024)} MB on disk; refusing it."  # type: ignore[operator]
        )
    if proc.returncode in (-9, 137) or time.monotonic() > deadline:
        raise GitSourceError(f"Timed out cloning {cmd[-2]}.")
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def clone_github_source(
    clone_url: str,
    ref: str | None,
    dest: Path,
    *,
    isolated: bool = False,
    token: str | None = None,
    max_bytes: int | None = None,
) -> None:
    """Shallow-clone without submodules, hooks, or credential prompts.

    :param isolated: Drop the inherited git configuration and credential
        helpers (a server fetching on a user's behalf). The CLI keeps the
        developer's own git setup.
    :param token: An operator-scoped token for this host, sent as an
        authorization header through the environment (:func:`auth_header_env`).
    :param max_bytes: Kill the clone once the checkout holds more than this on
        disk; ``None`` for the developer's own CLI.
    :raises GitSourceError: When git is missing, the clone times out, exceeds
        *max_bytes*, or git fails.
    """
    cmd = [GIT, "-c", "core.hooksPath=/dev/null", "-c", "protocol.allow=never",
           "-c", "protocol.https.allow=always"]  # fmt: skip
    if isolated:
        cmd += ["-c", "credential.helper="]
    cmd += ["clone", "--depth", "1", "--no-recurse-submodules", "--quiet"]
    if ref:
        cmd += ["--branch", ref]
    with tempfile.TemporaryDirectory(prefix="omnigent-git-home-") as home:
        env = (
            isolated_git_env(home)
            if isolated
            else {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"}
        )
        if token:
            env.update(auth_header_env(clone_url, token))
        proc = _run_clone([*cmd, "--", clone_url, str(dest)], env, dest, max_bytes)
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
