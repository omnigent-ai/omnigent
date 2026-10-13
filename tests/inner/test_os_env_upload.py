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
    target = tmp_path / "model.3mf"
    target.write_bytes(b"winner")
    assert helper.call({"op": "complete", "id": UID})["code"] == "file_exists"
    assert target.read_bytes() == b"winner"


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
