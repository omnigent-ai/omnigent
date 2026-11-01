"""``speculate()`` against real git, with plain-function drafters and no LLM."""

from __future__ import annotations

import errno
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from omnigent.host.speculate import Drafter, Outcome, SpeculationError, Verifier, speculate

# Runs every test_*.py, so hiding one from the verifier would make a broken tree pass.
_CHECK = """\
import pathlib, runpy, sys
tests = sorted(pathlib.Path().glob("test_*.py"))
failing = [t.name for t in tests if not runpy.run_path(str(t))["ok"]]
print("failing:", failing)
sys.exit(1 if failing else 0)
"""
_VERIFIER = Verifier((sys.executable, "check.py"), protect=("check.py", "test_*.py"))
_FIX_A, _FIX_B = {"a.py": "A = 1\n"}, {"b.py": "B = 1\n"}
_MOVE_SPECULATE_BRANCHES = """\
#!/bin/sh
[ "$1" = committed ] && [ -z "$MOVED" ] || exit 0
for ref in $(git for-each-ref --format='%(refname)' refs/heads/speculate); do
  MOVED=1 git update-ref "$ref" HEAD
done
"""


def _write(files: dict[str, str]) -> Drafter:
    def draft(workdir: Path, *_: object) -> None:
        for name, text in files.items():
            (workdir / name).parent.mkdir(exist_ok=True)
            (workdir / name).write_text(text)

    return draft


fix_a, fix_b, fix_both, noop = map(_write, (_FIX_A, _FIX_B, {**_FIX_A, **_FIX_B}, {}))


def _late(draft: Drafter) -> Drafter:
    """``draft`` 0.5 s late, so the instant replicas beside it are verified before it."""

    def late(workdir: Path, prompt: str, stop: threading.Event) -> None:
        time.sleep(0.5)
        draft(workdir, prompt, stop)

    return late


def _nested_repo(workdir: Path, *_: object) -> None:
    fix_both(workdir)
    (workdir / "vendor").mkdir(exist_ok=True)
    _git(workdir / "vendor", "init", "-q")
    _git(workdir / "vendor", "commit", "-q", "--allow-empty", "-m", "vendored")


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return done.stdout.strip()


def _worktrees(repo: Path) -> int:
    return _git(repo, "worktree", "list", "--porcelain").count("worktree ")


def _ledger(out: Outcome) -> list[str]:
    """Each entry's status and sorted attempts, e.g. ``"failed r1@1 r4@1"``."""
    return [" ".join([e.receipt.status, *sorted(map(str, e.attempts))]) for e in out.ledger]


