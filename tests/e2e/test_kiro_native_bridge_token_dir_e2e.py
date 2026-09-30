"""Check Kiro's bridge directory validation in fresh Python processes.

Stage hostile ancestors before startup so bridge roots derive from the real TMPDIR.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from omnigent._platform import stable_user_id

pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="POSIX uid/mode semantics required (symlink + permission-bit ancestor attacks)",
)

# Exercise directory preparation and token writing with the staged TMPDIR.
_CHILD_SCRIPT = """
import json
import os
import stat
import sys

from omnigent.harnesses.kiro_native import bridge as knb

result = {"raised": None, "token_written": False, "token_realpath": None, "ancestor_mode": None}
session_id = sys.argv[1]
bridge_dir = knb.bridge_dir_for_session_id(session_id)
try:
    knb.prepare_bridge_dir(session_id)
    knb.write_mcp_bridge_config(bridge_dir)
except RuntimeError as exc:
    result["raised"] = str(exc)
token_path = bridge_dir / "bridge.json"
if token_path.is_file():
    result["token_written"] = True
    result["token_realpath"] = os.path.realpath(token_path)
ancestor = knb.bridge_root().parent  # $TMPDIR/omnigent-<uid>
if ancestor.exists() or ancestor.is_symlink():
    result["ancestor_mode"] = stat.S_IMODE(os.lstat(ancestor).st_mode)
print(json.dumps(result))
"""


def _run_token_write(tmpdir: Path, session_id: str) -> dict:
    """Run the kiro-native token write in a fresh process with ``TMPDIR=tmpdir``."""
    env = os.environ.copy()
    env["TMPDIR"] = str(tmpdir)
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD_SCRIPT, session_id],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, (
        f"token-write child crashed (rc={proc.returncode}):\n"
        f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _uid_scoped_dirname() -> str:
    """Name of the uid-scoped temp dir kiro-native anchors under."""
    return f"omnigent-{stable_user_id()}"


def test_symlinked_ancestor_refuses_token_write(tmp_path: Path) -> None:
    """Reject symlinked ancestors without leaking the token into the target."""
    hostile_tmp = tmp_path / "tmp"
    hostile_tmp.mkdir()
    attacker = tmp_path / "attacker"
    attacker.mkdir()
    (hostile_tmp / _uid_scoped_dirname()).symlink_to(attacker, target_is_directory=True)

    result = _run_token_write(hostile_tmp, "sess-symlink-ancestor")

    leaked = list(attacker.rglob("bridge.json"))
    assert result["raised"] is not None and not result["token_written"] and not leaked, (
        "kiro-native wrote the relay token through a symlinked bridge ancestor "
        "instead of failing loudly: "
        f"raised={result['raised']!r} token_written={result['token_written']} "
        f"token_realpath={result['token_realpath']!r} leaked_into_attacker_dir={leaked}"
    )


def test_world_writable_ancestor_is_not_trusted_for_token_write(tmp_path: Path) -> None:
    """Repair a permissive ancestor to owner-only or refuse the token write."""
    hostile_tmp = tmp_path / "tmp"
    hostile_tmp.mkdir()
    uid_dir = hostile_tmp / _uid_scoped_dirname()
    uid_dir.mkdir()
    os.chmod(uid_dir, 0o777)

    result = _run_token_write(hostile_tmp, "sess-world-writable-ancestor")

    if result["token_written"]:
        # Writing is safe only after the ancestor becomes owner-only.
        assert result["ancestor_mode"] is not None and (result["ancestor_mode"] & 0o077) == 0, (
            "kiro-native wrote the relay token below a group/other-accessible "
            f"ancestor without repairing it: mode={oct(result['ancestor_mode'])} "
            f"token_realpath={result['token_realpath']!r}"
        )
    else:
        assert result["raised"] is not None, (
            f"token not written but no loud failure either: raised={result['raised']!r}"
        )
