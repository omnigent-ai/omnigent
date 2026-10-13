"""Upload operations use the real sandbox-helper subprocess and raw wire."""

from __future__ import annotations

import hashlib
import io
import os
import shutil
import sys
import threading
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.os_env import CallerProcessOSEnvironment, create_os_environment
from omnigent.inner.os_env_upload import UploadHelperClient, read_frame, write_frame

UID = "a" * 32


@pytest.fixture
def helper(tmp_path: Path) -> Iterator[UploadHelperClient]:
    env = create_os_environment(
        OSEnvSpec(cwd=str(tmp_path), sandbox=OSEnvSandboxSpec(type="none"))
    )
    assert isinstance(env, CallerProcessOSEnvironment)
    client = UploadHelperClient(env)
    yield client
    client.close()
    env.close()


def begin(helper: UploadHelperClient, **kwargs: object) -> dict[str, object]:
    return helper.call(
        {
            "op": "begin",
            "id": UID,
            "dir": "",
            "name": "model.3mf",
            "size": 3,
            "on_conflict": "fail",
            **kwargs,
        }
    )


def test_raw_wire_and_binary_roundtrip(helper: UploadHelperClient, tmp_path: Path) -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as archive:
        archive.writestr("mesh", bytes(range(256)) * 8)
    payload = buf.getvalue()
    digest = hashlib.sha256(payload).hexdigest()
    wire = io.BytesIO()
    write_frame(wire, {"op": "append", "id": UID, "offset": 0}, payload)
    assert wire.getvalue().endswith(payload)
    wire.seek(0)
    metadata, raw = read_frame(wire)
    assert metadata["op"] == "append"
    assert raw == payload
    assert "error" not in begin(helper, size=len(payload), sha256=digest)
    assert helper.call({"op": "append", "id": UID, "offset": 0}, payload)["offset"] == len(payload)
    result = helper.call({"op": "complete", "id": UID})
    assert result["sha256"] == digest
    assert (tmp_path / "model.3mf").read_bytes() == payload
    assert helper.call({"op": "complete", "id": UID}) == result
    helper.call({"op": "abort", "id": UID})
    assert (tmp_path / "model.3mf").read_bytes() == payload


def test_exclusive_staging(helper: UploadHelperClient, tmp_path: Path) -> None:
    staging = tmp_path / "model.3mf.omnigent-upload-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.part"
    staging.write_bytes(b"owned elsewhere")
    assert begin(helper)["code"] == "file_exists"
    assert staging.read_bytes() == b"owned elsewhere"


def test_offset_and_overflow(helper: UploadHelperClient, tmp_path: Path) -> None:
    begin(helper)
    helper.call({"op": "append", "id": UID, "offset": 0}, b"ab")
    result = helper.call({"op": "append", "id": UID, "offset": 0}, b"x")
    assert result["code"] == "offset_mismatch"
    assert result["offset"] == 2
    assert helper.call({"op": "append", "id": UID, "offset": 2}, b"cd")["code"] == "too_large"
    assert (
        tmp_path / "model.3mf.omnigent-upload-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.part"
    ).read_bytes() == b"ab"


def test_no_clobber_publish_race(helper: UploadHelperClient, tmp_path: Path) -> None:
    begin(helper)
    helper.call({"op": "append", "id": UID, "offset": 0}, b"new")
    publication = helper.call({"op": "plan", "id": UID})
    assert "error" not in publication
    target = tmp_path / "model.3mf"
    target.write_bytes(b"winner")
    result = helper.call({"op": "complete", "id": UID, "publication": publication})
    assert target.read_bytes() == b"winner"
    assert result["code"] == "file_exists"


def test_replace_atomic(helper: UploadHelperClient, tmp_path: Path) -> None:
    target = tmp_path / "model.3mf"
    old, new = b"a" * 500_000, b"b" * 500_000
    target.write_bytes(old)
    begin(helper, size=len(new), on_conflict="replace")
    helper.call({"op": "append", "id": UID, "offset": 0}, new)
    stop = threading.Event()
    seen: list[bytes] = [target.read_bytes()]

    def read() -> None:
        while not stop.is_set():
            seen.append(target.read_bytes())

    reader = threading.Thread(target=read)
    reader.start()
    try:
        result = helper.call({"op": "complete", "id": UID})
        seen.append(target.read_bytes())
    finally:
        stop.set()
        reader.join()
    assert result["outcome"] == "replaced"
    assert all(value in (old, new) for value in seen)
    assert target.read_bytes() == new
    assert target.stat().st_mode & 0o111 == 0


