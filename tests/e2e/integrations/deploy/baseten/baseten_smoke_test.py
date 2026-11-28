"""Live Baseten Sandboxes provider smoke test for a non-production team."""

from __future__ import annotations

import hashlib
import os
import secrets
import tempfile
import time
from pathlib import Path


def _require_confirmed_team() -> str:
    if os.environ.get("OMNIGENT_BASETEN_LIVE_TEST") != "1":
        raise RuntimeError("set OMNIGENT_BASETEN_LIVE_TEST=1 to allow the bounded live test")
    team = os.environ.get("BASETEN_TEAM_ID", "").strip()
    confirmed = os.environ.get("OMNIGENT_BASETEN_TEST_TEAM", "").strip()
    if not team or team != confirmed:
        raise RuntimeError(
            "set BASETEN_TEAM_ID to a non-production team and repeat it in "
            "OMNIGENT_BASETEN_TEST_TEAM to confirm"
        )
    return team


def _assert_deleted(sandbox_id: str) -> None:
    from omnigent.onboarding.sandboxes.baseten import _client

    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            status = _client().get_info(name=sandbox_id).status
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                return
            raise
        if status == "TERMINATED":
            return
        time.sleep(1)
    raise AssertionError(f"Baseten sandbox {sandbox_id!r} still exists after 120 seconds")


def main() -> None:
    from omnigent.onboarding.sandboxes.baseten import BasetenSandboxLauncher

    team = _require_confirmed_team()
    name = f"omnigent-e2e-{int(time.time())}-{secrets.token_hex(3)}"
    launcher = BasetenSandboxLauncher(ttl="1h")
    sandbox_id: str | None = None
    timings: dict[str, float] = {}
    try:
        started = time.monotonic()
        launcher.prepare()
        timings["prepare_s"] = round(time.monotonic() - started, 1)
        # Arm cleanup with the unique requested name before create, so the
        # finally block deletes the sandbox even if readiness reporting fails.
        sandbox_id = name
        started = time.monotonic()
        sandbox_id = launcher.provision(name)
        timings["provision_s"] = round(time.monotonic() - started, 1)
        success = launcher.run(sandbox_id, "printf omnigent-baseten-ok")
        assert success.returncode == 0
        assert "omnigent-baseten-ok" in success.stdout
        assert launcher.run(sandbox_id, "omnigent --version").returncode == 0

        failure = launcher.run(
            sandbox_id,
            "sh -c 'printf expected-error >&2; exit 17'",
            check=False,
        )
        assert failure.returncode == 17
        assert "expected-error" in failure.stderr

        payload = b"omnigent-baseten-binary\x00proof"
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

        running_process = launcher.stream_exec(sandbox_id, "sleep 600 # omnigent-smoke-sleep")
        running_process.close()
        # The bracket keeps grep from matching its own command line.
        leftover = launcher.run(
            sandbox_id, "ps -eo args | grep '[o]mnigent-smoke-sleep' || true", check=False
        )
        assert not leftover.stdout.strip(), leftover.stdout

        fresh_launcher = BasetenSandboxLauncher(image="attach-only:latest")
        fresh_launcher.attach(sandbox_id)
        assert fresh_launcher.is_running(sandbox_id) is True
    finally:
        if sandbox_id is not None:
            launcher.terminate(sandbox_id)
            _assert_deleted(sandbox_id)
            launcher.terminate(sandbox_id)
    print(
        {
            "team": team,
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
            **timings,
        }
    )


if __name__ == "__main__":
    main()
