#!/usr/bin/env python3
"""
Smoke test for the CoreWeave Sandbox (cwsandbox) provider.

Talks directly to the CW Sandbox HTTP API to validate the
primitives the managed-host launcher relies on: provision → RUNNING,
unary exec, AddFile, public egress, detach survival (setsid nohup), and
terminate.

    export CWSANDBOX_API_KEY=...
    python deploy/cwsandbox/smoke_test.py [--image IMG] [--keep]

Transport is curl (subprocess): the system Python's TLS may be too old
to reach the API, while curl uses the OS's. Zero pip dependencies.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
from urllib.parse import quote

DEFAULT_IMAGE = "python:3.11-slim"
MAX_LIFETIME_S = 3600
PROVISION_TIMEOUT_S = 300
POLL_INTERVAL_S = 3.0


class SmokeError(Exception):
    """A smoke-test check failed."""


class Client:
    """CW Sandbox API client over curl (Bearer auth, JSON)."""

    def __init__(self, base_url: str, api_key: str) -> None:
        self._base = base_url.rstrip("/")
        self._auth = f"Authorization: Bearer {api_key}"

    def _request(self, method: str, path: str, op: str, body: dict | None = None) -> dict:
        cmd = ["curl", "-sS", "-X", method, f"{self._base}{path}",
               "-H", self._auth, "-H", "Accept: application/json", "-w", "\n%{http_code}"]
        if body is not None:
            cmd += ["-H", "Content-Type: application/json", "--data-binary", json.dumps(body)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=70.0)
        except FileNotFoundError as exc:
            raise SmokeError("curl not found on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise SmokeError(f"{op} -> timed out") from exc
        if proc.returncode != 0:
            raise SmokeError(f"{op} -> curl failed: {proc.stderr.strip()[:300]}")
        raw, _, status = proc.stdout.rpartition("\n")
        if not status.isdigit() or int(status) >= 400:
            raise SmokeError(f"{op} -> HTTP {status}: {raw[:500]}")
        return json.loads(raw) if raw.strip() else {}

    def provision(self, *, image: str, name: str) -> str:
        body = {
            "containerImage": image,
            "command": "sleep",
            "args": ["infinity"],
            "resources": {"cpu": "1", "memory": "1Gi"},
            "maxLifetimeSeconds": MAX_LIFETIME_S,
            "network": {"egressMode": "internet"},  # egress defaults to none
            "tags": ["omnigent-smoke", name],
        }
        return self._request("POST", "/v1beta2/sandboxes", "provision", body)["sandboxId"]

    def wait_running(self, sandbox_id: str) -> None:
        deadline = time.monotonic() + PROVISION_TIMEOUT_S
        last = ""
        while time.monotonic() < deadline:
            payload = self._request("GET", f"/v1beta2/sandboxes/{sandbox_id}", "get")
            status = payload.get("sandboxStatus", "")
            if status != last:
                print(f"    status: {status}")
                last = status
            if status == "SANDBOX_STATUS_RUNNING":
                return
            if status.startswith("SANDBOX_STATUS_") and status not in (
                "SANDBOX_STATUS_CREATING", "SANDBOX_STATUS_PENDING", "SANDBOX_STATUS_UNSPECIFIED"
            ):
                raise SmokeError(f"terminal status {status}: {payload.get('statusReason', '?')}")
            time.sleep(POLL_INTERVAL_S)
        raise SmokeError(f"not RUNNING within {PROVISION_TIMEOUT_S}s (last={last})")

    def exec(self, sandbox_id: str, command: str) -> tuple[int, str, str]:
        result = self._request(
            "POST", f"/v1beta2/sandboxes/{sandbox_id}/exec", "exec",
            {"command": ["bash", "-lc", command], "maxTimeoutSeconds": 60},
        ).get("result", {})
        # exitCode is omitted from the response when it is 0 (zero default).
        return (int(result.get("exitCode", 0)),
                _b64(result.get("stdout", "")), _b64(result.get("stderr", "")))

    def put_file(self, sandbox_id: str, path: str, contents: bytes) -> None:
        payload = self._request(
            "POST", f"/v1beta2/sandboxes/{sandbox_id}/files", "addFile",
            {"filepath": path, "fileContents": base64.b64encode(contents).decode("ascii")},
        )
        if not payload.get("success", False):
            raise SmokeError(f"addFile failed: {payload.get('errorMessage')}")

    def get_file(self, sandbox_id: str, path: str) -> bytes:
        payload = self._request(
            "GET", f"/v1beta2/sandboxes/{sandbox_id}/files/{quote(path, safe='')}", "retrieveFile",
        )
        return base64.b64decode(payload.get("fileContents", ""))

    def stop(self, sandbox_id: str) -> None:
        self._request("POST", f"/v1beta2/sandboxes/{sandbox_id}/stop", "stop", {})


def _b64(value: str) -> str:
    try:
        return base64.b64decode(value).decode("utf-8", errors="replace") if value else ""
    except Exception:  # noqa: BLE001 — best-effort decode of an opaque field
        return value


def _check(failures: list[str], ok: bool, label: str) -> None:
    print(f"    {'✓' if ok else '✗'} {label}")
    if not ok:
        failures.append(label)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default=DEFAULT_IMAGE)
    parser.add_argument("--keep", action="store_true", help="don't terminate at the end")
    args = parser.parse_args()

    api_key = os.environ.get("CWSANDBOX_API_KEY")
    if not api_key:
        print("ERROR: set CWSANDBOX_API_KEY", file=sys.stderr)
        return 2
    base_url = os.environ.get("CWSANDBOX_BASE_URL", "https://api.cwsandbox.com")

    client = Client(base_url, api_key)
    name = f"smoke-{int(time.time())}"
    print(f"▸ smoke test against {base_url}  image={args.image}  tag={name}")

    sandbox_id: str | None = None
    failures: list[str] = []
    try:
        print("\n[1/6] provision")
        sandbox_id = client.provision(image=args.image, name=name)
        print(f"    sandbox_id={sandbox_id}")
        client.wait_running(sandbox_id)
        _check(failures, True, "RUNNING")

        print("\n[2/6] unary exec")
        code, out, _ = client.exec(sandbox_id, 'printf %s "$HOME"; echo; uname -sm')
        _check(failures, code == 0, "exec exit code 0")
        _check(failures, out.strip() != "", "exec returned $HOME")

        print("\n[3/6] AddFile (+ read-back via exec)")
        marker = b"cwsandbox-omnigent-smoke\n"
        client.put_file(sandbox_id, "/tmp/oa-smoke.txt", marker)
        _, out, _ = client.exec(sandbox_id, "cat /tmp/oa-smoke.txt")
        _check(failures, out.encode() == marker, "file readable via exec after AddFile")
        # RetrieveFile-over-REST can't route an absolute {filepath}; reads
        # (never needed by the launcher) go through exec+base64 instead.
        try:
            ok = client.get_file(sandbox_id, "/tmp/oa-smoke.txt") == marker
            print(f"    {'✓' if ok else 'ℹ'} RetrieveFile REST (bonus): {'works' if ok else 'no'}")
        except SmokeError:
            print("    ℹ RetrieveFile REST unsupported for absolute paths (expected)")

        print("\n[4/6] public egress (outbound https from inside)")
        code, out, _ = client.exec(
            sandbox_id,
            "python3 -c \"import urllib.request as u; "
            "print(u.urlopen('https://api.github.com', timeout=15).status)\"",
        )
        _check(failures, code == 0 and "200" in out, "outbound HTTPS reached api.github.com")

        print("\n[5/6] detach survives exec session (setsid nohup ... &)")
        client.exec(sandbox_id, "rm -f /tmp/oa-detach-alive")
        client.exec(
            sandbox_id,
            "setsid nohup sh -c 'sleep 4; echo alive > /tmp/oa-detach-alive' "
            "> /tmp/oa-detach.log 2>&1 < /dev/null & echo launched",
        )
        time.sleep(7)
        _, out, _ = client.exec(sandbox_id, "cat /tmp/oa-detach-alive 2>/dev/null")
        _check(failures, out.strip() == "alive", "detached process kept running after exec")

        print("\n[6/6] terminate")
        if args.keep:
            print(f"    --keep set; leaving {sandbox_id} running")
        else:
            client.stop(sandbox_id)
            _check(failures, True, "stop accepted")
            sandbox_id = None
    except SmokeError as exc:
        failures.append(f"FATAL: {exc}")
    finally:
        if sandbox_id is not None and not args.keep:
            try:
                client.stop(sandbox_id)
                print(f"\n  (cleaned up {sandbox_id})")
            except Exception as exc:  # noqa: BLE001 — best-effort cleanup
                print(f"\n  WARNING: failed to clean up {sandbox_id}: {exc}")

    print("\n" + "=" * 60)
    if failures:
        print("SMOKE TEST FAILED:")
        for f in failures:
            print(f"  ✗ {f}")
        return 1
    print("SMOKE TEST PASSED — every managed-host primitive works.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
