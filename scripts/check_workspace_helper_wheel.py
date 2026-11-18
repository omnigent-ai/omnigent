"""Release checks for the ``omnigent-workspace-helper`` wheels.

Standard library only, so it runs in the bare images the wheels are tested in::

    # The built wheel carries the expected platform tag and nothing else.
    python scripts/check_workspace_helper_wheel.py contents WHEEL \\
        --platform manylinux_2_28_x86_64 --version 0.23.0

    # In an environment the wheel was installed into: find_binary() finds a
    # helper that runs and reports this version.
    python scripts/check_workspace_helper_wheel.py installed --version 0.23.0

    # dist/ holds exactly one wheel per released platform.
    python scripts/check_workspace_helper_wheel.py complete dist --version 0.23.0
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path

DISTRIBUTION = "omnigent_workspace_helper"
EXECUTABLE = "omnigent-workspace-helper"
PLATFORMS = (
    "manylinux_2_28_x86_64",
    "manylinux_2_28_aarch64",
    "musllinux_1_2_x86_64",
    "musllinux_1_2_aarch64",
    "macosx_x86_64",
    "macosx_arm64",
)
"""Every platform a release ships; macOS tags also carry the OS floor."""

# Served only by builds that run terminals or stage files, never by this one.
_ABSENT_CAPABILITIES = {
    "pty",
    "InspectSession",
    "WriteAttachment",
    "ListStagedSessions",
    "ReleaseTerminal",
}
_WHEEL = re.compile(rf"^{DISTRIBUTION}-(?P<version>[^-]+)-py3-none-(?P<platform>[^-]+)\.whl$")


def _platform_matches(tag: str, platform: str) -> bool:
    if platform.startswith("macosx_"):
        return re.fullmatch(rf"macosx_\d+_\d+_{platform.removeprefix('macosx_')}", tag) is not None
    return tag == platform


def check_contents(wheel: Path, platform: str, version: str) -> list[str]:
    """Problems with a built wheel's name, tag and files; empty when it's right."""
    problems = []
    name = _WHEEL.match(wheel.name)
    if name is None:
        return [f"{wheel.name} isn't a {DISTRIBUTION} py3-none wheel"]
    if name.group("version") != version:
        problems.append(f"{wheel.name} has version {name.group('version')}, not {version}")
    if not _platform_matches(name.group("platform"), platform):
        problems.append(f"{wheel.name} is tagged {name.group('platform')}, not {platform}")
    allowed = {
        f"{DISTRIBUTION}/__init__.py",
        f"{DISTRIBUTION}/py.typed",
        f"{DISTRIBUTION}-{version}.data/scripts/{EXECUTABLE}",
    }
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
    if f"{DISTRIBUTION}-{version}.data/scripts/{EXECUTABLE}" not in names:
        problems.append(f"{wheel.name} has no {EXECUTABLE} executable")
    for entry in names:
        if entry not in allowed and not entry.startswith(f"{DISTRIBUTION}-{version}.dist-info/"):
            problems.append(f"{wheel.name} unexpectedly contains {entry}")
    return problems


def check_complete(directory: Path, version: str) -> list[str]:
    """Problems with the set of helper wheels in ``directory``."""
    tags = [
        match.group("platform")
        for path in sorted(directory.glob(f"{DISTRIBUTION}-*.whl"))
        if (match := _WHEEL.match(path.name)) and match.group("version") == version
    ]
    problems = []
    for platform in PLATFORMS:
        count = sum(_platform_matches(tag, platform) for tag in tags)
        if count != 1:
            problems.append(f"{count} {platform} wheels for {version}, expected 1")
    if len(tags) != len(PLATFORMS):
        problems.append(f"{len(tags)} helper wheels for {version}, expected {len(PLATFORMS)}")
    return problems


def check_installed(version: str) -> list[str]:
    """Problems with the helper installed in the running interpreter's environment."""
    from omnigent_workspace_helper import find_binary  # type: ignore[import-not-found]

    binary = find_binary()
    answer = subprocess.run(
        [str(binary), "--version"], capture_output=True, text=True, check=False, timeout=30
    )
    if answer.returncode != 0:
        return [f"{binary} --version exited {answer.returncode}: {answer.stderr.strip()}"]
    info = json.loads(answer.stdout)
    problems = []
    if info.get("version") != version:
        problems.append(f"{binary} reports version {info.get('version')!r}, not {version!r}")
    capabilities = set(info.get("capabilities", []))
    if "ReadFile" not in capabilities:
        problems.append(f"{binary} doesn't serve ReadFile")
    if served := sorted(capabilities & _ABSENT_CAPABILITIES):
        problems.append(f"{binary} serves {', '.join(served)}")
    print(f"{binary}: {info}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    contents = commands.add_parser("contents")
    contents.add_argument("wheel", type=Path)
    contents.add_argument("--platform", required=True, choices=PLATFORMS)
    contents.add_argument("--version", required=True)
    installed = commands.add_parser("installed")
    installed.add_argument("--version", required=True)
    complete = commands.add_parser("complete")
    complete.add_argument("directory", type=Path)
    complete.add_argument("--version", required=True)
    arguments = parser.parse_args(argv)
    if arguments.command == "contents":
        problems = check_contents(arguments.wheel, arguments.platform, arguments.version)
    elif arguments.command == "installed":
        problems = check_installed(arguments.version)
    else:
        problems = check_complete(arguments.directory, arguments.version)
    for problem in problems:
        print(f"::error::{problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
