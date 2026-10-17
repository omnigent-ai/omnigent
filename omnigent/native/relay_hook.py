"""Native hook commands answered by the runner's tool relay, without a Python start.

A per-tool hook that spawns Python pays ~0.1-0.9 s of interpreter start and
imports on every call. The runner's tool relay already holds that code, so a
hook can be a ``/bin/sh`` + ``curl`` POST of its stdin to a relay route whose
response body is the hook output.
"""

from __future__ import annotations

import shlex
from pathlib import Path

# Shell-sourceable sibling of ``tool_relay.json`` (written by the relay on every
# start, so hooks follow a restarted runner to its new port).
TOOL_RELAY_ENV_FILE = "tool_relay.env"


def relay_hook_command(bridge_dir: Path, route: str, fallback: str, *, max_time_s: int) -> str:
    """
    Build a hook command that POSTs its stdin to the relay at *route*.

    The relay's coordinates are re-read from ``tool_relay.env`` on every
    event. When that file is absent (a hook can beat the relay's start) or
    curl fails, the same stdin is replayed into *fallback*, the harness's
    Python hook, which owns the direct-server path and any fail-closed
    shaping. Wrapped in ``/bin/sh -c`` so it does not depend on the shell
    the harness runs hook commands with.

    :param bridge_dir: Bridge directory holding ``tool_relay.env``.
    :param route: Relay path, e.g. ``"/hook/observe-tool"``.
    :param fallback: Shell command for the Python hook, e.g.
        ``"/venv/bin/python -I -m omnigent.harnesses.codex_native.hook evaluate-policy
        --bridge-dir /b"``.
    :param max_time_s: curl's total budget in seconds.
    :returns: The shell command string.
    """
    env = shlex.quote(str(bridge_dir / TOOL_RELAY_ENV_FILE))
    script = (
        "p=$(cat); "
        f"if [ -r {env} ]; then . {env}; "
        f"out=$(printf '%s' \"$p\" | curl -sf --max-time {max_time_s} "
        '-H "Authorization: Bearer $OMNIGENT_RELAY_TOKEN" '
        "-H 'Content-Type: application/json' --data-binary @- "
        f'"$OMNIGENT_RELAY_URL{route}" 2>/dev/null) '
        "&& { printf '%s' \"$out\"; exit 0; }; fi; "
        f"printf '%s' \"$p\" | {fallback}"
    )
    return shlex.join(["/bin/sh", "-c", script])
