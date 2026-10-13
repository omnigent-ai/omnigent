"""Verified speculative replicas for one task.

The controller pins a checkpoint, runs K replicas in detached git worktrees, and takes a
snapshot of each worktree as a git tree SHA as soon as its replica finishes. It verifies
each distinct tree once, on its raw bytes and outside every replica. The first replica to
pass wins, and the rest are told to stop. When no replica passes, the controller composes
the failed trees with ``git merge-tree``. Identical trees are one ledger entry, because
execution fan-out is not evidence fan-out (Y. Eliaz, "Evidence-Aware MapReduce for
Forkable Compute", arXiv:2607.09689). Only a passing receipt is promoted, as a commit of
the receipt's tree, so the promoted bytes are exactly the verified bytes.

Controller git calls disable hooks, fsmonitor and replace refs, and replica snapshots read
attributes from the checkpoint. Replicas run as the user, so the guarantee covers which
bytes are promoted, not code execution.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import logging
import os
import secrets
import signal
import subprocess
import tempfile
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import astuple, dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath

_logger = logging.getLogger(__name__)

MAX_REPLICAS = 8
MAX_ROUNDS = 2
_DETAIL_CHARS = 4000
_GIT_TIMEOUT_S = 120.0
_VERIFY_TIMEOUT_S = 1800.0
_SYMLINK = b"120000"
_RETRY_HEADER = "Your previous attempt is in this directory. The verifier rejected it:"
# Copy errors a candidate's own paths cause. Any other, e.g. a full disk, is the controller's.
_CANDIDATE_ERRNOS = (errno.EEXIST, errno.EISDIR, errno.ENOTDIR, errno.ENAMETOOLONG, errno.EILSEQ)

Drafter = Callable[[Path, str, threading.Event], None]
"""Edits a replica's directory toward a prompt; its result and errors are never trusted.

The event is set once another replica passes, so the drafter can return early.
"""


class SpeculationError(Exception):
    """Raised when git or the verifier cannot run, or a promotion cannot be created."""


class _Unsafe(Exception):
    """Raised when a candidate's tree cannot be copied byte for byte for the verifier."""


class Status(StrEnum):
    """The controller's decision on a candidate. ``rejected`` means the verifier never ran."""

    PASSED = "passed"
    FAILED = "failed"
    REJECTED = "rejected"


@dataclass(frozen=True)
class Verifier:
    """Command run on a fresh copy of a candidate's exact bytes. Exit 0 passes.

    :param argv: Command run from the caller's directory in that copy, e.g.
        ``("python", "-m", "pytest", "-q", "tests/test_pager.py")``.
    :param protect: Pathspecs, relative to the caller's directory, that a candidate may
        not change, e.g. ``("tests/",)``. Each must match a file in the checkpoint.
    """

    argv: tuple[str, ...]
    protect: tuple[str, ...] = ()

    @property
    def digest(self) -> str:
        """Identity bound into every receipt, e.g. ``"sha256:9f2c..."``."""
        blob = json.dumps(astuple(self)).encode()
        return "sha256:" + hashlib.sha256(blob).hexdigest()


@dataclass(frozen=True)
class Receipt:
    """Controller-issued result binding exact bytes, a verifier and a checkpoint.

    :param tree: Git tree SHA of the candidate's bytes.
    :param checkpoint: Commit every replica started from.
    :param verifier: :attr:`Verifier.digest` of the verifier.
    :param status: The controller's decision on ``tree``.
    :param detail: Output tail, which is the counterexample when not passed.
    """

    tree: str
    checkpoint: str
    verifier: str
    status: Status
    detail: str

    @property
    def passed(self) -> bool:
        return self.status is Status.PASSED


@dataclass(frozen=True)
class Evidence:
    """One ledger entry, a distinct tree's receipt and every attempt that produced it.

    :param attempts: Replica and round of each attempt, e.g. ``("r3@1", "r1@2")``.
    """

    receipt: Receipt
    attempts: tuple[str, ...]


