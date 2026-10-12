"""Terminal launch when ``/dev/null`` has been replaced by a regular file.

tmux starts its server through glibc ``daemon()``, which refuses to run when
``/dev/null`` is not character device 1,3. The user then sees only "tmux launch
failed (rc=1): server exited unexpectedly". The broken device is staged in a
private mount namespace with Bubblewrap, so the host is never touched.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

_LAUNCH_DRIVER = textwrap.dedent(
    """
    import asyncio, json, os, stat, sys
    from pathlib import Path

    from omnigent.inner.terminal import TerminalInstance
    from omnigent.runner.native.orchestration import _native_terminal_start_error_payload

    async def main() -> dict:
        private = Path(sys.argv[1])
        instance = TerminalInstance(
            name="claude",
            session_key="main",
            socket_path=private / "tmux.sock",
            private_dir=private,
            command="true",
        )
        try:
            await instance.launch(cwd=private)
        except Exception as exc:
            payload = _native_terminal_start_error_payload(exc, "Claude", session_id="conv_test")
            return {
                "launched": False,
                "error_type": type(exc).__name__,
                "error": str(exc),
                "user_message": payload["message"],
            }
        finally:
            await instance.close()
        return {"launched": True}

    result = asyncio.run(main())
    mode = os.stat("/dev/null").st_mode
    result["dev_null"] = "regular file" if stat.S_ISREG(mode) else "character device"
    print(json.dumps(result))
    """
)


def _regular_file_dev_null_prefix(tmp_path: Path) -> list[str]:
    """Return a bwrap prefix whose namespace sees a regular file at /dev/null, or skip."""
    if shutil.which("bwrap") is None:
        pytest.skip("requires bwrap to stage a regular file over /dev/null")
    fake = tmp_path / "dev_null_file"
    fake.write_text("runner-host\n")
    prefix = ["bwrap", "--dev-bind", "/", "/", "--bind", str(fake), "/dev/null", "--"]
    probe = subprocess.run(
        [*prefix, "stat", "-c", "%F", "/dev/null"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if probe.returncode != 0 or probe.stdout.strip() != "regular file":
        pytest.skip(f"bwrap cannot stage /dev/null here: {probe.stderr.strip() or probe.stdout}")
    return prefix


@pytest.mark.skipif(shutil.which("tmux") is None, reason="requires a real tmux binary")
def test_launch_failure_names_regular_file_dev_null(tmp_path: Path) -> None:
    prefix = _regular_file_dev_null_prefix(tmp_path)
    private = tmp_path / "t"
    private.mkdir()
    env = {
        **os.environ,
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
    }
    completed = subprocess.run(
        [*prefix, sys.executable, "-c", _LAUNCH_DRIVER, str(private)],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["dev_null"] == "regular file"
    if result["launched"]:
        pytest.skip(
            "tmux launched despite the regular-file /dev/null; diagnosis path not exercised"
        )
    assert "/dev/null" in result["user_message"], (
        f"launch raised {result['error_type']}: {result['error']!r}; "
        f"user-facing message: {result['user_message']!r}"
    )
    assert result["error_type"] == "TerminalLaunchPrerequisiteError", result
    assert "rm -f /dev/null && mknod -m 666 /dev/null c 1 3" in result["user_message"], result
