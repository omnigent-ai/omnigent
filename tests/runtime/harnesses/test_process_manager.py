"""
Tests for :class:`omnigent.runtime.harnesses.process_manager.HarnessProcessManager`.

Covers the lifecycle / behavior surface defined by §Process
management of ``designs/SERVER_HARNESS_CONTRACT.md``: lazy spawn,
caching, crash detection, release, idle reaping, orphan sweep.

The tests use the real fixture harness module
``tests.runtime.harnesses._test_harness`` (a minimal FastAPI app
with ``/health`` + introspection endpoints) wired through a real
runner subprocess. End-to-end with real uvicorn rather than a mock
because the spawn handshake (waiting for the socket to appear) is
what's most likely to break.

Tests intentionally use a short tmp parent dir (``/tmp/omnigent-tests-...``)
rather than pytest's :data:`tmp_path` because macOS's
``AF_UNIX`` socket path limit is ~104 chars and pytest's
per-test temp dirs (under ``/private/var/folders/...``) push the
full socket path past that ceiling. Each test cleans up its
parent dir on teardown.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import sys
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from omnigent.runtime.harnesses import _HARNESS_MODULES
from omnigent.runtime.harnesses.process_manager import (
    _AP_PID_FILE,
    _TMP_PARENT_ENV_VAR,
    HarnessProcessManager,
    _pid_alive,
    _pids_holding_socket,
)

_TEST_HARNESS_NAME = "test"
_TEST_HARNESS_MODULE = "tests.runtime.harnesses._test_harness"


@pytest.fixture
def register_test_harness() -> Iterator[None]:
    """
    Add the test fixture harness to ``_HARNESS_MODULES`` for the
    test, removing it on teardown so other tests see a clean
    registry.

    :yields: Nothing — fixture exists for the side effect.
    """
    _HARNESS_MODULES[_TEST_HARNESS_NAME] = _TEST_HARNESS_MODULE
    try:
        yield
    finally:
        _HARNESS_MODULES.pop(_TEST_HARNESS_NAME, None)


@pytest.fixture
def short_tmp_parent() -> Iterator[Path]:
    """
    Per-test parent directory under a short writable temp root.

    macOS limits AF_UNIX socket paths to ~104 characters; pytest's
    :data:`tmp_path` resolves to ``/private/var/folders/...``
    which already eats most of that budget. Use a short
    ``/tmp/omni-pm-<short_uuid>`` parent when possible, falling
    back to :func:`tempfile.gettempdir` for sandboxes where the
    host's real ``/tmp`` is not writable.
    """
    roots = [Path("/tmp")]
    temp_root = Path(tempfile.gettempdir())
    if temp_root not in roots:
        roots.append(temp_root)

    last_error: OSError | None = None
    for root in roots:
        parent = root / f"omni-pm-{uuid.uuid4().hex[:8]}"
        try:
            parent.mkdir(mode=0o700)
        except OSError as exc:
            last_error = exc
            continue
        try:
            yield parent
        finally:
            shutil.rmtree(parent, ignore_errors=True)
        return

    assert last_error is not None
    raise last_error


@pytest.fixture
def manager(
    short_tmp_parent: Path,
    register_test_harness: None,
) -> HarnessProcessManager:
    """
    Manager rooted in an isolated tmp dir, with the test harness
    pre-registered.

    Tests own ``await manager.start()`` / ``shutdown()`` so they
    can assert state across the lifecycle.
    """
    return HarnessProcessManager(
        # Aggressive defaults so reaper / orphan tests don't have
        # to wait minutes. Individual tests override per-case.
        idle_timeout_s=60.0,
        reaper_interval_s=60.0,
        tmp_parent=short_tmp_parent,
    )


# ── Boot / shutdown ─────────────────────────────────────────────


async def test_get_client_before_start_raises(
    manager: HarnessProcessManager,
) -> None:
    """Calling get_client before start() is a programming error.

    Catches a regression where the lazy-init path silently
    initialized state without booting the reaper — the reaper
    not running would then leak subprocesses indefinitely.
    """
    with pytest.raises(RuntimeError, match="before start"):
        await manager.get_client("conv_x", _TEST_HARNESS_NAME)


async def test_start_creates_instance_dir_with_sentinel(
    manager: HarnessProcessManager,
) -> None:
    """start() creates the per-AP-instance dir and writes AP_PID.

    The sentinel is what the orphan sweep keys off; without it,
    a subsequent Omnigent boot can't tell live instances from dead.
    """
    await manager.start()
    try:
        assert manager.instance_dir.is_dir()
        sentinel = manager.instance_dir / _AP_PID_FILE
        assert sentinel.exists()
        # The recorded PID is this process — proves the sweep on
        # a sibling Omnigent boot would correctly identify us as alive.
        assert sentinel.read_text(encoding="utf-8").strip() == str(os.getpid())
    finally:
        await manager.shutdown()


async def test_start_is_idempotent(manager: HarnessProcessManager) -> None:
    """A second start() is a no-op; doesn't recreate / relaunch.

    Lifespan handlers sometimes fire start() more than once
    during AP's startup; the second call must not clobber state.
    """
    await manager.start()
    try:
        first_dir = manager.instance_dir
        await manager.start()
        # Same dir, still has sentinel, still serves clients.
        assert manager.instance_dir == first_dir
        assert (manager.instance_dir / _AP_PID_FILE).exists()
    finally:
        await manager.shutdown()


async def test_start_uses_harness_tmp_parent_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The production default socket root can be moved by env.

    Local and hosted runtimes sometimes deny writes to the host's
    real ``/tmp``. The process manager should honor the deployment
    knob without requiring the FastAPI server to thread a test-only
    constructor argument through production wiring.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(_TMP_PARENT_ENV_VAR, "harness-sockets")
    manager = HarnessProcessManager()
    await manager.start()
    try:
        assert manager.instance_dir.parent == Path("harness-sockets")
        assert (manager.instance_dir / _AP_PID_FILE).read_text(encoding="utf-8")
    finally:
        await manager.shutdown()


async def test_shutdown_without_start_is_noop(
    manager: HarnessProcessManager,
) -> None:
    """shutdown() before start() should not raise.

    Defensive — AP's lifespan teardown might run after a failed
    boot where start() never completed. shutdown() should be
    safe to call regardless.
    """
    # No start(); just shutdown.
    await manager.shutdown()


# ── Spawn / cache / crash ──────────────────────────────────────


async def _ping_health(client) -> None:  # type: ignore[no-untyped-def]
    """Drive a /health round-trip; raises on non-200 to fail the test."""
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_get_client_spawns_and_serves(
    manager: HarnessProcessManager,
) -> None:
    """First get_client spawns the runner; /health round-trips.

    End-to-end smoke test through the real runner. If this fails
    most likely culprits are: PYTHONPATH not propagating to the
    subprocess (the test fixture module won't import); the spawn
    handshake (socket appearance polling) breaking; or the
    runner CLI plumbing breaking.
    """
    await manager.start()
    try:
        client = await manager.get_client("conv_a", _TEST_HARNESS_NAME)
        await _ping_health(client)
        # The runner stashed the conversation id on app.state per
        # the contract; verify the round-trip works.
        cid_resp = await client.get("/conversation-id")
        assert cid_resp.json() == {"conversation_id": "conv_a"}
    finally:
        await manager.shutdown()


async def test_get_client_caches_subprocess(
    manager: HarnessProcessManager,
) -> None:
    """Subsequent get_client calls reuse the same subprocess.

    Verified by hitting /pid twice and asserting the same PID —
    if the manager were respawning per call, the PIDs would
    differ. This test exists because spawn cost is non-trivial
    (1–3s per uvicorn boot) and the contract explicitly says
    one subprocess per conversation, lazy.
    """
    await manager.start()
    try:
        client_a = await manager.get_client("conv_a", _TEST_HARNESS_NAME)
        pid_first = (await client_a.get("/pid")).json()["pid"]
        # Same conv_id again — should hit the cached client.
        client_a_again = await manager.get_client("conv_a", _TEST_HARNESS_NAME)
        assert client_a_again is client_a
        pid_second = (await client_a_again.get("/pid")).json()["pid"]
        # Same subprocess proves no respawn happened on the
        # second call.
        assert pid_first == pid_second
    finally:
        await manager.shutdown()


async def test_get_client_isolates_per_conversation(
    manager: HarnessProcessManager,
) -> None:
    """Different conversations get different subprocesses.

    Each conversation_id maps to its own subprocess per the
    contract (one-conversation-one-process). The isolation is
    enforced by separate Unix sockets keyed on conv_id; this
    test verifies the keying actually hits distinct PIDs.
    """
    await manager.start()
    try:
        client_a = await manager.get_client("conv_a", _TEST_HARNESS_NAME)
        client_b = await manager.get_client("conv_b", _TEST_HARNESS_NAME)
        assert client_a is not client_b
        pid_a = (await client_a.get("/pid")).json()["pid"]
        pid_b = (await client_b.get("/pid")).json()["pid"]
        # Different subprocesses → different PIDs. If they matched
        # we'd be running both conversations through one process,
        # violating the isolation guarantee.
        assert pid_a != pid_b
    finally:
        await manager.shutdown()


async def test_release_terminates_subprocess(
    manager: HarnessProcessManager,
) -> None:
    """release() ends the subprocess and cleans up the socket.

    Verifies both halves of the teardown contract: the OS
    process exits AND the socket file is removed. Either gap
    leaks resources across conversation lifetimes.
    """
    await manager.start()
    try:
        client = await manager.get_client("conv_a", _TEST_HARNESS_NAME)
        # Capture the subprocess PID via the introspection endpoint;
        # if release worked, the PID won't be alive after.
        pid = (await client.get("/pid")).json()["pid"]
        socket_path = manager.instance_dir / "conv-conv_a.sock"
        assert socket_path.exists()
        await manager.release("conv_a")
        # Socket cleanup is part of release's contract — leaving
        # the file behind would fail uvicorn binding on a
        # subsequent spawn for the same conv id.
        assert not socket_path.exists()
        # Process should be gone — give the OS a brief moment
        # since SIGTERM → wait is async.
        for _ in range(20):
            if not _pid_alive(pid):
                break
            await asyncio.sleep(0.05)
        assert not _pid_alive(pid)
    finally:
        await manager.shutdown()


async def test_get_client_respawns_after_crash(
    manager: HarnessProcessManager,
) -> None:
    """If the subprocess died, the next get_client respawns.

    This covers the "harness crashed mid-conversation" branch in
    get_client. We simulate the crash by sending SIGKILL to the
    runner subprocess directly, then call get_client again and
    verify a new PID comes back.
    """
    await manager.start()
    try:
        client = await manager.get_client("conv_a", _TEST_HARNESS_NAME)
        original_pid = (await client.get("/pid")).json()["pid"]
        os.kill(original_pid, signal.SIGKILL)
        # Wait for the OS to mark the process dead so the next
        # get_client's ``returncode`` check sees it.
        for _ in range(40):
            if not _pid_alive(original_pid):
                break
            await asyncio.sleep(0.05)
        # Now get_client should detect the corpse and respawn.
        new_client = await manager.get_client("conv_a", _TEST_HARNESS_NAME)
        new_pid = (await new_client.get("/pid")).json()["pid"]
        # New PID proves a fresh subprocess; same PID would mean
        # crash detection is broken.
        assert new_pid != original_pid
        assert _pid_alive(new_pid)
    finally:
        await manager.shutdown()


async def test_get_client_respawns_on_harness_change(
    manager: HarnessProcessManager,
) -> None:
    """A different harness for the same conversation respawns the subprocess.

    The socket is keyed by conversation only, so without the harness-change
    branch in get_client an in-place agent switch (which resolves a new
    harness for the same conv) would keep serving the OLD harness's
    subprocess. We register a second harness name pointing at the same
    fixture app, call get_client with each, and assert the PID changed —
    proving the old subprocess was torn down and a new one spawned.
    """
    # Second registry entry → same fixture app, different harness NAME.
    _HARNESS_MODULES["test2"] = _TEST_HARNESS_MODULE
    await manager.start()
    try:
        client_first = await manager.get_client("conv_a", _TEST_HARNESS_NAME)
        pid_first = (await client_first.get("/pid")).json()["pid"]

        # Same conversation, DIFFERENT harness → must respawn.
        client_second = await manager.get_client("conv_a", "test2")
        pid_second = (await client_second.get("/pid")).json()["pid"]

        # Different PID proves the harness-change branch tore down the old
        # subprocess and spawned a new one. Same PID would mean the switch
        # kept serving the old harness (the bug this branch fixes).
        assert pid_second != pid_first
        assert _pid_alive(pid_second)
        # The original subprocess was terminated by the respawn's close.
        for _ in range(40):
            if not _pid_alive(pid_first):
                break
            await asyncio.sleep(0.05)
        assert not _pid_alive(pid_first)
    finally:
        await manager.shutdown()
        _HARNESS_MODULES.pop("test2", None)


async def test_get_client_any_harness_sentinel_reuses_subprocess(
    manager: HarnessProcessManager,
) -> None:
    """``get_client(conv, "any")`` reuses the live subprocess — never respawns.

    ``"any"`` is the harness-AGNOSTIC sentinel that steering / cancel /
    interrupt callers pass to reach the already-running subprocess (it is not
    a real harness). It must NOT count as a harness mismatch — otherwise the
    harness-change branch tears down and respawns the harness on every such
    call, killing the in-flight turn. That is the regression that broke
    queued-message streaming: sending a 2nd message mid-turn issues a
    ``get_client(conv, "any")`` steering call, which respawned the live
    openai-agents subprocess and left the assistant with no output.
    """
    await manager.start()
    try:
        client_first = await manager.get_client("conv_a", _TEST_HARNESS_NAME)
        pid_first = (await client_first.get("/pid")).json()["pid"]

        # Harness-agnostic sentinel → must hit the cached client, not respawn.
        client_any = await manager.get_client("conv_a", "any")
        assert client_any is client_first
        pid_any = (await client_any.get("/pid")).json()["pid"]
        # Same PID proves no respawn. A different PID means the "any" sentinel
        # spuriously tripped the harness-change branch (the bug this guards).
        assert pid_any == pid_first
        assert _pid_alive(pid_first)
    finally:
        await manager.shutdown()


async def test_get_client_concurrent_first_calls_share_subprocess(
    manager: HarnessProcessManager,
) -> None:
    """Concurrent first get_client calls don't race two subprocesses.

    The per-conversation spawn lock should serialize the lazy-init
    window so only one subprocess gets created. Verified by
    issuing two get_client calls concurrently from different
    asyncio tasks and asserting the same client instance comes
    back from both.
    """
    await manager.start()
    try:
        # asyncio.gather schedules both calls before either has a
        # chance to populate the cache — exercises the spawn
        # lock's serialization.
        client_a, client_b = await asyncio.gather(
            manager.get_client("conv_a", _TEST_HARNESS_NAME),
            manager.get_client("conv_a", _TEST_HARNESS_NAME),
        )
        # Identity equality proves only one entry was created;
        # without the lock, the second call would race a second
        # subprocess and either race to bind the same socket
        # (one fails) or succeed with two distinct entries.
        assert client_a is client_b
    finally:
        await manager.shutdown()


# ── Idle reaping ───────────────────────────────────────────────


async def test_idle_reaper_releases_stale_entries(
    register_test_harness: None,
    short_tmp_parent: Path,
) -> None:
    """An entry untouched past idle_timeout_s gets reaped.

    Sets a short idle timeout and a fast reaper interval so the
    test completes promptly without letting the reaper kill the
    subprocess before the setup health probe has completed. After
    reaping, the conversation is no longer registered and its
    socket file is gone.
    """
    fast = HarnessProcessManager(
        idle_timeout_s=2.0,
        reaper_interval_s=0.1,
        tmp_parent=short_tmp_parent,
    )
    await fast.start()
    try:
        await fast.get_client("conv_a", _TEST_HARNESS_NAME)
        # No HTTP ping: with idle_timeout_s=0.0 the reaper can fire during an
        # inline HTTP call and yank the client mid-request. Socket-existence
        # loop below is the real "entry was reaped" assertion.
        socket_path = fast.instance_dir / "conv-conv_a.sock"
        assert socket_path.exists()
        # Wait long enough for the 2s idle window plus multiple
        # reaper passes. A 0s timeout races with subprocess startup
        # under CI load and can close the client before the socket is
        # ready to service requests.
        for _ in range(60):
            if not socket_path.exists():
                break
            await asyncio.sleep(0.1)
        # If this assertion flips, the reaper isn't running OR
        # isn't acting on stale entries — both regressions in
        # the contract.
        assert not socket_path.exists()
    finally:
        await fast.shutdown()


async def test_orphan_sweep_removes_dead_omnigent_dirs(
    short_tmp_parent: Path,
) -> None:
    """A sibling Omnigent dir with a non-running PID gets cleaned.

    Plants a fake AP-instance dir under tmp_parent with an
    AP_PID sentinel pointing at a non-running PID, then boots a
    fresh manager. start() runs the orphan sweep, which should
    remove the dead dir while leaving its own intact.

    Uses ``99999999`` as the non-running PID — a valid integer
    that's almost certainly not allocated. If this becomes
    flaky on a future host with that pid, increase the value.
    """
    fake_dir = short_tmp_parent / "ap-deaduuid"
    fake_dir.mkdir(mode=0o700)
    (fake_dir / _AP_PID_FILE).write_text("99999999", encoding="utf-8")
    # Plant a stale socket file too so the sweep has something to
    # try-and-clean (no live runner to kill, but the dir removal
    # path is what matters).
    (fake_dir / "conv-orphan.sock").write_text("", encoding="utf-8")

    fresh = HarnessProcessManager(tmp_parent=short_tmp_parent)
    await fresh.start()
    try:
        # The dead sibling dir is gone; ours is fresh.
        assert not fake_dir.exists()
        assert fresh.instance_dir.exists()
    finally:
        await fresh.shutdown()


async def test_orphan_sweep_preserves_live_omnigent_dirs(
    short_tmp_parent: Path,
) -> None:
    """A sibling Omnigent dir with a live PID is left alone.

    Plants a fake AP-instance dir whose AP_PID sentinel points at
    *this test process* (which is live by definition). The
    sweep on a fresh manager's start() should leave the sibling
    intact — that's the zero-downtime-restart / multi-tenant
    isolation guarantee from §Process management.
    """
    sibling_dir = short_tmp_parent / "ap-livepid"
    sibling_dir.mkdir(mode=0o700)
    (sibling_dir / _AP_PID_FILE).write_text(str(os.getpid()), encoding="utf-8")

    fresh = HarnessProcessManager(tmp_parent=short_tmp_parent)
    await fresh.start()
    try:
        # If sweep removed the sibling, a concurrent Omnigent would
        # have its dir deleted out from under it — exactly the
        # bug the live-PID check is meant to prevent.
        assert sibling_dir.exists()
        assert (sibling_dir / _AP_PID_FILE).exists()
    finally:
        await fresh.shutdown()


# ── Helper-level tests (small, fast) ───────────────────────────


def test_pid_alive_for_self() -> None:
    """The running process is, by definition, alive.

    Sanity check that the helper is wired up correctly — if this
    flips, the orphan-sweep "is sibling alive?" check is broken.
    """
    assert _pid_alive(os.getpid())


def test_pid_alive_for_unallocated_pid() -> None:
    """An almost-certainly-unallocated PID is not alive.

    Uses ``99999999`` as a sentinel that's a valid integer pid
    but vanishingly unlikely to exist. If this becomes flaky
    raise the number.
    """
    assert not _pid_alive(99999999)


async def test_pids_holding_socket_returns_empty_for_missing(
    short_tmp_parent: Path,
) -> None:
    """``lsof`` against a nonexistent socket yields no PIDs.

    Locks in the best-effort contract: the orphan sweep must
    not crash if a socket file disappears between glob and
    lookup. Empty list is the expected result.
    """
    nonexistent = short_tmp_parent / "no-such.sock"
    pids = await _pids_holding_socket(nonexistent)
    assert pids == []


async def test_pids_holding_socket_returns_empty_when_lsof_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    short_tmp_parent: Path,
) -> None:
    """Missing ``lsof`` is best-effort cleanup noise, not a boot failure."""

    async def missing_lsof(*_args: object, **_kwargs: object) -> object:
        raise FileNotFoundError(2, "No such file or directory", "lsof")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", missing_lsof)

    pids = await _pids_holding_socket(short_tmp_parent / "conv-stale.sock")
    assert pids == []


# ── Per-spawn env override ─────────────────────────────────────


async def test_get_client_env_override_propagates_to_subprocess(
    manager: HarnessProcessManager,
) -> None:
    """``get_client(env=...)`` threads env vars into the spawned subprocess.

    Verifies the v1 spec-config flow: Omnigent passes per-spec env vars
    via ``env`` to ``get_client``, and the spawned subprocess
    sees them in its own ``os.environ``. Without this propagation,
    Omnigent would have to mutate its own ``os.environ`` (which races
    across concurrent conversations with different specs).
    """
    await manager.start()
    try:
        client = await manager.get_client(
            "conv_env",
            _TEST_HARNESS_NAME,
            env={"HARNESS_TEST_CUSTOM": "marker_alpha"},
        )
        # Subprocess saw the override in its env.
        resp = await client.get("/env/HARNESS_TEST_CUSTOM")
        assert resp.json() == {"value": "marker_alpha"}
    finally:
        await manager.shutdown()


async def test_get_client_env_override_is_per_conversation(
    manager: HarnessProcessManager,
) -> None:
    """Different conversations get their own ``env`` overrides.

    Spawns two subprocesses with different env values for the
    same env-var name; each subprocess should see ITS OWN value.
    Without per-spawn isolation, AP's only option would be to
    mutate ``os.environ``, which would race when concurrent
    conversations want different config — the failure mode this
    test guards against.
    """
    await manager.start()
    try:
        client_a = await manager.get_client(
            "conv_a",
            _TEST_HARNESS_NAME,
            env={"HARNESS_TEST_CUSTOM": "alpha"},
        )
        client_b = await manager.get_client(
            "conv_b",
            _TEST_HARNESS_NAME,
            env={"HARNESS_TEST_CUSTOM": "beta"},
        )
        resp_a = await client_a.get("/env/HARNESS_TEST_CUSTOM")
        resp_b = await client_b.get("/env/HARNESS_TEST_CUSTOM")
        assert resp_a.json()["value"] == "alpha"
        assert resp_b.json()["value"] == "beta"
    finally:
        await manager.shutdown()


# ── Runner subprocess cleanup ──────────────────────


async def test_runner_subprocess_exits_on_sigterm(
    manager: HarnessProcessManager,
) -> None:
    """A harness runner exits promptly after a plain SIGTERM.

    This catches the failure mode where ``pkill`` left
    ``omnigent.runtime.harnesses._runner`` processes alive because
    shutdown never reached uvicorn's normal exit path.
    """
    await manager.start()
    try:
        client = await manager.get_client("conv_sigterm", _TEST_HARNESS_NAME)
        runner_pid = (await client.get("/pid")).json()["pid"]
        os.kill(runner_pid, signal.SIGTERM)

        for _ in range(60):
            if not _pid_alive(runner_pid):
                break
            await asyncio.sleep(0.1)
        assert not _pid_alive(runner_pid)
    finally:
        await manager.shutdown()


async def test_runner_subprocess_exits_when_spawning_parent_exits(
    short_tmp_parent: Path,
    register_test_harness: None,
) -> None:
    """A harness runner exits when its spawning parent process exits.

    The helper process below starts a real ``HarnessProcessManager``
    and real ``_runner`` child, then exits without shutting the
    manager down. The runner's ``--parent-pid`` watchdog should
    observe OS reparenting and terminate itself, preventing the
    orphan accumulation.
    """
    import subprocess
    import textwrap

    parent_script = textwrap.dedent(
        f"""
        import asyncio
        import os
        import pathlib
        from omnigent.runtime.harnesses import _HARNESS_MODULES
        from omnigent.runtime.harnesses.process_manager import HarnessProcessManager

        async def main():
            _HARNESS_MODULES[{_TEST_HARNESS_NAME!r}] = {_TEST_HARNESS_MODULE!r}
            mgr = HarnessProcessManager(tmp_parent=pathlib.Path({str(short_tmp_parent)!r}))
            await mgr.start()
            client = await mgr.get_client('conv_parent_death', {_TEST_HARNESS_NAME!r})
            pid = (await client.get('/pid')).json()['pid']
            print(pid, flush=True)
            os._exit(0)

        asyncio.run(main())
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", parent_script],
        check=True,
        text=True,
        capture_output=True,
        timeout=10,
        env={**os.environ, "PYTHONPATH": os.getcwd()},
    )
    runner_pid = int(proc.stdout.strip().splitlines()[-1])

    try:
        for _ in range(60):
            if not _pid_alive(runner_pid):
                break
            await asyncio.sleep(0.1)
        assert not _pid_alive(runner_pid)
    finally:
        if _pid_alive(runner_pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(runner_pid, signal.SIGKILL)


async def test_runner_subprocess_hard_exits_when_sigterm_shutdown_wedges(
    manager: HarnessProcessManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A SIGTERM'ed runner hard-exits even if uvicorn shutdown wedges.

    The fixture starts a background task that ignores cancellation,
    which prevents uvicorn's graceful shutdown from reaching lifespan
    teardown. This exercises the fallback for plain
    ``pkill -f omnigent.runtime.harnesses._runner``: the runner
    should not remain alive forever just because graceful shutdown is
    stuck.
    """
    monkeypatch.setenv("OMNIGENT_HARNESS_SHUTDOWN_TIMEOUT_S", "0.2")
    monkeypatch.setenv("OMNIGENT_HARNESS_HARD_EXIT_TIMEOUT_S", "0.5")
    await manager.start()
    try:
        client = await manager.get_client("conv_stuck_sigterm", _TEST_HARNESS_NAME)
        runner_pid = (await client.get("/pid")).json()["pid"]
        resp = await client.get("/stuck-shutdown")
        assert resp.json()["status"] == "stuck_task_started"
        os.kill(runner_pid, signal.SIGTERM)

        for _ in range(40):
            if not _pid_alive(runner_pid):
                break
            await asyncio.sleep(0.1)
        assert not _pid_alive(runner_pid)
    finally:
        await manager.shutdown()


async def test_orphan_sweep_escalates_to_sigkill(
    short_tmp_parent: Path,
    register_test_harness: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Orphan sweep SIGKILLs runners that survive SIGTERM."""
    from omnigent.runtime.harnesses import process_manager as pm_mod

    killed: list[tuple[int, signal.Signals]] = []
    calls = 0

    async def fake_pids_holding_socket(socket_path: Path) -> list[int]:
        assert socket_path.name == "conv-stale.sock"
        return [12345]

    def fake_pid_alive(pid: int) -> bool:
        assert pid == 12345
        return True

    def fake_kill(pid: int, sig: signal.Signals) -> None:
        nonlocal calls
        calls += 1
        assert pid == 12345
        killed.append((pid, sig))

    monkeypatch.setattr(pm_mod, "_ORPHAN_SIGTERM_GRACE_S", 0)
    monkeypatch.setattr(pm_mod, "_pids_holding_socket", fake_pids_holding_socket)
    monkeypatch.setattr(pm_mod, "_pid_alive", fake_pid_alive)
    monkeypatch.setattr(pm_mod.os, "kill", fake_kill)

    instance_dir = short_tmp_parent / "ap-dead"
    instance_dir.mkdir()
    (instance_dir / "conv-stale.sock").touch()

    mgr = HarnessProcessManager(tmp_parent=short_tmp_parent)
    await mgr._kill_orphan_runners(instance_dir)

    assert calls == 2
    assert killed == [(12345, signal.SIGTERM), (12345, signal.SIGKILL)]
