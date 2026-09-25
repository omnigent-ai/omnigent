#!/usr/bin/env python3
"""Check serverless sandbox primitives with explicit W&B SDK authentication.

    export WANDB_API_KEY=...
    python tests/e2e/integrations/deploy/cwsandbox/smoke_test.py [--image IMG] [--keep]

Install omnigent[cwsandbox] first. This checks provision, exec, file upload,
HTTPS egress, detached processes, and stop; it does not run an Omnigent host.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from cwsandbox import AuthStrategy, EgressRule, NetworkOptions, Sandbox

DEFAULT_IMAGE = "python:3.12-slim"
MAX_LIFETIME_S = 600


def _check(failures: list[str], ok: bool, label: str) -> None:
    print(f"    {'✓' if ok else '✗'} {label}")
    if not ok:
        failures.append(label)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--keep", action="store_true", help="don't terminate at the end")
    args = parser.parse_args()
    if not os.environ.get("WANDB_API_KEY"):
        print("ERROR: set WANDB_API_KEY", file=sys.stderr)
        return 2

    sandbox: Sandbox | None = None
    failures: list[str] = []
    try:
        print("\n[1/6] provision")
        sandbox = Sandbox.run(
            "sleep",
            "infinity",
            auth=AuthStrategy.WANDB,
            container_image=args.image,
            placement_mode="serverless",
            max_lifetime_seconds=MAX_LIFETIME_S,
            resources={"cpu": "1", "memory": "1Gi"},
            network=NetworkOptions(egress=[EgressRule(dns_name="api.github.com")]),
            tags=["omnigent-smoke", f"smoke-{int(time.time())}"],
        )
        print(f"    sandbox_id={sandbox.sandbox_id}")
        sandbox.wait(timeout=300)
        _check(failures, True, "RUNNING")

        print("\n[2/6] exec")
        result = sandbox.exec(["bash", "-lc", 'printf %s "$HOME"; echo; uname -sm']).result()
        _check(failures, result.returncode == 0, "exec exit code 0")
        _check(failures, bool(result.stdout.strip()), "exec returned output")

        print("\n[3/6] file upload and read-back")
        marker = b"cwsandbox-omnigent-smoke\n"
        sandbox.write_file("/tmp/oa-smoke.txt", marker).result()
        result = sandbox.exec(["cat", "/tmp/oa-smoke.txt"]).result()
        _check(failures, result.stdout.encode() == marker, "uploaded file readable via exec")

        print("\n[4/6] public HTTPS egress")
        result = sandbox.exec(
            [
                "python3",
                "-c",
                "import urllib.request as u; "
                "print(u.urlopen('https://api.github.com', timeout=15).status)",
            ]
        ).result()
        _check(
            failures,
            result.returncode == 0 and "200" in result.stdout,
            "outbound HTTPS reached api.github.com",
        )

        print("\n[5/6] detached process survives exec session")
        sandbox.exec(
            [
                "bash",
                "-lc",
                "setsid nohup sh -c 'sleep 4; echo alive > /tmp/oa-detach-alive' "
                "> /tmp/oa-detach.log 2>&1 < /dev/null & echo launched",
            ]
        ).result()
        time.sleep(7)
        result = sandbox.exec(["cat", "/tmp/oa-detach-alive"]).result()
        _check(failures, result.stdout.strip() == "alive", "detached process kept running")

        print("\n[6/6] terminate")
        if args.keep:
            print(f"    --keep set; leaving {sandbox.sandbox_id} running")
        else:
            sandbox.stop().result()
            _check(failures, True, "stop accepted")
            sandbox = None
    except Exception as exc:
        failures.append(f"FATAL: {exc}")
    finally:
        if sandbox is not None and not args.keep:
            sandbox.stop().result()
            print(f"\n  (cleaned up {sandbox.sandbox_id})")

    if failures:
        print("SMOKE TEST FAILED:")
        for failure in failures:
            print(f"  ✗ {failure}")
        return 1
    print("SMOKE TEST PASSED — SDK primitives only; Omnigent host and LLM not exercised.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
