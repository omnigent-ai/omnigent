"""Kiro-native tool-approval mirror (TUI ACP recorder -> web elicitation)."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import random
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from omnigent.harnesses.kiro_native.bridge import (
    KiroTerminalExited,
    acp_record_path,
    send_kiro_permission_verdict,
)

_logger = logging.getLogger(__name__)

_POLL_INTERVAL_S = 0.4
_POST_TIMEOUT_S = 86400.0
_DELIVERY_CONFIRM_TIMEOUT_S = 5.0
_RECEIPT_RETRY_MAX_S = 30.0
_PREVIEW_MAX = 1024
_SUPPORTED_ACCEPT_OPTION = "allow_once"
_SUPPORTED_DECLINE_OPTION = "reject_once"


@dataclass(frozen=True)
class KiroPermissionRequest:
    """A parsed Kiro ``session/request_permission`` request."""

    request_id: str
    tool_call_id: str
    title: str
    accept_option_id: str
    decline_option_id: str
    terminal_title: str | None = None

    @property
    def preview(self) -> str:
        return self.title[:_PREVIEW_MAX]


@dataclass(frozen=True)
class _PermissionEvent:
    """One parsed permission event from Kiro's ACP recorder."""

    kind: str
    request_id: str
    permission: KiroPermissionRequest | None = None


@dataclass(frozen=True)
class _PendingPermission:
    """One Kiro permission currently parked in the web UI."""

    elicitation_id: str
    task: asyncio.Task[None]


def kiro_permission_elicitation_id(session_id: str, request_id: str) -> str:
    """Return the deterministic Omnigent elicitation id for a Kiro request."""
    digest = hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:16]
    return f"elicit_kiro_{session_id}_{digest}"


def _consume_task_result(task: asyncio.Task[None]) -> None:
    """Retrieve task exceptions so cancelled loser tasks do not warn."""
    with contextlib.suppress(asyncio.CancelledError):
        task.exception()


def parse_permission_request(message: dict[str, object]) -> KiroPermissionRequest | None:
    """Parse a Kiro ACP ``session/request_permission`` message."""
    if message.get("method") != "session/request_permission":
        return None
    request_id = message.get("id")
    if not isinstance(request_id, str) or not request_id:
        return None
    params = message.get("params")
    if not isinstance(params, dict):
        return None
    tool_call = params.get("toolCall")
    if not isinstance(tool_call, dict):
        return None
    tool_call_id = tool_call.get("toolCallId")
    if not isinstance(tool_call_id, str) or not tool_call_id:
        return None
    title = tool_call.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    options = params.get("options")
    if not isinstance(options, list):
        return None
    accept_option_id: str | None = None
    decline_option_id: str | None = None
    for option in options:
        if not isinstance(option, dict):
            continue
        option_id = option.get("optionId")
        kind = option.get("kind")
        if not isinstance(option_id, str) or not isinstance(kind, str):
            continue
        if kind == _SUPPORTED_ACCEPT_OPTION:
            accept_option_id = option_id
        elif kind == _SUPPORTED_DECLINE_OPTION:
            decline_option_id = option_id
    if not accept_option_id or not decline_option_id:
        return None
    terminal_title = None
    raw_input = tool_call.get("rawInput")
    meta = params.get("_meta")
    trust_options = meta.get("trustOptions") if isinstance(meta, dict) else None
    path = raw_input.get("path") if isinstance(raw_input, dict) else None
    if (
        isinstance(path, str)
        and Path(path).is_absolute()
        and not any(character in path for character in "\r\n\t")
        and isinstance(trust_options, list)
        and any(
            isinstance(option, dict) and option.get("setting_key") == "runtime_write_paths"
            for option in trust_options
        )
    ):
        # ACP uses a basename summary; the TUI's Write heading uses the full path.
        with contextlib.suppress(OSError, RuntimeError):
            terminal_title = f"Write {Path(path).resolve()}"
    return KiroPermissionRequest(
        request_id=request_id,
        tool_call_id=tool_call_id,
        title=title.strip(),
        accept_option_id=accept_option_id,
        decline_option_id=decline_option_id,
        terminal_title=terminal_title,
    )


