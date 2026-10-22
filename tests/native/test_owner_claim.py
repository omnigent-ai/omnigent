"""Native runtime ownership must be meaningful in the sweeping process's namespace."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from omnigent.native import owner_claim


def test_owner_claim_round_trip(tmp_path: Path) -> None:
    owner_claim.write_owner_claim(tmp_path)
    claim = owner_claim.read_owner_claim(tmp_path)
    assert claim is not None
    assert claim.pid == os.getpid()
    assert claim.pid_ns == owner_claim.current_pid_namespace()
    assert claim.boot_id == owner_claim.current_boot_id()
    with pytest.raises(ValueError):
        int((tmp_path / "owner.pid").read_text())


@pytest.mark.parametrize("raw", [b"", b"123", b"\xff", b"0\npid_ns=none", b"123\npid_ns="])
def test_invalid_or_legacy_claim_is_unknown(tmp_path: Path, raw: bytes) -> None:
    (tmp_path / "owner.pid").write_bytes(raw)
    assert owner_claim.read_owner_claim(tmp_path) is None


@pytest.mark.parametrize(
    ("current_ns", "recorded_ns", "recorded_boot", "alive", "gone", "probe"),
    [
        ("pid:[1]", "pid:[1]", "boot-now", False, True, True),
        ("pid:[1]", "pid:[1]", "boot-now", True, False, True),
        ("pid:[1]", "pid:[2]", "boot-now", False, False, False),
        (None, "none", "boot-now", False, False, False),
        ("pid:[1]", "pid:[2]", "boot-before", True, False, False),
        ("pid:[1]", "pid:[1]", "boot-before", False, False, False),
        ("pid:[1]", "pid:[1]", None, False, False, False),
        ("none", "none", None, False, True, True),
    ],
)
def test_owner_death_requires_resolvable_identity(
    monkeypatch: pytest.MonkeyPatch,
    current_ns: str | None,
    recorded_ns: str,
    recorded_boot: str | None,
    alive: bool,
    gone: bool,
    probe: bool,
) -> None:
    monkeypatch.setattr(owner_claim, "current_pid_namespace", lambda: current_ns)
    monkeypatch.setattr(owner_claim, "IS_LINUX", current_ns != "none")
    monkeypatch.setattr(
        owner_claim, "current_boot_id", lambda: None if current_ns == "none" else "boot-now"
    )
    probes: list[int] = []

    def process_alive(pid: int) -> bool:
        probes.append(pid)
        return alive

    claim = owner_claim.OwnerClaim(123, recorded_ns, recorded_boot)
    assert owner_claim.owner_is_gone(claim, process_alive=process_alive) is gone
    assert probes == ([123] if probe else [])


def test_unreadable_linux_namespace_remains_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(owner_claim, "IS_LINUX", True)

    def unreadable(_path: str) -> str:
        raise PermissionError("namespace masked")

    monkeypatch.setattr(owner_claim.os, "readlink", unreadable)
    assert owner_claim.current_pid_namespace() is None
    owner_claim.write_owner_claim(tmp_path)
    assert owner_claim.read_owner_claim(tmp_path) is None
