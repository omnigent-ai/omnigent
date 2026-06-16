#!/usr/bin/env python3
"""
Smoke test for the E2B sandbox provider.

Drives the REAL :class:`~omnigent.onboarding.sandboxes.e2b.E2BSandboxLauncher`
against a live E2B sandbox to validate every primitive the managed-host /
CLI-bootstrap flows rely on: provision -> run (incl. the non-zero-exit
``CommandExitException`` path) -> put + read-back -> keep_alive ->
stream_exec (combined output) -> attach -> public egress -> terminate
(idempotent). This is the test that actually exercises the E2B SDK calls
the launcher makes, end to end.

By default it boots from E2B's stock ``base`` template, so it needs NO
pre-built omnigent host template — it validates the launcher's SDK wiring
in isolation. Pass ``--template omnigent-host`` (after ``e2b template
build``; see deploy/e2b/README.md) to smoke the real host template too.

    pip install 'omnigent[e2b]'
    export E2B_API_KEY=e2b_...
    python tests/e2e/integrations/deploy/e2b/e2b_smoke_test.py [--template NAME] [--keep]

Exit code 0 = every primitive worked; 1 = a check failed; 2 = setup error.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

# The launcher lazy-imports the e2b SDK; surface a clean hint if it (or the
# omnigent package) isn't importable rather than a raw traceback.
try:
    from omnigent.onboarding.sandboxes.e2b import (
        E2BSandboxLauncher,
        resolve_max_lifetime_s,
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
        "--template",
        default="base",
        help="E2B template to boot from (default: E2B's stock 'base'; pass "
        "'omnigent-host' to smoke the real host template once built).",
    )
    parser.add_argument("--keep", action="store_true", help="don't terminate at the end")
    args = parser.parse_args()

    if not os.environ.get("E2B_API_KEY"):
        print("ERROR: set E2B_API_KEY (https://e2b.dev/dashboard)", file=sys.stderr)
        return 2

    # A sentinel env var we inject at provision and read back from inside the
    # sandbox — exercises the env-passthrough path (resolved from THIS process
    # env by name, exactly like the server forwards its own environment).
    marker_name = "OMNIGENT_E2B_SMOKE_MARKER"
    marker_value = f"smoke-{int(time.time())}"
    os.environ[marker_name] = marker_value

    launcher = E2BSandboxLauncher(template=args.template, env=[marker_name])
    name = f"smoke-{int(time.time())}"
    print(f"▸ E2B launcher smoke  template={args.template}  tag={name}")

    sandbox_id: str | None = None
    failures: list[str] = []
    try:
        print("\n[1/8] prepare (SDK + credentials)")
        launcher.prepare()
        _check(failures, True, "prepare passed")

        print("\n[2/8] provision")
        sandbox_id = launcher.provision(name)
        _check(failures, bool(sandbox_id), f"provisioned sandbox_id={sandbox_id}")

        print("\n[3/8] run: exit code, output, and env passthrough")
        result = launcher.run(sandbox_id, f'echo "$HOME"; printf %s "${marker_name}"', check=True)
        _check(failures, result.returncode == 0, "run exit code 0")
        _check(failures, result.stdout.strip() != "", "run captured stdout")
        _check(failures, marker_value in result.stdout, "injected env var visible inside sandbox")

        print("\n[4/8] run: non-zero exit (CommandExitException path)")
        # E2B raises CommandExitException on non-zero exit — the launcher must
        # catch it and surface the code/streams rather than letting it escape.
        failed = launcher.run(sandbox_id, "echo to-stderr >&2; exit 7", check=False)
        _check(failures, failed.returncode == 7, "non-zero exit surfaced as returncode 7")
        _check(failures, "to-stderr" in failed.stderr, "stderr captured on failing command")

        print("\n[5/8] put: ship a binary file and read it back")
        import tempfile
        from pathlib import Path

        payload = b"e2b-omnigent-smoke\x00\x01binary\n"
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp.write(payload)
            local = Path(tmp.name)
        try:
            launcher.put(sandbox_id, local, "/tmp/oa-smoke.bin")
        finally:
            local.unlink(missing_ok=True)
        readback = launcher.run(sandbox_id, "base64 -w0 /tmp/oa-smoke.bin", check=True)
        import base64

        _check(
            failures,
            base64.b64decode(readback.stdout.strip()) == payload,
            "uploaded file bytes match read-back",
        )

        print("\n[6/8] keep_alive (set_timeout to max) + attach (connect + is_running)")
        launcher.keep_alive(sandbox_id)  # soft-fail; must not raise
        _check(failures, True, f"keep_alive extended (target {resolve_max_lifetime_s() // 3600}h)")
        # Fresh launcher to force a real Sandbox.connect (not the cached handle).
        E2BSandboxLauncher(template=args.template).attach(sandbox_id)
        _check(failures, True, "attach validated a running sandbox")

        print("\n[7/8] stream_exec: combined stdout+stderr line stream")
        proc = launcher.stream_exec(sandbox_id, "echo out-line; echo err-line >&2")
        streamed = "".join(proc.lines)
        code = proc.wait()
        _check(failures, code == 0, "stream_exec wait() exit code 0")
        _check(
            failures,
            "out-line" in streamed and "err-line" in streamed,
            "stream_exec merged stdout and stderr",
        )

        print("\n[8/8] public egress (outbound HTTPS from inside)")
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
                # A second terminate of an already-killed sandbox must be a no-op.
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
    print("SMOKE TEST PASSED — every E2B launcher primitive works against a live sandbox.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
