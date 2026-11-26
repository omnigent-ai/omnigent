"""What a session's runner reads in its user shells, for journeys that close them.

A close skips its confirmation only once the runner reads every shell idle, so a journey
waits for that reading rather than racing a shell that is still starting.
"""

from __future__ import annotations

import httpx

from tests._helpers.polling import poll_until


def shell_states(base_url: str, session_id: str) -> list[str]:
    """What the runner reads in each of the session's zsh shells, e.g. ``["busy"]``."""
    terminals = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/resources/terminals",
        params={"order": "asc", "limit": 1000},
        timeout=10.0,
    )
    terminals.raise_for_status()
    states = []
    for terminal in terminals.json()["data"]:
        if not terminal["id"].startswith("terminal_zsh"):
            continue
        shell = httpx.get(
            f"{base_url}/v1/sessions/{session_id}/resources/terminals/{terminal['id']}/shell",
            timeout=10.0,
        )
        states.append(shell.json().get("state") if shell.status_code == 200 else "missing")
    return states


def wait_until_shells_read(base_url: str, session_id: str, expected: list[str]) -> None:
    """Wait until the runner reads the session's zsh shells as ``expected``, in launch order."""
    poll_until(lambda: shell_states(base_url, session_id) == expected, timeout_s=30)
