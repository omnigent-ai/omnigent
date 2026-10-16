"""Private, bounded token-command transport for native Pi providers."""

from __future__ import annotations

import hashlib
import os
import shlex
import shutil
from importlib.resources import files
from pathlib import Path

from omnigent.harnesses.pi_native.bridge import _atomic_json, _atomic_text

AUTH_DIR_ENV = "OMNIGENT_PI_NATIVE_AUTH_DIR"
AUTH_DIR_NAME = "omnigent-auth"
# Leave time for cleanup and delivery inside Pi's ten-second shell deadline.
AUTH_TIMEOUT_MS = 8_000


def wrap_auth_command(agent_dir: Path, provider_id: str, command: str) -> str:
    """Write a private helper and return the ``!command`` Pi should invoke."""
    auth_dir = agent_dir / AUTH_DIR_NAME
    auth_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(auth_dir, 0o700)
    helper = auth_dir / "command.cjs"
    _atomic_text(
        helper,
        files("omnigent.resources.pi_native").joinpath("auth_command.cjs").read_text(),
    )
    name = hashlib.sha256(provider_id.encode()).hexdigest()[:16]
    config = auth_dir / f"auth-{name}.json"
    _atomic_json(config, {"command": command, "timeoutMs": AUTH_TIMEOUT_MS})
    argv = [shutil.which("node") or "node", str(helper), str(config)]
    return "!" + shlex.join(argv)
