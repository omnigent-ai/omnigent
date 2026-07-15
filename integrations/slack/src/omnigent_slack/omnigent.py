from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx


class OmnigentError(RuntimeError):
    pass


class RunnerUnavailableError(OmnigentError):
    pass


@dataclass(frozen=True, slots=True)
class OmnigentAuth:
    email: str | None = None
    header_name: str = "X-Forwarded-Email"
    session_cookie: str | None = None

    def headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.email:
            headers[self.header_name] = self.email
        if self.session_cookie:
            headers["Cookie"] = (
                self.session_cookie
                if "=" in self.session_cookie
                else f"ap_session={self.session_cookie}"
            )
        return headers


class OmnigentClient:
    def __init__(
        self,
        base_url: str,
        auth: OmnigentAuth | None = None,
        timeout: float = 30.0,
        runner_workspace: str | None = None,
        runner_host_id: str | None = None,
        runner_launch_timeout_seconds: float = 60.0,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout, read=None),
            headers=(auth or OmnigentAuth()).headers(),
        )
        self._runner_workspace = runner_workspace
        self._runner_host_id = runner_host_id
        self._runner_launch_timeout_seconds = runner_launch_timeout_seconds
        self._logger = logging.getLogger(__name__)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def create_session(self, agent_id: str, title: str) -> str:
        self._logger.info("Creating Omnigent session agent_id=%s title=%r", agent_id, title)
        response = await self._client.post(
            "/v1/sessions",
            json={"agent_id": agent_id, "title": title},
        )
        await _raise_for_status(response)
        payload = response.json()
        session_id = _extract_session_id(payload)
        if session_id is None:
            raise OmnigentError(f"Create session response did not include an id: {payload!r}")
        self._logger.info("Created Omnigent session session_id=%s", session_id)
        return session_id

    async def submit_message(self, session_id: str, text: str) -> None:
        self._logger.info(
            "Submitting Slack message to Omnigent session_id=%s chars=%s",
            session_id,
            len(text),
        )
        payload = {
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            },
        }
        response = await self._client.post(f"/v1/sessions/{session_id}/events", json=payload)
        await _raise_for_status(response)
        self._logger.debug("Submitted Omnigent message session_id=%s", session_id)

    async def bind_random_runner(self, session_id: str) -> str:
        runner_ids = await self.list_runner_ids()
        if not runner_ids:
            return await self.launch_random_runner(session_id)

        runner_id = random.choice(runner_ids)
        self._logger.info(
            "Binding random Omnigent runner session_id=%s runner_id=%s candidates=%s",
            session_id,
            runner_id,
            len(runner_ids),
        )
        response = await self._client.patch(
            f"/v1/sessions/{session_id}",
            json={"runner_id": runner_id},
        )
        await _raise_for_status(response)
        return runner_id

    async def list_runner_ids(self) -> list[str]:
        runner_ids = await self.list_runner_ids_from_hosts()
        self._logger.info("Loaded Omnigent runner ids from hosts count=%s", len(runner_ids))
        return runner_ids

    async def launch_random_runner(self, session_id: str) -> str:
        if not self._runner_workspace:
            raise OmnigentError(
                "No online Omnigent runners are available, and runner auto-launch is not "
                "configured. Set OMNIGENT_RUNNER_WORKSPACE to an absolute workspace path; "
                "optionally set OMNIGENT_RUNNER_HOST_ID to use a specific host."
            )

        host_id = self._runner_host_id or await self._select_random_online_host()
        self._logger.info(
            "Launching Omnigent runner session_id=%s host_id=%s workspace=%s",
            session_id,
            host_id,
            self._runner_workspace,
        )
        response = await self._client.post(
            f"/v1/hosts/{host_id}/runners",
            json={"session_id": session_id, "workspace": self._runner_workspace},
        )
        await _raise_for_status(response)
        payload = response.json()
        runner_id = _extract_runner_id(payload)
        if runner_id is None:
            raise OmnigentError(f"Launch runner response did not include a runner id: {payload!r}")

        await self.wait_for_runner_online(runner_id)
        self._logger.info(
            "Launched Omnigent runner session_id=%s runner_id=%s host_id=%s",
            session_id,
            runner_id,
            host_id,
        )
        return runner_id

    async def list_agents(self) -> list[dict[str, Any]]:
        self._logger.debug("Listing built-in Omnigent agents")
        response = await self._client.get("/v1/agents")
        await _raise_for_status(response)
        payload = response.json()
        data = _extract_list(payload, "data") or _extract_list(payload, "agents")
        if data is None:
            data = payload if isinstance(payload, list) else []
        agents = [item for item in data if isinstance(item, dict)]
        self._logger.info("Found built-in Omnigent agents count=%s", len(agents))
        return agents

    async def list_runners(self) -> list[dict[str, Any]]:
        self._logger.debug("Listing online Omnigent runners")
        response = await self._client.get("/v1/runners")
        await _raise_for_status(response)
        payload = response.json()
        data = _extract_list(payload, "data") or _extract_list(payload, "runners")
        if data is None:
            data = payload if isinstance(payload, list) else []
        runners = [item for item in data if isinstance(item, dict)]
        self._logger.info("Found online Omnigent runners count=%s", len(runners))
        return runners

    async def list_hosts(self) -> list[dict[str, Any]]:
        self._logger.debug("Listing Omnigent hosts")
        response = await self._client.get("/v1/hosts")
        await _raise_for_status(response)
        payload = response.json()
        data = _extract_list(payload, "hosts") or _extract_list(payload, "data")
        if data is None:
            data = payload if isinstance(payload, list) else []
        hosts = [item for item in data if isinstance(item, dict)]
        self._logger.info("Found Omnigent hosts count=%s", len(hosts))
        return hosts

    async def list_runner_ids_from_hosts(self) -> list[str]:
        hosts = await self.list_hosts()
        runner_ids: list[str] = []
        for host in hosts:
            if not _is_host_online(host):
                continue
            runner_ids.extend(_runner_ids_from_host(host))
        return sorted(set(runner_ids))

    async def wait_for_runner_online(self, runner_id: str) -> None:
        deadline = asyncio.get_running_loop().time() + self._runner_launch_timeout_seconds
        while True:
            response = await self._client.get(f"/v1/runners/{runner_id}/status")
            await _raise_for_status(response)
            payload = response.json()
            if isinstance(payload, dict) and payload.get("online") is True:
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise OmnigentError(
                    f"Timed out waiting for launched Omnigent runner to come online: {runner_id}"
                )
            await asyncio.sleep(1)

    async def _select_random_online_host(self) -> str:
        hosts = await self.list_hosts()
        host_ids = [
            host_id
            for host in hosts
            if _is_host_online(host) and (host_id := _host_id(host)) is not None
        ]
        if not host_ids:
            raise OmnigentError(
                "No online Omnigent hosts are available to launch a runner. "
                "Set OMNIGENT_RUNNER_HOST_ID to a specific online host, or start a host."
            )
        host_id = random.choice(host_ids)
        self._logger.info(
            "Selected random Omnigent host host_id=%s candidates=%s",
            host_id,
            len(host_ids),
        )
        return host_id

    @asynccontextmanager
    async def stream_session_events(
        self,
        session_id: str,
    ) -> AsyncIterator[AsyncIterator[dict[str, Any]]]:
        async with self._client.stream(
            "GET",
            f"/v1/sessions/{session_id}/stream",
            params={"idle": "false"},
        ) as response:
            await _raise_for_status(response)
            self._logger.debug("Connected to Omnigent SSE stream session_id=%s", session_id)
            yield iter_sse_events(response.aiter_lines())

    async def run_turn(self, session_id: str, text: str) -> AsyncIterator[dict[str, Any]]:
        try:
            async for event in self._run_turn_once(session_id, text):
                yield event
            return
        except RunnerUnavailableError:
            self._logger.info(
                "Session has no available runner; "
                "binding a random runner and retrying session_id=%s",
                session_id,
            )
            await self.bind_random_runner(session_id)

        async for event in self._run_turn_once(session_id, text):
            yield event

    async def _run_turn_once(self, session_id: str, text: str) -> AsyncIterator[dict[str, Any]]:
        async with self.stream_session_events(session_id) as events:
            await self.submit_message(session_id, text)
            async for event in events:
                self._logger.debug(
                    "Received Omnigent event session_id=%s type=%s",
                    session_id,
                    event.get("type"),
                )
                yield event
                if is_terminal_event(event):
                    self._logger.info(
                        "Omnigent turn reached terminal event session_id=%s type=%s",
                        session_id,
                        event.get("type"),
                    )
                    break

    async def latest_assistant_text(self, session_id: str) -> str | None:
        self._logger.debug("Fetching latest Omnigent assistant item session_id=%s", session_id)
        response = await self._client.get(
            f"/v1/sessions/{session_id}/items",
            params={"limit": 100, "order": "desc"},
        )
        await _raise_for_status(response)
        payload = response.json()
        items = payload.get("data", [])
        if not isinstance(items, list):
            return None
        for item in items:
            if isinstance(item, dict):
                text = extract_assistant_text(item)
                if text:
                    return text
        return None


