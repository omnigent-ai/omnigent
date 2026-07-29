"""Bridge Kimi 1.49 approval-runtime events to Omnigent.

This file is also loaded as ``sitecustomize`` inside the Kimi process. Keep its
imports limited to the standard library until the guarded Kimi import at the
bottom of the module.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import importlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Protocol, cast

ENABLED_ENV_VAR = "OMNIGENT_KIMI_APPROVAL_BRIDGE"
BRIDGE_DIR_ENV_VAR = "OMNIGENT_KIMI_APPROVAL_BRIDGE_DIR"

_CONFIG_FILE = "hook_config.json"
_STARTUP_DIR = "kimi-python-startup"
_SITE_CUSTOMIZE_FILE = "sitecustomize.py"
_WEB_TIMEOUT_S = 3600.0
_RESOLUTION_TIMEOUT_S = 10.0
_PATCHED_ATTR = "_omnigent_approval_bridge_patched"
_TASKS_ATTR = "_omnigent_approval_bridge_tasks"
_IN_PROGRESS_ATTR = "_omnigent_approval_bridge_in_progress"
_WEB_RESOLVING_ATTR = "_omnigent_approval_bridge_web_resolving"


class _ApprovalRequest(Protocol):
    id: str
    tool_call_id: str
    sender: str
    action: str
    description: str
    display: list[object]
    status: str


class _ApprovalRuntime(Protocol):
    def subscribe(self, callback: Callable[[object], None]) -> str: ...

    def resolve(
        self,
        request_id: str,
        response: str,
        feedback: str = "",
    ) -> bool: ...


class _Initializer(Protocol):
    def __call__(
        self,
        instance: object,
        *args: object,
        **kwargs: object,
    ) -> None: ...


def materialize_kimi_approval_bridge(bridge_dir: Path) -> dict[str, str]:
    """Expose this module to Kimi as a launch-scoped ``sitecustomize``."""
    bridge_dir.mkdir(parents=True, exist_ok=True)
    startup_dir = bridge_dir / _STARTUP_DIR
    startup_dir.mkdir(parents=True, exist_ok=True)
    for path in (bridge_dir, startup_dir):
        with contextlib.suppress(OSError):
            os.chmod(path, 0o700)

    startup_file = startup_dir / _SITE_CUSTOMIZE_FILE
    target = Path(__file__).resolve()
    if startup_file.is_symlink():
        if startup_file.resolve() != target:
            raise RuntimeError(f"unexpected Kimi sitecustomize target: {startup_file}")
    elif startup_file.exists():
        raise RuntimeError(f"refusing to replace Kimi sitecustomize file: {startup_file}")
    else:
        startup_file.symlink_to(target)

    return {
        "PYTHONPATH": str(startup_dir),
        ENABLED_ENV_VAR: "1",
        BRIDGE_DIR_ENV_VAR: str(bridge_dir),
    }


def _jsonable_display(block: object) -> object:
    model_dump = getattr(block, "model_dump", None)
    if callable(model_dump):
        try:
            return model_dump(mode="json")
        except TypeError:
            return model_dump()
    if isinstance(block, (dict, list, str, int, float, bool)) or block is None:
        return block
    return str(block)


def approval_elicitation_id(request_id: str) -> str:
    """Return the stable Omnigent elicitation id for one Kimi request."""
    return f"elicit_kimi_{request_id}"


def approval_payload(request: _ApprovalRequest) -> dict[str, object]:
    """Convert Kimi's approval record to the generic native card payload."""
    request_id = str(request.id)
    tool_call_id = str(getattr(request, "tool_call_id", ""))
    sender = str(getattr(request, "sender", "Kimi"))
    action = str(getattr(request, "action", "tool"))
    description = str(getattr(request, "description", "") or "Kimi wants approval to run a tool")
    display = getattr(request, "display", [])
    display_items = (
        [_jsonable_display(item) for item in display] if isinstance(display, list) else []
    )
    preview = json.dumps(
        {
            "tool_call_id": tool_call_id,
            "sender": sender,
            "display": display_items,
        },
        ensure_ascii=False,
        sort_keys=True,
    )[:1024]
    return {
        "elicitation_id": approval_elicitation_id(request_id),
        "agent": "Kimi",
        "policy_name": "kimi_native_permission",
        "operation_type": action,
        "message": description,
        "content_preview": preview,
    }


