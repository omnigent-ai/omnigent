from __future__ import annotations

import tarfile
import zipfile
from email import message_from_bytes
from email.message import Message
from pathlib import Path

import pytest

from scripts.build_subpackages import LEGAL_FILES, PACKAGE_DIRS, REPO_ROOT, build_packages


@pytest.mark.parametrize("package_name", PACKAGE_DIRS)
def test_subpackage_artifacts_contain_canonical_legal_files(
    tmp_path: Path, package_name: str
) -> None:
    build_packages([package_name], tmp_path, wheel_only=False)

    wheel_prefix = package_name.replace("-", "_")
    wheel_path = next(tmp_path.glob(f"{wheel_prefix}-*.whl"))
    with zipfile.ZipFile(wheel_path) as wheel:
        metadata_path = next(
            path for path in wheel.namelist() if path.endswith(".dist-info/METADATA")
        )
        metadata = message_from_bytes(wheel.read(metadata_path))
        _assert_license_metadata(metadata)

        dist_info = metadata_path.rsplit("/", 1)[0]
        for filename in LEGAL_FILES:
            archived = f"{dist_info}/licenses/{filename}"
            assert wheel.read(archived) == (REPO_ROOT / filename).read_bytes()

    sdist_path = next(tmp_path.glob(f"{wheel_prefix}-*.tar.gz"))
    with tarfile.open(sdist_path, "r:gz") as sdist:
        pkg_info = next(name for name in sdist.getnames() if name.endswith("/PKG-INFO"))
        pkg_info_file = sdist.extractfile(pkg_info)
        assert pkg_info_file is not None
        _assert_license_metadata(message_from_bytes(pkg_info_file.read()))

        archive_root = pkg_info.rsplit("/", 1)[0]
        for filename in LEGAL_FILES:
            archived_file = sdist.extractfile(f"{archive_root}/{filename}")
            assert archived_file is not None
            assert archived_file.read() == (REPO_ROOT / filename).read_bytes()


def _assert_license_metadata(metadata: Message) -> None:
    assert metadata["License-Expression"] == "Apache-2.0"
    assert metadata.get_all("License-File") == list(LEGAL_FILES)
