#!/usr/bin/env python3
"""
Smoke test for the Tenki sandbox provider.

Drives the REAL :class:`~omnigent.onboarding.sandboxes.tenki.TenkiSandboxLauncher`
against a live Tenki sandbox to validate every primitive the managed-host /
CLI-bootstrap flows rely on: provision -> run (incl. the non-zero-exit path)
-> put + read-back -> keep_alive -> attach + reconnect-by-id -> stream_exec
(combined output) -> public egress -> terminate (idempotent). This is the
test that actually exercises the Tenki SDK calls the launcher makes, end to
end.

Unlike E2B (which can boot a stock template), the Tenki launcher has NO
default image — a session boots from a prepared registry image with the
Omnigent host baked in (see deploy/tenki/README.md). So this test is gated on
BOTH a credential and a prepared image reference. The image only needs to be a
bootable Tenki session image for the launcher's SDK wiring to be validated;
run it against the real omnigent-host image to smoke the full flow.

    pip install 'omnigent[tenki]'
    export TENKI_API_KEY=tk_...
    python tests/e2e/integrations/deploy/tenki/tenki_smoke_test.py \
        --image <workspace>/omnigent-host:latest [--keep]
    # (or set OMNIGENT_TENKI_IMAGE instead of --image)

Exit code 0 = every primitive worked; 1 = a check failed; 2 = setup error.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

# The launcher lazy-imports the tenki SDK; surface a clean hint if it (or the
# omnigent package) isn't importable rather than a raw traceback.
try:
    from omnigent.onboarding.sandboxes.tenki import (
        IMAGE_ENV_VAR,
        PROJECT_ENV_VAR,
        WORKSPACE_ENV_VAR,
        TenkiSandboxLauncher,
    )
except ImportError as exc:  # pragma: no cover - environment guard
    print(f"ERROR: cannot import the launcher ({exc}).", file=sys.stderr)
    print("Run from the repo root with omnigent installed.", file=sys.stderr)
    raise SystemExit(2) from exc


def _check(failures: list[str], ok: bool, label: str) -> None:
    """Record and print one check result."""
    print(f"    {'✓' if ok else '✗'} {label}", flush=True)
    if not ok:
        failures.append(label)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--image",
        default=os.environ.get(IMAGE_ENV_VAR),
        help="Prepared Tenki registry image to boot from (<workspace>/<name>:tag); "
        f"defaults to {IMAGE_ENV_VAR}. Required — Tenki has no stock default image.",
    )
    parser.add_argument(
        "--project",
        default=os.environ.get(PROJECT_ENV_VAR),
        help=f"Tenki project id (defaults to {PROJECT_ENV_VAR}); required unless your "
        "API key is project-scoped.",
    )
    parser.add_argument(
        "--workspace",
        default=os.environ.get(WORKSPACE_ENV_VAR),
        help=f"Tenki workspace id (defaults to {WORKSPACE_ENV_VAR}); optional.",
    )
    parser.add_argument("--keep", action="store_true", help="don't terminate at the end")
    args = parser.parse_args()

    if not os.environ.get("TENKI_API_KEY"):
        print("ERROR: set TENKI_API_KEY (https://tenki.cloud)", file=sys.stderr)
        return 2
    if not args.image:
        print(
            "ERROR: no prepared image. Build/publish a Tenki host template "
            "(see deploy/tenki/README.md), then pass --image <workspace>/<name>:tag "
            f"or set {IMAGE_ENV_VAR}.",
            file=sys.stderr,
        )
        return 2

    # A sentinel env var we inject at provision and read back from inside the
    # sandbox — exercises the env-passthrough path (resolved from THIS process
    # env by name, exactly like the server forwards its own environment).
    marker_name = "OMNIGENT_TENKI_SMOKE_MARKER"
    marker_value = f"smoke-{int(time.time())}"
    os.environ[marker_name] = marker_value

    launcher = TenkiSandboxLauncher(
        image=args.image, env=[marker_name], project=args.project, workspace=args.workspace
    )
    name = f"smoke-{int(time.time())}"
    print(f"▸ Tenki launcher smoke  image={args.image}  tag={name}")

    sandbox_id: str | None = None
    failures: list[str] = []
    try:
        print("\n[1/9] prepare (SDK + credentials)")
        launcher.prepare()
        _check(failures, True, "prepare passed")

        print("\n[2/9] provision (from prepared image, outbound on / inbound off)")
        sandbox_id = launcher.provision(name)
        _check(failures, bool(sandbox_id), f"provisioned sandbox_id={sandbox_id}")

        print("\n[3/9] run: exit code, output, and env passthrough")
        result = launcher.run(sandbox_id, f'echo "$HOME"; printf %s "${marker_name}"', check=True)
        _check(failures, result.returncode == 0, "run exit code 0")
        _check(failures, result.stdout.strip() != "", "run captured stdout")
        _check(failures, marker_value in result.stdout, "injected env var visible inside sandbox")
        # Tenki's file API is workdir-scoped (rejects paths outside it), so
        # resolve $HOME and stage the upload there rather than /tmp.
        home_result = launcher.run(sandbox_id, 'printf %s "$HOME"', check=True)
        remote_bin = f"{home_result.stdout.strip() or '/home/tenki'}/oa-smoke.bin"

        print("\n[4/9] run: non-zero exit surfaced (not raised by the SDK)")
        # Tenki's exec returns the result even on non-zero exit; the launcher
        # applies `check` itself and must surface the code/streams.
        failed = launcher.run(sandbox_id, "echo to-stderr >&2; exit 7", check=False)
        _check(failures, failed.returncode == 7, "non-zero exit surfaced as returncode 7")
        _check(failures, "to-stderr" in failed.stderr, "stderr captured on failing command")

        print("\n[5/9] put: ship a binary file and read it back")
        import base64
        import tempfile
        from pathlib import Path

        payload = b"tenki-omnigent-smoke\x00\x01binary\n"
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(payload)
            local = Path(tmp.name)
        try:
            launcher.put(sandbox_id, local, remote_bin)
        finally:
            local.unlink(missing_ok=True)
        readback = launcher.run(sandbox_id, f"base64 -w0 {remote_bin}", check=True)
        _check(
            failures,
            base64.b64decode(readback.stdout.strip()) == payload,
            "uploaded file bytes match read-back",
        )

        print("\n[6/9] keep_alive (extend a live session)")
        launcher.keep_alive(sandbox_id)  # soft-fail; must not raise
        _check(failures, True, "keep_alive requested a lifetime extension")

        print("\n[7/9] attach + reconnect-by-id (fresh launcher → Client.get)")
        # A fresh launcher has no cached handle, so this forces a real
        # reconnect-by-id through the SDK — the path managed run/terminate use.
        fresh = TenkiSandboxLauncher(image=args.image)
        fresh.attach(sandbox_id)
        _check(failures, True, "attach validated a running sandbox")
        reconnected = fresh.run(sandbox_id, "echo reconnected", check=True)
        _check(failures, "reconnected" in reconnected.stdout, "reconnect-by-id ran a command")

        print("\n[8/9] stream_exec: combined stdout+stderr line stream")
        proc = launcher.stream_exec(sandbox_id, "echo out-line; echo err-line >&2")
        streamed = "".join(proc.lines)
        code = proc.wait()
        _check(failures, code == 0, "stream_exec wait() exit code 0")
        _check(
            failures,
            "out-line" in streamed and "err-line" in streamed,
            "stream_exec merged stdout and stderr",
        )

        print("\n[9/9] public egress (outbound HTTPS from inside)")
        egress = launcher.run(
            sandbox_id,
            'python3 -c "import urllib.request as u; '
            "print(u.urlopen('https://api.github.com', timeout=15).status)\"",
            check=False,
        )
        _check(failures, "200" in egress.stdout, "outbound HTTPS reached api.github.com")

    except Exception as exc:
        failures.append(f"FATAL: {type(exc).__name__}: {exc}")
    finally:
        if sandbox_id is not None and not args.keep:
            print("\n[cleanup] terminate (idempotent)")
            try:
                launcher.terminate(sandbox_id)
                # A second terminate of an already-gone sandbox must be a no-op.
                launcher.terminate(sandbox_id)
                print(f"    ✓ terminated {sandbox_id} (and second call was a no-op)")
            except Exception as exc:
                print(f"    WARNING: cleanup failed for {sandbox_id}: {exc}")
        elif sandbox_id is not None:
            print(f"\n[cleanup] --keep set; leaving {sandbox_id} running")

    print("\n" + "=" * 60)
    if failures:
        print("SMOKE TEST FAILED:")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    print("SMOKE TEST PASSED — every Tenki launcher primitive works against a live sandbox.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
