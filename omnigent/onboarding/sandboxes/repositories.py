"""Shared repository URL validation for project, session and host inputs."""

from __future__ import annotations

import ipaddress
import re
from typing import Any
from urllib.parse import urlsplit

from omnigent.db.db_models import LABEL_VALUE_MAX_LEN
from omnigent.onboarding.sandboxes.types import RepoWorkspace

# A full 40-hex object id — rejected as a clone fragment: cloning a
# commit lands the agent on a detached HEAD it cannot push from.
_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# Directory names a repo URL may resolve to. Conservative on purpose:
# the name is interpolated into an in-sandbox shell path.
_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# Git-forbidden ref characters, plus ``#`` (the workspace splits on the first one).
_BRANCH_FORBIDDEN_CHARS = set(" \t~^:?*[\\#")


def is_repo_workspace(workspace: str) -> bool:
    """
    Return whether *workspace* is a repository-URL workspace.

    Used by the create-session schema to tell the managed form (a git
    URL) apart from the external form (an absolute host path) without
    fully parsing it.

    :param workspace: The raw request workspace, e.g.
        ``"https://github.com/org/repo"`` or ``"/Users/me/repo"``.
    :returns: ``True`` for the ``https://`` / ``git@`` URL forms.
    """
    return workspace.startswith(("https://", "git@"))


def _validate_clone_branch(fragment: str) -> str:
    """
    Validate a ``#<branch>`` fragment as a clonable branch name.

    :param fragment: The fragment text after the first ``#``, e.g.
        ``"release-1.2"``.
    :returns: The validated branch name, unchanged.
    :raises ValueError: When the fragment is empty, is a commit SHA
        (detached HEAD — pin commits via git worktree options
        instead), or violates git ref-name rules.
    """
    if not fragment:
        raise ValueError("the '#' fragment must name a branch, e.g. '#main'")
    if _COMMIT_SHA_RE.fullmatch(fragment):
        raise ValueError(
            "the '#' fragment must be a branch, not a commit SHA — a commit "
            "checkout would leave the agent on a detached HEAD it cannot push"
        )
    if (
        any(c in _BRANCH_FORBIDDEN_CHARS or ord(c) < 0x20 or ord(c) == 0x7F for c in fragment)
        or fragment.startswith(("-", "/"))
        or fragment.endswith(("/", "."))
        or ".." in fragment
        or "@{" in fragment
        or "//" in fragment
        or fragment == "@"
        or any(part.startswith(".") or part.endswith(".lock") for part in fragment.split("/"))
    ):
        raise ValueError(f"'{fragment}' is not a valid git branch name")
    return fragment


def _derive_repo_name(url: str) -> str:
    """
    Derive the clone directory name from a repository URL.

    :param url: The fragment-stripped clone URL, e.g.
        ``"https://github.com/org/repo.git"``.
    :returns: The last path segment with ``.git`` stripped, e.g.
        ``"repo"``.
    :raises ValueError: When no usable name can be derived (empty
        path, or a name that is not filesystem-safe).
    """
    last = url.rstrip("/").split("/")[-1]
    # scp-style URLs with a single-segment path ("git@host:repo.git")
    # have no "/" after the colon — take what follows it.
    if ":" in last:
        last = last.rsplit(":", 1)[-1]
    name = last[: -len(".git")] if last.endswith(".git") else last
    if not name or name in (".", "..") or not _REPO_NAME_RE.fullmatch(name):
        raise ValueError(
            f"could not derive a repository directory name from '{url}' — "
            "the URL must end in the repository name, e.g. "
            "'https://github.com/org/repo'"
        )
    return name


