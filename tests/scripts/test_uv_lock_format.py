"""The lock-format script against a fake uv, so no network and no real resolver."""

from __future__ import annotations

import importlib.util
import os
import stat
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".github/scripts/uv_lock_format.py"

LOCK_REV3 = textwrap.dedent("""\
    version = 1
    revision = 3
    requires-python = ">=3.12"
    resolution-markers = ["sys_platform == 'win32'", "sys_platform != 'win32'"]

    [[package]]
    name = "anyio"
    version = "4.14.2"
    source = { registry = "https://pypi.org/simple" }
    sdist = { url = "https://files.pythonhosted.org/anyio.tar.gz", hash = "sha256:a" }
    wheels = [{ url = "https://files.pythonhosted.org/anyio.whl", hash = "sha256:b" }]

    [[package]]
    name = "omnigent"
    version = "0.1.0"
    source = { editable = "." }

    [[package]]
    name = "protobuf"
    version = "5.0.0"
    source = { registry = "https://pypi.org/simple" }

    [[package]]
    name = "protobuf"
    version = "6.0.0"
    source = { registry = "https://pypi.org/simple" }
""")


@pytest.fixture
def script():
    spec = importlib.util.spec_from_file_location("uv_lock_format", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def fake_uv(tmp_path: Path, *, writes_revision: int, bump_anyio_to: str | None = None) -> str:
    """A ``uv`` stand-in: ``lock`` rewrites the header to a fixed revision.

    With ``bump_anyio_to`` it also changes a pin, the failure a real migration
    must catch rather than commit.
    """
    body = textwrap.dedent(f'''\
        #!/usr/bin/env python3
        import re, sys
        from pathlib import Path
        assert sys.argv[1] == "lock", sys.argv
        lock = Path("uv.lock")
        text = lock.read_text() if lock.exists() else 'version = 1\\nrequires-python = ">=3.12"\\n'
        text = re.sub(r"^revision = \\d+\\n", "", text, flags=re.M)
        text = text.replace("version = 1\\n", "version = 1\\nrevision = {writes_revision}\\n", 1)
        if {bump_anyio_to!r} and "--upgrade-package" in sys.argv:
            text = text.replace('version = "4.14.2"', 'version = "{bump_anyio_to}"')
        lock.write_text(text)
    ''')
    path = tmp_path / "fake-uv"
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def test_lock_revision_reads_the_header_and_defaults_to_the_first_format(script):
    assert script.lock_revision(LOCK_REV3) == 3
    assert script.lock_revision('version = 1\nrequires-python = ">=3.12"\n') == 1


def test_anchor_is_a_registry_package_locked_once(script):
    # Not ``omnigent`` (editable) and not ``protobuf`` (two versions): forcing one
    # of two locked versions could move the other fork.
    assert script.anchor_package(LOCK_REV3) == "anyio==4.14.2"


def test_check_reports_drift_by_exit_code(script, tmp_path, monkeypatch, capsys):
    (tmp_path / "uv.lock").write_text(LOCK_REV3)
    monkeypatch.chdir(tmp_path)

    assert script.main(["check", "--uv", fake_uv(tmp_path, writes_revision=3)]) == 0
    assert script.main(["check", "--uv", fake_uv(tmp_path, writes_revision=5)]) == 1
    assert "uv.lock revision=3; uv writes revision=5" in capsys.readouterr().out


def test_migrate_moves_the_revision_and_keeps_every_pin(script, tmp_path, monkeypatch):
    lock = tmp_path / "uv.lock"
    lock.write_text(LOCK_REV3)
    monkeypatch.chdir(tmp_path)

    assert script.main(["migrate", "--uv", fake_uv(tmp_path, writes_revision=5)]) == 0

    migrated = lock.read_text()
    assert script.lock_revision(migrated) == 5
    assert script.pins(migrated) == script.pins(LOCK_REV3)


def test_migrate_is_a_no_op_when_already_current(script, tmp_path, monkeypatch, capsys):
    lock = tmp_path / "uv.lock"
    lock.write_text(LOCK_REV3)
    monkeypatch.chdir(tmp_path)

    assert script.main(["migrate", "--uv", fake_uv(tmp_path, writes_revision=3)]) == 0
    assert lock.read_text() == LOCK_REV3
    assert "already in the current format" in capsys.readouterr().out


def test_migrate_restores_the_lock_if_a_pin_moved(script, tmp_path, monkeypatch):
    # The relock must only change the format; a resolver that takes the chance
    # to bump something is a failure the job has to surface, not commit.
    lock = tmp_path / "uv.lock"
    lock.write_text(LOCK_REV3)
    monkeypatch.chdir(tmp_path)
    uv = fake_uv(tmp_path, writes_revision=5, bump_anyio_to="4.15.1")

    with pytest.raises(SystemExit, match="changed a pin"):
        script.main(["migrate", "--uv", uv])
    assert lock.read_text() == LOCK_REV3


def test_fake_uv_is_executable_here(tmp_path):
    # Guard the fixture itself: a lost exec bit would skip every test above.
    assert os.access(fake_uv(tmp_path, writes_revision=5), os.X_OK)