def _read_config(bridge_dir: Path) -> dict[str, object]:
    try:
        data = json.loads((bridge_dir / _CONFIG_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _request_details(bridge_dir: Path, suffix: str) -> tuple[str, dict[str, str]] | None:
    config = _read_config(bridge_dir)
    base_url = config.get("ap_server_url")
    session_id = config.get("session_id")
    if not isinstance(base_url, str) or not base_url:
        return None
    if not isinstance(session_id, str) or not session_id:
        return None
    raw_headers = config.get("ap_auth_headers")
    headers = (
        {str(key): str(value) for key, value in raw_headers.items()}
        if isinstance(raw_headers, dict)
        else {}
    )
    quoted_session = urllib.parse.quote(session_id, safe="")
    url = f"{base_url.rstrip('/')}/v1/sessions/{quoted_session}/{suffix.lstrip('/')}"
    return url, headers


def _post_json(
    url: str,
    headers: dict[str, str],
    payload: dict[str, object],
    *,
    timeout_s: float,
) -> bytes | None:
    request_headers = {"Content-Type": "application/json", **headers}
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=request_headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return cast(bytes, response.read())
    except (OSError, urllib.error.URLError, ValueError):
        return None


def request_web_verdict(bridge_dir: Path, request: _ApprovalRequest) -> str | None:
    """Park one Kimi approval in Omnigent and return a concrete web action."""
    details = _request_details(bridge_dir, "hooks/native-permission-request")
    if details is None:
        return None
    raw = _post_json(
        details[0],
        details[1],
        approval_payload(request),
        timeout_s=_WEB_TIMEOUT_S,
    )
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    action = data.get("action") if isinstance(data, dict) else None
    return action if action in {"accept", "decline", "cancel"} else None


def post_external_resolution(bridge_dir: Path, request_id: str) -> None:
    """Release an Omnigent card answered through Kimi's native modal."""
    details = _request_details(bridge_dir, "events")
    if details is None:
        return
    _post_json(
        details[0],
        details[1],
        {
            "type": "external_elicitation_resolved",
            "data": {"elicitation_id": approval_elicitation_id(request_id)},
        },
        timeout_s=_RESOLUTION_TIMEOUT_S,
    )


def _runtime_string_set(runtime: object, name: str) -> set[str]:
    value = getattr(runtime, name, None)
    if isinstance(value, set):
        return cast(set[str], value)
    strings: set[str] = set()
    setattr(runtime, name, strings)
    return strings


def _runtime_tasks(runtime: object) -> set[asyncio.Task[None]]:
    value = getattr(runtime, _TASKS_ATTR, None)
    if isinstance(value, set):
        return cast(set[asyncio.Task[None]], value)
    tasks: set[asyncio.Task[None]] = set()
    setattr(runtime, _TASKS_ATTR, tasks)
    return tasks


def _start_task(
    runtime: object,
    coroutine: Coroutine[object, object, None],
) -> None:
    try:
        task = asyncio.get_running_loop().create_task(coroutine)
    except RuntimeError:
        coroutine.close()
        return
    tasks = _runtime_tasks(runtime)
    tasks.add(task)

    def _done(completed: asyncio.Task[None]) -> None:
        tasks.discard(completed)
        if completed.cancelled():
            return
        exc = completed.exception()
        if exc is not None:
            print(f"Omnigent Kimi approval bridge failed: {exc}", file=sys.stderr)

    task.add_done_callback(_done)


async def _bridge_created_request(
    runtime: _ApprovalRuntime,
    bridge_dir: Path,
    request: _ApprovalRequest,
) -> None:
    request_id = str(request.id)
    try:
        verdict = await asyncio.to_thread(request_web_verdict, bridge_dir, request)
        if request.status != "pending":
            return
        response = "approve" if verdict == "accept" else "reject"
        if verdict not in {"accept", "decline", "cancel"}:
            return
        web_resolving = _runtime_string_set(runtime, _WEB_RESOLVING_ATTR)
        web_resolving.add(request_id)
        try:
            runtime.resolve(request_id, response, feedback="Resolved in Omnigent")
        finally:
            web_resolving.discard(request_id)
    finally:
        _runtime_string_set(runtime, _IN_PROGRESS_ATTR).discard(request_id)


def _handle_runtime_event(
    runtime: _ApprovalRuntime,
    bridge_dir: Path,
    event: object,
) -> None:
    raw_request = getattr(event, "request", None)
    request_id = getattr(raw_request, "id", None)
    if raw_request is None or not isinstance(request_id, str) or not request_id:
        return
    request = cast(_ApprovalRequest, raw_request)
    kind = getattr(event, "kind", None)
    if kind == "request_created":
        in_progress = _runtime_string_set(runtime, _IN_PROGRESS_ATTR)
        if request_id in in_progress:
            return
        in_progress.add(request_id)
        _start_task(runtime, _bridge_created_request(runtime, bridge_dir, request))
        return
    if kind == "request_resolved":
        if request_id in _runtime_string_set(runtime, _WEB_RESOLVING_ATTR):
            return
        _start_task(
            runtime,
            asyncio.to_thread(post_external_resolution, bridge_dir, request_id),
        )


def patch_approval_runtime(runtime_type: type[object], bridge_dir: Path) -> None:
    """Subscribe every Kimi ApprovalRuntime instance to the Omnigent bridge."""
    if getattr(runtime_type, _PATCHED_ATTR, False):
        return
    original_init = cast(_Initializer, runtime_type.__init__)

    @functools.wraps(original_init)
    def _bridged_init(self: object, *args: object, **kwargs: object) -> None:
        original_init(self, *args, **kwargs)
        runtime = cast(_ApprovalRuntime, self)
        runtime.subscribe(lambda event: _handle_runtime_event(runtime, bridge_dir, event))

    # The Kimi class is imported dynamically in sitecustomize, so assignment
    # through setattr is the intentional runtime patch boundary.
    setattr(runtime_type, "__init__", _bridged_init)  # noqa: B010
    setattr(runtime_type, _PATCHED_ATTR, True)


async def wait_for_bridge_tasks(runtime: object) -> None:
    """Wait until the bridge's current background tasks settle (test helper)."""
    while True:
        tasks = list(_runtime_tasks(runtime))
        if not tasks:
            return
        await asyncio.gather(*tasks)


def _install_from_environment() -> None:
    bridge_value = os.environ.get(BRIDGE_DIR_ENV_VAR, "")
    if not bridge_value:
        return
    try:
        approval_module = importlib.import_module("kimi_cli.approval_runtime")
    except ImportError:
        return
    runtime_type = cast(type[object], approval_module.ApprovalRuntime)
    patch_approval_runtime(runtime_type, Path(bridge_value))


if os.environ.get(ENABLED_ENV_VAR) == "1":
    _install_from_environment()
