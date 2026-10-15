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
# curl config holding the relay's Authorization header. curl reads it with
# ``-K``, so the bearer never appears in a process argument list.
TOOL_RELAY_CURL_FILE = "tool_relay.curlrc"


def relay_curl_config(token: str) -> str:
    """
    Render the curl config that carries the relay bearer.

    :param token: Relay bearer token (``token_urlsafe`` alphabet, so it needs
        no escaping inside the quoted value).
    :returns: File text for :data:`TOOL_RELAY_CURL_FILE`.
    """
    return f'header = "Authorization: Bearer {token}"\n'


def relay_hook_command(
    bridge_dir: Path,
    route: str,
    fallback: str,
    *,
    max_time_s: int,
    max_bytes: int | None = None,
) -> str:
    """
    Build a hook command that POSTs its stdin to the relay at *route*.

    The relay's coordinates are re-read from ``tool_relay.env`` and its bearer
    from ``tool_relay.curlrc`` on every event. When either file is absent (a
    hook can beat the relay's start) or curl fails, the same stdin is replayed
    into *fallback*, the harness's Python hook, which owns the direct-server
    path and any fail-closed shaping. Wrapped in ``/bin/sh -c`` so it does not
    depend on the shell the harness runs hook commands with.

    :param bridge_dir: Bridge directory holding the relay files.
    :param route: Relay path, e.g. ``"/hook/observe-tool"``.
    :param fallback: Shell command for the Python hook, e.g.
        ``"/venv/bin/python -I -m omnigent.harnesses.codex_native.hook evaluate-policy
        --bridge-dir /b"``.
    :param max_time_s: curl's total budget in seconds.
    :param max_bytes: When set, a payload larger than this many bytes is
        dropped without contacting the relay or starting the fallback, as
        the Python observer drops it; nothing larger is ever buffered. Policy
        hooks leave it unset: a dropped ``PreToolUse`` payload would turn the
        verdict into "no opinion".
    :returns: The shell command string.
    """
    env = shlex.quote(str(bridge_dir / TOOL_RELAY_ENV_FILE))
    curlrc = shlex.quote(str(bridge_dir / TOOL_RELAY_CURL_FILE))
    if max_bytes is None:
        read_stdin = "p=$(cat); "
    else:
        limit = int(max_bytes)
        read_stdin = (
            f"p=$(head -c {limit + 1}); "
            f'[ "$(printf \'%s\' "$p" | wc -c)" -gt {limit} ] && exit 0; '
        )
    # Source the env file inside the command substitution so a failure there
    # cannot abort the main shell before the fallback runs.
    script = (
        read_stdin + f"if [ -r {env} ] && [ -r {curlrc} ]; then "
        # ``-q`` first: ignore any ambient ~/.curlrc; only the relay's config applies.
        f"out=$(. {env} && printf '%s' \"$p\" | curl -q -sf --max-time {max_time_s} -K {curlrc} "
        "-H 'Content-Type: application/json' --data-binary @- "
        f'"$OMNIGENT_RELAY_URL{route}" 2>/dev/null) '
        "&& { printf '%s' \"$out\"; exit 0; }; fi; "
        f"printf '%s' \"$p\" | {fallback}"
    )
    return shlex.join(["/bin/sh", "-c", script])
