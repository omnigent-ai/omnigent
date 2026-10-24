"""The crash report the CLI just saved must survive its own rotation pass.

``omnigent/crash_handler.py::_save_report`` writes ``crash-<stamp>.md`` and
then rotates the crashes directory down to ``keep_reports`` files sorted by
modification time alone. Nothing excludes the report that was just written:
whenever its mtime does not sort strictly newest — mtimes tied at the
filesystem's timestamp granularity (directory order then decides), a clock
correction, reports restored from backup — the new report itself falls past
the retention boundary and is unlinked before ``_save_report`` returns. The
crash screen then points the user at a file that no longer exists and the
newest crash's details are lost.

Both tests drive the real user journey end-to-end: seed ten prior reports
(the CLI's rotation limit) in an isolated ``OMNIGENT_DATA_DIR``, crash the
real ``python -m omnigent setup`` subprocess via a hand-corrupted
``config.yaml`` (a YAML list where a mapping belongs — any unhandled CLI
exception works as the trigger), read the report path the crash screen
announces, and assert that file still exists afterwards. Before the fix the
announced report is deleted by rotation; after the fix it must survive
(rotation may only remove other, older reports).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Mirrors the ``keep_reports`` default the CLI's install_crash_handler uses.
_ROTATION_KEEP = 10

_TIE_MTIME_NS = 1_577_836_800_000_000_000  # 2020-01-01T00:00:00Z

_COARSE_FS_SITECUSTOMIZE = '''\
"""Simulate a coarse-timestamp filesystem for the crashes directory: a new
crash report lands in the same mtime granule as the seeded ones, and
directory enumeration order is name order (an order the filesystem is free
to return, and the unfavorable one for the just-written report)."""

import os
import pathlib

_dir = os.environ.get("CRASH_TIE_E2E_DIR")
_ns = os.environ.get("CRASH_TIE_E2E_MTIME_NS")
if _dir and _ns:
    _dir = os.path.realpath(_dir)
    _ns = int(_ns)

    _write_text = pathlib.Path.write_text

    def _granule_write_text(self, *args, **kwargs):
        result = _write_text(self, *args, **kwargs)
        if (
            self.name.startswith("crash-")
            and self.name.endswith(".md")
            and os.path.realpath(str(self.parent)) == _dir
        ):
            os.utime(str(self), ns=(_ns, _ns))
        return result

    pathlib.Path.write_text = _granule_write_text

    _glob = pathlib.Path.glob

    def _name_order_glob(self, *args, **kwargs):
        results = list(_glob(self, *args, **kwargs))
        if os.path.realpath(str(self)) == _dir:
            results.sort()
        return iter(results)

    pathlib.Path.glob = _name_order_glob
'''


def _seed_corrupt_config(config_home: Path) -> None:
    """Make the next ``omnigent setup`` crash: a hand-edited ``config.yaml``
    whose top level is a list raises an unhandled AttributeError."""
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "config.yaml").write_text("- oops\n- edited by hand\n", encoding="utf-8")


def _seed_prior_reports(crashes_dir: Path, mtime_ns: int) -> None:
    crashes_dir.mkdir(parents=True, exist_ok=True)
    for i in range(_ROTATION_KEEP):
        seed = crashes_dir / f"crash-20200101T00000{i}Z.md"
        seed.write_text(f"# prior report {i}\n", encoding="utf-8")
        os.utime(seed, ns=(mtime_ns, mtime_ns))


def _crash_real_cli(tmp_path: Path, extra_env: dict[str, str]) -> tuple[Path, str]:
    """Crash the real CLI; return the announced report path and stderr."""
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path),
            "OMNIGENT_DATA_DIR": str(tmp_path / "data"),
            "OMNIGENT_CONFIG_HOME": str(tmp_path / "cfg"),
        }
    )
    env.update(extra_env)

    result = subprocess.run(
        [sys.executable, "-m", "omnigent", "setup"],
        capture_output=True,
        text=True,
        env=env,
        cwd=_REPO_ROOT,
        stdin=subprocess.DEVNULL,
        timeout=120,
    )

    stderr = result.stderr
    assert result.returncode == 1, (
        "expected `omnigent setup` to crash on the corrupted config "
        f"(exit 1), got exit {result.returncode}:\n{stderr}\n{result.stdout}"
    )
    assert "ran into an issue" in stderr, f"crash screen missing from stderr:\n{stderr}"
    match = re.search(r"A crash report was saved to:\s*(\S+\.md)", stderr)
    assert match, f"crash screen did not announce a report path:\n{stderr}"
    return Path(match.group(1)), stderr


def _assert_announced_report_survives(crashes_dir: Path, report_path: Path, stderr: str) -> None:
    remaining = sorted(p.name for p in crashes_dir.glob("crash-*.md"))
    assert report_path.exists(), (
        "rotation deleted the crash report the CLI just wrote and announced "
        f"({report_path.name}); reports left on disk: {remaining}\n{stderr}"
    )


def test_announced_crash_report_survives_rotation_with_tied_mtimes(
    tmp_path: Path,
) -> None:
    """The reported condition: every report carries the same ``mtime_ns``."""
    _seed_corrupt_config(tmp_path / "cfg")
    crashes_dir = tmp_path / "data" / "crashes"
    _seed_prior_reports(crashes_dir, _TIE_MTIME_NS)

    site_dir = tmp_path / "sitepath"
    site_dir.mkdir()
    (site_dir / "sitecustomize.py").write_text(_COARSE_FS_SITECUSTOMIZE, encoding="utf-8")
    pythonpath = os.pathsep.join(p for p in (str(site_dir), os.environ.get("PYTHONPATH")) if p)

    report_path, stderr = _crash_real_cli(
        tmp_path,
        {
            "PYTHONPATH": pythonpath,
            "CRASH_TIE_E2E_DIR": str(crashes_dir),
            "CRASH_TIE_E2E_MTIME_NS": str(_TIE_MTIME_NS),
        },
    )

    _assert_announced_report_survives(crashes_dir, report_path, stderr)


def test_announced_crash_report_survives_rotation_when_prior_reports_sort_newer(
    tmp_path: Path,
) -> None:
    """No simulation at all: prior reports whose mtimes sort newer (clock
    correction, restored backup) must not displace the report being written."""
    _seed_corrupt_config(tmp_path / "cfg")
    crashes_dir = tmp_path / "data" / "crashes"
    _seed_prior_reports(crashes_dir, time.time_ns() + 10 * 24 * 3600 * 1_000_000_000)

    report_path, stderr = _crash_real_cli(tmp_path, {})

    _assert_announced_report_survives(crashes_dir, report_path, stderr)
