"""Trusted client the PATH shim execs INSIDE the sandbox.

Fetches a tool's credentials from the parent broker over the AF_UNIX socket,
injects them into THIS process's environment, and execs the real tool — so the
secret enters only the tool's process tree, never the agent's ambient env. The
secret is never placed on argv.

This module is intentionally self-contained: it imports ONLY the standard
library and is copied verbatim into the (bound) shim directory by
``omnigent.inner.credential_broker._write_shims``, then invoked by absolute
file path. It must never import ``omnigent`` (the omnigent package may not be
reachable inside every sandbox view; ``sys.executable`` always is).
"""

import json
import os
import socket
import sys


def _request(socket_path: str, payload: dict) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(socket_path)
        s.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    resp = json.loads(buf.decode("utf-8"))
    if "error" in resp:
        print(f"cred-broker: {resp['error']}", file=sys.stderr)
        raise SystemExit(2)
    return resp


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    # argv contract: --socket <path> --tool <name> -- <toolargs...>
    socket_path = args[args.index("--socket") + 1]
    tool = args[args.index("--tool") + 1]
    rest = args[args.index("--") + 1 :]
    token = os.environ.get("OMNIGENT_CRED_BROKER_TOKEN", "")
    resp = _request(socket_path, {"tool": tool, "token": token})
    os.environ.update(resp["env"])
    binary = resp["binary"]
    os.execv(binary, [binary, *rest])  # replaces this process; no return on success


if __name__ == "__main__":
    raise SystemExit(main())