async def iter_sse_events(lines: AsyncIterator[str]) -> AsyncIterator[dict[str, Any]]:
    event_name: str | None = None
    data_lines: list[str] = []

    async for raw_line in lines:
        line = raw_line.rstrip("\r")
        if line == "":
            event = _decode_sse_event(event_name, data_lines)
            event_name = None
            data_lines = []
            if event is None:
                continue
            if event == "[DONE]":
                break
            if isinstance(event, str):
                continue
            yield event
            continue

        if line.startswith(":"):
            continue

        field, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)

    event = _decode_sse_event(event_name, data_lines)
    if isinstance(event, dict):
        yield event


def is_terminal_event(event: dict[str, Any]) -> bool:
    # A turn ends at the SESSION level, not the response level. Orchestrator
    # agents emit a `response.completed`/`turn.completed` every time they end a
    # turn to wait on a background sub-agent, then resume with more responses in
    # the same turn — so treating those as terminal cuts the stream off at the
    # first sub-agent dispatch. `session.status` is the authoritative signal:
    # `running` -> `waiting` (parked on async work) -> `running` -> `idle`, and
    # only `idle`/`failed` mean the turn is truly over.
    event_type = str(event.get("type"))
    if event_type == "session.status":
        return str(event.get("status")) in {"idle", "failed"}
    # Explicit turn/response failure and cancellation still end the turn; keep
    # them as a fallback in case the session settles without an `idle` edge.
    return event_type in {
        "response.failed",
        "response.cancelled",
        "turn.failed",
        "turn.cancelled",
    }


