"""The workspace API's hardened Git runner, against real repositories and a fake ``git``.

The fake ``git`` is a shell script first on ``PATH``: it answers discovery for
a repository at the test's root, then runs the test's own commands, so a test
can make Git sleep, flood or fork without a real repository.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from omnigent.host.workspace_git import runner
from omnigent.host.workspace_git.runner import (
    Deadline,
    GitError,
    GitProblem,
    GitRepository,
    check_revision,
    is_object_name,
    parse_behavior_configuration,
    parse_version,
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="the runner signals POSIX process groups"
)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout


def _repository(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "dev@example.com")
    _git(root, "config", "user.name", "Dev")
    # Commit-triggered maintenance must finish before metadata snapshots begin.
    _git(root, "config", "maintenance.auto", "false")
    _git(root, "config", "gc.auto", "0")
    (root / "a.txt").write_text("a\n")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-q", "-m", "first")
    return root


def _fake_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> Path:
    """Put a fake ``git`` first on PATH; it answers discovery for ``tmp_path/repo``."""
    root = tmp_path / "repo"
    root.mkdir(exist_ok=True)
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir(exist_ok=True)
    script = bin_directory / "git"
    script.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        f"*rev-parse\\ --absolute-git-dir*) printf '%s\\n%s\\n%s\\n\\n' "
        f"'{root}/.git' '{root}/.git' '{root}'; exit 0;;\n"
        "*config\\ --null*) exit 1;;\n"
        "esac\n" + body
    )
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_directory}{os.pathsep}{os.environ['PATH']}")
    return root


def _alive(pid: int, *, within_s: float = 3.0) -> bool:
    """Whether ``pid`` still runs after a brief wait for a kill to land.

    A killed orphan can be left as this process's zombie (another test may have
    made it a child subreaper), so a zombie child is reaped and counts as gone.
    """
    deadline = time.monotonic() + within_s
    while True:
        try:
            if os.waitpid(pid, os.WNOHANG)[0] == pid:
                return False
        except ChildProcessError:
            pass
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        if time.monotonic() >= deadline:
            return True
        time.sleep(0.05)


async def _discover(root: Path, seconds: float = 10) -> GitRepository:
    repository = await GitRepository.discover(root, Deadline(seconds))
    assert repository is not None
    return repository


@pytest.mark.parametrize(
    "revision",
    ["", "-x", "--output=/tmp/x", "a" * 1025, "main\0x", "main\nx"],
    ids=["empty", "option", "output", "too-long", "nul", "newline"],
)
def test_revisions_git_must_not_see_are_refused(revision: str) -> None:
    with pytest.raises(GitError) as refused:
        check_revision(revision)
    assert refused.value.problem is GitProblem.INVALID_REVISION


def test_ordinary_revisions_pass() -> None:
    for revision in ("main", "origin/main", "HEAD~2", "v1.0^{commit}", "a" * 1024):
        assert check_revision(revision) == revision


def test_object_names_are_full_hex() -> None:
    assert is_object_name("a" * 40) and is_object_name("0" * 64)
    assert not is_object_name("a" * 39) and not is_object_name("g" * 40)


@pytest.mark.parametrize(
    ("text", "version"),
    [
        ("git version 2.54.0 (Apple Git-157)", (2, 54)),
        ("git version 2.43.1\n", (2, 43)),
        ("git version 2.44.0.windows.1", (2, 44)),
        ("not git", None),
    ],
)
def test_parse_version(text: str, version: tuple[int, int] | None) -> None:
    assert parse_version(text) == version


def test_behavior_configuration_names_drivers_and_partial_clones() -> None:
    records = (
        b"filter.lfs.clean\ngit-lfs clean\0"
        b"filter.Mixed.Case.smudge\nx\0"
        b"remote.origin.promisor\ntrue\0"
    )
    configuration = parse_behavior_configuration(records)
    assert configuration.filter_drivers == {"lfs", "Mixed.Case"}
    assert configuration.partial_clone
    assert parse_behavior_configuration(b"extensions.partialclone\norigin\0").partial_clone
    assert not parse_behavior_configuration(b"remote.origin.promisor\nfalse\0").partial_clone


@pytest.mark.parametrize(
    ("record", "partial_clone"),
    [
        (b"remote.origin.promisor\0", True),  # a bare key is true
        (b"remote.origin.promisor\nyes\0", True),
        (b"remote.origin.promisor\nOn\0", True),
        (b"remote.origin.promisor\n1\0", True),
        (b"remote.origin.promisor\n2\0", True),
        (b"remote.origin.promisor\n\0", False),  # an empty value is false
        (b"remote.origin.promisor\nNo\0", False),
        (b"remote.origin.promisor\noff\0", False),
        (b"remote.origin.promisor\n0\0", False),
    ],
)
def test_a_promisor_remote_is_read_as_git_reads_a_boolean(
    record: bytes, partial_clone: bool
) -> None:
    assert parse_behavior_configuration(record).partial_clone is partial_clone


@pytest.mark.parametrize("record", [b"filter.a=b.clean\nx\0", b"filter.\xff.clean\nx\0"])
def test_driver_names_an_override_cannot_carry_refuse_the_repository(record: bytes) -> None:
    with pytest.raises(GitError) as refused:
        parse_behavior_configuration(record)
    assert refused.value.problem is GitProblem.UNSAFE_CONFIGURATION


async def test_discovery_outside_a_repository_finds_none(tmp_path: Path) -> None:
    assert await GitRepository.discover(tmp_path, Deadline(10)) is None


async def test_discovery_of_a_subdirectory_keeps_its_prefix(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")
    (root / "src").mkdir()

    repository = await _discover(root / "src")

    assert repository.top_level == root.resolve()
    assert repository.prefix == "src/"


async def test_the_hosts_git_environment_cannot_redirect_the_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repository(tmp_path / "repo")
    decoy = _repository(tmp_path / "decoy")
    (decoy / "decoy.txt").write_text("d")
    _git(decoy, "add", "decoy.txt")
    monkeypatch.setenv("GIT_DIR", str(decoy / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(decoy))
    monkeypatch.setenv("GIT_INDEX_FILE", str(decoy / ".git" / "index"))
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.hooksPath'='/tmp'")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(decoy / ".git" / "config"))

    repository = await _discover(root)
    listed = await repository.run(["ls-files", "-z"], deadline=Deadline(10))

    assert repository.top_level == root.resolve()
    assert listed == b"a.txt\0"


async def test_only_check_ignore_runs_without_literal_pathspecs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """check-ignore refuses literal pathspecs; every other command keeps them."""
    root = _fake_git(
        tmp_path,
        monkeypatch,
        'printf "%s %s" "${GIT_LITERAL_PATHSPECS:-unset}" "${GIT_DIR:-unset}"\n',
    )
    monkeypatch.setenv("GIT_LITERAL_PATHSPECS", "1")
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "decoy"))
    repository = await _discover(root)

    checked = await repository.run(["check-ignore", "-z", "--stdin"], deadline=Deadline(10))
    listed = await repository.run(["ls-files", "-z"], deadline=Deadline(10))

    # The host's own GIT_* variables stay dropped either way.
    assert checked == b"unset unset"
    assert listed == b"1 unset"


async def test_configured_helpers_hooks_and_filters_never_run(tmp_path: Path) -> None:
    """fsmonitor, hooks, filter drivers, per-driver and external diff, and textconv are all off."""
    root = _repository(tmp_path / "repo")
    marker = tmp_path / "ran"
    touch = f"sh -c 'echo $0 >> {marker}'"
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for hook in ("pre-commit", "post-checkout", "reference-transaction"):
        (hooks / hook).write_text(f"#!/bin/sh\necho {hook} >> {marker}\n")
        (hooks / hook).chmod(0o755)
    for key, value in {
        "core.fsmonitor": f"{touch} fsmonitor",
        "core.hooksPath": str(hooks),
        "filter.x.clean": f"{touch} clean",
        "filter.x.smudge": f"{touch} smudge",
        "filter.x.process": f"{touch} process",
        "filter.x.required": "true",
        "diff.x.textconv": f"{touch} textconv",
        "diff.x.command": f"{touch} diff-command",
        "diff.external": f"{touch} external",
        "core.pager": f"{touch} pager",
    }.items():
        _git(root, "config", key, value)
    (root / ".gitattributes").write_text("*.txt filter=x diff=x\n")
    (root / "a.txt").write_text("changed\n")
    marker.unlink(missing_ok=True)

    repository = await _discover(root)
    raw = await repository.run(["diff", "--raw", "-z", "HEAD", "--", "."], deadline=Deadline(10))
    # The runner adds --no-ext-diff and --no-textconv; the caller needn't.
    patch = await repository.run(["diff", "HEAD", "--", "."], deadline=Deadline(10))
    await repository.run(["ls-files", "--cached", "-z", "--", "."], deadline=Deadline(10))

    assert b"a.txt" in raw and b"+changed" in patch
    assert not marker.exists(), marker.read_text()


async def test_reads_never_write_the_repository(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")
    (root / "a.txt").write_text("changed\n")
    os.utime(root / "a.txt", (1, 1))
    before = {p: p.read_bytes() for p in (root / ".git").rglob("*") if p.is_file()}

    repository = await _discover(root)
    await repository.run(["diff", "--raw", "-z", "HEAD", "--", "."], deadline=Deadline(10))
    await repository.run(
        ["ls-files", "--others", "--exclude-standard", "-z", "--", "."], deadline=Deadline(10)
    )

    after = {p: p.read_bytes() for p in (root / ".git").rglob("*") if p.is_file()}
    assert after == before


async def test_a_partial_clone_needs_git_that_cannot_fetch_lazily(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repository(tmp_path / "repo")
    _git(root, "config", "remote.origin.url", "https://example.com/r.git")
    _git(root, "config", "remote.origin.promisor", "true")
    monkeypatch.setattr("omnigent.host.workspace_git.runner._NO_LAZY_FETCH_VERSION", (999, 0))

    with pytest.raises(GitError) as refused:
        await GitRepository.discover(root, Deadline(10))

    assert refused.value.problem is GitProblem.PARTIAL_CLONE_NEEDS_NEWER_GIT


async def test_a_slow_git_and_its_descendants_end_at_the_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pids = tmp_path / "pids"
    root = _fake_git(
        tmp_path, monkeypatch, f"sleep 60 & echo $! >> {pids}\necho $$ >> {pids}\nwait\n"
    )
    repository = await _discover(root)

    started = time.monotonic()
    with pytest.raises(GitError) as timed_out:
        await repository.run(["ls-files"], deadline=Deadline(0.5))

    assert timed_out.value.problem is GitProblem.TIMEOUT
    assert time.monotonic() - started < 3
    leftover = [int(pid) for pid in pids.read_text().split()]
    assert len(leftover) == 2
    assert not any(_alive(pid) for pid in leftover)


async def test_output_past_its_cap_is_refused_or_kept_partially(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fake_git(tmp_path, monkeypatch, "yes abcdefgh\n")
    repository = await _discover(root)

    with pytest.raises(GitError) as flooded:
        await repository.run(["ls-files"], deadline=Deadline(10), stdout_bytes=100_000)
    partial = await repository.run_unchecked(
        ["ls-files"], deadline=Deadline(10), stdout_bytes=100_000, allow_partial=True
    )
    chunks: list[bytes] = []
    streamed = await repository.run_unchecked(
        ["ls-files"],
        deadline=Deadline(10),
        stdout_bytes=100_000,
        allow_partial=True,
        stdout_sink=chunks.append,
    )

    assert flooded.value.problem is GitProblem.OUTPUT_TOO_LARGE
    assert partial.overflowed and len(partial.stdout) == 100_000
    # A sink takes the same bytes as they arrive, and none are kept.
    assert streamed.overflowed and streamed.stdout == b""
    assert b"".join(chunks) == partial.stdout


async def test_a_sink_that_fails_ends_git_and_raises_its_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pids = tmp_path / "pids"
    root = _fake_git(
        tmp_path,
        monkeypatch,
        f"echo $$ >> {pids}\nsleep 60 & echo $! >> {pids}\necho a.txt\nwait\n",
    )
    repository = await _discover(root)

    def refuse(_chunk: bytes) -> None:
        raise RuntimeError("the reader refused the output")

    with pytest.raises(RuntimeError, match="the reader refused the output"):
        await repository.run_unchecked(
            ["ls-files"], deadline=Deadline(10), allow_partial=True, stdout_sink=refuse
        )

    leftover = [int(pid) for pid in pids.read_text().split()]
    assert len(leftover) == 2
    assert not any(_alive(pid) for pid in leftover)


async def test_failures_summarize_gits_last_error_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _fake_git(tmp_path, monkeypatch, "echo first >&2\necho 'fatal: last' >&2\nexit 128\n")
    repository = await _discover(root)

    with pytest.raises(GitError) as failed:
        await repository.run(["ls-files"], deadline=Deadline(10))

    assert failed.value.problem is GitProblem.FAILED
    assert failed.value.detail == "fatal: last"


async def test_concurrency_is_held_per_host_and_per_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    running = tmp_path / "running"
    running.mkdir()
    peaks = tmp_path / "peaks"
    root = _fake_git(
        tmp_path,
        monkeypatch,
        f"touch {running}/$$\nls {running} | wc -l >> {peaks}\nsleep 0.3\nrm {running}/$$\n",
    )
    same = await _discover(root)
    others = []
    for index in range(8):
        other = await _discover(root)
        # Each its own repository, as discovery of another common directory gives.
        other._slots = asyncio.Semaphore(2)
        others.append(other)

    await asyncio.gather(*(same.run(["ls-files"], deadline=Deadline(20)) for _ in range(6)))
    one_repository = max(int(line) for line in peaks.read_text().split())
    peaks.unlink()
    await asyncio.gather(*(other.run(["ls-files"], deadline=Deadline(20)) for other in others))
    whole_host = max(int(line) for line in peaks.read_text().split())

    assert one_repository == 2
    assert whole_host == 4


async def test_waiting_for_a_slot_counts_against_the_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gate = tmp_path / "gate"
    root = _fake_git(tmp_path, monkeypatch, f"while [ ! -e {gate} ]; do sleep 0.05; done\n")
    holders = []
    for _ in range(4):
        holder = await _discover(root)
        holder._slots = asyncio.Semaphore(2)
        holders.append(holder)
    queued = await _discover(root)
    queued._slots = asyncio.Semaphore(2)
    blocking = [
        asyncio.create_task(holder.run(["ls-files"], deadline=Deadline(20))) for holder in holders
    ]
    await asyncio.sleep(0.5)

    started = time.monotonic()
    with pytest.raises(GitError) as timed_out:
        await queued.run(["ls-files"], deadline=Deadline(0.5))
    waited = time.monotonic() - started
    gate.touch()
    await asyncio.gather(*blocking)

    assert timed_out.value.problem is GitProblem.TIMEOUT
    assert waited < 2


async def test_cancelling_twice_still_kills_and_reaps_the_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pids = tmp_path / "pids"
    root = _fake_git(
        tmp_path, monkeypatch, f"sleep 60 & echo $! >> {pids}\necho $$ >> {pids}\nwait\n"
    )
    repository = await _discover(root)
    call = asyncio.create_task(repository.run(["ls-files"], deadline=Deadline(30)))
    while not pids.exists() or len(pids.read_text().split()) < 2:
        await asyncio.sleep(0.02)

    call.cancel()
    await asyncio.sleep(0)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    await asyncio.gather(*runner._cleanups)

    leftover = [int(pid) for pid in pids.read_text().split()]
    assert not any(_alive(pid) for pid in leftover)
    # Reaped: the leader isn't left as a zombie of this process.
    with pytest.raises(ChildProcessError):
        os.waitpid(leftover[1], os.WNOHANG)


@pytest.mark.parametrize(
    "refusal",
    [
        "fatal: detected dubious ownership in repository at '/srv/w'",
        "fatal: this operation must be run in a work tree",
    ],
)
async def test_a_directory_git_refuses_to_treat_as_a_working_tree_has_no_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refusal: str
) -> None:
    """Callers then search the directory without Git, as the REST search does."""
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    (bin_directory / "git").write_text(f'#!/bin/sh\necho "{refusal}" >&2\nexit 128\n')
    (bin_directory / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_directory}{os.pathsep}{os.environ['PATH']}")

    assert await GitRepository.discover(tmp_path, Deadline(10)) is None


async def test_log_never_runs_a_configured_textconv_or_external_diff(tmp_path: Path) -> None:
    root = _repository(tmp_path / "repo")
    marker = tmp_path / "ran"
    (root / ".gitattributes").write_text("*.txt diff=x\n")
    (root / "a.txt").write_text("second\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "second")
    # Configured after committing, so only the runner's own log could run them.
    _git(root, "config", "diff.x.textconv", f"sh -c 'echo textconv >> {marker}; cat \"$0\"'")
    _git(root, "config", "diff.external", f"sh -c 'echo external >> {marker}'")

    repository = await _discover(root)
    patch = await repository.run(["log", "-p", "-2", "--", "."], deadline=Deadline(10))

    assert b"+second" in patch
    assert not marker.exists(), marker.read_text()


async def test_a_git_planted_in_the_workspace_never_runs_from_a_relative_path_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repository(tmp_path / "repo")
    marker = tmp_path / "planted-ran"
    (root / "git").write_text(f"#!/bin/sh\ntouch {marker}\nexit 1\n")
    (root / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f".{os.pathsep}{os.pathsep}{os.environ['PATH']}")

    repository = await _discover(root)
    listed = await repository.run(["ls-files", "-z"], deadline=Deadline(10))

    assert b"a.txt" in listed
    assert not marker.exists()


def _guard_signals(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record each process group the runner signals whose leader was already reaped."""
    real_killpg = os.killpg
    reaped: list[int] = []

    def killpg(pid: int, sig: int) -> None:
        try:
            real_killpg(pid, 0)
        except ProcessLookupError:
            # The group id may already belong to another process.
            reaped.append(pid)
            raise
        real_killpg(pid, sig)

    monkeypatch.setattr("omnigent.host.workspace_git.runner.os.killpg", killpg)
    return reaped


