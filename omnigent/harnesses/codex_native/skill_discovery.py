"""Read Codex's loaded plugin skills without starting a model turn."""

from __future__ import annotations

import asyncio
import contextlib
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from websockets.exceptions import WebSocketException

from omnigent.harnesses.codex_native.app_server import (
    CodexAppServerClient,
    _allocate_loopback_port,
    _start_codex_model_discovery_process,
    _stop_codex_model_discovery_process,
    _wait_for_discovery_listener,
)
from omnigent.util.json_types import JsonObject


async def _loaded_skills(codex_path: str, codex_home: Path, cwd: Path) -> list[JsonObject]:
    port = _allocate_loopback_port()
    listen_url = f"ws://127.0.0.1:{port}"
    discovery = await _start_codex_model_discovery_process(
        codex_path=codex_path,
        listen_url=listen_url,
        env={**os.environ, "CODEX_HOME": str(codex_home)},
        cwd=cwd,
    )
    client = CodexAppServerClient(ws_url=listen_url, client_name="omnigent-codex-skill-discovery")
    try:
        async with asyncio.timeout(5):
            await _wait_for_discovery_listener(discovery, port)
            await client.connect()
            response = await client.request(
                "skills/list", {"cwds": [str(cwd)], "forceReload": True}
            )
        result = response.get("result")
        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, list):
            raise ValueError("Codex skills/list data must be a list")
        return [
            skill
            for entry in data
            if isinstance(entry, dict) and isinstance(entry.get("skills"), list)
            for skill in entry["skills"]
            if isinstance(skill, dict)
        ]
    except WebSocketException as exc:
        raise RuntimeError(f"Codex skills/list connection failed: {exc}") from exc
    finally:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(client.close(), timeout=1)
        await _stop_codex_model_discovery_process(discovery)


def loaded_codex_skills(codex_path: str, codex_home: Path, cwd: Path) -> list[JsonObject]:
    """Run the bounded query from synchronous callers, including async applications."""
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="codex-skill-discovery") as pool:
        return pool.submit(
            lambda: asyncio.run(_loaded_skills(codex_path, codex_home, cwd))
        ).result()
