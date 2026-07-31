"""
Backend-agnostic cwd dotfile / escaping-symlink walker.

Both spawn-time sandbox backends (``linux_bwrap`` and
``darwin_seatbelt``) need to identify the same set of cwd entries
that must be hidden from the sandboxed helper:

1. **Hidden entries** — any file/directory whose basename starts with
   ``.`` and is NOT in the spec's ``cwd_allow_hidden`` allowlist.
2. **Escaping symlinks** — any symlink (at any depth) whose target
   resolves outside the set of paths the sandbox already exposes.

The two backends emit different tokens for the same masked path
(``--bind /dev/null`` / ``--tmpfs`` for bwrap, ``(deny file-*)``
expressions for Seatbelt), but the *decision* of which paths to mask
is identical. Centralising the walker here guarantees both backends
hide exactly the same set of entries; only the emit shape differs.

Recursion is controlled by the ``recursive`` flag, which defaults to
``False`` (top-level-only) — the same default as the spec key
``cwd_hidden_scan_recursive`` that every sandbox backend threads
through, so the walker and the product agree on one default. Set it
to ``True`` (spec: ``cwd_hidden_scan_recursive: true``) to walk the
whole tree.

Top-level-only is the scalable default for medium/large projects: it
still masks the credential-shaped dotfiles that live at the root of
``cwd`` / ``$HOME`` (``.git``, ``.env``, ``.aws``, ``.ssh``, ...)
without paying to walk the whole tree. Deeply nested dotfiles are left
visible in that mode; enable recursion for untrusted trees.

The walker also implements the bounded-traversal contract:

- ``cwd_hidden_scan_max_entries`` caps how many filesystem entries
  the recursive walk is allowed to visit. Realistic projects fit well
  under the default (50000) because the walker prunes at masked
  dot-directories (``.git/`` doesn't have its tens of thousands of
  objects counted toward the cap once ``.git`` itself is masked).
- ``cwd_hidden_scan_overflow`` chooses behaviour when the cap is hit:

  - ``"warn"`` (default): emit a logging warning, stop scanning, and
    return the partial mask built up so far. Dotfiles past the cap
    remain visible. This is the default because heavy-but-trusted
    trees (anything carrying ``node_modules``) routinely exceed the
    cap, and blocking every spawn is worse than a best-effort mask
    for the common case. Pair with the deprioritization below so the
    visible-past-the-cap entries are the least-sensitive ones.
  - ``"error"``: raise :class:`OSError` with an actionable message
    naming both spec keys the user can tune. Fail-Loud — the right
    pick for untrusted source trees.
  - ``"unlimited"``: ignore the cap and walk the full tree. O(N) on
    total entries; safe but can be slow on huge monorepos.

Deprioritized directories
-------------------------

Directory subtrees whose basename is in ``deprioritize_names``
(default: ``node_modules``, ``.venv``, ``.mypy_cache``,
``.codex-tmp``) are walked **last**. The dot-prefixed ones only ever
get walked when they're on ``allow_hidden`` — otherwise they're
masked and pruned, so deprioritizing them is a no-op until allowed.
They are large and
rarely carry the project's own secrets, yet every file in them counts
toward the cap. Deferring them means the cap budget is spent masking
the project's real dotfiles first; if the cap trips, the unscanned
remainder is the deprioritized tree (the least-bad thing to leave
unmasked) rather than a project ``.env``.

When the cap is hit, the overflow message (warn log line and ``error``
exception alike) names the directories the walk did not finish —
distinguishing the one directory it was mid-scan of ("partially
scanned") from those it never reached ("not scanned"), and flagging
the deprioritized ones — so an operator can tell at a glance that, for
example, ``node_modules`` was the part left unmasked. The list is
bounded (see :data:`_MAX_UNFINISHED_DIRS_REPORTED`) so a pathological
tree can't produce a multi-KB log line.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

_LOGGER = logging.getLogger(__name__)


MaskKind = Literal["file", "dir"]

# Directory basenames whose subtrees are walked LAST. See the module
# docstring ("Deprioritized directories") for the rationale. Kept as a
# module constant (rather than a spec field) so both backends get the
# same behaviour for free; callers can override via the
# ``deprioritize_names`` parameter of :func:`scan_cwd_mask_entries`.
#
# ``node_modules`` is a plain (non-dot) dir that is always walked.
# ``.venv`` / ``.mypy_cache`` / ``.codex-tmp`` are dot-dirs: the walker
# only descends into them (and so only spends cap budget on them) when
# their basename is on ``allow_hidden`` — otherwise they are masked and
# pruned before the deprioritization branch is even reached. Listing
# them here is therefore a no-op until they're allowed, and a
# walked-last optimization once they are (``.venv`` is allowed by
# default; the other two are big, regenerable caches an operator may
# opt into via ``cwd_allow_hidden``).
_DEFAULT_DEPRIORITIZED_DIRS: tuple[str, ...] = (
    "node_modules",
    ".venv",
    ".mypy_cache",
    ".codex-tmp",
)

# Cap on how many unfinished-directory paths the overflow message
# lists before collapsing the rest into a ``(+N more)`` suffix, so a
# pathological tree can't produce a multi-KB log line / SBPL comment.
_MAX_UNFINISHED_DIRS_REPORTED = 10


@dataclass(frozen=True)
class MaskedEntry:
    """
    A single cwd entry the sandbox must hide from the helper.

    :param path: Absolute, resolved-without-strict path to the entry.
        Backend emitters use this verbatim — ``bwrap`` as the mount
        destination, ``sandbox-exec`` as the ``literal`` / ``subpath``
        argument.
    :param kind: ``"file"`` for regular files, symlinks, sockets, and
        broken-symlink fall-throughs; ``"dir"`` for real directories.
        The bwrap emitter maps ``"file"`` to ``--bind /dev/null`` and
        ``"dir"`` to ``--tmpfs``; the Seatbelt emitter maps ``"file"``
        to ``(deny ... (literal ...))`` and ``"dir"`` to
        ``(deny ... (subpath ...))``.
    """

    path: Path
    kind: MaskKind


def scan_cwd_mask_entries(
    cwd: Path,
    *,
    allow_hidden: Sequence[str],
    safe_roots: Sequence[Path],
    max_entries: int,
    overflow: str,
    recursive: bool = False,
    logger_name: str | None = None,
    scope_label: str = "cwd",
    deprioritize_names: Sequence[str] = _DEFAULT_DEPRIORITIZED_DIRS,
) -> list[MaskedEntry]:
    """
    Walk *cwd* and identify entries that must be masked from the helper.

    Iterative DFS over *cwd* with early termination at masked
    dot-directories (the walker never descends into something it just
    marked for masking). For each entry it visits, the entry is masked
    when EITHER:

    - the basename starts with ``.`` and is not in *allow_hidden*, OR
    - the entry is a symlink whose resolved target lies outside every
      path in *safe_roots*.

    Walker termination is deterministic:

    - ``follow_symlinks=False`` on the recursion check ensures
      symlink loops can't cause infinite descent.
    - Masked dot-directories are pruned (we don't push their
      children onto the work stack), so masking ``.git`` doesn't
      cost an entry per ``.git/objects/...`` blob.

    Directories whose basename is in *deprioritize_names* (default:
    :data:`_DEFAULT_DEPRIORITIZED_DIRS` — ``node_modules``, ``.venv``,
    ``.mypy_cache``, ``.codex-tmp``) are deferred and walked only after
    every other directory has been processed, so the cap budget masks
    the real project tree first. They are still walked — and their
    dotfiles still masked — when budget remains; they are simply last
    in line. Nested deprioritized dirs are re-deferred on each drain, which
    still terminates.

    :param cwd: Absolute, resolved-without-strict path of the
        helper's working directory. The walk starts here. Must be a
        real directory; if it isn't, the function returns an empty
        list without raising (the backend wraps the missing dir at
        spawn time and gets the kernel error message).
    :param allow_hidden: Dotfile/dotdir basenames that pass through
        unmasked at any depth, e.g. ``[".venv"]``. Matched by
        basename, so ``".venv"`` exempts both ``cwd/.venv`` and
        ``cwd/services/api/.venv``. Pass an empty sequence to mask
        every dotfile.
    :param safe_roots: Paths the sandbox already exposes (typically
        ``cwd``, the backend's default mounts, the policy's read /
        write roots). A symlink whose resolved target lies inside
        any of these is considered safe and not masked. The backend
        is responsible for assembling this list — bwrap and Seatbelt
        expose different system paths.
    :param max_entries: Cap on the number of filesystem entries the
        walker may visit. The walker counts every child returned by
        :func:`os.scandir`, masked or not. Set to a large value
        (e.g. ``2**31``) together with ``overflow="unlimited"`` to
        disable the cap.
    :param overflow: One of ``"error"``, ``"warn"``, ``"unlimited"``.
        See module docstring for per-mode semantics.
    :param recursive: Whether to descend into subdirectories. Defaults
        to ``False`` (top-level-only), matching the spec key
        ``cwd_hidden_scan_recursive`` that every backend passes
        through. When ``False`` the walker scans only the immediate
        children of *cwd* — the top-level dotfile / escaping-symlink
        checks still run, but a dotfile nested below the top level
        (e.g. ``cwd/src/config/.env``) is NOT masked, and the entry
        cap and deprioritization machinery are effectively no-ops
        because no subtree is ever queued. Pass ``True`` to walk the
        whole tree.
    :param logger_name: Logger name used for the warn-mode warning
        message. ``None`` falls back to this module's logger; backends
        pass their own logger name so the warning surfaces under the
        backend's logging namespace (matches the bwrap docstring
        promise that the warning lives under
        ``omnigent.inner.bwrap_sandbox``).
    :param scope_label: Short label used in overflow log / error
        messages to identify what the walker was scanning. Defaults
        to ``"cwd"`` for backwards compatibility; backends that
        re-use the walker for ``read_paths`` roots pass e.g.
        ``"read_paths"`` so the operator-visible message says
        ``"read_paths dotfile scan"`` rather than the misleading
        ``"cwd dotfile scan"``.
    :param deprioritize_names: Directory basenames whose subtrees are
        walked last; defaults to :data:`_DEFAULT_DEPRIORITIZED_DIRS`
        (``node_modules``, ``.venv``, ``.mypy_cache``, ``.codex-tmp``).
        Matched by basename at any depth. Dot-prefixed entries only
        take effect when also on *allow_hidden* (otherwise the dir is
        masked and pruned before this is consulted). Pass an empty
        sequence to walk in plain DFS order with no deprioritization.
    :returns: A list of :class:`MaskedEntry`. Empty when *cwd* has
        nothing worth masking or when *cwd* is not a directory.
    :raises OSError: When the cap is reached and *overflow* is
        ``"error"``. The message names both tunable spec keys plus the
        directories the walk did not finish (partially- vs not-scanned,
        deprioritized flagged) so a user hitting the cap can find the
        escape hatches and the culprit without re-reading source.
    """
    entries: list[MaskedEntry] = []
    if not cwd.is_dir():
        return entries

    allow = set(allow_hidden)
    safe_root_list = list(safe_roots)
    cap_enabled = overflow != "unlimited"
    logger = logging.getLogger(logger_name) if logger_name else _LOGGER

    deprioritize = set(deprioritize_names)
    seen: set[str] = set()
    # Primary work stack plus a deferred tier for deprioritized dirs
    # (e.g. ``node_modules``). The deferred tier is promoted into the
    # primary stack only once the primary stack drains, so the cap
    # budget is spent on the rest of the tree first.
    stack: list[Path] = [cwd]
    deferred: list[Path] = []
    entries_visited = 0
    truncated = False
    # The directory the walk was mid-``scandir`` of when the cap
    # tripped — reported as "partially scanned". ``None`` until then.
    partial_dir: Path | None = None

    while (stack or deferred) and not truncated:
        if not stack:
            # Primary tier drained — promote the deprioritized subtrees
            # and keep going (they get whatever budget is left).
            stack = deferred
            deferred = []
        current = stack.pop()
        try:
            children = sorted(os.scandir(current), key=lambda e: e.name)
        except OSError:
            # Unreadable directory — skip without masking. The backend
            # will surface any deeper issue at spawn time. The parent
            # is in the safe set; its inaccessibility doesn't leak
            # content.
            continue

        for child in children:
            entries_visited += 1
            if cap_enabled and entries_visited > max_entries:
                truncated = True
                partial_dir = current
                break

            child_path = Path(child.path)
            should_mask = False
            if child.name.startswith(".") and child.name not in allow:
                should_mask = True
            elif child.is_symlink():
                resolved_target = child_path.resolve(strict=False)
                if not any(_is_within(resolved_target, root) for root in safe_root_list):
                    should_mask = True

            if should_mask:
                key = str(child_path)
                if key in seen:
                    continue
                seen.add(key)
                # ``is_dir`` follows symlinks by default — matches what
                # the agent would observe through the bind. For broken
                # symlinks it returns False; the backend's "file"
                # emitter handles both (``--bind /dev/null`` works on
                # a broken symlink; SBPL ``(literal ...)`` denies the
                # path regardless of what it points at).
                kind: MaskKind = "dir" if child.is_dir() else "file"
                entries.append(MaskedEntry(path=child_path, kind=kind))
                # Prune: don't descend into a masked dir.
                continue

            # Not masked — recurse only into real directories so a
            # rogue symlink-to-dir can't cause a loop. Deprioritized
            # dirs go to the deferred tier so they're walked last.
            # When ``recursive`` is False we scan only the top level of
            # each root, so we never descend past the immediate children.
            if recursive and child.is_dir(follow_symlinks=False):
                if child.name in deprioritize:
                    deferred.append(child_path)
                else:
                    stack.append(child_path)

    if truncated:
        _handle_scan_overflow(
            scope_label=scope_label,
            cwd=cwd,
            max_entries=max_entries,
            overflow=overflow,
            partial_dir=partial_dir,
            # Directories the walk never finished: the one it was
            # mid-scan of (partial) plus everything still queued in
            # either tier.
            not_scanned=[*stack, *deferred],
            deprioritize_names=deprioritize,
            entries_visited=entries_visited,
            masks_emitted=len(entries),
            logger=logger,
        )

    return entries


def _handle_scan_overflow(
    *,
    scope_label: str,
    cwd: Path,
    max_entries: int,
    overflow: str,
    partial_dir: Path | None,
    not_scanned: Sequence[Path],
    deprioritize_names: set[str],
    entries_visited: int,
    masks_emitted: int,
    logger: logging.Logger,
) -> None:
    """
    React to the entry cap being hit: raise (``"error"``) or log a
    ``CRITICAL`` warning (``"warn"`` / ``"unlimited"``).

    Builds the shared overflow message — which names the unfinished
    directories via :func:`_summarize_unfinished_dirs` — and then either
    fails loud or fails soft depending on *overflow*.

    :param scope_label: Human label for the scan scope, e.g. ``"cwd"``
        or ``"read_paths root /work"``; prefixes the message.
    :param cwd: The root the scan started from, e.g. ``Path("/work")``.
    :param max_entries: The cap that was exceeded, e.g. ``50000``.
    :param overflow: Resolved overflow mode, one of ``"error"``,
        ``"warn"``, ``"unlimited"``.
    :param partial_dir: The directory the walk was mid-scan of when the
        cap tripped, e.g. ``Path("/work/node_modules")``; ``None`` if it
        tripped exactly at a directory boundary.
    :param not_scanned: Directories still queued (in either tier) that
        were never entered, e.g. ``[Path("/work/src")]``.
    :param deprioritize_names: Deprioritized basenames, e.g.
        ``{"node_modules"}``; used only to flag entries in the summary.
    :param entries_visited: Total entries visited before stopping, e.g.
        ``50001``.
    :param masks_emitted: Number of mask entries produced so far, e.g.
        ``312``.
    :param logger: The resolved logger to emit the CRITICAL warning on
        (the caller's per-scope logger, falling back to the module
        logger), e.g. ``logging.getLogger("omnigent.inner._cwd_scan")``.
    :raises OSError: When *overflow* is ``"error"``.
    """
    unfinished = _summarize_unfinished_dirs(
        partial_dir=partial_dir,
        not_scanned=not_scanned,
        deprioritize_names=deprioritize_names,
    )
    message = (
        f"{scope_label} dotfile scan visited more than {max_entries} entries under "
        f"{cwd}. Unfinished directories (dotfiles inside these are NOT masked): "
        f"{unfinished}. Raise os_env.sandbox.cwd_hidden_scan_max_entries, or set "
        "os_env.sandbox.cwd_hidden_scan_overflow ('warn' = partial mask [default], "
        "'error' = fail loud, 'unlimited' = no cap)."
    )
    if overflow == "error":
        raise OSError(message)
    # ``"warn"`` and ``"unlimited"`` — fail-soft with an obvious log
    # line. We deliberately do NOT swallow the truncation silently;
    # entries past the cap remain visible to the agent.
    #
    # L6 (security): when ``overflow == "warn"`` (the default),
    # dotfiles past the cap are NOT masked, which means a deeply-
    # nested ``.aws`` / ``.ssh`` / ``.env`` checked in by the operator
    # (or planted by a previous compromised tool call) becomes
    # readable by the sandboxed agent. ``"warn"`` is an availability /
    # security trade-off; deprioritizing ``node_modules`` keeps the
    # most-likely-unmasked remainder low-sensitivity, and the warning
    # escalates to ``CRITICAL`` (naming the unfinished dirs) so it
    # can't be lost in INFO-noise logs. Untrusted trees should switch
    # to ``"error"``.
    logger.critical(
        "%s Mask is incomplete (overflow=%r): %d entries "
        "visited so far, %d masks emitted. Dotfiles past the "
        "cap are READABLE by the sandboxed helper — including "
        "any credentials checked in or planted under cwd. "
        "Switch overflow to 'error' to fail loud instead, or "
        "raise cwd_hidden_scan_max_entries to scan the full "
        "tree.",
        message,
        overflow,
        entries_visited,
        masks_emitted,
    )


def _summarize_unfinished_dirs(
    *,
    partial_dir: Path | None,
    not_scanned: Sequence[Path],
    deprioritize_names: set[str],
) -> str:
    """
    Build the human-readable "unfinished directories" clause for an
    overflow message.

    Each directory is annotated with its state ("partially scanned"
    for the one the walk was mid-scan of, "not scanned" for those it
    never reached) and flagged when it is deprioritized (so the reader
    immediately sees that, e.g., ``node_modules`` was the unmasked
    part). Deprioritized entries are listed first because they are the
    most likely culprit and the most useful to surface; the partially-
    scanned dir leads within each group. The list is truncated to
    :data:`_MAX_UNFINISHED_DIRS_REPORTED` with a ``(+N more)`` suffix.

    :param partial_dir: The directory the walk was mid-``scandir`` of
        when the cap tripped, e.g. ``Path("/work")``. ``None`` when the
        cap tripped exactly at a directory boundary (no dir is partial).
    :param not_scanned: Directories still queued (in either walk tier)
        that were never entered, e.g.
        ``[Path("/work/node_modules"), Path("/work/src")]``.
    :param deprioritize_names: Directory basenames that were
        deprioritized, e.g. ``{"node_modules"}``. Used only to flag
        entries in the summary.
    :returns: A ``"; "``-joined summary string, e.g.
        ``"/work/node_modules (not scanned, deprioritized); /work/src
        (not scanned)"``. Empty string when there is nothing to report
        (no partial dir and an empty *not_scanned*).
    """
    # Sort key: deprioritized (0) before others (1); within a group,
    # the partially-scanned dir (0) before not-scanned (1).
    described: list[tuple[tuple[int, int], str]] = []
    if partial_dir is not None:
        is_dep = partial_dir.name in deprioritize_names
        state = "partially scanned, deprioritized" if is_dep else "partially scanned"
        described.append(((0 if is_dep else 1, 0), f"{partial_dir} ({state})"))
    for path in not_scanned:
        is_dep = path.name in deprioritize_names
        state = "not scanned, deprioritized" if is_dep else "not scanned"
        described.append(((0 if is_dep else 1, 1), f"{path} ({state})"))

    described.sort(key=lambda pair: pair[0])
    lines = [text for _, text in described]
    shown = lines[:_MAX_UNFINISHED_DIRS_REPORTED]
    remainder = len(lines) - len(shown)
    summary = "; ".join(shown)
    if remainder > 0:
        summary += f" (+{remainder} more)"
    return summary


def _is_within(path: Path, root: Path) -> bool:
    """
    Return whether *path* equals or descends from *root*.

    Both paths are passed through :func:`Path.resolve` with
    ``strict=False`` first so symlinks pointing into a safe root
    (e.g. ``./.venv/bin/python -> /usr/bin/python3.12``) count as
    "within" the safe root for safety checks.

    :param path: Candidate path, e.g. ``/usr/bin/python3``.
    :param root: Prefix path, e.g. ``/usr``.
    :returns: ``True`` when *path* equals or lives under *root*
        after symlink-free resolution.
    """
    try:
        compare_path = path.resolve(strict=False)
        compare_root = root.resolve(strict=False)
        compare_path.relative_to(compare_root)
        return True
    except (ValueError, OSError):
        return False
