"""Run one eval prompt in a fresh Omnigent session and record what happened.

Both drivers reuse the harness bench's process and credential plumbing, so
the server, runner, and host come from the build that
``OMNIGENT_COMPAT_SERVER_PYTHON`` / ``OMNIGENT_COMPAT_RUNNER_PYTHON`` select.
The caller sets those per arm before entering a driver.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import tempfile
import threading
import time
import uuid
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol

import httpx
import yaml

from dev.benchmarks.omnigent.environment import bench_child_environ
from dev.evals.omnigent_guide.record import INFRA_ERROR, RunRecord
from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests._helpers.live_server import find_free_port
from tests._helpers.session import bundle_files, post_session_bundle
from tests.harness_bench.driver import ProvisioningError, TurnResult, infra_failure_reason
from tests.harness_bench.full_server import SharedFullServer, spawn_omnigent_server
from tests.harness_bench.native_tui_driver import NativeTuiDriver
from tests.harness_bench.profile import resolve_profile
from tests.harness_bench.runtime_env import resolve_bench_env
from tests.harness_bench.session_items import assistant_text, function_calls, item_data, item_type

_POLL_S = 0.5
_OUTPUT_KEEP = 1500
_BUSY = frozenset({"running", "waiting"})
_ELICITATION_EVENT = "response.elicitation_request"


class EvalDriver(Protocol):
    """A provisioned harness that answers one prompt per fresh session."""

    harness: str

    def __enter__(self) -> EvalDriver: ...

    def __exit__(self, *exc: object) -> None: ...

    def run(self, prompt: str, record: RunRecord) -> RunRecord:
        """Answer *prompt* in a new session and fill *record*."""
        ...


def isolated_claude_env(root: Path) -> dict[str, str]:
    """
    Keep the user's own Claude Code settings, skills, and plugins out of eval runs.

    :param root: A scratch directory, e.g. ``Path("/tmp/omni-eval")``.
    :returns: Env overrides for the spawned processes.
    """
    claude_dir = root / "claude-config"
    claude_dir.mkdir(parents=True, exist_ok=True)
    return {"CLAUDE_CONFIG_DIR": str(claude_dir)}


#: Tools a claude-native eval session may use without asking. Sessions run in
#: ``dontAsk`` mode, so anything else is refused instead of parking the turn
#: on a prompt nobody answers.
CLAUDE_ALLOWED_TOOLS = (
    "Read,Glob,Grep,WebFetch,WebSearch,Skill,Write,Edit,mcp__omnigent,"
    "Bash(omnigent:*),Bash(omni:*),Bash(which:*)"
)

#: The runner machine may hold an Omnigent checkout and the operator's own
#: Omnigent and Claude data; an installed user has neither. Path rules cover
#: Read, Grep, and Glob; the relay's shell and file tools would bypass them.
CLAUDE_DISALLOWED_TOOLS = (
    "Read(~/Repositories/**),Edit(~/Repositories/**),Read(~/.omnigent/**),"
    "Edit(~/.omnigent/**),Read(~/.claude/**),Edit(~/.claude/**),"
    "mcp__omnigent__sys_os_shell,mcp__omnigent__sys_os_read,mcp__omnigent__sys_os_write,"
    "mcp__omnigent__sys_os_edit,mcp__omnigent__sys_terminal_launch,"
    "mcp__omnigent__sys_terminal_send,mcp__omnigent__sys_terminal_read"
)

#: Mirrors the production ``claude-native-ui`` agent, minus the user's own
#: Claude Code settings: ``skills: none`` launches claude with
#: ``--setting-sources ""`` while framework skills still load through
#: ``--plugin-dir``.
_CLAUDE_AGENT = {
    "name": "eval-omnigent-guide-claude-native",
    "prompt": (
        "Claude Code is running in the session terminal. Web UI messages are "
        "forwarded into that Claude Code process through the native bridge."
    ),
    "executor": {"harness": "claude-native", "context_window": 200_000},
    "spawn": True,
    "os_env": {"type": "caller_process", "cwd": ".", "sandbox": {"type": "none"}},
    "skills": "none",
}


class ClaudeNativeEvalDriver(NativeTuiDriver):
    """
    claude-native on the user's own Claude Code login, one session per run.

    The scratch Omnigent config names only a subscription provider, so Claude
    Code uses its first-party login. Each run uploads the eval agent bound to
    the host daemon, which launches claude with ``--permission-mode dontAsk``,
    the allowed tools above, and the pinned model.

    :param model: Claude Code model or alias, e.g. ``"sonnet"``.
    :param databricks_profile: Unused; kept for a uniform driver signature.
    :param extra_env: Env overrides for the server and host, e.g. ``PATH``.
    :param timeout: Ceiling for one turn, in seconds.
    """

    harness = "claude-native"

    def __init__(
        self,
        *,
        model: str,
        databricks_profile: str | None,
        extra_env: Mapping[str, str],
        timeout: float,
    ) -> None:
        super().__init__(resolve_profile("claude-native"), databricks_profile=databricks_profile)
        self._model = model
        self._extra_env = dict(extra_env)
        self._timeout = timeout
        self._host_id: str | None = None

    def __enter__(self) -> ClaudeNativeEvalDriver:
        try:
            self._provision_stack()
        except BaseException:
            self._teardown()
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        self._teardown()

    def _provision_stack(self) -> None:
        """The bench's native provisioning, without gateway credentials."""
        self._tmp.mkdir(mode=0o700, parents=True, exist_ok=True)
        port = find_free_port()
        self._base_url = f"http://localhost:{port}"
        binding_token = uuid.uuid4().hex
        base_env = {
            **bench_child_environ(os.environ),
            **self._extra_env,
            "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token,
            "OMNIGENT_CONFIG_HOME": str(self._write_provider_config()),
        }
        # Any of these would route Claude Code away from the first-party login.
        for key in _GATEWAY_ENV:
            base_env.pop(key, None)
        self._proc = spawn_omnigent_server(self._tmp, port, base_env, binding_token)
        self._wait_health()
        self._daemon = self._spawn_host_daemon(base_env)
        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=300.0,
            headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
        )
        self._host_id = self._wait_host_online()

    def _write_provider_config(self) -> Path:
        """Only a subscription provider, plus a short runner idle timeout.

        Each run gets its own runner; without the timeout, idle runners from
        finished runs would linger for the default hour.
        """
        config_home = self._tmp / "omnigent-config"
        config_home.mkdir(exist_ok=True)
        config = {
            "providers": {
                "claude-subscription": {"cli": "claude", "default": True, "kind": "subscription"}
            },
            "runner": {"idle_timeout_s": 60},
        }
        (config_home / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
        return config_home

    def run(self, prompt: str, record: RunRecord) -> RunRecord:
        """Answer *prompt* in a new host-bound session, then delete the session."""
        assert self._client is not None and self._host_id is not None
        workspace = self._tmp / "workspaces" / f"{record.case_id}-{record.run_index}"
        workspace.mkdir(parents=True)
        record.workspace = str(workspace)
        bundle = bundle_files(
            {f"{_CLAUDE_AGENT['name']}.yaml": yaml.safe_dump(_CLAUDE_AGENT).encode()}
        )
        launch_args = [
            "--permission-mode",
            "dontAsk",
            "--model",
            self._model,
            "--allowedTools",
            CLAUDE_ALLOWED_TOOLS,
            "--disallowedTools",
            CLAUDE_DISALLOWED_TOOLS,
        ]
        try:
            created = post_session_bundle(
                self._client.post,
                "/v1/sessions",
                bundle,
                metadata={
                    "host_id": self._host_id,
                    "workspace": str(workspace),
                    "terminal_launch_args": launch_args,
                },
                timeout=90.0,
            )
            created.raise_for_status()
            body = created.json()
            self._session_id = str(body.get("session_id") or body["id"])
            self._wait_forwarder()
        except (httpx.HTTPError, ProvisioningError, KeyError) as exc:
            record.status, record.error = INFRA_ERROR, repr(exc)
            return record
        try:
            return ask(
                self._client, self._session_id, prompt, record, timeout=self._timeout, quiet_s=8.0
            )
        finally:
            _delete_session(self._client, self._session_id)

    def _wait_forwarder(self, timeout: float = 120.0) -> None:
        """Wait until claude is up and its transcript is mirrored into the session."""
        assert self._client is not None
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            snap = self._client.get(
                f"/v1/sessions/{self._session_id}", params={"include_items": "false"}
            )
            if snap.status_code == 200 and snap.json().get("external_session_id"):
                return
            time.sleep(1.0)
        raise ProvisioningError(f"claude did not come up within {timeout}s; logs in {self._tmp}")


_GATEWAY_ENV = (
    "OPENAI_BASE_URL",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_USE_GATEWAY",
    "DATABRICKS_CONFIG_PROFILE",
    "CLAUDE_CONFIG_DIR",
)


class SdkEvalDriver:
    """
    An SDK harness through a live server and runner, one session per run.

    The agent gets shell access (``os_env``) so it can run ``omnigent`` and
    fetch docs. Runs share the runner workspace, which is emptied before
    each run.

    :param harness: An SDK harness, e.g. ``"openai-agents"`` or ``"claude-sdk"``.
    :param model: Pinned model, e.g. ``"databricks-gpt-5-4-mini"``.
    :param databricks_profile: Profile for gateway credentials.
    :param extra_env: Env overrides for the server and runner, e.g. ``PATH``.
    :param timeout: Ceiling for one turn, in seconds.
    :param executor_config: Extra ``executor.config`` keys, e.g.
        ``{"use_responses": False}``.
    :param os_env: The agent's ``os_env``; defaults to an unsandboxed shell in
        the workspace.
    """

    def __init__(
        self,
        *,
        harness: str,
        model: str,
        databricks_profile: str | None,
        extra_env: Mapping[str, str],
        timeout: float,
        executor_config: Mapping[str, Any] | None = None,
        os_env: Mapping[str, Any] | None = None,
    ) -> None:
        self.harness = harness
        self._executor_config = dict(executor_config or {})
        self._os_env = dict(
            os_env or {"type": "caller_process", "cwd": ".", "sandbox": {"type": "none"}}
        )
        self._agent_name = f"eval-omnigent-guide-{harness}"
        self._model = model
        self._db_profile = databricks_profile
        self._extra_env = dict(extra_env)
        self._timeout = timeout
        self._root = Path(tempfile.mkdtemp(prefix="omni-eval-sdk-"))
        self._workspace = self._root / "workspace"
        self._server: SharedFullServer | None = None
        #: Where to keep server and runner logs when a run in the batch fails.
        self.keep_logs_in: Path | None = None
        self._had_errors = False

    def __enter__(self) -> SdkEvalDriver:
        try:
            env = resolve_bench_env(self._db_profile)
            self._workspace.mkdir(parents=True)
            base_env = {
                **bench_child_environ(env.base_env),
                **self._extra_env,
                "OMNIGENT_RUNNER_WORKSPACE": str(self._workspace),
            }
            self._server = SharedFullServer(replace(env, base_env=base_env)).__enter__()
            self._register_agent(env.db_profile)
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        if self._server is not None:
            # Keep the server and runner logs of a batch that hit errors.
            if self.keep_logs_in is not None and self._had_errors:
                target = self.keep_logs_in / f"batch-{uuid.uuid4().hex[:8]}"
                target.mkdir(parents=True, exist_ok=True)
                for name in ("server.log", "runner.log"):
                    with contextlib.suppress(OSError):
                        shutil.copy2(self._server._tmp / name, target / name)
            self._server.__exit__(*exc)
        shutil.rmtree(self._root, ignore_errors=True)

    def _register_agent(self, db_profile: str | None) -> None:
        assert self._server is not None and self._server.client is not None
        executor: dict[str, Any] = {
            "type": "omnigent",
            "model": self._model,
            "config": {"harness": self.harness, **self._executor_config},
        }
        if db_profile:
            executor["profile"] = db_profile
        config = {
            "spec_version": 1,
            "name": self._agent_name,
            "prompt": "You are a helpful assistant.",
            "executor": executor,
            "os_env": self._os_env,
            # Hide the operator's own host skills; framework skills still load.
            "skills": "none",
        }
        bundle = bundle_files({"config.yaml": yaml.safe_dump(config).encode()})
        response = post_session_bundle(self._server.client.post, "/v1/sessions", bundle)
        if response.status_code not in (200, 201, 409):
            raise ProvisioningError(
                f"agent upload failed: {response.status_code} {response.text[:300]}"
            )

    def run(self, prompt: str, record: RunRecord) -> RunRecord:
        """Answer *prompt* in a new runner-bound session, then delete the session."""
        assert self._server is not None and self._server.client is not None
        for child in self._workspace.iterdir():
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        record.workspace = str(self._workspace)
        try:
            session_id = self._server.create_session(self._agent_name)
        except (httpx.HTTPError, IndexError, KeyError) as exc:
            record.status, record.error = INFRA_ERROR, repr(exc)
            self._had_errors = True
            return record
        try:
            return ask(
                self._server.client, session_id, prompt, record, timeout=self._timeout, quiet_s=3.0
            )
        finally:
            self._had_errors |= record.status != "completed"
            _delete_session(self._server.client, session_id)


class _ApprovalWatcher(threading.Thread):
    """Accept every elicitation on a session's event stream and count them."""

    def __init__(self, client: httpx.Client, session_id: str) -> None:
        super().__init__(daemon=True)
        self._client = client
        self._session_id = session_id
        self._stop_event = threading.Event()
        self._response: httpx.Response | None = None
        self.ready = threading.Event()
        self.accepted = 0

    def run(self) -> None:
        event_type: str | None = None
        try:
            with self._client.stream(
                "GET", f"/v1/sessions/{self._session_id}/stream", timeout=None
            ) as response:
                self._response = response
                self.ready.set()
                for line in response.iter_lines():
                    if self._stop_event.is_set():
                        return
                    if line.startswith("event:"):
                        event_type = line[len("event:") :].strip()
                    elif line.startswith("data:"):
                        self._maybe_accept(event_type, line[len("data:") :].strip())
        except (httpx.HTTPError, RuntimeError):
            return  # stop() closes the stream; either way, nothing left to accept
        finally:
            self.ready.set()

    def _maybe_accept(self, event_type: str | None, payload: str) -> None:
        try:
            frame = json.loads(payload)
        except ValueError:
            return
        if not isinstance(frame, dict):
            return
        if _ELICITATION_EVENT not in (event_type, frame.get("type")):
            return
        elicitation_id = frame.get("elicitation_id")
        if not isinstance(elicitation_id, str):
            return
        with contextlib.suppress(httpx.HTTPError):
            self._client.post(
                f"/v1/sessions/{self._session_id}/events",
                json={
                    "type": "approval",
                    "data": {"elicitation_id": elicitation_id, "action": "accept"},
                },
                timeout=30.0,
            )
            self.accepted += 1

    def stop(self) -> None:
        self._stop_event.set()
        if self._response is not None:
            with contextlib.suppress(Exception):
                self._response.close()
        self.join(timeout=10.0)


def ask(
    client: httpx.Client,
    session_id: str,
    prompt: str,
    record: RunRecord,
    *,
    timeout: float,
    quiet_s: float,
) -> RunRecord:
    """
    Send *prompt*, wait for the turn to finish, and fill *record*.

    The turn is over once the session has been busy, is idle again, and its
    item count has not changed for *quiet_s* seconds. Native transcripts can
    land a few seconds after the status flips.

    :param client: Client with ``base_url`` set to the server.
    :param session_id: A fresh session bound to a runner.
    :param prompt: The user message.
    :param record: Filled in place and returned.
    :param timeout: Ceiling for the whole turn, in seconds.
    :param quiet_s: How long the items must stay unchanged.
    :returns: *record*.
    """
    watcher = _ApprovalWatcher(client, session_id)
    watcher.start()
    watcher.ready.wait(timeout=10.0)
    started = time.monotonic()
    record.status = "timed_out"
    try:
        client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "message",
                "data": {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
            },
            timeout=30.0,
        ).raise_for_status()
        seen_busy = False
        settled_count, settled_at = -1, 0.0
        while time.monotonic() - started < timeout:
            snapshot = _snapshot(client, session_id)
            status = snapshot.get("status")
            if status in _BUSY:
                seen_busy, settled_count = True, -1
            elif status == "failed":
                error = snapshot.get("last_task_error") or snapshot.get("error")
                # An expired token or a gateway outage says nothing about the agent.
                infra = infra_failure_reason(TurnResult(failed=True, error=error))
                record.status = INFRA_ERROR if infra else "failed"
                record.error = str(error)
                break
            elif status == "idle":
                items = _items(client, session_id)
                seen_busy = seen_busy or bool(assistant_text(items))
                if seen_busy and len(items) != settled_count:
                    settled_count, settled_at = len(items), time.monotonic()
                elif seen_busy and time.monotonic() - settled_at >= quiet_s:
                    record.status = "completed"
                    break
            time.sleep(_POLL_S)
        items = _items(client, session_id)
        snapshot = _snapshot(client, session_id)
    except httpx.HTTPError as exc:
        record.status, record.error = INFRA_ERROR, repr(exc)
        return record
    finally:
        watcher.stop()
        record.duration_s = time.monotonic() - started
        record.elicitations = watcher.accepted
    record.answer = assistant_text(items)
    record.tool_calls = _tool_calls_with_outputs(items)
    record.total_tokens = _total_tokens(snapshot)
    cost = snapshot.get("total_cost_usd")
    record.cost_usd = float(cost) if isinstance(cost, (int, float)) else None
    return record