async def test_a_reaped_git_is_never_signalled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Once Git is reaped, its process id may belong to another process."""
    root = _repository(tmp_path / "repo")
    repository = await _discover(root)
    reaped = _guard_signals(monkeypatch)

    await repository.run(["ls-files", "-z"], deadline=Deadline(10))

    assert reaped == []


_WATCHERS = {
    "pidfd": runner._pidfd_exit,
    "kqueue": runner._kqueue_exit,
    "poll": runner._polled_exit,
}


def _force_watcher(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    if _WATCHERS[name] not in runner._available_exit_watchers():
        pytest.skip(f"this host has no {name} exit watcher")
    monkeypatch.setattr(runner, "_EXIT_WATCHERS", [_WATCHERS[name]])


def _open_descriptors() -> int:
    return len(os.listdir("/dev/fd"))


@pytest.mark.parametrize("watcher", ["pidfd", "kqueue", "poll"])
async def test_descendants_end_with_git_even_when_git_exits_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, watcher: str
) -> None:
    _force_watcher(monkeypatch, watcher)
    pids = tmp_path / "pids"
    root = _fake_git(
        tmp_path,
        monkeypatch,
        f"sleep 60 </dev/null >/dev/null 2>&1 & echo $! >> {pids}\necho done\n",
    )
    repository = await _discover(root)

    output = await repository.run(["ls-files"], deadline=Deadline(10))

    assert output == b"done\n"
    descendant = int(pids.read_text().split()[-1])
    try:
        assert not _alive(descendant)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(descendant, signal.SIGKILL)


def test_exit_watchers_are_chosen_by_what_the_host_offers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delattr(os, "pidfd_open", raising=False)
    monkeypatch.delattr(select, "kqueue", raising=False)

    expected = [runner._polled_exit] if hasattr(os, "waitid") else []
    assert runner._available_exit_watchers() == expected


@pytest.mark.parametrize("refusal", [errno.ENOSYS, errno.EPERM])
async def test_a_refused_pidfd_falls_back_to_polling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refusal: int
) -> None:
    """A sandbox or an old kernel may refuse pidfds; Git's exit is still seen, unreaped."""
    _force_watcher(monkeypatch, "pidfd")
    monkeypatch.setattr(runner, "_EXIT_WATCHERS", [runner._pidfd_exit, runner._polled_exit])

    def refuse(pid: int) -> int:
        raise OSError(refusal, os.strerror(refusal))

    monkeypatch.setattr(os, "pidfd_open", refuse)
    pids = tmp_path / "pids"
    root = _fake_git(
        tmp_path,
        monkeypatch,
        f"sleep 60 </dev/null >/dev/null 2>&1 & echo $! >> {pids}\necho done\n",
    )
    repository = await _discover(root)

    output = await repository.run(["ls-files"], deadline=Deadline(10))

    assert output == b"done\n"
    assert not _alive(int(pids.read_text().split()[-1]))


