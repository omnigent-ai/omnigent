"""Hardened fetch of a GitHub repository for untrusted content.

Shared by ``omnigent agent add`` and the skills marketplace so both fetch the
same way: a shallow clone with hooks, submodules, credential prompts and LFS
smudging disabled, and a tarball of the checkout that refuses symlinks and
LFS pointers and leaves ``${VAR}`` references unexpanded.
"""

from __future__ import annotations

import base64
import contextlib
import io
import os
import re
import signal
import subprocess
import tarfile
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from omnigent.git_providers import EnvInstances, Instances
from omnigent.git_providers.github import GitHubProvider
from omnigent.skills.limits import MAX_SOURCE_PATH_CHARS

# ``owner/repo`` and ``github:`` shorthands mean github.com; a URL, or a bare
# ``host/owner/repo`` (the form ``allowed_sources`` uses; a GitHub owner has no
# dot, so a dotted first segment is a host), names its own host, which must be
# github.com or a GitHub Enterprise host the server knows.
_GITHUB_SOURCE_RE = re.compile(
    r"^(?:github:|https://(?P<host>[A-Za-z0-9.-]+)/|(?P<bare_host>[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)/)?"
    r"(?P<owner>\w[\w.-]*)/(?P<repo>\w[\w.-]*?)(?:\.git)?"
    r"(?:/tree/(?P<tree_ref>[^/@]+))?(?P<subpath>(?:/\w[\w.-]*)*)/?(?:@(?P<name>[\w.-]+))?$"
)
DEFAULT_HOST = "github.com"
# A repository directory: the same segments ``_GITHUB_SOURCE_RE`` accepts.
_SOURCE_PATH_RE = re.compile(r"\w[\w.-]*(?:/\w[\w.-]*)*")

LFS_POINTER = b"version https://git-lfs.github.com/spec/"

CLONE_TIMEOUT_S = 300
# How often the clone watchdog measures the checkout on disk.
CHECKOUT_POLL_S = 0.25
# How long to wait for git to exit once its process group has been killed.
KILL_WAIT_S = 5.0
# A transfer slower than 1 KB/s for 30 s fails instead of waiting out the timeout.
_LOW_SPEED_CONFIG = ("-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=30")
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
    host = (m["host"] or m["bare_host"] or DEFAULT_HOST).lower()
    if not GitHubProvider().matches_host(host, EnvInstances() if instances is None else instances):
        return None
    return GitHubSource(
        clone_url=f"https://{host}/{m['owner']}/{m['repo']}.git",
        ref=m["tree_ref"],
        subpath=m["subpath"].strip("/"),
        name=m["name"],
        host=host,
    )


def normalize_source_path(path: str | None) -> str:
    """A repository directory in POSIX form, ``""`` for the root.

    :raises GitSourceError: When it leaves the repository, uses characters a
        GitHub path link cannot carry, or is too long to store.
    """
    cleaned = (path or "").strip().strip("/")
    if not cleaned:
        return ""
    if not _SOURCE_PATH_RE.fullmatch(cleaned) or ".." in PurePosixPath(cleaned).parts:
        raise GitSourceError(f"{cleaned!r} is not a directory inside the repository.")
    if len(cleaned) > MAX_SOURCE_PATH_CHARS:
        raise GitSourceError(
            f"A source directory is limited to {MAX_SOURCE_PATH_CHARS} characters."
        )
    return cleaned


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


@dataclass(frozen=True)
class TreeUsage:
    """What a checkout occupies: allocated bytes and entries, directories included."""

    bytes: int
    entries: int


def _tree_usage(path: Path) -> TreeUsage:
    """Disk usage under *path*: allocated blocks where the OS reports them, links
    counted by their own size and never followed, every file and directory an entry."""
    total = entries = 0
    for dirpath, dirnames, filenames in os.walk(path):
        entries += len(dirnames)
        for filename in filenames:
            entries += 1
            try:
                info = os.lstat(os.path.join(dirpath, filename))
            except OSError:
                continue
            blocks = getattr(info, "st_blocks", None)
            total += max(info.st_size, blocks * 512 if blocks is not None else 0)
    return TreeUsage(bytes=total, entries=entries)


