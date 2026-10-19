"""Concurrent Claude bridge writers must not lose each other's JSON updates."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native.bridge import (
    prepare_bridge_dir,
    read_active_session_id,
    read_claude_session_id,
    read_launch_model,
    read_model_env,
    read_model_picker_values,
    read_seen_claude_session_ids,
    read_transcript_path,
    record_hook_event,
    record_model_vocabulary,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WRITER_TIMEOUT_S = 30.0
# Expected to elapse under correct locking: the competing writer stays blocked
# until the paused process resumes. The window only lets an unlocked write land.
_COMPETING_WRITER_WAIT_S = 1.0

_PAUSED_WRITER_SCRIPT = r"""
import json
import sys
import time
from pathlib import Path

from omnigent.harnesses.claude_native import bridge

args = json.loads(sys.argv[1])
root = Path(args["bridge_root"])
bridge._TRUSTED_PARENT = root
bridge._BRIDGE_ROOT = root
bridge_dir = Path(args["bridge_dir"])
read_done = Path(args["read_done"])
resume = Path(args["resume"])
original_read = bridge._read_json_file
paused = False


def paused_read(path):
    global paused
    value = original_read(path)
    if path.name == args["pause_on"] and not paused:
        paused = True
        read_done.touch()
        deadline = time.monotonic() + args["timeout_s"]
        while not resume.exists():
            if time.monotonic() > deadline:
                raise SystemExit("resume marker never appeared")
            time.sleep(0.01)
    return value


bridge._read_json_file = paused_read
if args["writer"] == "record_hook_event":
    bridge.record_hook_event(bridge_dir, args["payload"])
elif args["writer"] == "write_active_session_id":
    bridge.write_active_session_id(bridge_dir, args["session_id"])
else:
    raise SystemExit(f"unknown writer {args['writer']!r}")
