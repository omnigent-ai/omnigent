"""Clone a git repo and bundle an agent directory into a .tar.gz.

Server-side git import: the only new domain logic for importing custom
agents from git. Downstream (validate/store/cache) reuses the existing
bundle pipeline. Tokens must never appear in errors or logs.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from omnigent.errors import ErrorCode, OmnigentError

CLONE_TIMEOUT_SECONDS = 120
MAX_BUNDLE_BYTES = 50 * 1024 * 1024

# https URLs, or scp-style / ssh URLs. Explicitly excludes file:// and bare paths.
_HTTPS_RE = re.compile(r"^https://[^\s]+$")
# scp-style user@host:path. The user segment must start with an alphanumeric so
# a leading-dash argument (``--upload-pack=x@h:p``) can't masquerade as a remote.
_SCP_RE = re.compile(r"^[A-Za-z0-9][^\s@]*@[^\s:]+:[^\s]+$")
_SSH_RE = re.compile(r"^ssh://[^\s]+$")
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+\-.]*://")


def validate_git_url(url: str) -> None:
    """Reject anything that isn't an https / ssh / scp-style remote URL.

    Guards against SSRF and local-file exfil (``file://``, bare local paths).
    Scheme-prefixed URLs (``foo://...``) are ONLY allowed when they are
    ``https://`` or ``ssh://``; scp-style (``user@host:path``) is only
    matched when no scheme prefix is present, preventing ``file://u@h:p``
    from slipping through the SCP pattern.

    :param url: Candidate clone URL.
    :raises OmnigentError: ``INVALID_INPUT`` when the URL is not an
        acceptable remote git URL.
    """
    if not url:
        raise OmnigentError("Not a valid git URL.", code=ErrorCode.INVALID_INPUT)
    # Defence-in-depth against git argument injection: a leading '-' lets a
    # crafted "URL" like ``--upload-pack=<cmd>@h:p`` be parsed by ``git clone``
    # as an option (arbitrary command execution) rather than a repo. We also
    # pass ``--`` before the URL at the call site; rejecting leading '-' here
    # keeps the guard meaningful on its own and blocks the scp-regex bypass.
    if url.startswith("-"):
        raise OmnigentError("Not a valid git URL.", code=ErrorCode.INVALID_INPUT)
    if _SCHEME_RE.match(url):
        # URL has a scheme — only https:// and ssh:// are allowed.
        if _HTTPS_RE.match(url) or _SSH_RE.match(url):
            return
    elif _SCP_RE.match(url):
        # No scheme prefix — scp-style git remote (user@host:path).
        return
    raise OmnigentError("Not a valid git URL.", code=ErrorCode.INVALID_INPUT)


def clone_and_bundle(
    git_url: str,
    git_ref: str | None,
    git_subpath: str | None,
    *,
    token: str | None = None,
    _allow_local: bool = False,
) -> tuple[bytes, str, str]:
    """Shallow-clone a repo and bundle the agent directory.

    :param git_url: Remote clone URL.
    :param git_ref: Branch to track; ``None`` resolves the repo default branch.
    :param git_subpath: Agent directory within the repo; ``None`` = repo root.
    :param token: Optional access token injected into an https fetch URL. Never logged.
    :param _allow_local: Test-only; skip the remote-URL guard for local repo paths.
    :returns: ``(bundle_bytes, resolved_commit_sha, resolved_ref)``.
    :raises OmnigentError: INVALID_INPUT for bad URL/branch/subpath/oversize/timeout;
        INTERNAL_ERROR when the ``git`` binary is unavailable.
    """
    if not _allow_local:
        validate_git_url(git_url)
    clone_url = _inject_token(git_url, token) if token else git_url
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "repo"
        cmd = ["git", "clone", "--depth", "1"]
        if git_ref:
            cmd += ["--branch", git_ref]
        # ``--`` ends option parsing: without it a crafted URL beginning with
        # ``-`` (e.g. ``--upload-pack=<cmd>``) would be parsed by git as an
        # option, executing arbitrary commands on the host. Belt-and-suspenders
        # with validate_git_url's leading-dash rejection.
        cmd += ["--", clone_url, str(dest)]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=CLONE_TIMEOUT_SECONDS,
            )
        except FileNotFoundError as exc:
            raise OmnigentError(
                "Server is not configured for git import.",
                code=ErrorCode.INTERNAL_ERROR,
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise OmnigentError(
                "Repository clone exceeded the time limit.",
                code=ErrorCode.INVALID_INPUT,
            ) from exc
        if proc.returncode != 0:
            # Sanitize: never echo the token-injected URL.
            msg = (
                f"Branch {git_ref!r} not found in {git_url}."
                if git_ref and "not found" in proc.stderr.lower()
                else f"Could not clone {git_url} (check the URL, branch, and access)."
            )
            raise OmnigentError(msg, code=ErrorCode.INVALID_INPUT)

        resolved_ref = git_ref or _current_branch(dest)
        sha = _head_sha(dest)

        # Confine agent_dir to the clone root — guard against path traversal.
        clone_root = dest.resolve()
        if git_subpath:
            agent_dir = (dest / git_subpath).resolve()
            if agent_dir != clone_root and not str(agent_dir).startswith(str(clone_root) + os.sep):
                raise OmnigentError(
                    f"Path {git_subpath!r} is outside the repository.",
                    code=ErrorCode.INVALID_INPUT,
                )
        else:
            agent_dir = clone_root

        if not agent_dir.is_dir():
            raise OmnigentError(
                f"Path {git_subpath!r} not found in the repository.",
                code=ErrorCode.INVALID_INPUT,
            )

        # Remove the .git directory before bundling so it is never included.
        shutil.rmtree(clone_root / ".git", ignore_errors=True)

        from omnigent.agent_bundle import bundle_directory

        bundle = bundle_directory(agent_dir)
        if len(bundle) > MAX_BUNDLE_BYTES:
            raise OmnigentError(
                "Repository clone exceeded the size limit.",
                code=ErrorCode.INVALID_INPUT,
            )
        return bundle, sha, resolved_ref


def _current_branch(repo: Path) -> str:
    out = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=CLONE_TIMEOUT_SECONDS,
    )
    return out.stdout.strip()


def _head_sha(repo: Path) -> str:
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        timeout=CLONE_TIMEOUT_SECONDS,
    )
    return out.stdout.strip()


def _inject_token(url: str, token: str) -> str:
    """Insert a token into an https URL for the fetch. Result must never be logged."""
    if url.startswith("https://"):
        return url.replace("https://", f"https://x-access-token:{token}@", 1)
    return url  # ssh/scp URLs use key auth, not tokens
