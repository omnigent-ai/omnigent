"""Local git reads that the pull request panel's providers share.

Each function takes ``run``, which runs one git command in the workspace and
returns ``(returncode, stdout)``, with ``returncode`` ``None`` when git did not
start or did not finish. Callers keep their own runners, so each keeps its
timeout, environment, and output decoding. File content can be read as text or
as bytes. The module imports only the standard library; :mod:`omnigent.errors`
loads when an error is raised.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

_Output = TypeVar("_Output", str, bytes)

# A runner whose output is text.
TextRun = Callable[[list[str]], tuple[int | None, str]]


def remote_urls(run: TextRun) -> list[tuple[str, str]]:
    """Return each remote's first configured URL as ``(name, url)``, ``origin`` first.

    The other remotes follow in config order. The URLs are the configured ones,
    before any ``url.<base>.insteadOf`` rewrite. Reads git config because
    ``git remote -v`` lines vary; a partial clone's fetch line ends with its filter.
    """
    rc, out = run(["config", "-z", "--get-regexp", r"^remote\..*\.url$"])
    if rc != 0:
        return []
    urls: dict[str, str] = {}
    for entry in out.split("\0"):
        key, _, url = entry.partition("\n")
        if key.startswith("remote.") and key.endswith(".url"):
            urls.setdefault(key[len("remote.") : -len(".url")], url)
    return [(name, urls[name]) for name in sorted(urls, key=lambda name: name != "origin")]


def resolve_diff_base(run: TextRun, base: str) -> str:
    """Resolve a base branch name to the ref to diff HEAD against.

    Prefers the merge-base of ``origin/<base>`` (or ``<base>``) and HEAD, giving
    the three-dot / "Files changed" semantics GitHub shows. A missing merge base
    in a shallow repository raises instead of comparing branch tips. Full-history
    repositories retain the base-tip fallback. An unavailable base ref raises.

    :param base: Base branch name, e.g. ``"main"``.
    :returns: A ref (SHA or name) to diff against.
    :raises OmnigentError: If the base is unavailable or shallow ancestry is missing.
    """
    from omnigent.errors import ErrorCode, OmnigentError

    resolved: str | None = None
    for candidate in (f"origin/{base}", base):
        rc, _ = run(["rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"])
        if rc == 0:
            resolved = candidate
            break
    if resolved is None:
        raise OmnigentError(
            f"Diff base {base!r} is not available locally. Fetch the base branch explicitly "
            "with `git fetch origin <base>:refs/remotes/origin/<base>` (replace <base> "
            "with the branch name), then retry. Single-branch clones do not fetch "
            "other branches automatically.",
            code=ErrorCode.INVALID_INPUT,
        )
    rc, out = run(["merge-base", resolved, "HEAD"])
    if rc == 0 and out.strip():
        return out.strip()
    if rc == 1:
        shallow_rc, shallow = run(["rev-parse", "--is-shallow-repository"])
        if shallow_rc == 0 and shallow.strip() == "true":
            raise OmnigentError(
                f"No merge base found between HEAD and {resolved!r} in this shallow repository. "
                "Fetch more history with `git fetch --deepen=100 origin` or "
                "`git fetch --unshallow origin`, then retry. "
                "Include both branch refspecs if origin tracks only one branch.",
                code=ErrorCode.INVALID_INPUT,
            )
    return resolved


def read_file(
    run: Callable[[list[str]], tuple[int | None, _Output]], ref: str, path: str
) -> _Output | None:
    """Read ``path`` at ``ref``; only a confirmed absent tree entry means no content.

    :returns: The content, as text or bytes like ``run``'s output, or ``None`` when
        ``ref`` has no entry at ``path``.
    :raises OmnigentError: When the content cannot be read, such as a blob that a
        partial clone cannot fetch.
    """
    rc, content = run(["show", f"{ref}:{path}"])
    if rc == 0:
        return content
    # Tree entries remain available in blobless clones even if a lazy blob fetch fails.
    tree_rc, entries = run(
        ["--literal-pathspecs", "ls-tree", "-z", "--full-tree", ref, "--", path]
    )
    if tree_rc == 0 and not entries:
        return None
    from omnigent.errors import ErrorCode, OmnigentError

    raise OmnigentError(
        f"Unable to read file content for {path!r} at {ref!r}. Check repository access "
        "and connectivity, then retry; a partial clone may need to fetch missing objects.",
        code=ErrorCode.INTERNAL_ERROR,
    )