"""


@pytest.fixture(autouse=True)
def _trust_tmp_bridge_parent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._BRIDGE_ROOT", tmp_path)


@dataclass
class PausedBridgeWriter:
    """A bridge writer in another process, paused right after its first JSON read."""

    process: subprocess.Popen[str]
    read_done: Path
    resume: Path
    stderr: str = ""
    _reaped: bool = field(default=False, init=False, repr=False)

    def __enter__(self) -> PausedBridgeWriter:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> str:
        """Kill the child if it is still running, reap it, and return its stderr."""
        if not self._reaped:
            if self.process.poll() is None:
                self.process.kill()
            _, self.stderr = self.process.communicate()
            self._reaped = True
        return self.stderr

    def wait_until_read(self) -> None:
        deadline = time.monotonic() + _WRITER_TIMEOUT_S
        while not self.read_done.exists():
            if self.process.poll() is not None:
                raise AssertionError(f"paused writer exited before reading: {self.close()}")
            if time.monotonic() > deadline:
                raise AssertionError(f"paused writer never reported its read: {self.close()}")
            time.sleep(0.01)

    def resume_and_wait(self) -> None:
        self.resume.touch()
        try:
            _, self.stderr = self.process.communicate(timeout=_WRITER_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            raise AssertionError(
                f"paused writer never exited after resuming: {self.close()}"
            ) from None
        self._reaped = True
        assert self.process.returncode == 0, self.stderr


def start_paused_bridge_writer(
    bridge_root: Path,
    bridge_dir: Path,
    *,
    pause_on: str,
    writer: str,
    **writer_args: object,
) -> PausedBridgeWriter:
    """
    Start a bridge writer in a child process that pauses after reading *pause_on*.

    Use the result as a context manager so the child is always reaped.

    :param bridge_root: Bridge root the child trusts, e.g. the test's ``tmp_path``.
    :param bridge_dir: Bridge directory the writer targets.
    :param pause_on: JSON file name whose first read pauses the writer,
        e.g. ``"state.json"``.
    :param writer: ``"record_hook_event"`` or ``"write_active_session_id"``.
    :param writer_args: The writer's arguments (``payload`` or ``session_id``).
    :returns: Handle used to wait for the read and resume the writer.
    """
    markers = bridge_root / f"{writer}-markers"
    markers.mkdir()
    read_done = markers / "read-done"
    resume = markers / "resume"
    args = {
        "bridge_root": str(bridge_root),
        "bridge_dir": str(bridge_dir),
        "read_done": str(read_done),
        "resume": str(resume),
        "pause_on": pause_on,
        "writer": writer,
        "timeout_s": _WRITER_TIMEOUT_S,
        **writer_args,
    }
    process = subprocess.Popen(
        [sys.executable, "-c", _PAUSED_WRITER_SCRIPT, json.dumps(args)],
        cwd=_REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return PausedBridgeWriter(process=process, read_done=read_done, resume=resume)


def start_competing_writer(
    pool: ThreadPoolExecutor, writer: Callable[..., None], /, *args: object, **kwargs: object
) -> Future[None]:
    """
    Run *writer* in *pool* and check that the paused process still excludes it.

    :param pool: Executor that runs the competing writer.
    :param writer: Bridge writer to run, e.g. :func:`record_hook_event`.
    :param args: Positional arguments for *writer*.
    :param kwargs: Keyword arguments for *writer*.
    :returns: The writer's future, still pending while the paused process holds the lock.
    """
    started = threading.Event()

    def run() -> None:
        started.set()
        writer(*args, **kwargs)

    future = pool.submit(run)
    assert started.wait(timeout=_WRITER_TIMEOUT_S), "competing writer never started"
    wait([future], timeout=_COMPETING_WRITER_WAIT_S)
    assert not future.done(), (
        "competing writer finished while the paused writer still held its stale read; "
        "the bridge lock is not excluding writers"
    )
    return future


def test_stop_hook_state_update_keeps_a_concurrent_session_start_identity(
    tmp_path: Path,
) -> None:
    """
    An old session's Stop hook cannot restore the identity a new SessionStart pinned.

    Each hook invocation is its own process reading and rewriting ``state.json``;
    a Stop that read the file before SessionStart recorded the new native
    session must not write the obsolete transcript back, or the forwarder
    follows the wrong transcript.
    """
    bridge_dir = prepare_bridge_dir("conv_abc", workspace=tmp_path)
    old_transcript = tmp_path / "old.jsonl"
    new_transcript = tmp_path / "new.jsonl"
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-old",
            "transcript_path": str(old_transcript),
        },
    )
    assert read_claude_session_id(bridge_dir) == "claude-old"

    with (
        ThreadPoolExecutor(max_workers=1) as pool,
        start_paused_bridge_writer(
            tmp_path,
            bridge_dir,
            pause_on="state.json",
            writer="record_hook_event",
            payload={
                "hook_event_name": "Stop",
                "session_id": "claude-old",
                "transcript_path": str(old_transcript),
            },
        ) as stop_hook,
    ):
        stop_hook.wait_until_read()
        session_start = start_competing_writer(
            pool,
            record_hook_event,
            bridge_dir,
            {
                "hook_event_name": "SessionStart",
                "session_id": "claude-new",
                "transcript_path": str(new_transcript),
            },
        )
        stop_hook.resume_and_wait()
        session_start.result(timeout=_WRITER_TIMEOUT_S)

    assert (read_claude_session_id(bridge_dir), read_transcript_path(bridge_dir)) == (
        "claude-new",
        new_transcript,
    )
    assert read_seen_claude_session_ids(bridge_dir) == {"claude-old", "claude-new"}


def test_session_rotation_keeps_a_concurrent_model_vocabulary_record(tmp_path: Path) -> None:
    """
    Rotating the active session cannot erase a model vocabulary recorded meanwhile.

    ``/clear`` rotation rewrites ``bridge.json`` from the hook or forwarder process
    while the runner records the vocabulary it just served; both independent
    changes must survive.
    """
    bridge_dir = prepare_bridge_dir("conv_old", workspace=tmp_path)
    record_model_vocabulary(
        bridge_dir,
        launch_env={"ANTHROPIC_DEFAULT_OPUS_MODEL": "databricks-claude-opus-4-7"},
        launch_model="databricks-claude-opus-4-7",
        picker_values=["databricks-claude-opus-4-7"],
    )
    assert read_model_picker_values(bridge_dir) == ["databricks-claude-opus-4-7"]

    fresh_env = {
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "databricks-claude-opus-4-8",
        "ANTHROPIC_CUSTOM_MODEL_OPTION": "system.ai.glm-5-3",
    }
    fresh_picker = ["databricks-claude-opus-4-8", "system.ai.glm-5-3"]
    with (
        ThreadPoolExecutor(max_workers=1) as pool,
        start_paused_bridge_writer(
            tmp_path,
            bridge_dir,
            pause_on="bridge.json",
            writer="write_active_session_id",
            session_id="conv_new",
        ) as rotation,
    ):
        rotation.wait_until_read()
        vocabulary = start_competing_writer(
            pool,
            record_model_vocabulary,
            bridge_dir,
            launch_env=fresh_env,
            launch_model="databricks-claude-opus-4-8",
            picker_values=fresh_picker,
        )
        rotation.resume_and_wait()
        vocabulary.result(timeout=_WRITER_TIMEOUT_S)

    assert read_active_session_id(bridge_dir) == "conv_new"
    assert (
        read_model_env(bridge_dir),
        read_model_picker_values(bridge_dir),
        read_launch_model(bridge_dir),
    ) == (fresh_env, fresh_picker, "databricks-claude-opus-4-8")
