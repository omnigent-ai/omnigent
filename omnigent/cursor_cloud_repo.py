"""Repo/ref resolution for the ``cursor-cloud`` harness.

A Cursor Cloud / Background Agent runs in a fresh cloud VM that clones a
**GitHub-hosted** repository at a starting ref — it never touches the local
working tree. So unlike the local ``cursor`` harness (which operates on the
session ``cwd``), the cloud harness needs a *remote URL + ref* to launch.

This module resolves that pair:

- **Default:** the ``origin`` remote URL and current branch of the session
  ``cwd`` (so "run a cloud agent here" targets the repo you're sitting in).
- **Override:** an explicit repo URL and/or ref wins over the cwd-derived
  values (so you can target any connected repo without changing directory).

The resolved URL is normalized to the ``https://github.com/<org>/<repo>`` form
the Cursor API expects, accepting both SSH (``git@github.com:org/repo.git``)
and HTTPS (``https://github.com/org/repo.git``) remotes. Non-GitHub remotes
(GitLab, Azure DevOps, Bitbucket — all supported by the Cursor API) are
normalized where possible and otherwise passed through unchanged.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

__all__ = ["CursorCloudRepo", "RepoResolutionError", "resolve_cursor_cloud_repo"]


class RepoResolutionError(RuntimeError):
    """Raised when no repo can be resolved (no override and cwd has no remote)."""


@dataclass(frozen=True)
class CursorCloudRepo:
    """A resolved cloud-agent target: a normalized repo URL and a starting ref.

    :param url: Normalized repository URL, e.g.
        ``https://github.com/org/repo``.
    :param ref: Starting git ref (branch / tag / commit) the cloud agent
        clones at, or ``None`` to let the cloud default to the repo's default
        branch.
    """

    url: str
    ref: str | None


# ``git@host:org/repo(.git)`` — the scp-like SSH remote form.
_SSH_REMOTE_RE = re.compile(r"^(?:ssh://)?git@(?P<host>[^:/]+)[:/](?P<path>.+?)(?:\.git)?/?$")
# ``scheme://[user@]host/org/repo(.git)`` — the HTTP(S) / git protocol form.
_URL_REMOTE_RE = re.compile(
    r"^(?P<scheme>https?|git)://(?:[^@/]+@)?(?P<host>[^/]+)/(?P<path>.+?)(?:\.git)?/?$"
)


def normalize_remote_url(raw: str) -> str:
    """Normalize a git remote URL to the ``https://<host>/<path>`` form.

    Accepts SSH (``git@github.com:org/repo.git``), HTTPS
    (``https://github.com/org/repo.git``), and ``git://`` remotes; strips any
    ``.git`` suffix, embedded credentials, and trailing slash. A string that
    matches no known remote shape is returned stripped but otherwise unchanged
    (the Cursor API rejects a malformed URL more clearly than we can here).

    :param raw: The remote URL as reported by ``git remote get-url``.
    :returns: The normalized ``https://<host>/<org>/<repo>`` URL.
    """
    candidate = raw.strip()
    ssh = _SSH_REMOTE_RE.match(candidate)
    if ssh:
        return f"https://{ssh.group('host')}/{ssh.group('path')}"
    url = _URL_REMOTE_RE.match(candidate)
    if url:
        return f"https://{url.group('host')}/{url.group('path')}"
    return candidate


def _git(cwd: Path, *args: str) -> str | None:
    """Run a read-only ``git`` command in *cwd*, returning stdout or ``None``.

    Returns ``None`` on any failure (not a git repo, missing remote, git not
    installed) — callers treat ``None`` as "unavailable" and fall back or error.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    value = out.stdout.strip()
    return value or None


def resolve_cursor_cloud_repo(
    cwd: str | Path | None,
    *,
    repo_override: str | None = None,
    ref_override: str | None = None,
) -> CursorCloudRepo:
    """Resolve the cloud-agent repo URL + starting ref.

    Override precedence: an explicit ``repo_override`` / ``ref_override`` wins
    over the cwd-derived value, field by field. With no repo override, the
    ``origin`` remote of *cwd* supplies the URL and the current branch supplies
    the ref (unless ``ref_override`` is given).

    :param cwd: Session working directory to read git config from. ``None``
        means "no cwd available" — only an explicit ``repo_override`` can
        resolve in that case.
    :param repo_override: Explicit repository URL (any git remote form;
        normalized). Wins over the cwd ``origin`` remote.
    :param ref_override: Explicit starting ref. Wins over the cwd branch.
    :returns: The resolved :class:`CursorCloudRepo`.
    :raises RepoResolutionError: When no ``repo_override`` is given and *cwd*
        is unset or has no ``origin`` remote.
    """
    if repo_override and repo_override.strip():
        url = normalize_remote_url(repo_override)
        ref = ref_override.strip() if ref_override and ref_override.strip() else None
        return CursorCloudRepo(url=url, ref=ref)

    if cwd is None:
        raise RepoResolutionError(
            "cursor-cloud needs a GitHub repository to run against, but no repo "
            "was provided and there is no working directory to read an 'origin' "
            "remote from. Pass an explicit repo URL."
        )

    cwd_path = Path(cwd)
    origin = _git(cwd_path, "remote", "get-url", "origin")
    if not origin:
        raise RepoResolutionError(
            f"cursor-cloud needs a GitHub repository to run against, but {cwd_path} "
            "has no 'origin' git remote. Pass an explicit repo URL or run from a "
            "git repository with a GitHub remote."
        )

    url = normalize_remote_url(origin)
    if ref_override and ref_override.strip():
        ref: str | None = ref_override.strip()
    else:
        ref = _git(cwd_path, "rev-parse", "--abbrev-ref", "HEAD")
        # A detached HEAD reports "HEAD" — not a usable ref to clone; let the
        # cloud default to the repo's default branch instead.
        if ref == "HEAD":
            ref = None
    return CursorCloudRepo(url=url, ref=ref)
