"""Shared, observational post-tool hook for native harnesses."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import urllib.request
from collections.abc import Callable
from pathlib import Path


def hook_settings(
    bridge_dir: Path,
    python: str,
    module: str,
    *,
    wrap_python: Callable[[str], str] | None = None,
) -> dict[str, object]:
    """
    Post each tool event to the runner's relay with curl instead of starting Python.

    Runs *module* instead until the relay writes ``tool_relay.env``, or without
    curl; naming *module* also keeps codex trusting the hook. *wrap_python*
    wraps only that Python command. The relay token reaches curl through a
    here-doc, never through process arguments other local users can read, and
    ``-q`` and ``--noproxy`` keep a user's ``.curlrc`` and proxy settings from
    logging the request or sending it anywhere but the loopback relay.
    """
    python_command = shlex.join(
        [python, "-I", "-m", module, "observe-tool", "--bridge-dir", str(bridge_dir)]
    )
    if wrap_python is not None:
        python_command = wrap_python(python_command)
    relay_env = shlex.quote(str(bridge_dir / "tool_relay.env"))
    command = (
        f"if [ -r {relay_env} ] && command -v curl >/dev/null 2>&1; then . {relay_env}; "
        "curl -q -sS --noproxy '*' --max-time 1 -o /dev/null -H @/dev/fd/3 "
        "-H 'Content-Type: application/json' --data-binary @- "
        '"$OMNIGENT_RELAY_URL/hook/observe-tool" 3<<EOF\n'
        "Authorization: Bearer $OMNIGENT_RELAY_TOKEN\n"
        "EOF\n"
        f"exit 0; fi; exec {python_command}"
    )
    return {"type": "command", "command": command, "timeout": 3}


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
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=1) as response:
            response.read()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"omnigent tool observer: {type(exc).__name__}", file=sys.stderr)
    return 0