@dataclass(frozen=True)
class Promotion:
    """The commit of a passing tree, created once as ``refs/heads/speculate/<run>``."""

    commit: str
    evidence: Evidence


@dataclass(frozen=True)
class Outcome:
    """Result of one run.

    :param rounds: Rounds the replicas ran, 0 when the checkpoint already passed.
    :param ledger: Distinct trees in verification order, checkpoint first.
    """

    run_id: str
    checkpoint: str
    rounds: int
    ledger: tuple[Evidence, ...]
    promotion: Promotion | None

    @property
    def admitted(self) -> bool:
        """``False`` when the checkpoint already passed, so no replica ran."""
        return self.rounds > 0

    @property
    def branch(self) -> str | None:
        """Branch created by promotion, e.g. ``"speculate/1a2b3c4d"``."""
        return f"speculate/{self.run_id}" if self.promotion else None

    @property
    def composed_only_win(self) -> bool:
        """The promoted tree is a composition, and no replica's own tree passed."""
        return self.promotion is not None and not self.promotion.evidence.attempts


def speculate(cwd: Path, task: str, *, drafters: Sequence[Drafter], verifier: Verifier) -> Outcome:
    """Run one replica per drafter from one checkpoint and promote at most one candidate.

    :param cwd: Directory inside the repo. Drafters and the verifier run at the same
        relative path in their copies, e.g. ``Path("/src/app/pkg")``.
    :param task: Prompt every replica receives in round 1.
    :param drafters: One per replica, run concurrently, e.g. ``[claude, codex]``.
    :param verifier: Command run outside every replica.
    :returns: The ledger, and the promotion when a candidate passed.
    :raises ValueError: If there are no drafters or more than :data:`MAX_REPLICAS`.
    :raises SpeculationError: If the run itself cannot proceed, e.g. git fails, a
        protected path matches nothing, or the promotion branch already exists.
    """
    if not 1 <= len(drafters) <= MAX_REPLICAS:
        raise ValueError(f"need 1 to {MAX_REPLICAS} drafters, got {len(drafters)}")
    run = _pin(cwd, verifier)
    admission = run.record(run.checkpoint_tree)
    if admission.status is Status.REJECTED:
        raise SpeculationError(f"cannot verify the checkpoint: {admission.detail}")
    if admission.passed:
        return run.outcome(rounds=0)
    # An interrupted run may leave replicas writing here, so cleanup is best effort.
    with tempfile.TemporaryDirectory(
        prefix="omnigent-speculate-", ignore_cleanup_errors=True
    ) as tmp:
        rounds, winner = _explore(run, task, drafters, Path(tmp))
    # Promote last, so no controller step after it can touch the branch.
    return run.outcome(rounds, run.promote(winner) if winner else None)


