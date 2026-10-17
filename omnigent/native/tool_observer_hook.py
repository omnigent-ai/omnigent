"""Shared, observational post-tool hook for native harnesses."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

from omnigent.native.relay_hook import relay_hook_command


def hook_settings(bridge_dir: Path, fallback: str) -> dict[str, object]:
    """
    Build the PostToolUse observer hook, answered by the relay without Python.

    The relay acknowledges before it records, so the tool result never waits
    on the observer.

    :param bridge_dir: Bridge directory holding the relay coordinates.
    :param fallback: The harness's own ``observe-tool`` Python hook command
        (its trust-registered module), run when no relay answers.
    :returns: One command-hook entry.
    """
    return {
        "type": "command",
        "command": relay_hook_command(bridge_dir, "/hook/observe-tool", fallback, max_time_s=1),
        "timeout": 3,
    }


def main(argv: list[str]) -> int:
    """Send bounded hook input to the local relay, with no model-visible output."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--bridge-dir", required=True)
    args = parser.parse_args(argv)
    try:
        raw = sys.stdin.read(1_048_577).encode("utf-8")
        if len(raw) > 1_048_576:
            return 0
        payload = json.loads(raw)
        if not isinstance(payload, dict) or payload.get("hook_event_name") != "PostToolUse":
            return 0
        relay = json.loads((Path(args.bridge_dir) / "tool_relay.json").read_text())
        request = urllib.request.Request(
            relay["url"].rstrip("/") + "/hook/observe-tool",
            data=raw,
            headers={
                "Authorization": "Bearer " + relay["token"],
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=1) as response:
            response.read()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"omnigent tool observer: {type(exc).__name__}", file=sys.stderr)
    return 0