def _permission_result_request_id(message: dict[str, object]) -> str | None:
    """Return the request id for a Kiro permission response message."""
    request_id = message.get("id")
    if not isinstance(request_id, str) or not request_id:
        return None
    result = message.get("result")
    if not isinstance(result, dict):
        return None
    outcome = result.get("outcome")
    if not isinstance(outcome, dict):
        return None
    option_id = outcome.get("optionId")
    return request_id if isinstance(option_id, str) and option_id else None


def _decode_acp_message(record: object) -> dict[str, object] | None:
    """Decode the JSON-RPC message from one Kiro recorder line."""
    if not isinstance(record, dict):
        return None
    message = record.get("msg")
    if isinstance(message, str):
        try:
            message = json.loads(message)
        except ValueError:
            return None
    return message if isinstance(message, dict) else None


def _read_new_permission_events(
    record_file: Path, offset: int
) -> tuple[list[_PermissionEvent], int]:
    """Read complete Kiro ACP recorder lines after *offset*."""
    try:
        size = record_file.stat().st_size
    except OSError:
        return [], offset
    if size < offset:
        offset = 0
    if size == offset:
        return [], offset
    try:
        with record_file.open("rb") as handle:
            handle.seek(offset)
            data = handle.read(size - offset)
    except OSError:
        return [], offset
    last_nl = data.rfind(b"\n")
    if last_nl == -1:
        return [], offset
    consumed = data[: last_nl + 1]
    new_offset = offset + len(consumed)
    events: list[_PermissionEvent] = []
    for raw in consumed.split(b"\n"):
        raw = raw.strip()
        if not raw:
            continue
        try:
            record = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            continue
        message = _decode_acp_message(record)
        if message is None:
            continue
        permission = parse_permission_request(message)
        if permission is not None:
            events.append(_PermissionEvent("request", permission.request_id, permission))
            continue
        response_id = _permission_result_request_id(message)
        if response_id is not None:
            events.append(_PermissionEvent("response", response_id, None))
    return events, new_offset


async def supervise_kiro_permission_mirror(
    *,
    base_url: str,
    headers: dict[str, str],
    session_id: str,
    bridge_dir: Path,
    auth: httpx.Auth | None = None,
    poll_interval_s: float = _POLL_INTERVAL_S,
) -> None:
    """Tail Kiro's TUI ACP recorder and mirror approvals to web elicitations."""
    record_file = acp_record_path(bridge_dir)
    try:
        offset = record_file.stat().st_size
    except OSError:
        offset = 0
    pending: dict[str, _PendingPermission] = {}
    receipts: set[asyncio.Task[None]] = set()
    queued: dict[str, tuple[KiroPermissionRequest, int]] = {}
    timeout = httpx.Timeout(_POST_TIMEOUT_S, connect=10.0)
    from omnigent.cli_auth import open_server_client

    async with open_server_client(base_url, headers=headers, auth=auth, timeout=timeout) as client:
        try:
            while True:
                try:
                    batch_offset = offset
                    events, offset = await asyncio.to_thread(
                        _read_new_permission_events, record_file, offset
                    )
                    # Confirmed or abandoned hooks free the slot. Unconfirmed
                    # deliveries keep their worker parked for an explicit retry.
                    for done_id in [rid for rid, entry in pending.items() if entry.task.done()]:
                        pending.pop(done_id, None)
                    resolved_in_batch = {
                        event.request_id for event in events if event.kind == "response"
                    }
                    for event in events:
                        if event.kind == "request":
                            if (
                                event.permission is None
                                or event.request_id in resolved_in_batch
                                or event.request_id in pending
                                or event.request_id in queued
                            ):
                                continue
                            queued[event.request_id] = (event.permission, batch_offset)
                        else:
                            queued.pop(event.request_id, None)
                            entry = pending.pop(event.request_id, None)
                            if entry is None:
                                continue
                            if not entry.task.done():
                                entry.task.cancel()
                                await asyncio.gather(entry.task, return_exceptions=True)
                                receipt = asyncio.create_task(
                                    _post_external_elicitation_resolved(
                                        client, session_id, entry.elicitation_id
                                    )
                                )
                                receipts.add(receipt)
                                receipt.add_done_callback(receipts.discard)
                                receipt.add_done_callback(_consume_task_result)
                    if not pending and queued:
                        request_id = next(iter(queued))
                        permission, record_offset = queued.pop(request_id)
                        elicitation_id = kiro_permission_elicitation_id(session_id, request_id)
                        task = asyncio.create_task(
                            _run_one_permission(
                                client,
                                session_id=session_id,
                                bridge_dir=bridge_dir,
                                permission=permission,
                                elicitation_id=elicitation_id,
                                record_offset=record_offset,
                            ),
                            name=f"kiro-permission-{request_id}",
                        )
                        task.add_done_callback(_consume_task_result)
                        pending[request_id] = _PendingPermission(elicitation_id, task)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _logger.exception(
                        "kiro permission mirror poll failed; session=%s bridge_dir=%s",
                        session_id,
                        bridge_dir,
                    )
                await asyncio.sleep(poll_interval_s)
        finally:
            for entry in pending.values():
                entry.task.cancel()
            for receipt in receipts:
                receipt.cancel()
            await asyncio.gather(
                *(entry.task for entry in pending.values()), *receipts, return_exceptions=True
            )