@dataclass
class _Run:
    """One call's checkpoint and ledger, and the only issuer of receipts."""

    id: str
    repo: Path
    prefix: str
    checkpoint: str
    checkpoint_tree: str
    verifier: Verifier
    ledger: dict[str, Evidence] = field(default_factory=dict)

    def record(self, tree: str, *attempts: str) -> Receipt:
        """Verify ``tree`` the first time it is seen and add ``attempts`` to its entry."""
        known = self.ledger.get(tree) or Evidence(_verify(self, tree), ())
        self.ledger[tree] = Evidence(known.receipt, known.attempts + attempts)
        return known.receipt

    def compose(self, receipts: Iterable[Receipt]) -> Receipt | None:
        """Compose the distinct failed candidates with ``git merge-tree`` and verify that.

        Rejected candidates never compose. A candidate that conflicts with the composition
        so far is skipped. Returns ``None`` when fewer than two candidates compose, since
        that tree is already in the ledger.
        """
        failed = (r.tree for r in receipts if r.status is Status.FAILED)
        candidates = [t for t in dict.fromkeys(failed) if t != self.checkpoint_tree]
        if len(candidates) < 2:
            return None
        composed, parts = candidates[0], 1
        for tree in candidates[1:]:
            pair = (self._commit(composed), self._commit(tree))
            result = _git_raw(self.repo, "merge-tree", "--write-tree", *pair, ok=(0, 1))
            if result.returncode == 0:  # exit 1 means conflicting edits, so skip this one
                composed, parts = result.stdout.decode().split("\n", 1)[0], parts + 1
        return self.record(composed) if parts > 1 else None

    def promote(self, receipt: Receipt) -> Promotion:
        """Commit exactly ``receipt.tree`` on the checkpoint and create the run's branch."""
        evidence = self.ledger.get(receipt.tree)
        if evidence is None or evidence.receipt != receipt or not receipt.passed:
            raise SpeculationError("only a passing receipt from this run can be promoted")
        lineage = " ".join(evidence.attempts) or "composed"
        commit = self._commit(
            receipt.tree,
            f"speculate {self.id}: verified candidate\n\nSpeculate-Checkpoint: {self.checkpoint}\n"
            f"Speculate-Verifier: {receipt.verifier}\nSpeculate-Lineage: {lineage}\n",
        )
        # A create-only update fails the run when the branch exists, instead of moving it.
        ref = f"create refs/heads/speculate/{self.id} {commit}\n"
        _git(self.repo, "update-ref", "--stdin", stdin=ref.encode())
        return Promotion(commit, evidence)

    def outcome(self, rounds: int, promotion: Promotion | None = None) -> Outcome:
        return Outcome(self.id, self.checkpoint, rounds, tuple(self.ledger.values()), promotion)

    def _commit(self, tree: str, message: str = "speculate: candidate") -> str:
        # The checkpoint is the only parent, so merge-tree diffs both sides against it.
        return _git(self.repo, "commit-tree", tree, "-p", self.checkpoint, "-m", message)


def _explore(
    run: _Run, task: str, drafters: Sequence[Drafter], tmp: Path
) -> tuple[int, Receipt | None]:
    """Run the rounds in detached worktrees under ``tmp``. Returns the rounds and winner."""
    roots = [tmp / f"r{i}" for i in range(1, len(drafters) + 1)]
    try:
        for root in roots:
            _git(run.repo, "worktree", "add", "--detach", str(root), run.checkpoint)
            (root / run.prefix).mkdir(parents=True, exist_ok=True)
        prompts = [task] * len(roots)
        for rnd in range(1, MAX_ROUNDS + 1):
            receipts = _round(run, rnd, drafters, roots, prompts)
            # The first replica to pass in time wins, and composition never displaces it.
            winner = next((r for r in receipts.values() if r.passed), None)
            winner = winner or run.compose(receipts.values())
            if winner is not None and winner.passed:
                return rnd, winner
            for i, receipt in receipts.items():
                prompts[i - 1] = f"{task}\n\n{_RETRY_HEADER}\n{receipt.detail}"
        return MAX_ROUNDS, None
    finally:
        for root in roots:
            # Only this run's worktrees, since a repo-wide prune also drops the user's own
            # worktrees whose directories are missing, e.g. on an unplugged drive.
            try:
                _git(run.repo, "worktree", "remove", "--force", str(root))
            except SpeculationError:  # e.g. the replica deleted its .git file
                _git(run.repo, "worktree", "prune")


def _round(
    run: _Run, rnd: int, drafters: Sequence[Drafter], roots: list[Path], prompts: list[str]
) -> dict[int, Receipt]:
    """Verify each replica's tree as it finishes. Returns the receipts by replica number.

    A replica that cannot be snapshotted sits out the round. The first pass ends the round
    and stops the rest, whose unfinished trees are not evidence, so none is snapshotted.
    """
    stop = threading.Event()
    receipts: dict[int, Receipt] = {}
    errors: list[BaseException] = []
    with ThreadPoolExecutor(len(drafters)) as pool:
        try:
            replicas = {
                pool.submit(drafters[i], roots[i] / run.prefix, prompts[i], stop): i + 1
                for i in range(len(drafters))
            }
            for future in as_completed(replicas):
                i = replicas[future]
                if (error := future.exception()) is not None:
                    errors.append(error)
                    _logger.warning("replica r%d failed to draft: %s", i, error)
                try:
                    tree = _snapshot(roots[i - 1], run.checkpoint)
                except SpeculationError as exc:
                    _logger.warning("could not snapshot replica r%d: %s", i, exc)
                    continue
                receipts[i] = run.record(tree, f"r{i}@{rnd}")
                if receipts[i].passed:
                    return receipts
        finally:
            # A pass or an error, e.g. a full disk, must not leave the pool waiting on replicas.
            stop.set()
    if len(errors) == len(drafters):
        raise SpeculationError(f"every replica failed to draft: {errors[0]}")
    if not receipts:
        raise SpeculationError("could not snapshot any replica")
    return receipts