def _over_budget(dest: Path, max_bytes: int | None, max_entries: int | None) -> str | None:
    """A user-facing reason when *dest* exceeds either budget, else ``None``."""
    if (max_bytes is None and max_entries is None) or not dest.exists():
        return None
    usage = _tree_usage(dest)
    if max_bytes is not None and usage.bytes > max_bytes:
        return f"The repository exceeds {max_bytes // (1024 * 1024)} MB on disk; refusing it."
    if max_entries is not None and usage.entries > max_entries:
        return f"The repository has more than {max_entries} files and directories; refusing it."
    return None


@dataclass(frozen=True)
class SparseCheckout:
    """Which parts of a partial clone's tree to check out.

    :param patterns: Non-cone sparse-checkout patterns (gitignore syntax),
        e.g. ``("/team-a/**/SKILL.md",)``.
    :param expand: Called with the checkout once *patterns* are on disk; the
        patterns it returns are checked out as well, before the clone returns.
        Lets a caller index manifests first and then fetch only what it needs.
    """

    patterns: tuple[str, ...]
    expand: Callable[[Path], Sequence[str]] | None = None


def _kill_process_group(proc: subprocess.Popen[str]) -> None:
    """Kill git and every helper it started (``git-remote-https``, ``index-pack``).

    A helper inherits git's pipes, so killing git alone leaves them open and a
    later read never returns.
    """
    if os.name == "posix":
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
    else:
        proc.kill()


def _drain_after_kill(proc: subprocess.Popen[str]) -> tuple[str, str]:
    """Collect what a killed git wrote, giving up after :data:`KILL_WAIT_S`."""
    try:
        return proc.communicate(timeout=KILL_WAIT_S)
    except subprocess.TimeoutExpired:
        # Something outside the group still holds a pipe: stop reading.
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=KILL_WAIT_S)
        return "", ""


