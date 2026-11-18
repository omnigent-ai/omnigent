"""Pick the ``omnigent-workspace-helper`` wheel a machine can install.

The helper ships one binary wheel per platform: glibc Linux
(``manylinux_2_28``), musl Linux (``musllinux_1_2``) and macOS, each for
x86_64 and arm64. pip refuses a musllinux wheel on glibc and a manylinux wheel
on musl, so a machine's architecture alone isn't enough: this module reads the
system, architecture and C library from :data:`PROBE_COMMAND`'s output and
selects the matching wheel by its platform tag.

The release workflow picks the wheel its runner installs with::

    python -m omnigent.onboarding.sandboxes.workspace_helper_wheels select dist/
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

DISTRIBUTION = "omnigent-workspace-helper"
"""The helper wheel's distribution name."""

PROBE_COMMAND = "uname -s; uname -m; ldd --version 2>&1 | head -n 2"
"""Prints the system, the architecture, and the C library's ``ldd`` banner.

glibc's banner ends with its version (``ldd (GNU libc) 2.36``); musl's names
itself on the first line and its version on the second
(``musl libc (x86_64)`` / ``Version 1.2.4``). macOS has no ``ldd``.
"""

_MACHINES = {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}
_WHEEL = re.compile(r"^(?P<name>[^-]+)-(?P<version>[^-]+)-[^-]+-[^-]+-(?P<platforms>[^-]+)\.whl$")
_LINUX_TAG = re.compile(
    r"^(?P<libc>manylinux|musllinux)_(?P<major>\d+)_(?P<minor>\d+)_(?P<machine>\w+)$"
)
_MAC_TAG = re.compile(r"^macosx_\d+_\d+_(?P<machine>\w+)$")


@dataclass(frozen=True)
class Platform:
    """What a machine can install.

    :param system: ``"linux"`` or ``"darwin"``.
    :param machine: ``"x86_64"`` or ``"aarch64"``; macOS ``arm64`` is ``"aarch64"``.
    :param libc: ``"glibc"`` or ``"musl"`` on Linux, ``None`` on macOS.
    :param libc_version: The C library's version, e.g. ``(2, 36)``; ``None`` on macOS.
    """

    system: Literal["linux", "darwin"]
    machine: str
    libc: Literal["glibc", "musl"] | None = None
    libc_version: tuple[int, int] | None = None


def _version(text: str) -> tuple[int, int] | None:
    found = re.findall(r"(\d+)\.(\d+)", text)
    return (int(found[-1][0]), int(found[-1][1])) if found else None


def parse_probe(output: str) -> Platform:
    """Read :data:`PROBE_COMMAND`'s output.

    :param output: What the command printed, e.g.
        ``"Linux\\naarch64\\nldd (GNU libc) 2.36\\n"``.
    :returns: The machine's platform.
    :raises ValueError: When the system, architecture or C library isn't one
        the helper ships for, so the caller knows to go without it.
    """
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if len(lines) < 2:
        raise ValueError(f"Unexpected platform probe output: {output!r}")
    system, machine = lines[0].lower(), _MACHINES.get(lines[1].lower())
    if machine is None:
        raise ValueError(f"The workspace helper isn't built for {lines[1]}")
    if system == "darwin":
        return Platform("darwin", machine)
    if system != "linux":
        raise ValueError(f"The workspace helper isn't built for {lines[0]}")
    banner = lines[2:]
    if banner and "musl" in banner[0].lower():
        version = _version(" ".join(banner[1:2]))
        libc: Literal["glibc", "musl"] = "musl"
    else:
        version = _version(banner[0]) if banner else None
        libc = "glibc"
    if version is None:
        raise ValueError(f"Couldn't tell this machine's C library from {output!r}")
    return Platform("linux", machine, libc, version)


def probe_local() -> Platform:
    """The platform of the machine running this code, by :data:`PROBE_COMMAND`."""
    result = subprocess.run(
        ["sh", "-c", PROBE_COMMAND], capture_output=True, text=True, check=False, timeout=10
    )
    return parse_probe(result.stdout)


def _tag_fits(platform: Platform, tag: str) -> tuple[int, int] | None:
    """The tag's floor when ``platform`` can install a wheel carrying it."""
    if platform.system == "darwin":
        mac = _MAC_TAG.match(tag)
        machine = mac and _MACHINES.get(mac.group("machine"))
        return (0, 0) if machine == platform.machine else None
    linux = _LINUX_TAG.match(tag)
    if linux is None or _MACHINES.get(linux.group("machine")) != platform.machine:
        return None
    wanted = "manylinux" if platform.libc == "glibc" else "musllinux"
    floor = (int(linux.group("major")), int(linux.group("minor")))
    if linux.group("libc") != wanted or platform.libc_version is None:
        return None
    return floor if floor <= platform.libc_version else None


def select_wheel(platform: Platform, filenames: Iterable[str]) -> str | None:
    """The helper wheel among ``filenames`` that ``platform`` should install.

    :param platform: The target machine, e.g. from :func:`parse_probe`.
    :param filenames: Wheel file names; other distributions are ignored.
    :returns: The matching wheel with the highest platform floor, or ``None``
        when none of them fits.
    """
    best: tuple[tuple[int, int], str] | None = None
    for filename in filenames:
        wheel = _WHEEL.match(Path(filename).name)
        if wheel is None or wheel.group("name") != DISTRIBUTION.replace("-", "_"):
            continue
        floors = [
            floor
            for tag in wheel.group("platforms").split(".")
            if (floor := _tag_fits(platform, tag)) is not None
        ]
        if floors and (best is None or max(floors) > best[0]):
            best = (max(floors), filename)
    return best[1] if best else None


def main(argv: list[str]) -> int:
    """``select DIRECTORY``: print the wheel in DIRECTORY this machine can install."""
    if len(argv) != 2 or argv[0] != "select":
        print(
            f"usage: python -m {__spec__.name if __spec__ else __name__} select DIRECTORY",
            file=sys.stderr,
        )
        return 2
    names = sorted(path.name for path in Path(argv[1]).glob("*.whl"))
    chosen = select_wheel(probe_local(), names)
    if chosen is None:
        print(f"No {DISTRIBUTION} wheel in {argv[1]} fits this machine", file=sys.stderr)
        return 1
    print(chosen)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