def _tool_calls_with_outputs(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Calls in order, each with the start of its output (fetch results, errors)."""
    outputs: dict[Any, str] = {}
    for item in items:
        if item_type(item) == "function_call_output":
            data = item_data(item)
            out = data.get("output")
            text = out if isinstance(out, str) else json.dumps(out, default=str)
            outputs[data.get("call_id")] = text[:_OUTPUT_KEEP]
    return [
        {"name": c["name"], "arguments": c["arguments"], "output": outputs.get(c["call_id"], "")}
        for c in function_calls(items)
    ]


def _snapshot(client: httpx.Client, session_id: str) -> dict[str, Any]:
    response = client.get(
        f"/v1/sessions/{session_id}", params={"include_items": "false"}, timeout=30.0
    )
    response.raise_for_status()
    return response.json()


def _items(client: httpx.Client, session_id: str) -> list[dict[str, Any]]:
    response = client.get(
        f"/v1/sessions/{session_id}/items", params={"order": "asc", "limit": 1000}, timeout=30.0
    )
    response.raise_for_status()
    return list(response.json().get("data", []))


def _total_tokens(snapshot: Mapping[str, Any]) -> int | None:
    """Session-wide tokens: the per-model totals when present, else the last task's."""
    by_model = snapshot.get("usage_by_model")
    if isinstance(by_model, dict) and by_model:
        totals = [
            usage.get("total_tokens") for usage in by_model.values() if isinstance(usage, dict)
        ]
        if any(isinstance(total, int) for total in totals):
            return sum(total for total in totals if isinstance(total, int))
    last = snapshot.get("last_total_tokens")
    return last if isinstance(last, int) else None


def _delete_session(client: httpx.Client, session_id: str) -> None:
    # Deleting tears down the session's terminals, so CLIs don't pile up across runs.
    with contextlib.suppress(httpx.HTTPError):
        client.delete(f"/v1/sessions/{session_id}", timeout=60.0)
