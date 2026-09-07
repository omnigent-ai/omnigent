"""Live Blaxel provider smoke test for a non-production workspace."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import tempfile
import time
from pathlib import Path


def _production_workspace_name(workspace: str) -> bool:
    lowered = workspace.lower()
    tokens = set(filter(None, re.split(r"[^a-z0-9]+", lowered)))
    return "production" in lowered or bool(tokens & {"main", "prod", "production"})


def _require_safe_workspace() -> str:
    from blaxel.core.authentication import get_credentials

    if os.environ.get("OMNIGENT_BLAXEL_LIVE_TEST") != "1":
        raise RuntimeError("set OMNIGENT_BLAXEL_LIVE_TEST=1 to allow the bounded live test")
    credentials = get_credentials()
    workspace = str(getattr(credentials, "workspace", "") or "").strip()
    confirmed = os.environ.get("OMNIGENT_BLAXEL_TEST_WORKSPACE", "").strip()
    if not workspace or not confirmed or workspace != confirmed:
        raise RuntimeError(
            "OMNIGENT_BLAXEL_TEST_WORKSPACE must exactly match the active BL_WORKSPACE"
        )
    if _production_workspace_name(workspace):
        raise RuntimeError("select a clearly non-production BL_WORKSPACE before this test")
    return workspace


def _assert_deleted(sandbox_id: str) -> None:
    from blaxel.core import SyncSandboxInstance
    from blaxel.core.sandbox import SandboxAPIError

    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            SyncSandboxInstance.get(sandbox_id)
        except SandboxAPIError as exc:
            if exc.status_code == 404:
                return
            raise
        time.sleep(1)
    raise AssertionError(f"Blaxel sandbox {sandbox_id!r} still exists after 120 seconds")


def main() -> None:
    from omnigent.onboarding.sandboxes.blaxel import BlaxelSandboxLauncher

    workspace = _require_safe_workspace()
    name = f"omnigent-e2e-{int(time.time())}-{secrets.token_hex(3)}"
    launcher = BlaxelSandboxLauncher(ttl="1h")
    sandbox_id: str | None = None
    try:
        launcher.prepare()
        # Arm cleanup with the unique requested name before create. If create
        # succeeds but readiness reporting fails, the outer finally still makes
        # an independent deletion attempt instead of relying only on provision().
        sandbox_id = name
        sandbox_id = launcher.provision(name)
        success = launcher.run(sandbox_id, "printf omnigent-blaxel-ok")
        assert success.returncode == 0
        assert "omnigent-blaxel-ok" in success.stdout
        assert launcher.run(sandbox_id, "omnigent --version").returncode == 0

        failure = launcher.run(
            sandbox_id,
            "sh -c 'printf expected-error >&2; exit 17'",
            check=False,
        )
        assert failure.returncode == 17
        assert "expected-error" in failure.stderr

        payload = b"omnigent-blaxel-binary\x00proof"
        with tempfile.NamedTemporaryFile() as source:
            source.write(payload)
            source.flush()
            launcher.put(sandbox_id, Path(source.name), "/tmp/omnigent-proof.bin")
        digest = hashlib.sha256(payload).hexdigest()
        copied = launcher.run(
            sandbox_id,
            'python -c "import hashlib; '
            "print(hashlib.sha256(open('/tmp/omnigent-proof.bin','rb').read()).hexdigest())\"",
        )
        assert digest in copied.stdout

        streamed_process = launcher.stream_exec(
            sandbox_id,
            "printf 'stream-out\n'; printf 'stream-err\n' >&2",
        )
        try:
            streamed = "".join(streamed_process.lines)
            assert streamed_process.wait() == 0
        finally:
            streamed_process.close()
        assert "stream-out" in streamed and "stream-err" in streamed

        running_process = launcher.stream_exec(sandbox_id, "sleep 600")
        running_process.close()
        fresh_launcher = BlaxelSandboxLauncher(image="attach-only")
        fresh_launcher.attach(sandbox_id)
        assert fresh_launcher.is_running(sandbox_id) is True
    finally:
        if sandbox_id is not None:
            launcher.terminate(sandbox_id)
            _assert_deleted(sandbox_id)
            launcher.terminate(sandbox_id)
    print(
        {
            "workspace": workspace,
            "sandbox": name,
            "created": sandbox_id is not None,
            "deleted": True,
            "success_command": True,
            "failure_command": True,
            "binary_file": True,
            "streaming_exec": True,
            "running_process_cleanup": True,
            "attach": True,
            "idempotent_terminate": True,
        }
    )


if __name__ == "__main__":
    main()
