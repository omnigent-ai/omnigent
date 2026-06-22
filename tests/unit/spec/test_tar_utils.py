"""Tests for omnigent.spec.tar_utils."""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest
import yaml

from omnigent.spec.tar_utils import ExtractionError, extract_safe


def _create_tar(tmp_path: Path, members: dict[str, bytes | str]) -> Path:
    """Create a tar.gz at tmp_path/bundle.tar.gz with given members."""
    tar_path = tmp_path / "bundle.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tf:
        for name, content in members.items():
            data = content.encode() if isinstance(content, str) else content
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return tar_path


@pytest.fixture()
def dest(tmp_path: Path) -> Path:
    return tmp_path / "extracted"


def test_extract_valid_tarball(tmp_path: Path, dest: Path) -> None:
    config = yaml.dump({"spec_version": 1, "name": "test"})
    tar_path = _create_tar(tmp_path, {"config.yaml": config})
    result = extract_safe(tar_path, dest)
    assert result == dest
    assert (dest / "config.yaml").exists()
    assert yaml.safe_load((dest / "config.yaml").read_text())["name"] == "test"


def test_extract_nested_files(tmp_path: Path, dest: Path) -> None:
    tar_path = _create_tar(
        tmp_path,
        {
            "config.yaml": "spec_version: 1",
            "skills/search/SKILL.md": "---\nname: search\n---\ncontent",
        },
    )
    extract_safe(tar_path, dest)
    assert (dest / "skills" / "search" / "SKILL.md").exists()


def test_extract_rejects_path_traversal(tmp_path: Path, dest: Path) -> None:
    tar_path = _create_tar(tmp_path, {"../escape.txt": "evil"})
    with pytest.raises(ExtractionError, match="path traversal"):
        extract_safe(tar_path, dest)


def test_extract_rejects_absolute_path(tmp_path: Path, dest: Path) -> None:
    tar_path = _create_tar(tmp_path, {"/etc/passwd": "evil"})
    with pytest.raises(ExtractionError, match="absolute path"):
        extract_safe(tar_path, dest)


def test_extract_rejects_symlink(tmp_path: Path, dest: Path) -> None:
    tar_path = tmp_path / "symlink.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tf:
        info = tarfile.TarInfo(name="evil-link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tf.addfile(info)
    with pytest.raises(ExtractionError, match="link"):
        extract_safe(tar_path, dest)


def test_extract_rejects_hardlink(tmp_path: Path, dest: Path) -> None:
    tar_path = tmp_path / "hardlink.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tf:
        info = tarfile.TarInfo(name="evil-link")
        info.type = tarfile.LNKTYPE
        info.linkname = "/etc/passwd"
        tf.addfile(info)
    with pytest.raises(ExtractionError, match="link"):
        extract_safe(tar_path, dest)


@pytest.mark.parametrize(
    "member_type,type_label",
    [
        (tarfile.FIFOTYPE, "FIFO"),
        (tarfile.CHRTYPE, "character device"),
        (tarfile.BLKTYPE, "block device"),
    ],
)
def test_extract_rejects_special_file_types(
    tmp_path: Path, dest: Path, member_type: bytes, type_label: str
) -> None:
    """
    Special tar members (FIFOs, char/block devices) must be
    rejected by the allow-list, and must NOT be created on disk.

    The original deny-list only blocked links, so a FIFO named
    ``config.yaml`` was materialized on disk; the spec loader then
    hung forever on ``read_text()`` waiting for a writer, exhausting
    the AP-server worker pool.
    """
    tar_path = tmp_path / f"special-{type_label.replace(' ', '_')}.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tf:
        info = tarfile.TarInfo(name="config.yaml")
        info.type = member_type
        tf.addfile(info)

    with pytest.raises(ExtractionError, match="unsupported entry type"):
        extract_safe(tar_path, dest)

    # The whole point of the fix: the special node is never created,
    # so there is no FIFO for a later read_text() to block on. If this
    # fails, the node was written before the check rejected it and the
    # DoS vector is still open.
    assert not (dest / "config.yaml").exists()


def test_extract_rejects_special_member_alongside_valid_files(tmp_path: Path, dest: Path) -> None:
    """
    A bundle that mixes a valid ``config.yaml`` with a FIFO entry is
    rejected wholesale — extraction must not partially succeed and
    leave the FIFO behind.
    """
    tar_path = tmp_path / "mixed.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tf:
        good = b"spec_version: 1\nname: test\n"
        good_info = tarfile.TarInfo(name="config.yaml")
        good_info.size = len(good)
        tf.addfile(good_info, io.BytesIO(good))

        fifo_info = tarfile.TarInfo(name="pipe")
        fifo_info.type = tarfile.FIFOTYPE
        tf.addfile(fifo_info)

    with pytest.raises(ExtractionError, match="unsupported entry type"):
        extract_safe(tar_path, dest)

    # No FIFO node materialized regardless of member ordering.
    assert not (dest / "pipe").exists()


def test_extract_rejects_size_bomb(tmp_path: Path, dest: Path) -> None:
    tar_path = tmp_path / "bomb.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tf:
        data = b"x" * 1024
        info = tarfile.TarInfo(name="big.bin")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
    with pytest.raises(ExtractionError, match="max extracted size"):
        extract_safe(tar_path, dest, max_bytes=512)


def test_extract_rejects_entry_bomb(tmp_path: Path, dest: Path) -> None:
    tar_path = tmp_path / "entries.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tf:
        for i in range(10):
            info = tarfile.TarInfo(name=f"file_{i}.txt")
            info.size = 1
            tf.addfile(info, io.BytesIO(b"x"))
    with pytest.raises(ExtractionError, match="max entry count"):
        extract_safe(tar_path, dest, max_entries=5)


def test_extract_missing_tarball(tmp_path: Path, dest: Path) -> None:
    with pytest.raises(FileNotFoundError, match="tarball not found"):
        extract_safe(tmp_path / "nonexistent.tar.gz", dest)


def test_extract_creates_dest_directory(tmp_path: Path) -> None:
    dest = tmp_path / "deep" / "nested" / "dir"
    tar_path = _create_tar(tmp_path, {"config.yaml": "spec_version: 1"})
    extract_safe(tar_path, dest)
    assert dest.is_dir()
    assert (dest / "config.yaml").exists()


def test_extract_from_bytes(tmp_path: Path, dest: Path) -> None:
    config = yaml.dump({"spec_version": 1, "name": "bytes-agent"})
    tar_path = _create_tar(tmp_path, {"config.yaml": config})
    bundle_bytes = tar_path.read_bytes()

    result = extract_safe(bundle_bytes, dest)
    assert result == dest
    assert (dest / "config.yaml").exists()
    assert yaml.safe_load((dest / "config.yaml").read_text())["name"] == "bytes-agent"


def test_extract_from_bytes_rejects_traversal(dest: Path) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = b"evil"
        info = tarfile.TarInfo(name="../escape.txt")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))

    with pytest.raises(ExtractionError, match="path traversal"):
        extract_safe(buf.getvalue(), dest)
