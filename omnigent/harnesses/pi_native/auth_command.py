"""Private, bounded token-command transport for native Pi providers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import shlex
import shutil
import time
from importlib.resources import files
from pathlib import Path

from omnigent.harnesses.pi_native.bridge import _atomic_json, _atomic_text

_logger = logging.getLogger(__name__)

AUTH_DIR_ENV = "OMNIGENT_PI_NATIVE_AUTH_DIR"
AUTH_DIR_NAME = "omnigent-auth"
# Leave time for cleanup and delivery inside Pi's ten-second shell deadline.
AUTH_TIMEOUT_MS = 8_000


def wrap_auth_command(
    agent_dir: Path,
    provider_id: str,
    command: str,
    *,
    refresh_interval_ms: int = 0,
    refresh_timeout_ms: int = 20_000,
) -> str:
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
    # Deduplicate filenames only; this is not a password verifier.
    cache_name = hashlib.sha256(command.encode(), usedforsecurity=False).hexdigest()
    _atomic_json(
        config,
        {
            "command": command,
            "timeoutMs": AUTH_TIMEOUT_MS,
            "refreshIntervalMs": refresh_interval_ms,
            "refreshTimeoutMs": refresh_timeout_ms,
            "cachePath": str(auth_dir / f"token-{cache_name}.json"),
        },
    )
    argv = [shutil.which("node") or "node", str(helper), str(config)]
    return "!" + shlex.join(argv)


async def refresh_model_auth(auth_dir: Path) -> float:
    """Warm configured token caches outside Pi's ten-second request deadline."""
    delay_s = 30.0
    for config_path in sorted(auth_dir.glob("auth-*.json")):
        if config_path.name.endswith(".result.json"):
            continue
        try:
            config = json.loads(config_path.read_text())
        except (OSError, ValueError):
            continue
        interval = config.get("refreshIntervalMs", 0)
        if interval <= 0:
            continue
        delay_s = min(delay_s, interval / 4_000)
        try:
            cached = json.loads(Path(config["cachePath"]).read_text())
            expires_at = cached["expiresAt"]
            cached_at = cached.get("cachedAt", expires_at - interval)
            if expires_at - time.time() * 1000 > min(interval, expires_at - cached_at) / 2:
                continue
        except (OSError, ValueError, KeyError, TypeError):
            pass
        try:
            process = await asyncio.create_subprocess_exec(
                shutil.which("node") or "node",
                str(auth_dir / "command.cjs"),
                str(config_path),
                "--refresh",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError as error:
            _logger.warning(
                "Pi model auth refresh for %s failed to start (%s)",
                config_path.name,
                type(error).__name__,
            )
            continue
        try:
            if await process.wait() != 0:
                _logger.warning(
                    "Pi model auth refresh for %s exited %s", config_path.name, process.returncode
                )
        finally:
            if process.returncode is None:
                process.terminate()
                await process.wait()
    return delay_s


async def supervise_model_auth(auth_dir: Path) -> None:
    """Refresh model credentials before the private request cache expires."""
    while True:
        await asyncio.sleep(await refresh_model_auth(auth_dir))
