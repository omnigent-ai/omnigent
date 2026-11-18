"""A machine gets the helper wheel its system, architecture and C library can install."""

from pathlib import Path

import pytest

from omnigent.onboarding.sandboxes import workspace_helper_wheels
from omnigent.onboarding.sandboxes.workspace_helper_wheels import (
    Platform,
    parse_probe,
    select_wheel,
)

RELEASE = [
    "omnigent-0.23.0-py3-none-any.whl",
    "omnigent_client-0.23.0-py3-none-any.whl",
    "omnigent_workspace_helper-0.23.0-py3-none-manylinux_2_28_x86_64.whl",
    "omnigent_workspace_helper-0.23.0-py3-none-manylinux_2_28_aarch64.whl",
    "omnigent_workspace_helper-0.23.0-py3-none-musllinux_1_2_x86_64.whl",
    "omnigent_workspace_helper-0.23.0-py3-none-musllinux_1_2_aarch64.whl",
    "omnigent_workspace_helper-0.23.0-py3-none-macosx_10_12_x86_64.whl",
    "omnigent_workspace_helper-0.23.0-py3-none-macosx_11_0_arm64.whl",
]


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        (
            "Linux\nx86_64\nldd (Debian GLIBC 2.36-9+deb12u4) 2.36\nCopyright (C) 2022\n",
            Platform("linux", "x86_64", "glibc", (2, 36)),
        ),
        (
            "Linux\naarch64\nldd (Ubuntu GLIBC 2.39-0ubuntu8.4) 2.39\n",
            Platform("linux", "aarch64", "glibc", (2, 39)),
        ),
        ("Linux\nx86_64\nldd (GNU libc) 2.28\n", Platform("linux", "x86_64", "glibc", (2, 28))),
        (
            "Linux\nx86_64\nmusl libc (x86_64)\nVersion 1.2.5\n",
            Platform("linux", "x86_64", "musl", (1, 2)),
        ),
        (
            "Linux\narm64\nmusl libc (aarch64)\nVersion 1.2.4\n",
            Platform("linux", "aarch64", "musl", (1, 2)),
        ),
        ("Darwin\narm64\nsh: ldd: command not found\n", Platform("darwin", "aarch64")),
        ("Darwin\nx86_64\n", Platform("darwin", "x86_64")),
    ],
)
def test_the_probe_names_the_system_architecture_and_c_library(
    output: str, expected: Platform
) -> None:
    assert parse_probe(output) == expected


@pytest.mark.parametrize(
    ("output", "message"),
    [
        ("", "Unexpected platform probe output"),
        ("Linux\n", "Unexpected platform probe output"),
        ("Linux\nriscv64\nldd (GNU libc) 2.36\n", "isn't built for riscv64"),
        ("FreeBSD\namd64\n", "isn't built for FreeBSD"),
        ("Linux\nx86_64\n", "Couldn't tell this machine's C library"),
        ("Linux\nx86_64\nmusl libc (x86_64)\n", "Couldn't tell this machine's C library"),
    ],
)
def test_a_platform_the_helper_isnt_built_for_is_an_error(output: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_probe(output)


@pytest.mark.parametrize(
    ("platform", "expected"),
    [
        (Platform("linux", "x86_64", "glibc", (2, 36)), "manylinux_2_28_x86_64"),
        (Platform("linux", "x86_64", "glibc", (2, 28)), "manylinux_2_28_x86_64"),
        (Platform("linux", "aarch64", "glibc", (2, 39)), "manylinux_2_28_aarch64"),
        (Platform("linux", "x86_64", "musl", (1, 2)), "musllinux_1_2_x86_64"),
        (Platform("linux", "aarch64", "musl", (1, 2)), "musllinux_1_2_aarch64"),
        (Platform("darwin", "x86_64"), "macosx_10_12_x86_64"),
        (Platform("darwin", "aarch64"), "macosx_11_0_arm64"),
    ],
)
def test_each_platform_gets_the_wheel_for_its_architecture_and_c_library(
    platform: Platform, expected: str
) -> None:
    assert select_wheel(platform, RELEASE) == (
        f"omnigent_workspace_helper-0.23.0-py3-none-{expected}.whl"
    )


@pytest.mark.parametrize(
    "platform",
    [
        # Older than the manylinux_2_28 floor.
        Platform("linux", "x86_64", "glibc", (2, 17)),
        Platform("linux", "x86_64", "musl", (1, 1)),
    ],
)
def test_a_c_library_older_than_every_floor_gets_no_wheel(platform: Platform) -> None:
    assert select_wheel(platform, RELEASE) is None


def test_a_glibc_machine_never_gets_the_musl_wheel_or_the_reverse() -> None:
    musl_only = [name for name in RELEASE if "musllinux" in name]
    glibc_only = [name for name in RELEASE if "manylinux" in name]
    assert select_wheel(Platform("linux", "x86_64", "glibc", (2, 36)), musl_only) is None
    assert select_wheel(Platform("linux", "x86_64", "musl", (1, 2)), glibc_only) is None


def test_the_highest_floor_the_machine_meets_wins_and_other_distributions_are_ignored() -> None:
    names = [
        "omnigent_workspace_helper-0.23.0-py3-none-manylinux_2_17_x86_64.manylinux2014_x86_64.whl",
        "/dist/omnigent_workspace_helper-0.23.0-py3-none-manylinux_2_28_x86_64.whl",
        "omnigent_workspace_helper-0.23.0-py3-none-manylinux_2_39_x86_64.whl",
        "other_helper-0.23.0-py3-none-manylinux_2_34_x86_64.whl",
        "not-a-wheel.tar.gz",
    ]
    assert (
        select_wheel(Platform("linux", "x86_64", "glibc", (2, 36)), names)
        == "/dist/omnigent_workspace_helper-0.23.0-py3-none-manylinux_2_28_x86_64.whl"
    )


def test_select_prints_the_wheel_this_machine_installs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    for name in RELEASE:
        (tmp_path / name).touch()
    monkeypatch.setattr(
        workspace_helper_wheels,
        "probe_local",
        lambda: Platform("linux", "aarch64", "musl", (1, 2)),
    )
    assert workspace_helper_wheels.main(["select", str(tmp_path)]) == 0
    assert capsys.readouterr().out == (
        "omnigent_workspace_helper-0.23.0-py3-none-musllinux_1_2_aarch64.whl\n"
    )

    monkeypatch.setattr(
        workspace_helper_wheels,
        "probe_local",
        lambda: Platform("linux", "x86_64", "glibc", (2, 17)),
    )
    assert workspace_helper_wheels.main(["select", str(tmp_path)]) == 1
    assert "fits this machine" in capsys.readouterr().err
    assert workspace_helper_wheels.main(["pick"]) == 2


def test_the_local_probe_reads_this_machine() -> None:
    platform = workspace_helper_wheels.probe_local()
    assert platform.system in {"linux", "darwin"}
    assert platform.machine in {"x86_64", "aarch64"}
