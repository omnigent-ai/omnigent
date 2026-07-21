#!/usr/bin/env python3
"""
Smoke test for the microsandbox sandbox provider.

Drives the REAL
:class:`~omnigent.onboarding.sandboxes.microsandbox.MicrosandboxSandboxLauncher`
against a live local microVM to validate the primitives the managed-host /
CLI-bootstrap flows rely on: prepare -> provision -> run (incl. the non-zero
exit path and stderr surfacing) -> put + read-back -> stream_exec (combined
output + exit code) -> exec_foreground (TTY) -> keep_alive -> is_running ->
forward_local_port (an in-guest connection reaching a local listener) ->
stop + resume in place -> attach -> terminate (idempotent). Edge conditions
(split UTF-8, close-retry, relay races) live in the unit suite; the
interactive in-sandbox OAuth login is not driven here.

By default it boots from ``python:alpine`` (small, has the python3 the
port-forward relay needs), so it needs NO pre-built omnigent host image - it
validates the launcher's SDK wiring in isolation. Pass
``--image ghcr.io/omnigent-ai/omnigent-host:latest`` to smoke the real host
image too.

    pip install 'omnigent[microsandbox]'
    python tests/e2e/integrations/deploy/microsandbox/microsandbox_smoke_test.py \
        [--image REF] [--keep]

Requires hardware virtualization (Apple Silicon macOS / KVM Linux).
Exit code 0 = every primitive worked; 1 = a check failed; 2 = setup error.
"""

from __future__ import annotations

import argparse
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

import click

try:
    from omnigent.onboarding.sandboxes.microsandbox import (
        MicrosandboxSandboxLauncher,
        _run,
    )
except ImportError as exc:  # pragma: no cover - environment guard
    print(f"ERROR: cannot import the launcher ({exc}).", file=sys.stderr)
    print("Run from the repo root with omnigent installed.", file=sys.stderr)
    raise SystemExit(2) from exc


def _check(failures: list[str], ok: bool, label: str) -> None:
    """Record and print one check result."""
    print(f"    {'v' if ok else 'x'} {label}", flush=True)
    if not ok:
        failures.append(label)


def _free_port() -> int:
    """Grab an ephemeral local port."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _serve_once(port: int, payload: bytes) -> threading.Thread:
    """Run a one-shot local TCP server answering *payload* to any connection."""

    def _serve() -> None:
        with socket.socket() as server:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(("127.0.0.1", port))
            server.listen(1)
            server.settimeout(30)
            try:
                conn, _addr = server.accept()
            except TimeoutError:
                return
            with conn:
                conn.recv(1024)
                conn.sendall(payload)

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    return thread


def _stop_sandbox(sandbox_id: str) -> None:
    """Stop (not remove) the sandbox through the SDK, to exercise resume."""
    import microsandbox as msb

    async def _do() -> None:
        handle = await msb.Sandbox.get(sandbox_id)
        await handle.stop()

    _run(_do(), timeout=60)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--image",
        default="python:alpine",
        help="OCI image to boot (default: python:alpine - small and carries "
        "the python3 the port-forward relay needs; pass the omnigent host "
        "image to smoke the real thing).",
    )
    parser.add_argument("--keep", action="store_true", help="don't terminate at the end")
    args = parser.parse_args()

    failures: list[str] = []
    launcher = MicrosandboxSandboxLauncher(image=args.image, idle_timeout_s=3600)

    print("==> prepare (SDK + virtualization preflight)")
    launcher.prepare()

    print("==> provision")
    started = time.monotonic()
    sandbox_id = launcher.provision("oa-msb-smoke")
    _check(failures, bool(sandbox_id), f"provisioned '{sandbox_id}'")
    print(f"    boot took {time.monotonic() - started:.2f}s")

    try:
        print("==> run")
        result = launcher.run(sandbox_id, "echo hello-$(uname -m)")
        _check(failures, "hello-" in result.stdout, "run captures stdout")
        result = launcher.run(sandbox_id, "exit 7", check=False)
        _check(failures, result.returncode == 7, "run propagates exit codes")
        try:
            launcher.run(sandbox_id, "echo doom >&2; exit 3")
        except click.ClickException as exc:
            _check(failures, "doom" in exc.message, "run(check=True) surfaces stderr")
        else:
            _check(failures, False, "run(check=True) surfaces stderr")

        print("==> put + read-back")
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as handle:
            handle.write(b"payload-42")
            local_path = Path(handle.name)
        launcher.put(sandbox_id, local_path, "/tmp/oa-smoke-payload.txt")
        result = launcher.run(sandbox_id, "cat /tmp/oa-smoke-payload.txt")
        _check(failures, result.stdout == "payload-42", "put ships file content")

        print("==> stream_exec")
        process = launcher.stream_exec(sandbox_id, "echo out; echo err >&2; exit 5")
        lines = list(process.lines)
        _check(
            failures,
            any("out" in line for line in lines) and any("err" in line for line in lines),
            "stream_exec merges stdout+stderr",
        )
        _check(failures, process.wait() == 5, "stream_exec wait returns exit code")

        print("==> exec_foreground (TTY)")
        rc = launcher.exec_foreground(sandbox_id, "printf 'fg says %s\\n' \"$TERM\"; exit 6")
        _check(failures, rc == 6, "exec_foreground returns the exit code")

        print("==> keep_alive / is_running")
        launcher.keep_alive(sandbox_id)
        _check(failures, launcher.is_running(sandbox_id) is True, "is_running True while up")

        print("==> forward_local_port (guest -> local listener)")
        port = _free_port()
        _serve_once(port, b"pong-from-host")
        with launcher.forward_local_port(sandbox_id, port):
            result = launcher.run(
                sandbox_id,
                "python3 -c \"import socket; s=socket.create_connection(('127.0.0.1',"
                f" {port}), 10); s.sendall(b'ping'); print(s.recv(64).decode())\"",
                check=False,
            )
        _check(
            failures,
            "pong-from-host" in result.stdout,
            "guest reaches the local listener through the relay",
        )

        print("==> stop + resume in place")
        # The marker must live on the writable LAYER (not /tmp, a tmpfs that
        # vanishes on stop) - $HOME persistence is what omnigent workspaces
        # rely on across resume.
        launcher.run(sandbox_id, "echo persisted > /root/oa-smoke-marker")
        _stop_sandbox(sandbox_id)
        _check(failures, launcher.is_running(sandbox_id) is False, "is_running False when stopped")
        started = time.monotonic()
        launcher.resume(sandbox_id)
        print(f"    resume took {time.monotonic() - started:.2f}s")
        result = launcher.run(sandbox_id, "cat /root/oa-smoke-marker")
        _check(failures, "persisted" in result.stdout, "resume keeps the writable layer")

        print("==> attach")
        launcher.attach(sandbox_id)
        _check(failures, launcher.is_running(sandbox_id) is True, "attach on a running sandbox")
    finally:
        if args.keep:
            print(f"==> keeping sandbox '{sandbox_id}' (remove with the SDK or terminate)")
        else:
            print("==> terminate (idempotent)")
            launcher.terminate(sandbox_id)
            launcher.terminate(sandbox_id)
            _check(
                failures, launcher.is_running(sandbox_id) is False, "terminated sandbox is gone"
            )

    if failures:
        print(f"\nFAILED checks ({len(failures)}):")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
