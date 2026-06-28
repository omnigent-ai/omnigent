"""Unit tests for :class:`omnigent_client.tools.ToolState`.

Exercises the public API directly using a tmp_path root. No
subprocess, no server — that part is covered by e2e tests.
"""

from __future__ import annotations

import multiprocessing
import threading
from pathlib import Path

import pytest
from omnigent_client.tools import ToolState


@pytest.fixture()
def state_root(tmp_path: Path) -> Path:
    """Per-test root directory for ToolState — isolated via tmp_path."""
    return tmp_path / "tool_state" / "ag_test"
    # Deliberately do NOT mkdir here — ToolState creates lazily; we
    # want to verify that behavior.


@pytest.fixture()
def state(state_root: Path) -> ToolState:
    return ToolState(state_root)


# ── get / set / delete / keys ───────────────────────────────────────────


def test_get_absent_returns_default(state: ToolState) -> None:
    """Reading a never-written key returns the default."""
    # default=None implicitly.
    assert state.get("missing") is None
    # Explicit default is honored.
    assert state.get("missing", default=[]) == []
    assert state.get("missing", default={"a": 1}) == {"a": 1}


def test_set_then_get_round_trip(state: ToolState) -> None:
    """set() persists; get() returns the same value."""
    state.set("name", "alice")
    state.set("count", 42)
    state.set("nested", {"list": [1, 2, 3], "flag": True})

    # Exact equality proves JSON round-trip preserves structure —
    # a regression would e.g. turn int into str or drop keys.
    assert state.get("name") == "alice"
    assert state.get("count") == 42
    assert state.get("nested") == {"list": [1, 2, 3], "flag": True}


def test_set_overwrites(state: ToolState) -> None:
    """A second set() replaces the first value."""
    state.set("k", 1)
    state.set("k", 2)
    assert state.get("k") == 2


def test_delete_removes_key(state: ToolState) -> None:
    """delete() makes get() return the default again."""
    state.set("k", "v")
    state.delete("k")
    assert state.get("k") is None
    # Post-delete keys() no longer lists it.
    assert "k" not in state


def test_delete_missing_is_noop(state: ToolState) -> None:
    """Deleting a never-set key does not raise."""
    # Tools commonly don't know whether the key was ever set; a
    # raise here would force them into check-then-delete races.
    state.delete("never_existed")


def test_keys_reflects_current_state(state: ToolState) -> None:
    """keys() returns exactly the set of keys that have been set."""
    # Empty root: no keys.
    assert state.keys() == []
    state.set("a", 1)
    state.set("b", 2)
    # Sorted for determinism — the fixture guarantees this.
    assert state.keys() == ["a", "b"]
    state.delete("a")
    assert state.keys() == ["b"]


def test_directory_created_lazily_on_set(state: ToolState, state_root: Path) -> None:
    """The namespace dir is not created until the first write.

    This matters because a stateless tool shouldn't leave empty
    ``.tool_state/{agent_id}/`` directories scattered across every
    conversation's workspace.
    """
    # Fixture intentionally doesn't mkdir — verify get/keys tolerate
    # a missing directory.
    assert not state_root.exists()
    assert state.get("x") is None
    assert state.keys() == []
    assert not state_root.exists()

    # First write creates the directory.
    state.set("x", 1)
    assert state_root.is_dir()


# ── transaction() ──────────────────────────────────────────────────────


def test_transaction_commits_on_normal_exit(state: ToolState) -> None:
    """Mutations made inside ``transaction()`` persist after the block."""
    state.set("queue", [])
    with state.transaction("queue") as q:
        q.append("first")
        q.append("second")
    # After exit, the mutations must be visible to a fresh read.
    assert state.get("queue") == ["first", "second"]


def test_transaction_reads_default_for_absent_key(state: ToolState) -> None:
    """First use of a key yields ``default``; the default is persisted."""
    with state.transaction("new", default=[]) as v:
        # The caller gets a fresh list; mutating it in place means
        # the post-transaction state is the mutated list (not None).
        assert v == []
        v.append("first")
    # After exit, the mutated default is persisted — a read sees it.
    # If this returns None, the transaction isn't writing back the
    # yielded value, or isn't honoring the default.
    assert state.get("new") == ["first"]


def test_transaction_default_none_yields_none(state: ToolState) -> None:
    """Without a default, an absent key yields None (back-compat)."""
    with state.transaction("missing") as v:
        assert v is None