def extract_delta(event: dict[str, Any]) -> str | None:
    if event.get("type") != "response.output_text.delta":
        return None
    delta = event.get("delta")
    return delta if isinstance(delta, str) else None


def extract_error_text(event: dict[str, Any]) -> str | None:
    event_type = str(event.get("type"))
    if event_type == "response.error":
        error = event.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str):
                return message
        message = event.get("message")
        if isinstance(message, str):
            return message
    if event_type in {"response.failed", "turn.failed"}:
        response = event.get("response")
        if isinstance(response, dict):
            last_error = response.get("error") or response.get("last_error")
            if isinstance(last_error, dict):
                message = last_error.get("message")
                if isinstance(message, str):
                    return message
        error = event.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str):
                return message
        if isinstance(error, str):
            return error
    return None


def extract_assistant_text(event_or_item: dict[str, Any]) -> str | None:
    if event_or_item.get("type") == "response.output_item.done":
        item = event_or_item.get("item")
        return extract_assistant_text(item) if isinstance(item, dict) else None

    item_type = event_or_item.get("type")
    if item_type != "message":
        return None

    data = event_or_item.get("data")
    message = data if isinstance(data, dict) else event_or_item
    if message.get("role") != "assistant":
        return None

    content = message.get("content")
    if not isinstance(content, list):
        return None

    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts).strip() or None


