"""Sub-package wheels must carry license metadata.

Corporate license/SCA scanners (FOSSA, Black Duck, ...) block installing
``omnigent-client`` / ``omnigent-ui-sdk`` / ``omnigent-slack`` because the
wheels' core METADATA has no ``License-Expression``, ``License-File``, or
``License ::`` classifier — even though the repo root is Apache-2.0.

This artifact check actually builds each wheel with ``uv build`` and
asserts the METADATA inside it carries the license fields scanners look
for. It skips (with the concrete reason) when the build backend cannot be
fetched in a network-restricted environment. The fast, network-free static
companion check on each sub-package's ``pyproject.toml`` runs in the
default unit lane: ``tests/test_sub_package_license_metadata.py``.

Run with::

    pytest tests/e2e/test_sdk_wheel_license_metadata_e2e.py -v
"""

from __future__ import annotations

import shutil
import subprocess
import zipfile
from email import message_from_string
from email.message import Message
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[2]

# (distribution name, package dir relative to repo root)
SUB_PACKAGES = [
    ("omnigent-client", Path("sdks/python-client")),
    ("omnigent-ui-sdk", Path("sdks/ui")),
    ("omnigent-slack", Path("integrations/slack")),
]

# Error fragments that mean "the build backend could not be fetched", not
# "the package metadata is wrong" — e.g. the registry proxy being down.
_NETWORK_ERROR_MARKERS = (
    "Failed to fetch",
    "operation timed out",
    "tunnel error",
    "Request failed after",
    "No solution found when resolving",
    "error sending request",
)


def _core_metadata_license_fields(metadata: Message) -> list[tuple[str, str]]:
    """License-bearing core-metadata fields, as SCA scanners read them."""
    fields: list[tuple[str, str]] = []
    for key in ("License", "License-Expression", "License-File"):
        for value in metadata.get_all(key) or []:
            fields.append((key, value))
    for classifier in metadata.get_all("Classifier") or []:
        if classifier.startswith("License ::"):
            fields.append(("Classifier", classifier))
    return fields


@pytest.mark.parametrize(
    ("dist_name", "pkg_dir"),
    SUB_PACKAGES,
    ids=[name for name, _ in SUB_PACKAGES],
)
def test_built_wheel_carries_license_metadata(
    tmp_path: Path, dist_name: str, pkg_dir: Path
) -> None:
    """Building the wheel (the reporter's journey) must yield license fields."""
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("uv is not on PATH; cannot build the wheel here")

    result = subprocess.run(
        [uv, "build", "--wheel", "--out-dir", str(tmp_path), str(REPO_ROOT / pkg_dir)],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if result.returncode != 0:
        combined = result.stdout + result.stderr
        if any(marker in combined for marker in _NETWORK_ERROR_MARKERS):
            pytest.skip(
                "cannot fetch the hatchling build backend in this "
                f"network-restricted environment: {combined.strip()[-500:]}"
            )
        pytest.fail(f"uv build failed for {pkg_dir}: {combined.strip()[-2000:]}")

    wheel_name_prefix = dist_name.replace("-", "_")
    wheels = sorted(tmp_path.glob(f"{wheel_name_prefix}-*.whl"))
    assert wheels, f"uv build produced no {wheel_name_prefix}-*.whl in {tmp_path}"
    wheel_path = wheels[-1]

    with zipfile.ZipFile(wheel_path) as wheel:
        metadata_names = [
            n for n in wheel.namelist() if n.count("/") == 1 and n.endswith(".dist-info/METADATA")
        ]
        assert metadata_names, f"{wheel_path.name} contains no dist-info METADATA"
        metadata = message_from_string(wheel.read(metadata_names[0]).decode("utf-8"))
        license_fields = _core_metadata_license_fields(metadata)
        assert license_fields, (
            f"{wheel_path.name} METADATA carries no license fields "
            "(no License, License-Expression, License-File, or "
            "`License ::` classifier) — corporate SCA scanners block this wheel"
        )

        # The license text itself must ship inside the wheel, next to the
        # metadata that references it.
        license_file_entries = metadata.get_all("License-File") or []
        dist_info_dir = metadata_names[0].rsplit("/", 1)[0]
        for entry in license_file_entries:
            shipped = f"{dist_info_dir}/licenses/{entry}"
            assert shipped in wheel.namelist(), (
                f"{wheel_path.name} declares License-File {entry!r} but does not ship {shipped}"
            )
