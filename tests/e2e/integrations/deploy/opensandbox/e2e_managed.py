#!/usr/bin/env python3
"""Managed OpenSandbox happy-path test against an existing Omnigent server.

The target server must be configured with ``sandbox.provider: opensandbox`` and
have a working model credential. This driver verifies the public behavior:
managed session creation, host registration, a completed agent turn, and
session deletion (which triggers OpenSandbox termination).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any

import httpx

PROMPT = "Reply with exactly E2E_OPEN_SANDBOX_OK and no other text."


def log(message: str) -> None:
    print(message, flush=True)


def _get(client: httpx.Client, path: str) -> dict[str, Any]:
    response = client.get(path)
    response.raise_for_status()
    return response.json()


def choose_agent(client: httpx.Client, requested: str | None) -> str:
    agents = _get(client, "/v1/agents").get("data", [])
    if not agents:
        raise SystemExit("server has no agents")
    if requested is None:
        chosen = agents[0]
    else:
        chosen = next((agent for agent in agents if agent.get("id") == requested), None)
        if chosen is None:
            raise SystemExit(f"agent {requested!r} was not found")
    log(f"agent={chosen['id']} ({chosen.get('name', 'unnamed')})")
    return str(chosen["id"])


def create_session(client: httpx.Client, agent_id: str) -> str:
    response = client.post(
        "/v1/sessions",
        json={
            "agent_id": agent_id,
            "host_type": "managed",
            "initial_items": [
                {
                    "type": "message",
                    "data": {
                        "role": "user",
                        "content": [{"type": "input_text", "text": PROMPT}],
                    },
                }
            ],
        },
    )
    response.raise_for_status()
    session_id = str(response.json()["id"])
    log(f"session={session_id}")
    return session_id


def assistant_text(items: list[dict[str, Any]]) -> str:
    blocks: list[str] = []
    for item in items:
        data = item.get("data") or {}
        if item.get("type") != "message" or data.get("role") != "assistant":
            continue
        for block in data.get("content") or []:
            if isinstance(block, str):
                blocks.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                blocks.append(block["text"])
    return " ".join(blocks).strip()


def wait_for_result(client: httpx.Client, session_id: str, timeout_s: float) -> str:
    deadline = time.monotonic() + timeout_s
    host_logged = False
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        try:
            last = _get(client, f"/v1/sessions/{session_id}")
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status < 500 and status not in (408, 429):
                raise RuntimeError(f"session poll failed: HTTP {status}") from exc
            log(f"transient poll error: {exc}")
            time.sleep(4)
            continue
        except httpx.HTTPError as exc:
            log(f"transient poll error: {exc}")
            time.sleep(4)
            continue
        if last.get("last_task_error"):
            raise RuntimeError(f"managed task failed: {last['last_task_error']}")
        sandbox_status = last.get("sandbox_status") or {}
        if sandbox_status.get("stage") == "failed":
            raise RuntimeError(f"managed sandbox launch failed: {sandbox_status.get('error')}")
        if last.get("host_online") and not host_logged:
            log(f"host online: {last.get('host_id')}")
            host_logged = True
        text = assistant_text(last.get("items") or [])
        if text and last.get("status") == "idle":
            return text
        if last.get("status") == "failed":
            raise RuntimeError(f"session ended in failed status: {json.dumps(last)[:1200]}")
        time.sleep(4)
    raise TimeoutError(
        f"session did not finish within {timeout_s:.0f}s: {json.dumps(last)[:1200]}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True, help="Omnigent server base URL")
    parser.add_argument("--agent-id")
    parser.add_argument("--timeout", type=float, default=600)
    args = parser.parse_args()

    token = os.environ.get("OMNIGENT_TOKEN")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    session_id: str | None = None
    with httpx.Client(
        base_url=args.server.rstrip("/"),
        headers=headers,
        timeout=180,
    ) as client:
        info = _get(client, "/v1/info")
        log(f"server ready: {json.dumps(info)}")
        try:
            session_id = create_session(client, choose_agent(client, args.agent_id))
            reply = wait_for_result(client, session_id, args.timeout)
            if "E2E_OPEN_SANDBOX_OK" not in reply:
                raise RuntimeError(f"unexpected reply: {reply!r}")
            log("PASS: managed OpenSandbox agent turn completed")
            return 0
        finally:
            if session_id is not None:
                had_error = sys.exception() is not None
                try:
                    response = client.delete(f"/v1/sessions/{session_id}")
                    log(f"cleanup: HTTP {response.status_code}")
                    response.raise_for_status()
                except httpx.HTTPError as exc:
                    log(f"cleanup failed; sandbox may leak: {exc}")
                    if not had_error:
                        raise


if __name__ == "__main__":
    raise SystemExit(main())
