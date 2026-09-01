"""Sub-package pyprojects must declare license metadata.

Corporate license/SCA scanners (FOSSA, Black Duck, ...) block installing
``omnigent-client`` / ``omnigent-ui-sdk`` / ``omnigent-slack`` when the
wheels' core METADATA has no ``License-Expression``, ``License-File``, or
``License ::`` classifier — even though the repo root is Apache-2.0.

Hatchling generates a wheel's METADATA mechanically from the ``[project]``
table, so a pyproject with no license declarations provably yields a
license-less wheel. This static check runs in the default unit lane on every
PR; the companion artifact check that actually builds each wheel lives in
``tests/e2e/test_sdk_wheel_license_metadata_e2e.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import tomllib

REPO_ROOT = Path(__file__).parents[1]

# (distribution name, package dir relative to repo root)
SUB_PACKAGES = [
    ("omnigent-client", Path("sdks/python-client")),
    ("omnigent-ui-sdk", Path("sdks/ui")),
    ("omnigent-slack", Path("integrations/slack")),
]


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
        problems.append("no `license` (PEP 621/639 expression) and no `License ::` classifier")

    license_files = project.get("license-files")
    if not license_files:
        problems.append("no `license-files` — the wheel ships no license text")
    else:
        for pattern in license_files:
            if not list((REPO_ROOT / pkg_dir).glob(pattern)):
                problems.append(
                    f"`license-files` glob {pattern!r} matches no file under {pkg_dir}"
                )

    assert not problems, (
        f"{dist_name} ({pkg_dir}/pyproject.toml) declares no license metadata, "
        f"so its wheel METADATA carries no license fields and SCA scanners "
        f"block the install: " + "; ".join(problems)
    )
