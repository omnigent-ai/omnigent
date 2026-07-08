"""Fetch a public GitHub repo and repackage it into an agent bundle.

The server fetches a repo's codeload tarball, strips GitHub's
``<repo>-<sha>/`` top directory (and an optional subdir), filters editor /
VCS junk, and re-tars into the canonical bundle shape (``config.yaml`` at the
tar root) that :func:`omnigent.server.bundles.validate_agent_bundle` expects.

Structure safety is layered: the fetched archive is extracted through
:func:`omnigent.spec.tar_utils.extract_safe` (path-traversal, symlink,
decompression-bomb, entry-cap, type-allowlist guards), and the resulting
bundle is validated again by the caller via ``validate_agent_bundle``. On top
of those, we cap the *download* size before extraction, since ``extract_safe``
only sees the buffer once it is fully in memory.

Public repos only: no token is sent, so ``${VAR}`` references stay literal
(the server never expands tenant uploads) — the repo must hold no resolved
secrets.
"""

from __future__ import annotations

import io
import re
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import httpx

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.spec.tar_utils import (
    DEFAULT_MAX_BYTES,
    ExtractionError,
    extract_safe,
)

# Path segments / basenames considered editor or VCS junk, dropped during
# repackage so a checked-in ``.git`` tree or ``node_modules`` can't trip the
# entry-count / size caps or add noise. Mirrors the client junk filter in
# ``web/src/lib/agentBundle.ts``.
_IGNORED_SEGMENTS = frozenset({".git", "node_modules", ".venv", "__pycache__", ".idea", ".vscode"})
_IGNORED_BASENAMES = frozenset({".DS_Store"})

# Cap the codeload download before extraction. Matches ``extract_safe``'s
# uncompressed ceiling; a gzip'd repo will always be smaller, so this is a
# conservative pre-extraction guard rather than an exact bound.
_MAX_DOWNLOAD_BYTES = DEFAULT_MAX_BYTES

_FETCH_TIMEOUT_SECONDS = 30.0

# owner/repo path segments allow letters, digits, hyphen, underscore, dot.
_SEGMENT = r"[A-Za-z0-9_.-]+"


@dataclass(frozen=True)
class GithubSource:
    """A resolved GitHub bundle source."""

    owner: str
    repo: str
    #: Tag, branch, or commit SHA. ``None`` means the repo default branch.
    ref: str | None
    #: Optional path within the repo where the bundle root lives.
    subdir: str | None

    @property
    def codeload_url(self) -> str:
        """The codeload tarball URL for this source's ref.

        When ``ref`` is ``None`` we target ``HEAD`` so codeload resolves the
        repo's default branch without an extra API round-trip.
        """
        ref = self.ref or "HEAD"
        return f"https://codeload.github.com/{self.owner}/{self.repo}/tar.gz/{ref}"


def parse_github_source(
    source_url: str,
    *,
    ref: str | None = None,
    subdir: str | None = None,
) -> GithubSource:
    """Resolve a user-supplied repo reference into a :class:`GithubSource`.

    Accepts three shapes:

    - ``https://github.com/<owner>/<repo>`` (optionally ``.git`` / trailing
      slash), with ``ref`` / ``subdir`` supplied as arguments.
    - ``https://github.com/<owner>/<repo>/tree/<ref>/<subdir...>`` — ``ref``
      and ``subdir`` parsed from the URL.
    - ``<owner>/<repo>@<ref>`` shorthand.

    Explicit ``ref`` / ``subdir`` arguments override anything parsed from a
    ``/tree/`` URL.

    :raises OmnigentError: ``invalid_input`` when the reference can't be parsed
        into an ``owner/repo``.
    """
    url = source_url.strip()
    if not url:
        raise OmnigentError("GitHub source URL is required", code=ErrorCode.INVALID_INPUT)

    parsed_ref: str | None = None
    parsed_subdir: str | None = None

    # github.com URLs first (scheme optional); a /tree/<ref>/<subdir> URL is
    # more specific than the plain repo URL, so it is matched first. The bare
    # ``<owner>/<repo>`` shorthand is last so a scheme-less ``github.com/a/b``
    # can't be mis-parsed as owner=github.com.
    host = r"(?:https?://)?github\.com/"
    tree = re.fullmatch(
        rf"{host}({_SEGMENT})/({_SEGMENT})/tree/([^/]+)(?:/(.+?))?/?",
        url,
    )
    plain = re.fullmatch(rf"{host}({_SEGMENT})/({_SEGMENT}?)(?:\.git)?/?", url)
    shorthand = re.fullmatch(rf"({_SEGMENT})/({_SEGMENT})(?:@(.+))?", url)
    if tree:
        owner, repo, parsed_ref, parsed_subdir = tree.groups()
    elif plain:
        owner, repo = plain.groups()
    elif shorthand:
        owner, repo, parsed_ref = shorthand.groups()
    else:
        raise OmnigentError(
            "unrecognized GitHub repo URL; expected github.com/<owner>/<repo>, "
            "a /tree/<ref>/<subdir> URL, or <owner>/<repo>@<ref>",
            code=ErrorCode.INVALID_INPUT,
        )

    repo = _strip_git_suffix(repo)
    if not owner or not repo:
        raise OmnigentError(
            "GitHub URL must include both an owner and a repo",
            code=ErrorCode.INVALID_INPUT,
        )

    final_ref = ref if ref is not None else parsed_ref
    final_subdir = subdir if subdir is not None else parsed_subdir
    return GithubSource(
        owner=owner,
        repo=repo,
        ref=_clean(final_ref),
        subdir=_normalize_subdir(final_subdir),
    )