def test_rename_and_abort(helper: UploadHelperClient, tmp_path: Path) -> None:
    (tmp_path / "model.3mf").write_bytes(b"old")
    (tmp_path / "model (1).3mf").write_bytes(b"old1")
    begin(helper, on_conflict="rename")
    helper.call({"op": "append", "id": UID, "offset": 0}, b"new")
    result = helper.call({"op": "complete", "id": UID})
    assert result["final_name"] == "model (2).3mf"
    assert result["outcome"] == "renamed"
    assert (tmp_path / "model (2).3mf").read_bytes() == b"new"
    begin(helper, id="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", name="cancel", size=0)
    staging = tmp_path / "cancel.omnigent-upload-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb.part"
    assert staging.stat().st_mode & 0o777 == 0o600
    assert (
        helper.call({"op": "abort", "id": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"})["aborted"] is True
    )
    assert not staging.exists()


def test_symlink_directory_refused(helper: UploadHelperClient, tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "alias").symlink_to(tmp_path / "real", target_is_directory=True)
    assert begin(helper, dir="alias")["code"] == "invalid_path"
    assert not list((tmp_path / "real").iterdir())


def test_parent_identity_and_owned_staging(helper: UploadHelperClient, tmp_path: Path) -> None:
    (tmp_path / "folder").mkdir()
    begin(helper, dir="folder")
    (tmp_path / "folder").rename(tmp_path / "moved")
    (tmp_path / "folder").mkdir()
    assert helper.call({"op": "append", "id": UID, "offset": 0}, b"new")["code"] == "invalid_path"
    assert not list((tmp_path / "folder").iterdir())


@pytest.mark.parametrize("op", ["status", "append", "complete", "abort"])
def test_grants_gate_every_operation(helper: UploadHelperClient, tmp_path: Path, op: str) -> None:
    begin(helper)
    # Move the workspace itself; every operation must revalidate its identity.
    moved = tmp_path.with_name(tmp_path.name + "-moved")
    tmp_path.rename(moved)
    tmp_path.mkdir()
    try:
        result = helper.call(
            {"op": op, "id": UID, "offset": 0},
            b"new" if op == "append" else b"",
        )
        assert result["code"] == "invalid_path"
    finally:
        tmp_path.rmdir()
        moved.rename(tmp_path)


def test_incomplete_and_digest_mismatch(helper: UploadHelperClient) -> None:
    begin(helper, sha256="0" * 64)
    assert helper.call({"op": "complete", "id": UID})["code"] == "incomplete"
    helper.call({"op": "append", "id": UID, "offset": 0}, b"new")
    assert helper.call({"op": "complete", "id": UID})["code"] == "checksum_mismatch"


def test_directory_conflict(helper: UploadHelperClient, tmp_path: Path) -> None:
    (tmp_path / "model.3mf").mkdir()
    assert begin(helper, on_conflict="replace")["code"] == "is_directory"


def test_umask(helper: UploadHelperClient, tmp_path: Path) -> None:
    begin(helper, size=0)
    helper.call({"op": "complete", "id": UID})
    mask = os.umask(0)
    os.umask(mask)
    assert (tmp_path / "model.3mf").stat().st_mode & 0o777 == 0o666 & ~mask


def test_staging_identity(helper: UploadHelperClient, tmp_path: Path) -> None:
    """An unrelated file swapped onto the staging path is never written/deleted."""
    begin(helper)
    staging = tmp_path / "model.3mf.omnigent-upload-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.part"
    staging.rename(tmp_path / "owned")
    staging.write_bytes(b"keep")
    for op in ("append", "complete", "abort"):
        result = helper.call(
            {"op": op, "id": UID, "offset": 0},
            b"new" if op == "append" else b"",
        )
        assert result["code"] == "invalid_path"
        assert staging.read_bytes() == b"keep"


def test_commits_release_descriptors(helper: UploadHelperClient) -> None:
    """Completed uploads free their handles while preserving repeat completion."""
    import psutil

    helper.call({"op": "probe"})
    assert helper._proc is not None
    process = psutil.Process(helper._proc.pid)
    before = process.num_fds()
    for n in range(32):
        uid = f"{n:032x}"
        assert "error" not in begin(helper, id=uid, name=f"file-{n}", size=0)
        assert "error" not in helper.call({"op": "complete", "id": uid})
    assert process.num_fds() <= before + 2
    assert (
        helper.call({"op": "complete", "id": "00000000000000000000000000000000"})["final_name"]
        == "file-0"
    )


@pytest.mark.parametrize("name", ["a" * 250, "é" * 125, ".hidden"])
def test_staging_name_length(helper: UploadHelperClient, tmp_path: Path, name: str) -> None:
    result = begin(helper, name=name, size=0)
    assert "error" not in result, result
    staging = Path(str(result["staging_path"]))
    assert len(staging.name.encode("utf-8")) <= 255
    assert not staging.name.startswith(".")
    assert staging.name.endswith(".omnigent-upload-" + "a" * 32 + ".part")
    assert staging.stat().st_mode & 0o777 == 0o600
    assert helper.call({"op": "complete", "id": "a" * 32})["final_name"] == name
    assert (tmp_path / name).read_bytes() == b""


@pytest.mark.parametrize(
    "sandbox_type",
    [
        pytest.param(
            "linux_bwrap",
            marks=pytest.mark.skipif(
                not (sys.platform.startswith("linux") and shutil.which("bwrap")),
                reason="linux_bwrap requires Linux + bwrap on PATH",
            ),
        ),
        pytest.param(
            "darwin_seatbelt",
            marks=pytest.mark.skipif(
                not (sys.platform == "darwin" and shutil.which("sandbox-exec")),
                reason="darwin_seatbelt requires macOS + sandbox-exec on PATH",
            ),
        ),
    ],
)
def test_upload_under_real_sandbox_refuses_masked_directory(
    tmp_path: Path, sandbox_type: str
) -> None:
    (tmp_path / ".masked").mkdir()
    env = create_os_environment(
        OSEnvSpec(
            cwd=str(tmp_path), sandbox=OSEnvSandboxSpec(type=sandbox_type, write_paths=["."])
        ),
        additional_read_roots=[Path(__file__).resolve().parents[2]],
    )
    assert isinstance(env, CallerProcessOSEnvironment)
    client = UploadHelperClient(env)
    try:
        result = begin(client, dir=".masked")
        assert result["code"] == "permission_denied", result
        assert not list((tmp_path / ".masked").iterdir())
        assert "error" not in begin(client)
    finally:
        client.close()
        env.close()


@pytest.mark.parametrize("name", ["a" * 255, "é" * 125 + ".txt"])
def test_rename_byte_budget(helper: UploadHelperClient, tmp_path: Path, name: str) -> None:
    original = tmp_path / name
    original.write_bytes(b"old")
    assert "error" not in begin(helper, name=name, on_conflict="rename", size=0)
    result = helper.call({"op": "complete", "id": "a" * 32})
    assert "error" not in result, result
    assert len(str(result["final_name"]).encode()) <= 255
    assert result["outcome"] == "renamed"
    assert original.read_bytes() == b"old"


def test_rename_skips_directory(helper: UploadHelperClient, tmp_path: Path) -> None:
    (tmp_path / "model.3mf").write_bytes(b"old")
    (tmp_path / "model (1).3mf").mkdir()
    begin(helper, on_conflict="rename", size=0)
    result = helper.call({"op": "complete", "id": "a" * 32})
    assert result["final_name"] == "model (2).3mf"


def test_rename_exhaustion_is_typed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import omnigent.inner.os_env_upload as upload

    monkeypatch.setattr(upload, "MAX_RENAME_ATTEMPTS", 2, raising=False)
    env = create_os_environment(
        OSEnvSpec(cwd=str(tmp_path), sandbox=OSEnvSandboxSpec(type="none"))
    )
    assert isinstance(env, CallerProcessOSEnvironment)
    engine = upload.UploadEngine(tmp_path, env.sandbox)
    for name in ["file", "file (1)", "file (2)"]:
        (tmp_path / name).write_bytes(b"keep")
    try:
        assert "error" not in engine.dispatch(
            {
                "op": "begin",
                "id": "a" * 32,
                "dir": "",
                "name": "file",
                "size": 0,
                "on_conflict": "rename",
            },
            b"",
        )
        assert engine.dispatch({"op": "complete", "id": "a" * 32}, b"")["code"] == "file_exists"
    finally:
        engine.close()
        env.close()


def test_platform_negative_probe(
    helper: UploadHelperClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    import omnigent.inner.os_env_upload as upload

    engine = upload.UploadEngine(helper.cwd, helper.sandbox)
    monkeypatch.setattr(upload, "sys", SimpleNamespace(platform="win32"))
    try:
        result = engine.dispatch({"op": "probe"}, b"")
        assert result["raw_upload"] is False
        assert result["reason"]
    finally:
        engine.close()


def test_upload_helper_has_no_network_or_credentials(helper: UploadHelperClient) -> None:
    assert helper.sandbox.allow_network is False
    assert helper._egress_rules is None
    assert helper.sandbox.credential_proxy is None


def test_real_pipe_is_raw(helper: UploadHelperClient) -> None:
    import base64
    from unittest.mock import patch

    payload = bytes(range(256)) * 16
    begin(helper, size=len(payload))
    assert helper._proc is not None and helper._proc.stdin is not None
    original = helper._proc.stdin
    captured = bytearray()

    class Writer:
        def write(self, data: bytes):
            captured.extend(data)
            return original.buffer.write(data)

        def flush(self):
            return original.buffer.flush()

    class Stdin:
        buffer = Writer()

    with patch.object(helper._proc, "stdin", Stdin()):
        assert helper.call({"op": "append", "id": "a" * 32, "offset": 0}, payload)[
            "offset"
        ] == len(payload)
    assert bytes(captured).endswith(payload)
    assert base64.b64encode(payload) not in captured


def test_stalled_helper_call_is_bounded(helper: UploadHelperClient) -> None:
    import signal
    import time

    helper.call({"op": "probe"})
    assert helper._proc is not None
    proc = helper._proc
    os.kill(proc.pid, signal.SIGSTOP)

    def resume() -> None:
        if proc.poll() is None:
            os.kill(proc.pid, signal.SIGCONT)

    watchdog = threading.Timer(0.5, resume)
    watchdog.start()
    started = time.monotonic()
    try:
        result = helper.call({"op": "probe"}, timeout_s=0.1)
        assert result["code"] == "timeout"
        assert time.monotonic() - started < 2
    finally:
        watchdog.cancel()
        watchdog.join()
        helper.interrupt()


def test_append_waits_for_slow_append_on_shared_helper(
    helper: UploadHelperClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time
    from concurrent.futures import ThreadPoolExecutor

    import omnigent.inner.os_env_upload as upload

    assert "error" not in begin(helper, size=1)
    second = "b" * 32
    assert "error" not in helper.call(
        {"op": "begin", "id": second, "dir": "", "name": "other", "size": 1, "on_conflict": "fail"}
    )
    original = upload.read_frame
    entered, release = threading.Event(), threading.Event()

    def slow(stream):
        if not entered.is_set():
            entered.set()
            assert release.wait(5)
        return original(stream)

    monkeypatch.setattr(upload, "read_frame", slow)
    with ThreadPoolExecutor(max_workers=1) as executor:
        holder = executor.submit(helper.call, {"op": "append", "id": UID, "offset": 0}, b"a")
        assert entered.wait(5)
        timer = threading.Timer(1.3, release.set)
        timer.start()
        started = time.monotonic()
        try:
            result = helper.call({"op": "append", "id": second, "offset": 0}, b"b", timeout_s=3)
            assert result.get("offset") == 1, result
            assert time.monotonic() - started >= 1.2
        finally:
            release.set()
            timer.join()
        assert holder.result(timeout=5)["offset"] == 1
    assert helper.call({"op": "status", "id": UID})["offset"] == 1


def test_helper_lock_wait_is_bounded(helper: UploadHelperClient) -> None:
    import time

    helper._lock.acquire()
    release = threading.Timer(0.2, helper._lock.release)
    release.start()
    try:
        started = time.monotonic()
        result = helper.call({"op": "probe"}, timeout_s=0.05)
        assert result["code"] == "busy"
        assert time.monotonic() - started < 0.15
    finally:
        release.join()


@pytest.mark.parametrize("launch_error,timeout_s", [(False, 0.05), (True, 0.05), (True, 1)])
def test_launch_and_ipc_share_deadline(
    helper: UploadHelperClient,
    monkeypatch: pytest.MonkeyPatch,
    launch_error: bool,
    timeout_s: float,
) -> None:
    import time

    original = helper._ensure_started_locked

    def delayed():
        time.sleep(0.15)
        if launch_error:
            raise OSError("Launch interrupted")
        original()

    monkeypatch.setattr(helper, "_ensure_started_locked", delayed)
    result = helper.call({"op": "probe"}, timeout_s=timeout_s)
    assert result["code"] == "busy"
    assert helper._proc is None or helper._proc.poll() is not None


def test_closed_helper_does_not_dispatch_begin(helper: UploadHelperClient) -> None:
    assert helper.call({"op": "probe"}) == {"raw_upload": True}
    helper.close()
    assert begin(helper)["code"] == "busy"


def test_recovery_skips_fifo(helper: UploadHelperClient, tmp_path: Path) -> None:
    prepared = helper.call({"op": "prepare", "id": UID, "dir": "", "name": "model.3mf"})
    started = begin(helper, size=0)
    os.mkfifo(tmp_path / "model.3mf")
    result = helper.call(
        {
            "op": "recover",
            "id": UID,
            "dir": "",
            "name": "model.3mf",
            "on_conflict": "fail",
            **prepared,
            **started,
        },
        timeout_s=0.2,
    )
    assert result["code"] == "gone"
    assert helper.call({"op": "status", "id": UID})["offset"] == 0


def test_slow_complete_has_size_budget_and_keeps_other_upload(
    helper: UploadHelperClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    import omnigent.inner.os_env_upload as upload

    begin(helper, size=0)
    second = "b" * 32
    assert "error" not in helper.call(
        {"op": "begin", "id": second, "dir": "", "name": "other", "size": 1, "on_conflict": "fail"}
    )
    original = upload.read_frame

    def slow(stream):
        time.sleep(0.15)
        return original(stream)

    monkeypatch.setattr(upload, "CALL_TIMEOUT_S", 0.05, raising=False)
    monkeypatch.setattr(upload, "COMPLETE_TIMEOUT_FLOOR_S", 0.5, raising=False)
    monkeypatch.setattr(upload, "read_frame", slow)
    assert "error" not in helper.call({"op": "complete", "id": UID, "size": 1024**3})
    monkeypatch.setattr(upload, "read_frame", original)
    assert helper.call({"op": "append", "id": second, "offset": 0}, b"x")["offset"] == 1
    assert upload.operation_timeout(
        {"op": "complete", "size": 1024**3}
    ) > upload.operation_timeout({"op": "complete", "size": 0})


def test_engine_publishes_empty_upload(helper: UploadHelperClient, tmp_path: Path) -> None:
    from omnigent.inner.os_env_upload import UploadEngine

    engine = UploadEngine(tmp_path.resolve(), helper.sandbox)
    try:
        assert engine.dispatch({"op": "probe"}, b"") == {"raw_upload": True}
        result = engine.dispatch(
            {
                "op": "begin",
                "id": UID,
                "dir": "",
                "name": "empty",
                "size": 0,
                "on_conflict": "fail",
            },
            b"",
        )
        assert "error" not in result
        assert "error" not in engine.dispatch({"op": "complete", "id": UID}, b"")
        assert (tmp_path / "empty").exists()
    finally:
        engine.close()