def _decode_sse_event(event_name: str | None, data_lines: list[str]) -> dict[str, Any] | str | None:
    if not data_lines:
        return None
    data = "\n".join(data_lines)
    if data == "[DONE]":
        return data
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as exc:
        raise OmnigentError(f"Invalid SSE JSON payload: {data}") from exc
    if not isinstance(payload, dict):
        return None
    if event_name and "type" not in payload:
        payload["type"] = event_name
    return payload


def _extract_session_id(payload: Any) -> str | None:
    if isinstance(payload, dict):
        for key in ("id", "session_id", "conversation_id"):
            value = payload.get(key)
            if isinstance(value, str):
                return value
        for key in ("session", "data"):
            value = _extract_session_id(payload.get(key))
            if value:
                return value
    return None


def _extract_list(payload: Any, key: str) -> list[Any] | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get(key)
    return value if isinstance(value, list) else None


def _runner_id(runner: dict[str, Any]) -> str | None:
    for key in ("id", "runner_id"):
        value = runner.get(key)
        if isinstance(value, str):
            return value
    return None


def _runner_ids_from_host(host: dict[str, Any]) -> list[str]:
    runner_ids: list[str] = []

    for key in (
        "runner_id",
        "active_runner_id",
        "current_runner_id",
    ):
        value = host.get(key)
        if isinstance(value, str):
            runner_ids.append(value)

    for key in (
        "runner_ids",
        "active_runner_ids",
        "current_runner_ids",
        "live_runner_ids",
        "online_runner_ids",
    ):
        value = host.get(key)
        if isinstance(value, list):
            runner_ids.extend(item for item in value if isinstance(item, str))

    for key in ("runner", "active_runner", "current_runner"):
        value = host.get(key)
        if isinstance(value, str):
            runner_ids.append(value)
        elif isinstance(value, dict):
            runner_id = _runner_id(value)
            if runner_id:
                runner_ids.append(runner_id)

    for key in (
        "runners",
        "active_runners",
        "current_runners",
        "live_runners",
        "online_runners",
    ):
        value = host.get(key)
        if not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, str):
                runner_ids.append(item)
            elif isinstance(item, dict):
                runner_id = _runner_id(item)
                if runner_id:
                    runner_ids.append(runner_id)

    return runner_ids


def _extract_runner_id(payload: Any) -> str | None:
    if isinstance(payload, dict):
        value = _runner_id(payload)
        if value:
            return value
        for key in ("runner", "data"):
            value = _extract_runner_id(payload.get(key))
            if value:
                return value
    return None


def _host_id(host: dict[str, Any]) -> str | None:
    for key in ("id", "host_id"):
        value = host.get(key)
        if isinstance(value, str):
            return value
    return None


def _is_host_online(host: dict[str, Any]) -> bool:
    if host.get("online") is True or host.get("host_online") is True:
        return True
    status = host.get("status")
    return isinstance(status, str) and status.lower() == "online"


async def _raise_for_status(response: httpx.Response) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        error_code = _extract_error_code(response)
        if response.status_code == 503 and error_code == "runner_unavailable":
            raise RunnerUnavailableError(
                f"Omnigent runner unavailable for {response.request.url}: {response.text}"
            ) from exc
        raise OmnigentError(
            f"Omnigent request failed with {response.status_code}: {response.text}"
        ) from exc


def _extract_error_code(response: httpx.Response) -> str | None:
    try:
        payload = response.json()
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    return code if isinstance(code, str) else None