def _pin(cwd: Path, verifier: Verifier) -> _Run:
    version = _git(cwd, "version")
    if tuple(map(int, version.split()[2].split(".")[:2])) < (2, 41):
        raise SpeculationError(f"speculate needs git >= 2.41 (--attr-source), found {version}")
    root = Path(_git(cwd, "rev-parse", "--show-toplevel"))
    head = _git(root, "rev-parse", "--verify", "HEAD^{commit}")
    tree = _snapshot(root)
    # Uncommitted and untracked files belong to the checkpoint, not to any replica.
    checkpoint = (
        head
        if tree == _git(root, "rev-parse", "HEAD^{tree}")
        else _git(root, "commit-tree", tree, "-p", head, "-m", "speculate: checkpoint")
    )
    prefix = _git(cwd, "rev-parse", "--show-prefix")
    empty = _git(root, "hash-object", "-t", "tree", os.devnull)
    for spec in verifier.protect:
        if not _git(root / prefix, "diff-tree", "-r", "--name-only", empty, tree, "--", spec):
            raise SpeculationError(f"protected path {spec!r} matches no file in the checkpoint")
    return _Run(secrets.token_hex(4), root, prefix, checkpoint, tree, verifier)


def _snapshot(worktree: Path, checkpoint: str | None = None) -> str:
    """Tree SHA of everything in ``worktree``, including edits, deletions and new files.

    :param checkpoint: For a replica, the checkpoint commit. The temp index starts from it,
        and in-tree ``.gitattributes`` are read from it rather than from the replica.
    """
    with tempfile.TemporaryDirectory(prefix="omnigent-speculate-") as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        if checkpoint is not None:
            env["GIT_ATTR_SOURCE"] = checkpoint
        _git(worktree, "read-tree", checkpoint or "HEAD", env=env)
        _git(worktree, "add", "-A", env=env)
        return _git(worktree, "write-tree", env=env)


