#!/usr/bin/env python3
"""Lifecycle-hooks server for the Omnigent host running as a Lambda MicroVM.

Binds the microVM hooks port (default 9000) and answers the platform's lifecycle
probes so snapshotting and idle suspend/resume work. It also owns host startup:
``RunMicrovm`` carries no per-launch environment, so the launcher passes the host
identity + token in the ``runHookPayload``, and the platform delivers that string
as the **body of the /run hook** once the VM thaws from its build-time snapshot.
This server reads that body and starts ``omnigent host`` with the identity.

- ``/ready`` (build-time): 200 once the image is booted, so the snapshot is
  taken after boot. The host is NOT started here — at build time there is no
  identity yet; the snapshot captures a warm image waiting for /run.
- ``/run`` (runtime, first launch): the body carries the launcher's payload
  (``OMNIGENT_HOST_ID`` / ``_NAME`` / ``_TOKEN`` / ``OMNIGENT_SERVER`` / harness
  env), accepted whether wrapped as ``{microvmId, runHookPayload}`` or delivered
  as the bare identity map. This server injects it into a child ``omnigent host``
  process's env (never to disk) and spawns it; the host dials back over the
  launch-token tunnel. Acknowledged fast (1-60s budget); tunnel work is async.
- ``/resume`` (runtime): the host reconnects its own tunnel on thaw; ack fast.
- ``/suspend`` / ``/terminate`` / ``/validate``: fast acknowledgements.

Stdlib only, so it runs under the image's bare ``python3``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HOOKS_PORT = int(os.environ.get("OMNIGENT_MICROVM_HOOKS_PORT", "9000"))

# The started host's stdout/stderr go here so a start failure (bad clone, missing
# binary, unset HOME) is diagnosable instead of vanishing — the "hard-to-diagnose"
# failure the identity gate warns about must not itself be silent.
_HOST_LOG_PATH = os.environ.get("OMNIGENT_MICROVM_HOST_LOG", "/tmp/omnigent-host.log")

_PREFIX = "/aws/lambda-microvms/runtime/v1"
_READY_PATH = f"{_PREFIX}/ready"
_RUN_PATH = f"{_PREFIX}/run"
_RUNTIME_PATHS = frozenset(
    {
        f"{_PREFIX}/run",
        f"{_PREFIX}/resume",
        f"{_PREFIX}/suspend",
        f"{_PREFIX}/terminate",
        f"{_PREFIX}/validate",
    }
)

# Guard so a redelivered /run (platform retry) racing the original starts the
# host only once. ThreadingHTTPServer handles each request on its own thread, so
# the check-and-set must hold the lock — a bare Event.is_set()/set() has a window
# where two concurrent /run deliveries both spawn a host.
_host_started = False
_host_start_lock = threading.Lock()

# The env keys the launcher always puts in runHookPayload; the host can't dial
# back and authenticate without all of them.
_REQUIRED_IDENTITY = (
    "OMNIGENT_SERVER",
    "OMNIGENT_HOST_ID",
    "OMNIGENT_HOST_NAME",
    "OMNIGENT_HOST_TOKEN",
)


def _extract_identity_payload(raw_body: bytes) -> dict | None:
    """Recover the identity env map from the /run body, tolerating its shapes.

    The platform's exact framing isn't contractually fixed, so accept any of:
      1. wrapped: ``{"microvmId": "...", "runHookPayload": "<json-string-or-obj>"}``
         (the shape the launcher emits) — unwrap and parse the inner value;
      2. the identity env map delivered directly as the JSON body;
      3. the inner runHookPayload JSON string delivered as the whole body.
    Returns the identity dict, or ``None`` when the body isn't usable JSON.
    """
    try:
        parsed = json.loads(raw_body or b"{}")
    except (ValueError, TypeError):
        return None
    # A bare JSON string body (shape 3): parse it once more into the env map.
    if isinstance(parsed, str):
        try:
            parsed = json.loads(parsed)
        except (ValueError, TypeError):
            return None
    if not isinstance(parsed, dict):
        return None
    # Shape 1: unwrap the runHookPayload wrapper (string or object).
    inner = parsed.get("runHookPayload")
    if isinstance(inner, str) and inner:
        try:
            inner = json.loads(inner)
        except (ValueError, TypeError):
            return None
    if isinstance(inner, dict):
        return inner
    # Shape 2: the body itself is already the identity map (no wrapper).
    return parsed


def _start_host_from_payload(raw_body: bytes) -> None:
    """Spawn ``omnigent host`` from the /run payload's identity env.

    The payload is the launcher's ``runHookPayload``: a flat map of env names to
    values (host identity, token, server URL, harness credentials). We inject it
    into the child process env — the identity travels in-process only, never to
    disk (a persisted copy would land world-readable and be baked into every
    suspend snapshot).
    """
    global _host_started
    payload = _extract_identity_payload(raw_body)
    # Require the FULL host identity before starting. A partial payload — a bare
    # {microvmId} from a manual run-microvm, or a body missing the id/name/token
    # — would spawn a host that can't authenticate its dial-back and fails in a
    # hard-to-diagnose way. Demand the complete set the launcher always sends.
    if payload is None or any(not payload.get(key) for key in _REQUIRED_IDENTITY):
        print("hooks: /run payload missing required host identity; not starting", file=sys.stderr)
        return
    with _host_start_lock:
        if _host_started:
            return
        _host_started = True
    env = {k: str(v) for k, v in payload.items()}
    child_env = {**os.environ, **env}
    # Detached so the /run ack returns fast; the host holds its own tunnel. Child
    # output goes to a log file so a start failure is diagnosable (not DEVNULL).
    try:
        # Outlives this try block (used by the Popen call and closed in the
        # finally below), so a context manager doesn't fit here.
        log_fh = open(_HOST_LOG_PATH, "ab")  # noqa: SIM115
    except OSError:
        log_fh = subprocess.DEVNULL
    try:
        subprocess.Popen(
            ["/opt/omnigent/start_host.sh"],
            env=child_env,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as exc:
        # The spawn itself failed (missing/non-executable start_host.sh, fork
        # limit, ...). Clear the started flag so a platform retry of /run can
        # try again — otherwise the sticky flag strands the VM host-less.
        with _host_start_lock:
            _host_started = False
        print(f"hooks: failed to spawn start_host.sh: {exc}", file=sys.stderr)
    finally:
        # The child inherits its own copy of the fd on a successful spawn; the
        # parent's handle is only needed to hand off stdout, so close it here
        # either way (spawn success or failure) to avoid leaking it for the
        # life of the MicroVM.
        if log_fh is not subprocess.DEVNULL:
            log_fh.close()


class _HookHandler(BaseHTTPRequestHandler):
    """Answers the platform lifecycle probes; /run also starts the host."""

    def _ok(self) -> None:
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        # /ready is a GET in the platform contract; a 200 signals "booted".
        self._ok()

    def _read_body(self) -> bytes:
        """Read the request body, tolerating a missing/garbled Content-Length
        and chunked transfer encoding (either would otherwise drop the payload
        or raise on the int() parse)."""
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            chunks = []
            while True:
                line = self.rfile.readline().strip()
                try:
                    size = int(line.split(b";", 1)[0], 16)
                except ValueError:
                    break
                if size == 0:
                    self.rfile.readline()  # trailing CRLF
                    break
                chunks.append(self.rfile.read(size))
                self.rfile.readline()  # per-chunk CRLF
            return b"".join(chunks)
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            length = 0
        return self.rfile.read(length) if length > 0 else b""

    def do_POST(self) -> None:
        if self.path == _RUN_PATH:
            _start_host_from_payload(self._read_body())
            self._ok()
        elif self.path in _RUNTIME_PATHS or self.path == _READY_PATH:
            self._ok()
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        # Quiet: the host's own logging is the signal; hook probes are noise.
        del format, args


def main() -> None:
    """Serve the hooks port until the process is terminated."""
    # Binds all interfaces and starts the host from an unauthenticated /run body
    # (whose payload becomes the host's env, including the dial-back target).
    # This is safe only because the platform reaches the hooks port over the
    # MicroVM's ingress connector — it is NOT exposed to the guest workload or
    # the public internet. The isolation boundary is the auth; do not expose
    # this port to any less-trusted network.
    server = ThreadingHTTPServer(("0.0.0.0", _HOOKS_PORT), _HookHandler)
    server.serve_forever()


if __name__ == "__main__":
    main()