def parse_repo_workspace(workspace: str) -> RepoWorkspace:
    """
    Parse and validate a managed session's repository-URL workspace.

    Grammar (Docker build-context style)::

        <repo>[#<branch>]
        <repo> := https://<host>/<path>  |  git@<host>:<path>

    The fragment splits on the FIRST ``#``; branches containing ``#``
    are not supported in this form. Fails loud on anything malformed
    so a bad workspace 422s at validation instead of surfacing as a
    mid-provision clone error.

    :param workspace: The raw request workspace, e.g.
        ``"https://github.com/org/repo#release-1.2"``.
    :returns: The parsed, validated :class:`RepoWorkspace`.
    :raises ValueError: When the URL or branch fragment is malformed.
    """
    url, sep, fragment = workspace.partition("#")
    if any(ch.isspace() for ch in workspace):
        raise ValueError("a repository workspace must not contain whitespace")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in workspace):
        raise ValueError("a repository workspace must not contain control characters")
    # Each workspace is stored verbatim in its own relaunch label, whose value
    # column is capped; reject an over-long one at parse so it can't be silently
    # truncated on write and relaunch/fork the wrong ref.
    if len(workspace) > LABEL_VALUE_MAX_LEN:
        raise ValueError(
            f"a repository workspace must be at most {LABEL_VALUE_MAX_LEN} characters "
            f"(got {len(workspace)}) — shorten the URL or branch"
        )
    # Reject credentials before any error echoes the URL, so tokens never reach
    # labels, Pod specs, or logs. Any ``@`` after the scheme/``git@`` is userinfo.
    _cred_msg = (
        "a repository URL must not embed credentials (user:token@host) — "
        "connect the account instead of putting a token in the URL"
    )
    if "?" in url:
        raise ValueError("a repository URL must not contain a query")
    if url.startswith("https://"):
        rest = url[len("https://") :]
        if "@" in rest:
            raise ValueError(_cred_msg)
        host, slash, path = rest.partition("/")
        if not host or not slash or not path.strip("/"):
            raise ValueError(
                f"'{url}' is not a usable https repository URL — expected "
                "'https://<host>/<org>/<repo>'"
            )
        parsed = urlsplit(url)
        # Accessing port validates its syntax and range without contacting the host.
        _ = parsed.port
        host = parsed.hostname or ""
        if ":" in host:
            ipaddress.IPv6Address(host)
        elif (
            not host
            or len(host) > 253
            or not all(
                re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                for label in host.removesuffix(".").split(".")
            )
        ):
            raise ValueError("a repository URL must have a valid hostname")
    elif url.startswith("git@"):
        rest = url[len("git@") :]
        if "@" in rest:
            raise ValueError(_cred_msg)
        if rest.startswith("["):
            host, close, suffix = rest[1:].partition("]")
            colon, path = suffix[:1], suffix[1:]
            if not close or colon != ":" or not path.strip("/"):
                raise ValueError("an IPv6 SSH repository URL must use 'git@[address]:path'")
            if "%" in host:
                raise ValueError("an IPv6 SSH repository URL must not contain a scope identifier")
            ipaddress.IPv6Address(host)
        else:
            host, colon, path = rest.partition(":")
            # SSH aliases are configuration names, not necessarily DNS hostnames.
            # A slash before the colon is a local path to Git, not an SSH alias.
            if any(character in host for character in "/\\[]"):
                raise ValueError("an SSH repository URL must have an unambiguous host alias")
        if not host or not colon or not path.strip("/"):
            raise ValueError(
                f"'{url}' is not a usable ssh repository URL — expected 'git@<host>:<org>/<repo>'"
            )
    else:
        raise ValueError(
            f"'{url}' is not a supported repository URL — use "
            "'https://<host>/<org>/<repo>' or 'git@<host>:<org>/<repo>'"
        )
    branch = _validate_clone_branch(fragment) if sep else None
    return RepoWorkspace(url=url, branch=branch, repo_name=_derive_repo_name(url))


def normalize_repository(repository: Any) -> dict[str, str]:
    """Validate a clone descriptor without ever echoing credential-bearing input."""
    if not isinstance(repository, dict) or set(repository) - {"url", "branch"}:
        raise ValueError("repository must contain only url and optional branch")
    url = repository.get("url")
    branch = repository.get("branch", "")
    if not isinstance(url, str) or not url.strip():
        raise ValueError("repository.url must be a nonempty string")
    if not isinstance(branch, str):
        raise ValueError("repository.branch must be a string")
    url, branch = url.strip(), branch.strip()
    if "#" in url:
        raise ValueError("repository.url must be a clone URL; provide branch separately")
    try:
        parse_repo_workspace(url + (f"#{branch}" if branch else ""))
    except ValueError:
        raise ValueError(
            "Invalid repository URL or branch; use a credential-free HTTPS or SSH clone URL "
            "and a valid branch"
        ) from None
    return {"url": url, **({"branch": branch} if branch else {})}