def _verify(run: _Run, tree: str) -> Receipt:
    def receipt(status: Status, detail: str) -> Receipt:
        return Receipt(tree, run.checkpoint, run.verifier.digest, status, detail[-_DETAIL_CHARS:])

    if protect := run.verifier.protect:
        # Run from the caller's directory so pathspecs resolve as they do for the user.
        diff = ["diff-tree", "-r", "--name-only", run.checkpoint_tree, tree, "--", *protect]
        if touched := _git(run.repo / run.prefix, *diff).splitlines():
            return receipt(Status.REJECTED, f"changes protected paths: {', '.join(touched)}")
    changes = _git(run.repo, "diff-tree", "-r", run.checkpoint_tree, tree).splitlines()
    if any("160000" in line[1:].split()[:2] for line in changes):
        return receipt(Status.REJECTED, "changes submodules")
    with tempfile.TemporaryDirectory(prefix="omnigent-verify-", ignore_cleanup_errors=True) as tmp:
        try:
            workdir = _materialize(run, tree, Path(tmp).resolve())
        except (_Unsafe, OSError) as exc:
            if isinstance(exc, OSError) and exc.errno not in _CANDIDATE_ERRNOS:
                raise SpeculationError(f"cannot copy a candidate: {exc}") from exc
            return receipt(Status.REJECTED, f"cannot copy the candidate: {exc}")
        try:
            proc = subprocess.Popen(
                run.verifier.argv,
                cwd=workdir,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise SpeculationError(f"verifier could not start: {exc}") from exc
        with proc:
            try:
                out, err = proc.communicate(timeout=_VERIFY_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                return receipt(Status.FAILED, f"verifier timed out after {_VERIFY_TIMEOUT_S:.0f}s")
            finally:
                # What the verifier started runs candidate code, so none of it may outlive it.
                # macOS raises EPERM for a group whose only member is an unreaped zombie.
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)
    status = Status.PASSED if proc.returncode == 0 else Status.FAILED
    return receipt(status, (out + err).decode(errors="replace"))


def _materialize(run: _Run, tree: str, root: Path) -> Path:
    """Write ``tree``'s raw blobs under ``root`` and return the caller's directory in it.

    :raises _Unsafe: If a path, or a symlink once resolved, leaves the tree.
    :raises OSError: If two paths collide, e.g. ``A`` and ``a`` on a case-insensitive disk.
    """
    # Raw blobs via cat-file, so in-tree .gitattributes (export-ignore, eol, filters) cannot
    # hide or rewrite what the verifier sees, as they can with git archive or checkout.
    blobs = []
    for entry in filter(None, _git_raw(run.repo, "ls-tree", "-r", "-z", tree).stdout.split(b"\0")):
        meta, path = entry.split(b"\t", 1)
        mode, kind, oid = meta.split()
        if kind == b"blob":
            blobs.append((mode, oid, os.fsdecode(path)))
    # Directories exist before any symlink and files are created exclusively, so no write
    # follows a symlink, and a case-variant collision raises instead of overwriting.
    for _, _, path in blobs:
        if ".." in PurePosixPath(path).parts:
            raise _Unsafe(f"unsafe path {path!r}")
        (root / path).parent.mkdir(parents=True, exist_ok=True)
    batch = b"".join(oid + b"\n" for _, oid, _ in blobs)
    out, pos = _git_raw(run.repo, "cat-file", "--batch", stdin=batch).stdout, 0
    for mode, _, path in blobs:
        end = out.index(b"\n", pos)
        size = int(out[pos:end].split()[2])
        data, pos = out[end + 1 : end + 1 + size], end + 2 + size
        if mode != _SYMLINK:
            with (root / path).open("xb") as file:
                file.write(data)
            (root / path).chmod(0o755 if mode == b"100755" else 0o644)
            continue
        (root / path).symlink_to(os.fsdecode(data))
    # Checked once every link exists, since with up -> . a target like up/../x leaves.
    for path in (path for mode, _, path in blobs if mode == _SYMLINK):
        try:
            target = (root / path).resolve()
        except (RuntimeError, OSError) as exc:  # a symlink loop
            raise _Unsafe(f"symlink {path} cannot be resolved: {exc}") from exc
        if not target.is_relative_to(root):
            raise _Unsafe(f"symlink {path} points outside the tree")
    workdir = root / run.prefix
    workdir.mkdir(parents=True, exist_ok=True)
    return workdir


def _git_raw(
    cwd: Path,
    *args: str,
    env: Mapping[str, str] | None = None,
    stdin: bytes | None = None,
    ok: tuple[int, ...] = (0,),
) -> subprocess.CompletedProcess[bytes]:
    # Hooks, fsmonitor and replace refs live in the .git dir every replica can write.
    argv = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", *args]
    env = {**os.environ, "LC_ALL": "C", "GIT_NO_REPLACE_OBJECTS": "1", **(env or {})}
    try:
        result = subprocess.run(
            argv, cwd=cwd, input=stdin, capture_output=True, env=env, timeout=_GIT_TIMEOUT_S
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SpeculationError(f"git {args[0]} failed: {exc}") from exc
    if result.returncode not in ok:
        detail = result.stderr.decode(errors="replace").strip()
        raise SpeculationError(f"git {args[0]} failed ({result.returncode}): {detail}")
    return result


def _git(
    cwd: Path, *args: str, env: Mapping[str, str] | None = None, stdin: bytes | None = None
) -> str:
    return _git_raw(cwd, *args, env=env, stdin=stdin).stdout.decode().strip()