def _exits(pid: int) -> bool:
    """Whether process ``pid`` is gone within 2 s."""
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Two independent bugs, ``A = 0`` and ``B = 0``, each with its own check."""
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "t")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "t@t")
    tests = {
        "test_a.py": "from a import A\nok = A == 1\n",
        "test_b.py": "from b import B\nok = B == 1\n",
    }
    _write({"a.py": "A = 0\n", "b.py": "B = 0\n", "check.py": _CHECK, **tests})(tmp_path)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "initial")
    return tmp_path


def test_composed_tree_wins_when_each_replica_fixes_half(repo: Path) -> None:
    """r1 fixes a.py and r2 fixes b.py. Neither passes alone, so their composition wins.

    r4 repeats r1's bytes and r3 changes nothing, so identical trees share one entry. The
    untracked notes.txt belongs to the checkpoint every replica starts from.
    """
    (repo / "notes.txt").write_text("draft\n")
    head, status = _git(repo, "rev-parse", "HEAD"), _git(repo, "status", "--porcelain")

    out = speculate(repo, "fix", drafters=[fix_a, fix_b, noop, fix_a], verifier=_VERIFIER)

    assert out.promotion is not None and out.composed_only_win
    assert sorted(_ledger(out)) == ["failed r1@1 r4@1", "failed r2@1", "failed r3@1", "passed"]
    commit = out.promotion.commit
    assert out.promotion.evidence == out.ledger[-1]
    assert _git(repo, "rev-parse", f"{commit}^{{tree}}") == out.ledger[-1].receipt.tree
    assert _git(repo, "rev-parse", f"{commit}^") == out.checkpoint
    assert _git(repo, "rev-parse", f"{out.checkpoint}^") == head
    assert _git(repo, "show", f"{commit}:a.py", f"{commit}:b.py", f"{commit}:notes.txt") == (
        "A = 1\nB = 1\ndraft"
    )
    refs = ["for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads/speculate"]
    assert _git(repo, *refs) == f"{out.branch} {commit}"
    assert (_git(repo, "rev-parse", "HEAD"), _git(repo, "status", "--porcelain")) == (head, status)
    assert _worktrees(repo) == 1


def test_passing_replica_is_promoted_without_composition(repo: Path) -> None:
    """r3 passes after r1 and r2 fail, so their composition, which would also pass, never runs."""
    fix_both_with_notes = _late(_write({**_FIX_A, **_FIX_B, "notes.txt": "draft\n"}))

    out = speculate(repo, "fix", drafters=[fix_a, fix_b, fix_both_with_notes], verifier=_VERIFIER)

    assert out.promotion is not None and not out.composed_only_win
    assert sorted(_ledger(out)) == ["failed", "failed r1@1", "failed r2@1", "passed r3@1"]
    assert out.promotion.evidence == out.ledger[3]


def test_first_replica_to_pass_wins_and_the_rest_are_stopped(repo: Path) -> None:
    """r2 passes at once, so r1, which would pass only after 30 s, is stopped unverified."""
    woke: list[bool] = []

    def slow(workdir: Path, prompt: str, stop: threading.Event) -> None:
        woke.append(stop.wait(30))
        fix_both(workdir)

    start = time.monotonic()
    out = speculate(repo, "fix", drafters=[slow, fix_both], verifier=_VERIFIER)

    assert time.monotonic() - start < 10
    assert _ledger(out) == ["failed", "passed r2@1"]
    assert out.promotion is not None and out.promotion.evidence == out.ledger[1]
    assert woke == [True]


def test_rejected_candidate_never_composes(repo: Path) -> None:
    """r1 fixes a.py but rewrites check.py, so only r2 and r3 compose, and that passes."""
    tamper = _write({**_FIX_A, "check.py": "print('ok')\n"})

    out = speculate(repo, "fix", drafters=[tamper, fix_a, fix_b], verifier=_VERIFIER)

    assert out.composed_only_win
    ledger = sorted(_ledger(out))
    assert ledger == ["failed", "failed r2@1", "failed r3@1", "passed", "rejected r1@1"]


def test_checkpoint_that_already_passes_starts_no_replica(repo: Path) -> None:
    """Uncommitted fixes belong to the checkpoint, which passes, so no replica runs."""
    fix_both(repo)
    prompts: list[str] = []

    out = speculate(repo, "fix", drafters=[lambda _, p, __: prompts.append(p)], verifier=_VERIFIER)

    assert (out.admitted, out.rounds, out.promotion, prompts) == (False, 0, None, [])
    assert _ledger(out) == ["passed"]
    assert _worktrees(repo) == 1


def test_second_round_receives_the_counterexample(repo: Path) -> None:
    """A replica rejected in round 1 is re-prompted with the verifier's own output."""
    prompts: list[str] = []

    def draft(workdir: Path, prompt: str, *_: object) -> None:
        prompts.append(prompt)
        (fix_b if len(prompts) == 2 else fix_a)(workdir)

    out = speculate(repo, "fix", drafters=[draft], verifier=_VERIFIER)

    assert out.rounds == 2 and out.promotion is not None
    assert prompts[0] == "fix" and "failing: ['test_b.py']" in prompts[1]
    assert [str(a) for a in out.promotion.evidence.attempts] == ["r1@2"]


def test_run_from_a_subdirectory_resolves_paths_from_it(repo: Path) -> None:
    """Protected paths, drafters and the verifier all work relative to ``pkg/``."""
    (repo / "pkg").mkdir()
    _git(repo, "mv", "a.py", "b.py", "check.py", "test_a.py", "test_b.py", "pkg")
    tamper = _write({**_FIX_A, **_FIX_B, "check.py": "print('ok')\n"})

    out = speculate(repo / "pkg", "fix", drafters=[tamper, _late(fix_both)], verifier=_VERIFIER)

    assert _ledger(out) == ["failed", "rejected r1@1", "passed r2@1"]
    assert out.ledger[1].receipt.detail == "changes protected paths: pkg/check.py"
    assert out.promotion is not None
    assert _git(repo, "show", f"{out.promotion.commit}:pkg/a.py") == "A = 1"