def _run_clone(
    cmd: list[str],
    env: dict[str, str],
    dest: Path,
    max_bytes: int | None,
    max_entries: int | None = None,
    *,
    deadline: float | None = None,
    label: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run git, killing it once *dest* exceeds its byte or entry budget on disk.

    A shallow clone or a blob filter does not bound what the server writes,
    since the checkout may fetch what the filter omitted; the watchdog does,
    and the finished checkout is measured once more, so a clone that crosses
    the budget and exits before the next poll is refused too. git runs in its
    own process group so a timeout kills its helpers with it.

    :param deadline: ``time.monotonic()`` value to stop at; :data:`CLONE_TIMEOUT_S`
        from now when ``None``.
    :param label: What a timeout message names; the clone URL by default.
    :raises GitSourceError: When git is missing, the clone times out, or it
        exceeds a budget.
    """
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            # Its own process group, so a kill reaches git's helpers too.
            start_new_session=os.name == "posix",
        )
    except FileNotFoundError:
        raise GitSourceError("Installing from GitHub needs git on your PATH.") from None
    deadline = time.monotonic() + CLONE_TIMEOUT_S if deadline is None else deadline
    refusal: str | None = None
    timed_out = False
    while True:
        try:
            stdout, stderr = proc.communicate(timeout=CHECKOUT_POLL_S)
            break
        except subprocess.TimeoutExpired:
            refusal = _over_budget(dest, max_bytes, max_entries)
            timed_out = time.monotonic() > deadline
            if refusal is not None or timed_out:
                _kill_process_group(proc)
                stdout, stderr = _drain_after_kill(proc)
                break
    refusal = refusal or _over_budget(dest, max_bytes, max_entries)
    if refusal is not None:
        raise GitSourceError(refusal)
    if timed_out or proc.returncode in (-9, 137):
        raise GitSourceError(f"Timed out cloning {label or cmd[-2]}.")
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def clone_github_source(
    clone_url: str,
    ref: str | None,
    dest: Path,
    *,
    isolated: bool = False,
    token: str | None = None,
    max_bytes: int | None = None,
    max_entries: int | None = None,
    sparse: SparseCheckout | None = None,
    timeout_s: float | None = None,
) -> None:
    """Shallow-clone without submodules, hooks, or credential prompts.

    :param isolated: Drop the inherited git configuration and credential
        helpers (a server fetching on a user's behalf). The CLI keeps the
        developer's own git setup.
    :param token: An operator-scoped token for this host, sent as an
        authorization header through the environment (:func:`auth_header_env`).
    :param max_bytes: Kill or refuse the clone once the checkout holds more than
        this on disk; ``None`` for the developer's own CLI.
    :param max_entries: The same for files and directories together.
    :param sparse: Check out only these parts of the tree, from a partial
        (``--filter=blob:none``) clone, so a large monorepo costs what the
        selected directories cost; ``None`` checks out the whole tree.
    :param timeout_s: Wall-clock limit for every git step together;
        :data:`CLONE_TIMEOUT_S` when ``None``.
    :raises GitSourceError: When git is missing, the clone times out, exceeds
        *max_bytes*, or git fails.
    """
    base = [GIT, "-c", "core.hooksPath=/dev/null", "-c", "protocol.allow=never",
            "-c", "protocol.https.allow=always", *_LOW_SPEED_CONFIG]  # fmt: skip
    if isolated:
        base += ["-c", "credential.helper="]
    cmd = [*base, "clone", "--depth", "1", "--no-recurse-submodules", "--quiet"]
    if sparse is not None:
        cmd += ["--filter=blob:none", "--no-checkout"]
    if ref:
        cmd += ["--branch", ref]
    deadline = time.monotonic() + (CLONE_TIMEOUT_S if timeout_s is None else timeout_s)
    with tempfile.TemporaryDirectory(prefix="omnigent-git-home-") as home:
        env = (
            isolated_git_env(home)
            if isolated
            else {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_LFS_SKIP_SMUDGE": "1"}
        )
        if token:
            env.update(auth_header_env(clone_url, token))

        def run(step: list[str]) -> subprocess.CompletedProcess[str]:
            return _run_clone(
                step, env, dest, max_bytes, max_entries, deadline=deadline, label=clone_url
            )

        proc = run([*cmd, "--", clone_url, str(dest)])
        if proc.returncode == 0 and sparse is not None:
            proc = _sparse_checkout(dest, base, sparse.patterns, run)
            extra = sparse.expand(dest) if proc.returncode == 0 and sparse.expand else ()
            if extra:
                proc = _sparse_checkout(dest, base, (*sparse.patterns, *extra), run)
    if proc.returncode != 0:
        hint = (
            "\n(Only branch and tag names work as a ref, not commit SHAs.)"
            if ref and re.fullmatch(r"[0-9a-f]{7,40}", ref)
            else ""
        )
        raise GitSourceError(f"Could not clone {clone_url}: {proc.stderr.strip()}{hint}")


def _sparse_checkout(
    dest: Path,
    base: list[str],
    patterns: Sequence[str],
    run: Callable[[list[str]], subprocess.CompletedProcess[str]],
) -> subprocess.CompletedProcess[str]:
    """Check out *patterns* of HEAD; the partial clone fetches just their blobs.

    The pattern file and ``core.sparseCheckout`` are used directly rather than
    ``git sparse-checkout``, whose non-cone flags differ between git releases.
    """
    info = dest / ".git" / "info"
    info.mkdir(parents=True, exist_ok=True)
    (info / "sparse-checkout").write_text("".join(f"{pattern}\n" for pattern in patterns))
    return run(
        [*base, "-C", str(dest), "-c", "core.sparseCheckout=true", "read-tree", "-mu", "HEAD"]
    )


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