def test_transaction_does_not_commit_on_exception(state: ToolState) -> None:
    """An exception inside the with block leaves the prior value intact."""
    state.set("queue", ["safe"])
    with pytest.raises(RuntimeError, match=r"boom"):
        with state.transaction("queue") as q:
            q.append("doomed")
            raise RuntimeError("boom")
    # The mutation must be discarded — the pre-transaction value
    # is what a subsequent read sees. A bug that writes on exception
    # would leave ["safe", "doomed"] here.
    assert state.get("queue") == ["safe"]


def test_transaction_same_key_serializes_across_threads(
    state: ToolState,
) -> None:
    """Concurrent transactions on one key must not clobber each other.

    The whole point of ``transaction()`` is preventing the classic
    "read, modify, write" race. Spawn N threads that each append
    their thread id to a shared list; the final list must have
    exactly N entries. Without flock, writers would overwrite each
    other's appends and the final length would be < N.
    """
    num_threads = 20
    barrier = threading.Barrier(num_threads)

    def bump() -> None:
        # Sync all threads to the same wall-clock moment before
        # entering the critical section — maximizes race exposure
        # if the lock is broken.
        barrier.wait()
        with state.transaction("log", default=[]) as log:
            log.append(threading.get_ident())

    threads = [threading.Thread(target=bump) for _ in range(num_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    log = state.get("log", default=[])
    assert len(log) == num_threads, (
        f"Expected {num_threads} entries (one per thread), got {len(log)}. "
        f"A short log means the flock in transaction() is not serializing "
        f"concurrent writers."
    )


def test_transaction_different_keys_do_not_block_each_other(
    state: ToolState,
) -> None:
    """A transaction on key A must not serialize with one on key B.

    Each key has its own file, so flock on one file doesn't hold
    up the other. This test exposes a regression where someone
    moved to a single coarse-grained lock.
    """
    holding_a = threading.Event()
    release_b = threading.Event()

    def hold_a() -> None:
        with state.transaction("a"):
            holding_a.set()
            # Block until the other thread signals it's done.
            release_b.wait(timeout=5.0)

    def touch_b() -> None:
        holding_a.wait(timeout=5.0)
        # Should NOT block on a's lock — they're different keys.
        with state.transaction("b") as v:
            # No real mutation needed; just succeed.
            assert v is None
        release_b.set()

    t1 = threading.Thread(target=hold_a)
    t2 = threading.Thread(target=touch_b)
    t1.start()
    t2.start()
    t1.join(timeout=5.0)
    t2.join(timeout=5.0)
    assert not t1.is_alive() and not t2.is_alive(), (
        "A transaction on key 'a' blocked a transaction on key 'b'. "
        "flock must be per-file, not global."
    )


# ── key validation ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad_key",
    [
        "",
        "a/b",
        "a\\b",
        ".hidden",
        "../escape",
    ],
)
def test_illegal_keys_raise(state: ToolState, bad_key: str) -> None:
    """Keys with separators, leading dot, or empty names are rejected.

    Protects against directory traversal and hidden-file collisions
    in the state dir. Test coverage for each rejection branch.
    """
    with pytest.raises(ValueError, match=r"illegal|non-empty"):
        state.set(bad_key, "v")


# ── subprocess concurrency — proves per-key flock works cross-process ──


def _bump_in_subprocess(root: str, key: str) -> None:
    """Body for test_transaction_serializes_across_processes.

    Must be a module-level function for multiprocessing's pickling.
    """
    s = ToolState(Path(root))
    # ``default=[]`` gives every process a list on first entry, so
    # in-place append persists through the transaction's write-back.
    with s.transaction(key, default=[]) as v:
        v.append(1)


def test_transaction_serializes_across_processes(state_root: Path) -> None:
    """Cross-process concurrency: N child processes each append once;
    the final list must have exactly N entries.

    Threading isn't a full test of ``flock`` — under the GIL two
    Python threads don't both read the file at the same wall-clock
    instant. Subprocesses actually run in parallel on different
    cores, so this exposes any missing lock.
    """
    num_procs = 8
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("cross-process flock test requires POSIX fork")
    ctx = multiprocessing.get_context("fork")
    procs = [
        ctx.Process(target=_bump_in_subprocess, args=(str(state_root), "q"))
        for _ in range(num_procs)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=10.0)
        assert p.exitcode == 0, f"subprocess exited {p.exitcode}"

    state = ToolState(state_root)
    result = state.get("q", default=[])
    assert len(result) == num_procs, (
        f"Expected {num_procs} appends, got {len(result)}. Some writes "
        f"were lost — flock is not serializing cross-process transactions."
    )