@pytest.mark.parametrize(
    ("tamper", "detail"),
    [
        pytest.param(
            _write({"check.py": "print('all good')\n"}),
            "changes protected paths: check.py",
            id="protected-path",
        ),
        pytest.param(
            _write({**_FIX_A, ".gitattributes": "test_b.py export-ignore\n"}),
            "failing: ['test_b.py']",
            id="export-ignore",
        ),
        pytest.param(_nested_repo, "changes submodules", id="submodule"),
    ],
)
def test_tampering_replica_is_never_promoted(repo: Path, tamper: Drafter, detail: str) -> None:
    """The verifier runs outside the replica, so rewriting it or hiding a check fails."""
    out = speculate(repo, "fix", drafters=[tamper], verifier=_VERIFIER)

    assert out.promotion is None
    assert detail in out.ledger[-1].receipt.detail
    assert _git(repo, "for-each-ref", "refs/heads/speculate") == ""


def test_symlink_chain_out_of_the_tree_is_rejected(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """``a.py -> up/../a.py`` with ``up -> .`` reads the copy's parent, where A = 1 waits."""
    shared = tmp_path_factory.mktemp("shared")
    monkeypatch.setattr(tempfile, "tempdir", str(shared))
    (shared / "a.py").write_text("A = 1\n")

    def chain(workdir: Path, *_: object) -> None:
        fix_b(workdir)
        if not (workdir / "up").is_symlink():
            (workdir / "up").symlink_to(".")
            (workdir / "a.py").unlink()
            (workdir / "a.py").symlink_to("up/../a.py")

    out = speculate(repo, "fix", drafters=[chain], verifier=_VERIFIER)

    assert (_ledger(out), out.promotion) == (["failed", "rejected r1@1 r1@2"], None)
    assert "symlink a.py points outside the tree" in out.ledger[1].receipt.detail


def test_caller_directory_turned_into_a_symlink_is_rejected(repo: Path) -> None:
    """``pkg`` becomes ``up/..`` with ``up -> .``, which resolves outside the verifier's copy."""
    (repo / "pkg").mkdir()
    _git(repo, "mv", "a.py", "b.py", "check.py", "test_a.py", "test_b.py", "pkg")

    def escape(workdir: Path, *_: object) -> None:
        if not workdir.is_symlink():
            shutil.rmtree(workdir)
            (workdir.parent / "up").symlink_to(".")
            workdir.symlink_to("up/..")

    out = speculate(repo / "pkg", "fix", drafters=[escape], verifier=Verifier(_VERIFIER.argv))

    assert _ledger(out)[1] == "rejected r1@1 r1@2"
    assert "symlink pkg points outside the tree" in out.ledger[1].receipt.detail


def test_planted_hook_cannot_move_the_promoted_branch(repo: Path) -> None:
    """A replica's reference-transaction hook never runs in the controller's git calls."""

    def plant(workdir: Path, *_: object) -> None:
        fix_both(workdir)
        common = _git(workdir, "rev-parse", "--path-format=absolute", "--git-common-dir")
        hook = Path(common) / "hooks" / "reference-transaction"
        hook.parent.mkdir(exist_ok=True)
        hook.write_text(_MOVE_SPECULATE_BRANCHES)
        hook.chmod(0o755)

    out = speculate(repo, "fix", drafters=[plant], verifier=_VERIFIER)

    assert out.promotion is not None
    assert _git(repo, "rev-parse", f"refs/heads/{out.branch}") == out.promotion.commit


def test_promotion_never_moves_an_existing_branch(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run whose branch already exists fails instead of moving it."""
    monkeypatch.setattr("secrets.token_hex", lambda _: "feedface")
    _git(repo, "branch", "speculate/feedface")

    with pytest.raises(SpeculationError, match="already exists"):
        speculate(repo, "fix", drafters=[fix_both], verifier=_VERIFIER)

    assert _git(repo, "rev-parse", "speculate/feedface") == _git(repo, "rev-parse", "HEAD")
    assert _worktrees(repo) == 1


def test_replica_that_cannot_be_snapshotted_sits_out_the_round(repo: Path) -> None:
    """r1 deletes its own worktree, so only r2's candidate is verified and promoted."""

    def vandal(workdir: Path, *_: object) -> None:
        shutil.rmtree(workdir)

    out = speculate(repo, "fix", drafters=[vandal, _late(fix_both)], verifier=_VERIFIER)

    assert _ledger(out) == ["failed", "passed r2@1"]
    assert _worktrees(repo) == 1


def test_run_keeps_the_users_worktree_whose_directory_is_missing(
    repo: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Only the run's own worktrees are removed, not one on a drive that is unplugged."""
    usb = tmp_path_factory.mktemp("usb")
    _git(repo, "worktree", "add", "-q", "-b", "feature", str(usb / "wt"))
    (usb / "wt").rename(usb / "away")

    speculate(repo, "fix", drafters=[fix_both], verifier=_VERIFIER)

    worktrees = _git(repo, "worktree", "list", "--porcelain").split("\n\n")
    assert [w.splitlines()[0] for w in worktrees] == [f"worktree {repo}", f"worktree {usb}/wt"]


def test_run_fails_when_every_replica_fails_to_draft(repo: Path) -> None:
    """A broken harness is not mistaken for a hard task, and its worktrees are removed."""

    def broken(workdir: Path, *_: object) -> None:
        raise RuntimeError("harness not logged in")

    with pytest.raises(SpeculationError, match="every replica failed to draft: harness not"):
        speculate(repo, "fix", drafters=[broken, broken], verifier=_VERIFIER)

    assert _worktrees(repo) == 1


def test_protected_path_that_matches_nothing_fails_before_forking(repo: Path) -> None:
    """A misspelled protected path would protect nothing, so the run refuses to start."""
    verifier = Verifier(_VERIFIER.argv, protect=("chek.py",))

    with pytest.raises(SpeculationError, match=r"'chek\.py' matches no file in the checkpoint"):
        speculate(repo, "fix", drafters=[fix_both], verifier=verifier)


def test_checkpoint_the_verifier_cannot_copy_fails_before_forking(repo: Path) -> None:
    """A symlink out of the checkpoint would reject every candidate, so the run refuses."""
    (repo / "etc").symlink_to("/etc")

    with pytest.raises(SpeculationError, match=r"checkpoint: .* symlink etc points outside"):
        speculate(repo, "fix", drafters=[fix_both], verifier=_VERIFIER)


def test_verifier_timeout_kills_what_the_verifier_started(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """A child the verifier left writing in its copy dies with it, so the run completes."""
    monkeypatch.setattr("omnigent.host.speculate._VERIFY_TIMEOUT_S", 1.0)
    pid = tmp_path_factory.mktemp("verifier") / "pid"
    child = (
        f"import os, pathlib, time; pathlib.Path({str(pid)!r}).write_text(str(os.getpid()))\n"
        "for i in range(10**4):\n    try: pathlib.Path(f'log{i}').touch()\n"
        "    except OSError: pass\n    time.sleep(0.001)"
    )
    spawn = f"import subprocess, sys; subprocess.Popen([sys.executable, '-c', {child!r}])"
    verifier = Verifier((sys.executable, "-c", spawn))

    out = speculate(repo, "fix", drafters=[noop], verifier=verifier)

    assert _ledger(out) == ["failed r1@1 r1@2"]
    assert out.ledger[0].receipt.detail == "verifier timed out after 1s"
    assert _exits(int(pid.read_text()))


def test_disk_error_while_copying_a_candidate_fails_the_run(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full disk is the controller's error, so it fails the run and stops every replica."""
    open_path = Path.open
    woke: list[bool] = []

    def waiting(workdir: Path, prompt: str, stop: threading.Event) -> None:
        woke.append(stop.wait(30))

    def full_disk(path: Path, mode: str = "r", *args: object, **kwargs: object) -> object:
        if mode == "xb" and path.name == "cache.bin":
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        return open_path(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", full_disk)
    start = time.monotonic()

    with pytest.raises(SpeculationError, match="No space left on device"):
        speculate(repo, "fix", drafters=[_write({"cache.bin": "x"}), waiting], verifier=_VERIFIER)
    assert time.monotonic() - start < 10
    assert woke == [True]
    assert _worktrees(repo) == 1
