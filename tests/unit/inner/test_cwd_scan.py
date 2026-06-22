"""
Shared cwd-walker decision tests for the sandbox backends.

The walker in :mod:`omnigent.inner._cwd_scan` is consumed by every
spawn-time sandbox backend (``linux_bwrap``, ``darwin_seatbelt``)
to decide which cwd entries must be masked from the helper. Backend
emit code (``--bind /dev/null`` / ``--tmpfs`` for bwrap, ``(deny
file-* (literal/subpath ...))`` for Seatbelt) lives in each backend
module and is asserted there. This module verifies the
**decision** layer once so a regression that would expose ``.env`` to
the agent fails the same test for both backends on whichever host
runs the suite.

Tests assert on :class:`MaskedEntry` tuples directly, not on backend-
specific tokens. They run on every platform — the walker is pure
Python and doesn't shell out to ``bwrap`` / ``sandbox-exec``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from omnigent.inner._cwd_scan import MaskedEntry, scan_cwd_mask_entries

# The walker contract says ``safe_roots`` should include cwd plus the
# backend-specific exposed mounts. For these decision-level tests we
# pass a minimal set: cwd and ``/usr`` (the only "system" root the
# escaping-symlink defense looks at on both Linux and macOS). Both
# backends expose ``/usr`` to the helper.
_SYSTEM_SAFE_ROOTS = (Path("/usr"),)
_DEFAULT_MAX = 50000


def _scan(
    cwd: Path,
    *,
    allow_hidden: list[str] | None = None,
    safe_roots: list[Path] | None = None,
    max_entries: int = _DEFAULT_MAX,
    overflow: str = "error",
    deprioritize_names: list[str] | None = None,
) -> list[MaskedEntry]:
    """
    Thin wrapper that mirrors what each backend passes through.

    Passing ``cwd`` resolved (not strict) matches what
    ``bwrap_sandbox`` and ``seatbelt_sandbox`` do at spawn time. The
    tests in this module are written against absolute resolved paths
    on the returned :class:`MaskedEntry` instances.

    :param cwd: The throwaway tempdir each test mutates.
    :param allow_hidden: Dotfile/dotdir basenames to exempt.
        Defaults to ``[]`` (mask every dotfile) so each test states
        its allowlist intent explicitly.
    :param safe_roots: Override the safe-root set. Defaults to
        ``[cwd, /usr]`` — cwd because the walker always trusts
        traversal into its own tree, ``/usr`` to mimic the bwrap +
        seatbelt default mounts.
    :param max_entries: Visit cap. Default is the production
        baseline (50000).
    :param overflow: Behavior at the cap. Default ``"error"`` so the
        cap/overflow tests can assert on the raised :class:`OSError`;
        note this differs from the production default (``"warn"``),
        which is pinned in the spec-parser tests instead.
    :param deprioritize_names: Directory basenames walked last.
        ``None`` (the default) lets the walker apply its own default
        (``("node_modules",)``); pass ``[]`` to disable
        deprioritization for the no-defer contrast cases.
    :returns: List of :class:`MaskedEntry`.
    """
    roots = [cwd.resolve(strict=False), *_SYSTEM_SAFE_ROOTS]
    if safe_roots is not None:
        roots = safe_roots
    # Only override the walker's default when the test asked to, so the
    # common case still exercises the production default (node_modules).
    if deprioritize_names is None:
        return scan_cwd_mask_entries(
            cwd.resolve(strict=False),
            allow_hidden=allow_hidden or [],
            safe_roots=roots,
            max_entries=max_entries,
            overflow=overflow,
        )
    return scan_cwd_mask_entries(
        cwd.resolve(strict=False),
        allow_hidden=allow_hidden or [],
        safe_roots=roots,
        max_entries=max_entries,
        overflow=overflow,
        deprioritize_names=deprioritize_names,
    )


def _entry_for(entries: list[MaskedEntry], path: Path) -> MaskedEntry | None:
    """
    Look up a :class:`MaskedEntry` by absolute path.

    Comparing on ``Path`` directly works because the walker stores
    absolute paths sourced from :func:`os.scandir`. Callers pass the
    same absolute path they'd expect the backend to mount over.

    :param entries: Output of :func:`scan_cwd_mask_entries`.
    :param path: Absolute path to search for.
    :returns: The matching :class:`MaskedEntry`, or ``None`` if the
        walker chose not to mask it. Tests use ``None`` to assert
        "this path was allowed through".
    """
    needle = Path(path)
    for entry in entries:
        if entry.path == needle:
            return entry
    return None


# ---------------------------------------------------------------------------
# Top-level dotfile masking + symlink defense
# ---------------------------------------------------------------------------


def test_top_level_dotfile_is_marked_as_file(tmp_path: Path) -> None:
    """
    A top-level dotfile in cwd that isn't on the allowlist returns a
    :class:`MaskedEntry` with ``kind="file"``.

    This is the central security goal: project secrets in dotfiles
    must be marked for masking by the walker regardless of which
    backend consumes the result.
    """
    secret = tmp_path / ".env"
    secret.write_text("SECRET=42")
    entries = _scan(tmp_path, allow_hidden=[".venv"])
    entry = _entry_for(entries, secret)
    assert entry is not None, (
        f".env was not masked. Walker returned: {[(e.path, e.kind) for e in entries]}"
    )
    assert entry.kind == "file"


def test_top_level_dotdir_is_marked_as_dir(tmp_path: Path) -> None:
    """
    A top-level dot-directory (e.g. ``.aws``) returns a
    :class:`MaskedEntry` with ``kind="dir"`` so backends can pick
    the right "hide a directory" primitive.
    """
    aws = tmp_path / ".aws"
    aws.mkdir()
    (aws / "credentials").write_text("[default]\naws_access_key_id=x")
    entries = _scan(tmp_path, allow_hidden=[".venv"])
    entry = _entry_for(entries, aws)
    assert entry is not None
    assert entry.kind == "dir"


def test_allowlisted_dotdir_is_not_masked(tmp_path: Path) -> None:
    """
    A dot-directory on ``allow_hidden`` passes through unmasked at
    the top level. ``.venv`` is the documented default exemption so
    Python projects don't have their virtualenv hidden from the
    helper.
    """
    venv = tmp_path / ".venv"
    venv.mkdir()
    entries = _scan(tmp_path, allow_hidden=[".venv"])
    assert _entry_for(entries, venv) is None


def test_regular_file_is_not_masked(tmp_path: Path) -> None:
    """
    Non-dotfile content is never returned by the walker — the
    sandbox lets the helper read it through the cwd bind / SBPL
    allow rule. Regression here would over-mask everything in cwd.
    """
    plain = tmp_path / "regular.txt"
    plain.write_text("not secret")
    entries = _scan(tmp_path, allow_hidden=[".venv"])
    assert _entry_for(entries, plain) is None


def test_symlink_pointing_outside_safe_roots_is_marked_as_file(tmp_path: Path) -> None:
    """
    A non-dotfile symlink whose target resolves outside every
    ``safe_roots`` entry returns a :class:`MaskedEntry` with
    ``kind="file"`` (the link itself, not the target).

    Backends translate this into a ``--bind /dev/null <link>`` or
    ``(deny file-* (literal <link>))`` — both reject reads through
    the link path. The escape they defend against is
    ``./link -> /etc/shadow`` showing up in cwd.
    """
    target = Path("/etc/shadow")  # exists on Linux + macOS, outside _SYSTEM_SAFE_ROOTS
    if not target.exists():
        pytest.skip("/etc/shadow not present on this host")
    link = tmp_path / "outward_link"
    link.symlink_to(target)
    entries = _scan(tmp_path, allow_hidden=[".venv"])
    entry = _entry_for(entries, link)
    assert entry is not None, (
        f"Escaping symlink was not masked. Walker returned: {[(e.path, e.kind) for e in entries]}"
    )
    assert entry.kind == "file"


def test_symlink_pointing_into_safe_root_is_not_marked(tmp_path: Path) -> None:
    """
    A symlink whose target resolves inside ``safe_roots`` is NOT
    marked — the agent has legit reasons to symlink to system tools
    and over-masking would break realistic project layouts (e.g.
    ``./bin/python -> /usr/bin/python3``).
    """
    inside = Path("/usr/bin")
    if not inside.exists():
        pytest.skip("/usr/bin not present (unexpected on Linux/macOS)")
    link = tmp_path / "tool_link"
    link.symlink_to(inside)
    entries = _scan(tmp_path, allow_hidden=[".venv"])
    assert _entry_for(entries, link) is None


# ---------------------------------------------------------------------------
# Recursive dotfile masking
# ---------------------------------------------------------------------------


def test_nested_dotfile_is_marked(tmp_path: Path) -> None:
    """
    A dotfile under a regular subdirectory is masked. The previous
    pre-refactor walker only inspected cwd's immediate children,
    leaving ``cwd/services/api/.env`` exposed in monorepo layouts.
    The recursive walker now hides at any depth.
    """
    nested_dir = tmp_path / "services" / "api"
    nested_dir.mkdir(parents=True)
    secret = nested_dir / ".env"
    secret.write_text("DB_PASSWORD=secret")
    (nested_dir / "main.py").write_text("# normal file")
    entries = _scan(tmp_path, allow_hidden=[".venv"])
    entry = _entry_for(entries, secret)
    assert entry is not None
    assert entry.kind == "file"
    # Sibling non-dotfile stays visible.
    assert _entry_for(entries, nested_dir / "main.py") is None


def test_walker_prunes_at_masked_dotdir(tmp_path: Path) -> None:
    """
    Once a dot-directory is marked for masking, the walker must NOT
    descend into it. Two reasons: it would waste cap budget on
    entries the agent can't see anyway, and it would emit redundant
    masks that bloat the backend's argv / SBPL profile.
    """
    git_dir = tmp_path / ".git"
    (git_dir / "objects" / "ab").mkdir(parents=True)
    (git_dir / "objects" / "ab" / "cdef").write_text("blob")
    (git_dir / "config").write_text("[core]")
    entries = _scan(tmp_path)
    # The .git dir itself is masked.
    assert _entry_for(entries, git_dir) is not None
    # Nothing under .git appears as a separate entry.
    nested = [e for e in entries if str(e.path).startswith(str(git_dir) + os.sep)]
    assert nested == [], (
        "Walker descended into a masked .git directory. Expected "
        f"pruning at the dotdir boundary; got nested entries: "
        f"{[e.path for e in nested]}"
    )


def test_allowlist_matches_basename_at_any_depth(tmp_path: Path) -> None:
    """
    ``allow_hidden=[".venv"]`` exempts the basename at every depth,
    not just at cwd root. ``cwd/services/api/.venv`` passes through
    unmasked too.
    """
    (tmp_path / ".venv").mkdir()
    nested_venv = tmp_path / "services" / "api" / ".venv"
    nested_venv.mkdir(parents=True)
    nested_secret = tmp_path / "services" / "api" / ".env"
    nested_secret.write_text("SECRET")
    entries = _scan(tmp_path, allow_hidden=[".venv"])
    assert _entry_for(entries, tmp_path / ".venv") is None
    assert _entry_for(entries, nested_venv) is None
    nested_secret_entry = _entry_for(entries, nested_secret)
    assert nested_secret_entry is not None, "Nested .env (not on allowlist) must still be masked."
    assert nested_secret_entry.kind == "file"


def test_nested_escaping_symlink_is_marked(tmp_path: Path) -> None:
    """
    The symlink-escape defense applies at any depth, not only at the
    cwd root. ``cwd/sub/leak -> /etc/shadow`` is masked the same way
    a top-level ``cwd/leak -> /etc/shadow`` would be.
    """
    target = Path("/etc/shadow")
    if not target.exists():
        pytest.skip("/etc/shadow not present on this host")
    sub = tmp_path / "sub"
    sub.mkdir()
    link = sub / "leak"
    link.symlink_to(target)
    entries = _scan(tmp_path, allow_hidden=[".venv"])
    entry = _entry_for(entries, link)
    assert entry is not None
    assert entry.kind == "file"


def test_walker_does_not_follow_symlink_loops(tmp_path: Path) -> None:
    """
    A self-referential symlink (``cwd/loop -> cwd``) must not cause
    the walker to recurse forever. ``follow_symlinks=False`` on the
    recursion check is what guarantees this; if it regresses this
    test will hang rather than fail.

    The loop symlink resolves to cwd, which is inside ``safe_roots``,
    so the symlink itself is NOT masked — the walker just must not
    follow it for recursion.
    """
    (tmp_path / "loop").symlink_to(tmp_path)
    (tmp_path / "real_file").write_text("content")
    entries = _scan(tmp_path)
    assert _entry_for(entries, tmp_path / "real_file") is None


# ---------------------------------------------------------------------------
# Cap / overflow behavior
# ---------------------------------------------------------------------------


def test_overflow_error_raises_with_actionable_message(tmp_path: Path) -> None:
    """
    With ``overflow="error"`` (the production default), exceeding the
    cap raises :class:`OSError` whose message names both spec keys
    the user can tune — Fail-Loud per project conventions.
    """
    for i in range(50):
        (tmp_path / f"file_{i}.txt").write_text("x")
    with pytest.raises(OSError) as exc_info:
        _scan(tmp_path, max_entries=10, overflow="error")
    msg = str(exc_info.value)
    assert "cwd_hidden_scan_max_entries" in msg, (
        f"OSError must name the cap field so users can find the tuning knob. Got: {msg!r}"
    )
    assert "cwd_hidden_scan_overflow" in msg, (
        f"OSError must name the overflow field so users know about the "
        f"warn / unlimited escape hatches. Got: {msg!r}"
    )


def test_overflow_warn_returns_partial_mask_and_logs(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """
    With ``overflow="warn"``, hitting the cap stops scanning, emits a
    logging warning, and returns the partial mask built so far. The
    warning must be visible because dotfiles past the cap remain
    exposed.
    """
    (tmp_path / ".env").write_text("SECRET")
    for i in range(50):
        (tmp_path / f"file_{i}.txt").write_text("x")
    caplog.set_level("WARNING", logger="omnigent.inner._cwd_scan")
    entries = _scan(tmp_path, max_entries=5, overflow="warn")
    env_entry = _entry_for(entries, tmp_path / ".env")
    assert env_entry is not None, (
        "Partial mask should still include the .env we created before the cap was hit."
    )
    assert any("Mask is incomplete" in record.message for record in caplog.records), (
        "Warn-mode overflow must emit a logging warning so the partial mask isn't silent. "
        f"Captured: {[r.message for r in caplog.records]}"
    )


def test_overflow_unlimited_walks_full_tree(tmp_path: Path) -> None:
    """
    With ``overflow="unlimited"``, the cap is ignored and every
    nested dotfile is masked regardless of how many regular entries
    are in cwd. Trade-off: O(N) on the cwd tree, but the user
    explicitly opted in.
    """
    deep = tmp_path / "a" / "b" / "c" / "d"
    deep.mkdir(parents=True)
    deep_env = deep / ".env"
    deep_env.write_text("DEEP_SECRET")
    for i in range(100):
        (tmp_path / f"sibling_{i}.txt").write_text("x")
    entries = _scan(tmp_path, max_entries=5, overflow="unlimited")
    entry = _entry_for(entries, deep_env)
    assert entry is not None, (
        "Unlimited overflow mode must still mask the deeply-nested .env. "
        "Either the walker bailed early or the recursion is broken."
    )


# ---------------------------------------------------------------------------
# Deprioritized directories (node_modules walked last)
# ---------------------------------------------------------------------------


def _build_deprioritize_tree(cwd: Path) -> None:
    """
    Create a fixed three-way tree used by the deprioritization tests.

    Layout (each directory has exactly three children so the entry
    counts are deterministic):

    - ``cwd/.env``                 (project dotfile)
    - ``cwd/app/.secret``          (project dotfile, + two plain files)
    - ``cwd/node_modules/.npmrc``  (dep-tree dotfile, + two plain files)

    ``app`` sorts before ``node_modules`` alphabetically, so a plain
    DFS (no deprioritization) would pop ``node_modules`` off the LIFO
    stack *before* ``app`` and spend the cap budget there — leaving
    ``app/.secret`` unmasked. With deprioritization, ``app`` is walked
    first and ``node_modules`` last. The two regimes therefore drop
    opposite dotfiles, which is what the contrast test asserts.

    :param cwd: Throwaway tempdir to populate.
    """
    (cwd / ".env").write_text("PROJECT_SECRET=1")
    app = cwd / "app"
    app.mkdir()
    (app / ".secret").write_text("APP_SECRET=1")
    (app / "a.txt").write_text("x")
    (app / "b.txt").write_text("x")
    node_modules = cwd / "node_modules"
    node_modules.mkdir()
    (node_modules / ".npmrc").write_text("token=zzz")
    (node_modules / "m1.txt").write_text("x")
    (node_modules / "m2.txt").write_text("x")


def test_deprioritized_dir_walked_last_so_project_dotfiles_win(tmp_path: Path) -> None:
    """
    With ``node_modules`` deprioritized, a cap that can't cover the
    whole tree masks the project's own dotfiles (``cwd/.env`` and
    ``cwd/app/.secret``) and leaves only the ``node_modules`` dotfile
    unmasked. The no-deprioritization contrast drops the opposite
    dotfile, proving the deferral — not luck of DFS ordering — is what
    protects the project secret.

    A failure of the first block means deprioritization stopped
    deferring ``node_modules`` (project secret would leak instead);
    a failure of the contrast block means the tree no longer
    distinguishes the two regimes and the test has lost its teeth.
    """
    _build_deprioritize_tree(tmp_path)
    # max_entries=6: budget covers cwd's 3 children + app's 3 children;
    # node_modules contents are reached only after, so the 7th entry
    # (node_modules/.npmrc) trips the cap. warn => partial mask returned.
    defer = _scan(tmp_path, max_entries=6, overflow="warn")
    assert _entry_for(defer, tmp_path / ".env") is not None, (
        "Top-level project .env must be masked — it is visited before "
        "the cap trips regardless of deprioritization."
    )
    assert _entry_for(defer, tmp_path / "app" / ".secret") is not None, (
        "app/.secret must be masked: deprioritizing node_modules means "
        "app is walked before the budget is exhausted. If this is None, "
        "node_modules was NOT deferred and stole the budget."
    )
    assert _entry_for(defer, tmp_path / "node_modules" / ".npmrc") is None, (
        "node_modules/.npmrc must be the dropped (unmasked) entry — it is "
        "walked last, after the cap is already exhausted."
    )

    # Same tree + same cap, but deprioritization disabled: plain DFS
    # pops node_modules before app, so node_modules/.npmrc is masked
    # and app/.secret is the one dropped — the mirror image.
    no_defer = _scan(tmp_path, max_entries=6, overflow="warn", deprioritize_names=[])
    assert _entry_for(no_defer, tmp_path / "node_modules" / ".npmrc") is not None, (
        "Without deprioritization, node_modules is walked first and its "
        ".npmrc is masked — confirming the budget went there."
    )
    assert _entry_for(no_defer, tmp_path / "app" / ".secret") is None, (
        "Without deprioritization, app is walked last and app/.secret is "
        "dropped — the exact leak deprioritization exists to prevent."
    )


def test_deprioritized_dir_dotfiles_masked_when_under_cap(tmp_path: Path) -> None:
    """
    Deprioritize means "walk last", NOT "skip". When the cap is large
    enough to cover the whole tree, the ``node_modules`` dotfile is
    masked just like every other dotfile.

    Regression guard: if a future change implemented deprioritization
    as "exclude from the walk" instead of "defer", this would fail
    because node_modules/.npmrc would never be masked.
    """
    _build_deprioritize_tree(tmp_path)
    entries = _scan(tmp_path)  # default cap (50000) comfortably covers the tree
    npmrc = _entry_for(entries, tmp_path / "node_modules" / ".npmrc")
    assert npmrc is not None and npmrc.kind == "file", (
        "node_modules/.npmrc must still be masked when budget remains; "
        "deprioritization defers the subtree, it does not skip it."
    )


def test_nested_deprioritized_dir_terminates_and_masks_deep_dotfile(tmp_path: Path) -> None:
    """
    A ``node_modules`` nested inside another ``node_modules`` must be
    re-deferred on each drain and still walked — the walk terminates
    (no infinite re-deferral) and the deeply nested dotfile is masked
    when the cap allows.

    If the re-deferral logic looped forever, this test would hang
    rather than fail; the mask assertion confirms the inner subtree
    was actually reached.
    """
    deep = tmp_path / "node_modules" / "pkg" / "node_modules"
    deep.mkdir(parents=True)
    deep_secret = deep / ".npmrc"
    deep_secret.write_text("nested=secret")
    entries = _scan(tmp_path, overflow="unlimited")
    entry = _entry_for(entries, deep_secret)
    assert entry is not None and entry.kind == "file", (
        "The dotfile inside a nested node_modules must be masked under "
        "unlimited overflow — proving nested deferral terminates and "
        "still visits the inner subtree."
    )


# The dot-prefixed deprioritized basenames. These are only walked at
# all when on ``allow_hidden`` (otherwise they are masked + pruned),
# which is the "if they are allowed" condition under which
# deprioritization can apply.
_DEPRIORITIZED_DOT_DIRS = [".venv", ".mypy_cache", ".codex-tmp"]


@pytest.mark.parametrize("dotdir", _DEPRIORITIZED_DOT_DIRS)
def test_allowed_dot_dir_is_treated_as_deprioritized(dotdir: str, tmp_path: Path) -> None:
    """
    An *allowed* deprioritized dot-dir (``.venv`` / ``.mypy_cache`` /
    ``.codex-tmp``) is walked last like ``node_modules``: when the cap
    trips, the overflow message flags that dir as ``deprioritized``,
    and the project's own dotfile (``cwd/proj/.secret``) is masked
    rather than dropped.

    Why the ``deprioritized`` flag is the load-bearing assertion: a
    dot-dir already sorts before sibling regular dirs, so plain DFS
    happens to pop it last anyway — the masked *set* alone wouldn't
    distinguish the two regimes. The flag is emitted only because the
    basename is in ``_DEFAULT_DEPRIORITIZED_DIRS``. If this name were
    removed from that set, the dir would be a normal stack entry and
    the message would say ``partially scanned`` WITHOUT
    ``deprioritized`` — so this assertion fails exactly when the
    feature regresses.
    """
    cache = tmp_path / dotdir
    cache.mkdir()
    (cache / "inner.txt").write_text("x")
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".secret").write_text("PROJECT_SECRET=1")
    (proj / "a.txt").write_text("x")
    (proj / "b.txt").write_text("x")
    # cwd children sorted: <dotdir>, proj. dotdir is allowed → deferred;
    # proj (3 children) is walked in the primary phase. With cap=5 the
    # budget covers <dotdir> (1) + proj (2) as cwd children, then proj's
    # 3 children (3,4,5); the 6th entry (inner.txt under the promoted
    # dotdir) trips the cap, leaving the dotdir partially scanned.
    with pytest.raises(OSError) as exc_info:
        _scan(tmp_path, allow_hidden=[dotdir], max_entries=5, overflow="error")
    msg = str(exc_info.value)
    dotdir_path = str((tmp_path / dotdir).resolve(strict=False))
    assert f"{dotdir_path} (partially scanned, deprioritized)" in msg, (
        f"{dotdir} must be flagged as deprioritized in the overflow message "
        f"when allowed. If the flag is missing, {dotdir} is no longer in "
        f"_DEFAULT_DEPRIORITIZED_DIRS. Got: {msg!r}"
    )


@pytest.mark.parametrize("dotdir", _DEPRIORITIZED_DOT_DIRS)
def test_unallowed_deprioritized_dot_dir_is_masked_not_walked(dotdir: str, tmp_path: Path) -> None:
    """
    The "if they are allowed" guard: when a deprioritized dot-dir is
    NOT on ``allow_hidden``, membership in the deprioritize set is a
    no-op — the dir is masked and pruned (its contents never walked),
    exactly as any other un-allowed dotdir.

    This guards the ordering of the two branches: masking is decided
    before the deprioritize/defer branch, so a name in the deprioritize
    set can never be promoted to "walked" while un-allowed (which would
    expose its contents to the cap walk).
    """
    cache = tmp_path / dotdir
    cache.mkdir()
    (cache / ".inner_secret").write_text("SECRET=1")
    # allow_hidden=[] (mask every dotfile). The cache dir is not allowed.
    entries = _scan(tmp_path)
    cache_entry = _entry_for(entries, cache)
    assert cache_entry is not None and cache_entry.kind == "dir", (
        f"Un-allowed {dotdir} must be masked as a directory, not walked."
    )
    # Pruned: nothing under the masked cache dir is emitted separately.
    nested = [e for e in entries if str(e.path).startswith(str(cache) + os.sep)]
    assert nested == [], (
        f"Walker descended into a masked {dotdir}; deprioritization must not "
        f"override masking + pruning for an un-allowed dotdir. Got: "
        f"{[e.path for e in nested]}"
    )


# ---------------------------------------------------------------------------
# Overflow message names the unfinished directories
# ---------------------------------------------------------------------------


def test_overflow_error_message_names_unfinished_node_modules(tmp_path: Path) -> None:
    """
    The ``error`` overflow message must name the directories the walk
    did not finish and flag the deprioritized ``node_modules`` so an
    operator can see at a glance which subtree was left unmasked.

    A failure here means the enriched message regressed back to the
    counts-only form, which left operators guessing which folder
    (node_modules vs a real source dir) was only partially walked.
    """
    _build_deprioritize_tree(tmp_path)
    # Budget math (see _build_deprioritize_tree, every dir has 3 children):
    # cwd's children .env/app/node_modules are entries 1-3 (node_modules
    # deferred), app's children are 4-6, then node_modules is promoted and
    # its first child trips the 7th entry > cap=6 — so node_modules is the
    # partially-scanned, deprioritized dir the message must name.
    with pytest.raises(OSError) as exc_info:
        _scan(tmp_path, max_entries=6, overflow="error")
    msg = str(exc_info.value)
    nm = str((tmp_path / "node_modules").resolve(strict=False))
    assert "Unfinished directories" in msg, (
        f"Message must introduce the unfinished-dirs clause. Got: {msg!r}"
    )
    assert nm in msg, (
        f"Message must name the node_modules path that was left unwalked. Got: {msg!r}"
    )
    assert "deprioritized" in msg, (
        f"node_modules must be flagged as deprioritized so the operator "
        f"knows why it was last in line. Got: {msg!r}"
    )
    # The tuning knobs stay in the message so users can find the escape hatches.
    assert "cwd_hidden_scan_max_entries" in msg and "cwd_hidden_scan_overflow" in msg, (
        f"Message must keep naming both tunable spec keys. Got: {msg!r}"
    )


def test_overflow_warn_message_distinguishes_partial_and_bounds_list(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """
    The ``warn`` overflow log must (a) distinguish the directory it was
    mid-scan of ("partially scanned") and (b) bound the list with a
    ``(+N more)`` suffix when many directories remain, so a huge tree
    can't produce a multi-KB log line.

    Setup: 15 top-level dirs each holding one file, cap=15. The walker
    pushes all 15 dirs while scanning cwd (entries 1..15), then trips
    on the first grandchild file (entry 16) while inside the
    last-popped dir — making that dir "partially scanned" and leaving
    14 dirs queued. 1 partial + 14 not-scanned = 15 lines; the first
    10 are shown and the remaining 5 collapse to ``(+5 more)``.
    """
    for i in range(15):
        d = tmp_path / f"d{i:02d}"
        d.mkdir()
        (d / "f.txt").write_text("x")
    caplog.set_level("CRITICAL", logger="omnigent.inner._cwd_scan")
    _scan(tmp_path, max_entries=15, overflow="warn")
    records = [r for r in caplog.records if "Mask is incomplete" in r.getMessage()]
    assert len(records) == 1, (
        f"Exactly one overflow CRITICAL expected, got {len(records)}: "
        f"{[r.getMessage() for r in caplog.records]}"
    )
    rendered = records[0].getMessage()
    assert "partially scanned" in rendered, (
        f"Message must mark the mid-scan directory as partially scanned. Got: {rendered!r}"
    )
    assert "not scanned" in rendered, (
        f"Message must mark the never-reached directories as not scanned. Got: {rendered!r}"
    )
    # 15 unfinished dirs - 10 shown = 5 collapsed into the suffix.
    assert "(+5 more)" in rendered, (
        f"Unfinished-dir list must be bounded to {10} entries with a (+5 more) "
        f"suffix so the log line stays small. Got: {rendered!r}"
    )


# ---------------------------------------------------------------------------
# Defensive edge cases
# ---------------------------------------------------------------------------


def test_missing_cwd_returns_empty_list(tmp_path: Path) -> None:
    """
    The walker swallows a missing/non-directory cwd and returns an
    empty list. Backends raise the user-facing error at spawn time
    (when bwrap / sandbox-exec try to enter the directory and fail
    loudly with the kernel's own message).
    """
    missing = tmp_path / "does_not_exist"
    entries = scan_cwd_mask_entries(
        missing,
        allow_hidden=[],
        safe_roots=[tmp_path],
        max_entries=_DEFAULT_MAX,
        overflow="error",
    )
    assert entries == []


def test_unreadable_subdirectory_is_skipped_silently(tmp_path: Path) -> None:
    """
    A subdirectory that can't be opened (e.g. permission denied)
    is skipped without raising. The parent stays in the safe set,
    and the inaccessibility itself doesn't leak the content the
    sandbox is trying to hide.
    """
    sub = tmp_path / "locked"
    sub.mkdir()
    (sub / ".env").write_text("masked-if-readable")
    # Drop read+execute permissions so os.scandir raises PermissionError
    # inside the walker; the walker is contractually required to
    # swallow this rather than propagate.
    sub.chmod(0o000)
    try:
        entries = _scan(tmp_path)
    finally:
        sub.chmod(0o700)
    # The locked subdirectory itself is a non-dot dir with a non-
    # escaping target (cwd), so it's not in the result; the .env
    # inside is unreachable and also absent. The contract is
    # "no crash, no leak", which is what we assert.
    for entry in entries:
        assert "locked" not in str(entry.path) or entry.path == sub, (
            f"Unreadable subdir leaked a child mask entry: {entry.path}"
        )