@pytest.mark.parametrize(
    ("watcher", "failure"),
    [("pidfd", "register"), ("kqueue", "register"), ("kqueue", "subscribe")],
)
async def test_a_watcher_that_fails_to_start_closes_its_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, watcher: str, failure: str
) -> None:
    """With no watcher left, Git is unavailable, and its group is still killed and reaped."""
    _force_watcher(monkeypatch, watcher)
    pids = tmp_path / "pids"
    root = _fake_git(
        tmp_path,
        monkeypatch,
        f"sleep 60 </dev/null >/dev/null 2>&1 & echo $! >> {pids}\necho $$ >> {pids}\n",
    )
    repository = await _discover(root)
    if failure == "register":

        def refuse(descriptor: int, callback: object) -> None:
            raise OSError(errno.EPERM, "refused")

        monkeypatch.setattr(asyncio.get_running_loop(), "add_reader", refuse)
    else:
        # An unknown filter makes kqueue refuse the subscription with EINVAL; only
        # the exit watcher uses this filter, so the event loop's own kqueue is unharmed.
        monkeypatch.setattr(select, "KQ_FILTER_PROC", -100)
    # Held here, so only the watcher itself can close them.
    queues: list[Any] = []
    if watcher == "kqueue":
        # pyrefly: ignore[missing-attribute]  # macOS and BSD only
        real_kqueue = select.kqueue

        def tracked_kqueue() -> Any:
            queues.append(real_kqueue())
            return queues[-1]

        monkeypatch.setattr(select, "kqueue", tracked_kqueue)
    before = _open_descriptors()

    for _ in range(20):
        with pytest.raises(GitError) as refused:
            await repository.run(["ls-files"], deadline=Deadline(10))
        assert refused.value.problem == GitProblem.UNAVAILABLE

    assert _open_descriptors() - before < 5
    assert all(queue.closed for queue in queues)
    assert not any(_alive(int(pid), within_s=0.5) for pid in pids.read_text().split())


async def test_partial_output_never_signals_a_reaped_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Git may finish writing and exit before the runner sees the output pass its cap."""
    root = _fake_git(tmp_path, monkeypatch, "head -c 5000 /dev/zero\n")
    repository = await _discover(root)
    reaped = _guard_signals(monkeypatch)
    real_read_capped = runner._read_capped

    async def read_after_git_exits(
        stream: asyncio.StreamReader, limit: int, *rest: object
    ) -> tuple[bytes, bool]:
        await asyncio.sleep(0.5)
        return await real_read_capped(stream, limit, *rest)  # type: ignore[arg-type]

    monkeypatch.setattr(runner, "_read_capped", read_after_git_exits)

    output = await repository.run_unchecked(
        ["ls-files"], deadline=Deadline(10), stdout_bytes=1000, allow_partial=True
    )

    assert output.overflowed and len(output.stdout) == 1000
    assert reaped == []
