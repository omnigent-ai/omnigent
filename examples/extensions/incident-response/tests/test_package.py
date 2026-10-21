from pathlib import Path

from omnigent_incident_response.plugin import get_manifest

from omnigent.extensions import check_extension_package


def test_package() -> None:
    root = Path(__file__).parents[1]
    check_extension_package(
        get_manifest(), project_root=root, package_root=root / "src" / "omnigent_incident_response"
    )