class _VerdictDeliveryProbe:
    """Read only new recorder bytes and remember this request's response."""

    def __init__(self, record_file: Path, request_id: str, offset: int) -> None:
        self.record_file = record_file
        self.request_id = request_id
        self.offset = offset
        self.recorded = False
        self.stopped = threading.Event()

    def is_resolved(self) -> bool:
        if not self.recorded and not self.stopped.is_set():
            events, self.offset = _read_new_permission_events(self.record_file, self.offset)
            self.recorded = any(
                event.kind == "response" and event.request_id == self.request_id
                for event in events
            )
        return self.recorded or self.stopped.is_set()


async def _run_one_permission(
    client: httpx.AsyncClient,
    *,
    session_id: str,
    bridge_dir: Path,
    permission: KiroPermissionRequest,
    elicitation_id: str,
    record_offset: int,
) -> None:
    """Keep the card pending until Kiro answers; retry only on a new user decision."""
    probe = _VerdictDeliveryProbe(
        acp_record_path(bridge_dir), permission.request_id, record_offset
    )
    delivery = {"state": "ready", "attempt": secrets.token_hex(16)}
    payload = {
        "elicitation_id": elicitation_id,
        "agent": "Kiro",
        "policy_name": "kiro_native_permission",
        "operation_type": "tool",
        "message": f"Kiro wants approval for {permission.preview}",
        "content_preview": permission.preview,
        "native_delivery": delivery,
    }
    hook_backoff_s = _POLL_INTERVAL_S
    try:
        while not await asyncio.to_thread(probe.is_resolved):
            try:
                response = await client.post(
                    f"/v1/sessions/{session_id}/hooks/native-permission-request",
                    json=payload,
                )
                if response.status_code >= 400:
                    _logger.warning("kiro permission hook rejected: %s", response.status_code)
                    if response.status_code < 500 and response.status_code != 429:
                        return
                    # Repost the same attempt; HTTP retries must not send a key.
                    await asyncio.sleep(random.uniform(hook_backoff_s / 2, hook_backoff_s))
                    hook_backoff_s = min(hook_backoff_s * 2, _RECEIPT_RETRY_MAX_S)
                    continue
                hook_backoff_s = _POLL_INTERVAL_S
                if not response.content:
                    # A hook wait can expire without a native response.
                    await asyncio.sleep(_POLL_INTERVAL_S)
                    continue
                try:
                    result = response.json()
                except ValueError:
                    _logger.warning("kiro permission hook returned non-JSON")
                    return
            except httpx.HTTPError:
                _logger.exception("kiro permission hook failed; session=%s", session_id)
                # Reconnect with the same attempt id; this does not send a key.
                await asyncio.sleep(random.uniform(hook_backoff_s / 2, hook_backoff_s))
                hook_backoff_s = min(hook_backoff_s * 2, _RECEIPT_RETRY_MAX_S)
                continue
            action = result.get("action") if isinstance(result, dict) else None
            if action not in ("accept", "decline", "cancel"):
                return
            attempt = delivery["attempt"]
            acknowledged_attempt = result.get("native_delivery_attempt")
            supports_retry = acknowledged_attempt == attempt
            if (
                acknowledged_attempt is not None or delivery["state"] != "ready"
            ) and not supports_retry:
                _logger.warning("kiro permission hook did not acknowledge the current attempt")
                return
            if await asyncio.to_thread(probe.is_resolved):
                break
            # One five-second budget covers readiness and recorder confirmation.
            started = time.monotonic()
            try:
                await asyncio.to_thread(
                    send_kiro_permission_verdict,
                    bridge_dir,
                    action=action,
                    expected_title=permission.terminal_title or permission.title,
                    timeout_s=_DELIVERY_CONFIRM_TIMEOUT_S,
                    verdict_recorded=probe.is_resolved,
                )
            except KiroTerminalExited:
                _logger.warning("kiro permission terminal exited; session=%s", session_id)
                return
            except RuntimeError:
                _logger.exception(
                    "failed to deliver kiro permission verdict for %s; session=%s",
                    permission.request_id,
                    session_id,
                )
            if not supports_retry:
                # Older servers can replay a verdict tombstone on every repost.
                # Keep their single-shot contract; a replay is not a new decision.
                return
            while time.monotonic() - started < _DELIVERY_CONFIRM_TIMEOUT_S:
                if await asyncio.to_thread(probe.is_resolved):
                    break
                await asyncio.sleep(_POLL_INTERVAL_S)
            if await asyncio.to_thread(probe.is_resolved):
                break
            delivery = {
                "state": "unconfirmed",
                "action": action,
                "attempt": secrets.token_hex(16),
            }
            payload["native_delivery"] = delivery
        if probe.recorded:
            await _post_external_elicitation_resolved(client, session_id, elicitation_id)
    finally:
        # Cancelling an asyncio waiter does not stop its tmux worker thread.
        probe.stopped.set()


async def _post_external_elicitation_resolved(
    client: httpx.AsyncClient, session_id: str, elicitation_id: str
) -> None:
    """Tell the server the native Kiro TUI answered a pending prompt."""
    backoff_s = _POLL_INTERVAL_S
    while True:
        try:
            response = await client.post(
                f"/v1/sessions/{session_id}/events",
                json={
                    "type": "external_elicitation_resolved",
                    "data": {"elicitation_id": elicitation_id},
                },
                timeout=10.0,
            )
            if response.status_code < 400:
                return
            _logger.warning(
                "kiro external_elicitation_resolved rejected: status=%s body=%s",
                response.status_code,
                response.text[:512],
            )
            if response.status_code < 500 and response.status_code != 429:
                return
        except httpx.HTTPError:
            _logger.exception("kiro external_elicitation_resolved POST failed")
        # Retrying the receipt is safe; it never sends terminal input.
        await asyncio.sleep(random.uniform(backoff_s / 2, backoff_s))
        backoff_s = min(backoff_s * 2, _RECEIPT_RETRY_MAX_S)


__all__ = [
    "KiroPermissionRequest",
    "kiro_permission_elicitation_id",
    "parse_permission_request",
    "supervise_kiro_permission_mirror",
]
