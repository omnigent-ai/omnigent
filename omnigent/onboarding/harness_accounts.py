"""Non-secret account metadata for locally installed harness CLIs."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from queue import Empty, Queue
from typing import Any, cast

from omnigent.harness_accounts import HarnessAccount, HarnessAccounts
from omnigent.inner import _proc
from omnigent.onboarding.opencode_auth import _ENV_PROVIDER_VARS, opencode_auth_summary

_PROBE_TIMEOUT_S = 5.0
HARNESS_ACCOUNT_KEYS = ("codex", "claude", "pi", "opencode")


class _CodexTokenExpired(RuntimeError):
    """Codex explicitly reported a revoked or expired credential."""


class _StdioJsonRpc:
    """Small bounded JSON-RPC client for one Codex app-server probe."""

    def __init__(
        self,
        timeout_s: float = _PROBE_TIMEOUT_S,
        cancel_event: threading.Event | None = None,
    ) -> None:
        codex = shutil.which("codex")
        if codex is None:
            raise FileNotFoundError("codex is not installed")
        self._deadline = time.monotonic() + timeout_s
        self._cancel_event = cancel_event
        self._next_id = 1
        self._lines: Queue[str | None] = Queue()
        self._proc = subprocess.Popen(
            [codex, "app-server"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            **_proc.spawn_kwargs(),
        )
        _proc.remember_process_group(self._proc)
        self._reader = threading.Thread(target=self._read_lines, daemon=True)
        self._reader.start()

    def close(self) -> None:
        """Terminate the short-lived probe process."""
        _proc.terminate_tree(self._proc)
        if self._proc.poll() is None:
            try:
                self._proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                _proc.kill_tree(self._proc)
                self._proc.wait(timeout=1)

    def request(self, method: str, params: dict[str, object]) -> dict[str, Any]:
        """Send a request and return its result object."""
        request_id = self._next_id
        self._next_id += 1
        self._write({"id": request_id, "method": method, "params": params})
        while True:
            message = self._read()
            if message.get("id") != request_id:
                continue
            if error := message.get("error"):
                serialized = json.dumps(error, sort_keys=True).lower()
                if "token_expired" in serialized or "token_revoked" in serialized:
                    raise _CodexTokenExpired("codex account token is no longer valid")
                raise RuntimeError(f"codex app-server rejected {method}")
            result = message.get("result")
            return cast(dict[str, Any], result) if isinstance(result, dict) else {}

    def notify(self, method: str) -> None:
        """Send a notification."""
        self._write({"method": method})

    def _write(self, message: dict[str, object]) -> None:
        if self._proc.stdin is None:
            raise RuntimeError("codex app-server stdin is unavailable")
        self._proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
        self._proc.stdin.flush()

    def _read(self) -> dict[str, Any]:
        while True:
            if self._cancel_event is not None and self._cancel_event.is_set():
                raise TimeoutError("codex account probe cancelled")
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("codex account probe timed out")
            try:
                line = self._lines.get(timeout=min(remaining, 0.05))
                break
            except Empty:
                continue
        if line is None:
            raise RuntimeError("codex app-server exited during account probe")
        message = json.loads(line)
        if not isinstance(message, dict):
            raise RuntimeError("codex app-server returned a non-object message")
        return cast(dict[str, Any], message)

    def _read_lines(self) -> None:
        """Move complete stdout lines into a queue without stdio buffering races."""
        stdout = self._proc.stdout
        if stdout is None:
            self._lines.put(None)
            return
        try:
            for line in stdout:
                self._lines.put(line)
        finally:
            self._lines.put(None)


def _codex_account(
    *,
    refresh_token: bool = False,
    deadline: float,
    cancel_event: threading.Event | None,
) -> HarnessAccount | None:
    if shutil.which("codex") is None:
        return None
    rpc: _StdioJsonRpc | None = None
    try:
        rpc = _StdioJsonRpc(
            max(0.0, deadline - time.monotonic()),
            cancel_event=cancel_event,
        )
        rpc.request(
            "initialize",
            {
                "clientInfo": {"name": "omnigent", "version": "0.1"},
                "capabilities": {"experimentalApi": True},
            },
        )
        rpc.notify("initialized")
        result = rpc.request("account/read", {"refreshToken": refresh_token})
        raw_account = result.get("account")
        if raw_account is None:
            return {"kind": "none"}
        if not isinstance(raw_account, dict):
            return {"kind": "unknown"}
        account_type = raw_account.get("type")
        if account_type == "chatgpt":
            account: HarnessAccount = {"kind": "chatgpt"}
            email = raw_account.get("email")
            plan = raw_account.get("planType")
            if isinstance(email, str) and email:
                account["email"] = email
            if isinstance(plan, str) and plan:
                account["plan"] = plan
            return account
        if account_type in {"apiKey", "api_key"}:
            return {"kind": "api_key"}
        return {"kind": "unknown"}
    except _CodexTokenExpired:
        return {"kind": "unknown", "expired": True}
    except Exception:
        return {"kind": "unknown"}
    finally:
        if rpc is not None:
            rpc.close()


def _run_json_command(
    argv: list[str],
    *,
    deadline: float,
    cancel_event: threading.Event | None,
) -> tuple[int, dict[str, Any] | None]:
    if cancel_event is not None and cancel_event.is_set():
        return 1, None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return 1, None
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            **_proc.spawn_kwargs(),
        )
        _proc.remember_process_group(process)
    except OSError:
        return 1, None
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise subprocess.TimeoutExpired(argv, 0)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, 0)
            try:
                stdout, _stderr = process.communicate(timeout=min(remaining, 0.05))
                break
            except subprocess.TimeoutExpired:
                continue
    except subprocess.TimeoutExpired:
        _proc.terminate_tree(process)
        try:
            process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            _proc.kill_tree(process)
            process.communicate()
        return 1, None
    try:
        payload = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return process.returncode, None
    return process.returncode, cast(dict[str, Any], payload) if isinstance(payload, dict) else None


def _claude_account(
    *, deadline: float, cancel_event: threading.Event | None
) -> HarnessAccount | None:
    claude = shutil.which("claude")
    if claude is None:
        return None
    return_code, payload = _run_json_command(
        [claude, "auth", "status", "--json"],
        deadline=deadline,
        cancel_event=cancel_event,
    )
    if payload is None:
        return {"kind": "unknown"}
    if payload.get("loggedIn") is not True:
        return {"kind": "none"}
    if return_code != 0:
        return {"kind": "unknown"}
    nested = payload.get("account")
    source = nested if isinstance(nested, dict) else payload
    auth_method = payload.get("authMethod")
    account: HarnessAccount = {
        "kind": "api_key" if auth_method == "api_key" else "subscription",
    }
    for target, candidates in {
        "email": ("email",),
        "org": ("organization", "org"),
        "plan": ("subscriptionType", "plan"),
    }.items():
        value = next((source.get(key) for key in candidates if source.get(key)), None)
        if isinstance(value, str):
            account[target] = value
    return account


def _pi_account(*, deadline: float, cancel_event: threading.Event | None) -> HarnessAccount | None:
    pi = shutil.which("pi")
    if pi is None:
        return None
    providers: list[dict[str, str]] = []
    labels = {
        "anthropic": "Anthropic",
        "openai": "OpenAI",
        "openai-codex": "OpenAI Codex",
    }
    for provider_id, label in labels.items():
        return_code, payload = _run_json_command(
            [pi, "auth", "check", "--provider", provider_id, "--json"],
            deadline=deadline,
            cancel_event=cancel_event,
        )
        if return_code != 0 or payload is None or payload.get("status") != "ready":
            continue
        auth_type = payload.get("authType")
        if isinstance(auth_type, str) and auth_type:
            providers.append({"id": provider_id, "label": label, "auth_type": auth_type})
    return {"kind": "providers", "providers": providers}


def _opencode_account() -> HarnessAccount | None:
    if shutil.which("opencode") is None:
        return None
    summary = opencode_auth_summary()
    providers: list[dict[str, str]] = []
    for provider_id in summary.stored_providers:
        providers.append({"id": provider_id, "label": provider_id, "auth_type": "stored"})
    provider_ids_by_label = {label: provider_id for provider_id, label, _ in _ENV_PROVIDER_VARS}
    for label in summary.env_providers:
        providers.append(
            {
                "id": provider_ids_by_label.get(label, label.lower()),
                "label": label,
                "auth_type": "env",
            }
        )
    for provider_id in summary.configured_providers:
        providers.append({"id": provider_id, "label": provider_id, "auth_type": "config"})
    return {"kind": "providers", "providers": providers}


def probe_harness_account(
    harness: str,
    *,
    refresh_codex: bool = False,
    timeout_s: float = _PROBE_TIMEOUT_S,
    cancel_event: threading.Event | None = None,
) -> HarnessAccount | None:
    """Probe one supported harness account without exposing credential material."""
    deadline = time.monotonic() + timeout_s
    probes = {
        "codex": lambda: _codex_account(
            refresh_token=refresh_codex,
            deadline=deadline,
            cancel_event=cancel_event,
        ),
        "claude": lambda: _claude_account(
            deadline=deadline,
            cancel_event=cancel_event,
        ),
        "pi": lambda: _pi_account(
            deadline=deadline,
            cancel_event=cancel_event,
        ),
        "opencode": _opencode_account,
    }
    probe = probes.get(harness)
    return probe() if probe is not None else None


def harness_account_map(*, refresh_codex: bool = False) -> HarnessAccounts:
    """Probe installed harnesses and return non-secret account metadata."""
    accounts: HarnessAccounts = {}
    for harness in HARNESS_ACCOUNT_KEYS:
        account = probe_harness_account(harness, refresh_codex=refresh_codex)
        if account is not None:
            accounts[harness] = account
    return accounts
