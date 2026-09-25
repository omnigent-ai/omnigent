"""E2E: kiro-native must validate the bridge-dir ancestor chain before writing the relay token.

``kiro-native`` keeps its per-session bridge tree under
``$TMPDIR/omnigent-<uid>/kiro-native/<digest>/``. When the runner auto-creates
the Kiro TUI terminal it calls
:func:`omnigent.harnesses.kiro_native.bridge.prepare_bridge_dir` and then, via
:func:`write_kiro_workspace_mcp_config`, :func:`write_mcp_bridge_config`, which
writes ``bridge.json`` -- the bearer token for the Omnigent MCP relay's
localhost control endpoint. Both entry points build the tree with
``bridge_dir.mkdir(mode=0o700, parents=True, exist_ok=True)``, which applies the
mode to the *leaf only* and accepts any pre-existing ancestor as-is.

On a multi-user POSIX host an attacker can pre-create an ancestor of that tree
(``$TMPDIR/omnigent-<uid>``) as a symlink or a group/other-writable directory.
The token write must then fail loudly (or repair an owned-but-permissive dir to
owner-only) instead of landing the token in a directory chain the user does not
exclusively control -- exactly what ``qwen-native`` and ``cursor-native``
already do by routing the write through ``claude_native_bridge._ensure_secure_dir``
(the kiro-native root is already in that helper's trusted-parent allowlist).

Each scenario runs in a **fresh Python subprocess** with a hostile ``TMPDIR``
staged before interpreter start, so ``kiro_native_bridge._BRIDGE_ROOT`` is
computed from the environment exactly as in a real runner process -- no
monkeypatching of the module under test.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="POSIX uid/mode semantics required (symlink + permission-bit ancestor attacks)",
)

# Runs inside a fresh interpreter whose TMPDIR the test staged. Drives both
# runner token-write entry points (prepare_bridge_dir, then
# write_mcp_bridge_config) and reports what the production path did as JSON on
# stdout; never asserts itself.
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
    # The exact calls the runner makes at kiro terminal launch before the relay
    # token is persisted: prepare_bridge_dir builds the per-session tree, then
    # write_kiro_workspace_mcp_config -> write_mcp_bridge_config writes bridge.json.
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
    from omnigent._platform import stable_user_id

    return f"omnigent-{stable_user_id()}"


def test_symlinked_ancestor_refuses_token_write(tmp_path: Path) -> None:
    """A symlinked ``$TMPDIR/omnigent-<uid>`` ancestor must refuse the token write.

    An attacker pre-creates the uid-scoped ancestor as a symlink into a
    directory they control. Writing ``bridge.json`` through it silently hands
    the relay bearer token to the attacker, so the write must fail loudly
    (RuntimeError) and leave no token behind the redirect -- matching the
    hardened qwen-native / cursor-native behaviour on the identical layout.
    """
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
    """A pre-existing 0o777 uid-scoped ancestor must not be accepted as-is.

    The ancestor is owned by this uid but group/other-writable, so any local
    user can replace entries beneath it. Before the token lands, the chain must
    be validated: an owned-but-permissive dir is repaired to owner-only (0o700)
    or the write is refused -- never "token written, mode left 0o777".
    """
    hostile_tmp = tmp_path / "tmp"
    hostile_tmp.mkdir()
    uid_dir = hostile_tmp / _uid_scoped_dirname()
    uid_dir.mkdir()
    os.chmod(uid_dir, 0o777)

    result = _run_token_write(hostile_tmp, "sess-world-writable-ancestor")

    if result["token_written"]:
        # Token written is acceptable only once the ancestor was repaired to
        # owner-only, i.e. the chain was actually validated before the write.
        assert result["ancestor_mode"] is not None and (result["ancestor_mode"] & 0o077) == 0, (
            "kiro-native wrote the relay token below a group/other-accessible "
            f"ancestor without repairing it: mode={oct(result['ancestor_mode'])} "
            f"token_realpath={result['token_realpath']!r}"
        )
    else:
        assert result["raised"] is not None, (
            f"token not written but no loud failure either: raised={result['raised']!r}"
        )