def fetch_github_bundle(source: GithubSource) -> bytes:
    """Fetch and repackage a public GitHub repo into canonical bundle bytes.

    :returns: A gzipped tarball with ``config.yaml`` (or a single top-level
        YAML) at the root, ready for ``validate_agent_bundle``.
    :raises OmnigentError: ``invalid_input`` for a missing/private repo (404),
        an oversize download, a non-tarball response, or a bundle whose subdir
        is empty; ``internal_error`` for network/transport failures.
    """
    raw = _download(source.codeload_url)
    return _repackage(raw, subdir=source.subdir)


# ── Internal helpers ─────────────────────────────────────────────────


def _download(url: str) -> bytes:
    """Stream a codeload tarball, enforcing the download-size cap.

    :raises OmnigentError: ``invalid_input`` on a 4xx (e.g. 404 for a
        missing/private repo) or when the response exceeds the size cap;
        ``internal_error`` on transport failure.
    """
    buffer = io.BytesIO()
    total = 0
    try:
        with (
            httpx.Client(follow_redirects=True, timeout=_FETCH_TIMEOUT_SECONDS) as client,
            client.stream("GET", url) as response,
        ):
            if response.status_code == 404:
                raise OmnigentError(
                    "GitHub repo or ref not found (is the repo public?)",
                    code=ErrorCode.INVALID_INPUT,
                )
            if response.status_code >= 400:
                raise OmnigentError(
                    f"failed to fetch GitHub repo: HTTP {response.status_code}",
                    code=ErrorCode.INVALID_INPUT,
                )
            for chunk in response.iter_bytes():
                total += len(chunk)
                if total > _MAX_DOWNLOAD_BYTES:
                    raise OmnigentError(
                        f"GitHub repo exceeds the maximum bundle size "
                        f"({_MAX_DOWNLOAD_BYTES} bytes)",
                        code=ErrorCode.INVALID_INPUT,
                    )
                buffer.write(chunk)
    except httpx.HTTPError as exc:
        raise OmnigentError(
            f"failed to fetch GitHub repo: {exc}",
            code=ErrorCode.INTERNAL_ERROR,
        ) from exc
    return buffer.getvalue()


def _repackage(raw_tar_gz: bytes, *, subdir: str | None) -> bytes:
    """Extract a codeload tarball and re-tar into canonical bundle bytes.

    Strips GitHub's ``<repo>-<sha>/`` top directory and the optional *subdir*,
    drops junk, and writes a fresh gzipped tar with the bundle root at the top.

    :raises OmnigentError: ``invalid_input`` when the archive is invalid, the
        subdir is empty, or nothing survives filtering.
    """
    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "repo"
        try:
            extract_safe(raw_tar_gz, dest)
        except ExtractionError as exc:
            raise OmnigentError(
                f"invalid GitHub repo archive: {exc}",
                code=ErrorCode.INVALID_INPUT,
            ) from exc

        # codeload wraps everything in a single ``<repo>-<sha>/`` directory.
        top_dirs = [p for p in dest.iterdir() if p.is_dir()]
        root = top_dirs[0] if len(top_dirs) == 1 else dest
        if subdir:
            root = root / subdir
            if not root.is_dir():
                raise OmnigentError(
                    f"subdir not found in GitHub repo: {subdir!r}",
                    code=ErrorCode.INVALID_INPUT,
                )

        buffer = io.BytesIO()
        entry_count = 0
        with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
            for path in sorted(root.rglob("*")):
                if not path.is_file():
                    continue
                arcname = path.relative_to(root).as_posix()
                if _is_ignored(arcname):
                    continue
                tar.add(path, arcname=arcname)
                entry_count += 1

        if entry_count == 0:
            raise OmnigentError(
                "GitHub repo (or subdir) contains no bundle files",
                code=ErrorCode.INVALID_INPUT,
            )
        return buffer.getvalue()


def _is_ignored(arcname: str) -> bool:
    """Whether a bundle-relative path is editor/VCS junk to skip."""
    parts = PurePosixPath(arcname).parts
    if not parts:
        return True
    if parts[-1] in _IGNORED_BASENAMES:
        return True
    return any(seg in _IGNORED_SEGMENTS for seg in parts)


def _strip_git_suffix(repo: str) -> str:
    return repo[:-4] if repo.endswith(".git") else repo


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    trimmed = value.strip().strip("/")
    return trimmed or None


def _normalize_subdir(value: str | None) -> str | None:
    """Trim and validate an optional subdir; reject traversal/absolute paths."""
    subdir = _clean(value)
    if subdir is None:
        return None
    parts = PurePosixPath(subdir).parts
    if PurePosixPath(subdir).is_absolute() or ".." in parts:
        raise OmnigentError(
            f"invalid subdir (no absolute paths or '..'): {subdir!r}",
            code=ErrorCode.INVALID_INPUT,
        )
    return subdir
