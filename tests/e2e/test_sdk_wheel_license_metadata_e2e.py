"""Sub-package wheels must carry license metadata.

Corporate license/SCA scanners (FOSSA, Black Duck, ...) block installing
``omnigent-client`` / ``omnigent-ui-sdk`` / ``omnigent-slack`` because the
wheels' core METADATA has no ``License-Expression``, ``License-File``, or
``License ::`` classifier — even though the repo root is Apache-2.0.

The wheels are built by hatchling straight from each sub-package's
``pyproject.toml`` ``[project]`` table, so the reproduction/guard is
two-layered:

* a static check that each sub-package's ``pyproject.toml`` declares a
  license (PEP 621/639 ``license`` or an OSI ``License ::`` classifier)
  plus ``license-files`` globs that resolve to real files — the absence of
  these declarations is exactly what produces a license-less wheel;
* an artifact check that actually builds each wheel with ``uv build`` and
  asserts the METADATA inside it carries the license fields scanners look
  for. This layer skips (with the concrete reason) when the build backend
  cannot be fetched in a network-restricted environment.

Run with::

    pytest tests/e2e/test_sdk_wheel_license_metadata_e2e.py -v
"""

from __future__ import annotations

import shutil
import subprocess
import tomllib
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
def test_pyproject_declares_license_metadata(dist_name: str, pkg_dir: Path) -> None:
    """Each sub-package pyproject must declare the license hatchling emits.

    A hatchling wheel's METADATA is generated mechanically from the
    ``[project]`` table, so a pyproject with no ``license``, no
    ``license-files``, and no ``License ::`` classifier provably yields a
    wheel with zero license fields — the exact state SCA scanners reject.
    """
    pyproject_path = REPO_ROOT / pkg_dir / "pyproject.toml"
    project = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))["project"]

    problems: list[str] = []

    has_license_key = "license" in project
    has_license_classifier = any(
        c.startswith("License ::") for c in project.get("classifiers", [])
    )
    if not has_license_key and not has_license_classifier:
        problems.append(
            "no `license` (PEP 621/639 expression) and no `License ::` classifier"
        )

    license_files = project.get("license-files")
    if not license_files:
        problems.append("no `license-files` — the wheel ships no license text")
    else:
        for pattern in license_files:
            if not list((REPO_ROOT / pkg_dir).glob(pattern)):
                problems.append(
                    f"`license-files` glob {pattern!r} matches no file "
                    f"under {pkg_dir}"
                )

    assert not problems, (
        f"{dist_name} ({pkg_dir}/pyproject.toml) declares no license metadata, "
        f"so its wheel METADATA carries no license fields and SCA scanners "
        f"block the install: " + "; ".join(problems)
    )


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
            n
            for n in wheel.namelist()
            if n.count("/") == 1 and n.endswith(".dist-info/METADATA")
        ]
        assert metadata_names, f"{wheel_path.name} contains no dist-info METADATA"
        metadata = message_from_string(
            wheel.read(metadata_names[0]).decode("utf-8")
        )
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
                f"{wheel_path.name} declares License-File {entry!r} but does "
                f"not ship {shipped}"
            )
