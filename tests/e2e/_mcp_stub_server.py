"""Minimal stdio MCP stub server for the codex-native /clear e2e test."""

import json
import os
import sys
import time


def main() -> None:
    name, log = sys.argv[1], sys.argv[2]
    with open(log, "a") as fh:
        rec = {
            "name": name,
            "pid": os.getpid(),
            "pgid": os.getpgid(0),
            "ppid": os.getppid(),
            "t": time.time(),
        }
        fh.write(json.dumps(rec) + "\n")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method, mid = msg.get("method"), msg.get("id")
        if method == "initialize":
            result = {
                "protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-03-26"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": name, "version": "0.0.1"},
            }
        elif method == "tools/list":
            result = {"tools": []}
        else:
            result = {}
        if mid is not None:
            sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}) + "\n")
            sys.stdout.flush()
    time.sleep(600)


if __name__ == "__main__":
    main()
