"""Real OMP TUI acceptance through the production native harness.

Only inference is mocked. The server, host, runner, native launcher, extension,
policy engine and tmux terminal are real. Run this file explicitly with pytest.
"""

from __future__ import annotations

import base64
import contextlib
import json
import shutil
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from PIL import Image

from omnigent.harnesses.omp_native.bridge import bridge_dir_for_session_id
from tests._helpers.host import isolated_host
from tests._helpers.polling import poll_until
from tests._helpers.session import bundle_files, post_session_bundle, send_user_text
from tests.e2e.conftest import configure_mock_llm, release_mock_gate

_TITLE_MODEL = "mock-omp-title"
_MODEL = "mock-omp-native"
_OTHER_MODEL = "mock-omp-native-other"
_API_PROMPT = "OMP_API_INPUT: describe the first transport direction."
_TUI_PROMPT = "OMP_TUI_INPUT: describe the second transport direction."
_API_ANSWER = "API input reached the resident OMP terminal and returned through the native bridge."
_TUI_ANSWER = "Terminal input reached OMP and returned through the mirrored conversation."


def _tmux(metadata: dict, *args: str) -> str:
    return subprocess.run(
        ["tmux", "-S", metadata["tmux_socket"], *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout


@dataclass
class OmpRig:
    client: httpx.Client
    server: str
    model_server: str
    root: Path
    home: Path
    session_id: str
    sessions: list[str] = field(default_factory=list)
    model: str = _MODEL

    def snapshot(self, session_id: str | None = None) -> dict:
        response = self.client.get(f"/v1/sessions/{session_id or self.session_id}")
        response.raise_for_status()
        return response.json()

    def items(self, session_id: str | None = None) -> list[dict]:
        response = self.client.get(
            f"/v1/sessions/{session_id or self.session_id}/items", params={"order": "asc"}
        )
        response.raise_for_status()
        return response.json()["data"]

    def terminal(self, session_id: str | None = None) -> dict:
        response = self.client.get(
            f"/v1/sessions/{session_id or self.session_id}/resources/terminals/terminal_omp_main"
        )
        response.raise_for_status()
        return response.json()["metadata"]

    def pane(self, session_id: str | None = None) -> str:
        metadata = self.terminal(session_id)
        return _tmux(metadata, "capture-pane", "-p", "-S", "-", "-t", metadata["tmux_target"])

    def send(self, text: str, session_id: str | None = None) -> None:
        send_user_text(self.client, session_id=session_id or self.session_id, text=text)

    def wait_answer(self, answer: str, session_id: str | None = None) -> list[dict]:
        items: list[dict] = []

        def received() -> bool:
            nonlocal items
            items = self.items(session_id)
            return any(
                item.get("role") == "assistant" and answer in json.dumps(item) for item in items
            )

        try:
            poll_until(received, timeout_s=60, interval_s=0.2)
        except AssertionError as error:
            raise AssertionError(
                f"Missing OMP answer {answer!r}: {items!r}\n{self.pane(session_id)}"
            ) from error
        return items

    def script(self, responses: list[dict], *, model: str | None = None) -> None:
        configure_mock_llm(self.model_server, responses, key=model or self.model)

    def requests(self) -> list[dict]:
        response = httpx.get(f"{self.model_server}/mock/requests", timeout=10)
        response.raise_for_status()
        return [
            r for r in response.json()["requests"] if r.get("model") in {self.model, _OTHER_MODEL}
        ]

    def control(self, kind: str, **data: object) -> dict:
        response = self.client.post(
            f"/v1/sessions/{self.session_id}/events", json={"type": kind, "data": data}, timeout=30
        )
        response.raise_for_status()
        return response.json()


@pytest.fixture
def omp_rig(
    live_server: str,
    http_client: httpx.Client,
    isolated_mock_llm_server_url: str,
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> Iterator[OmpRig]:
    if shutil.which("omp") is None or shutil.which("tmux") is None:
        pytest.skip("native OMP acceptance requires real omp and tmux binaries")
    family = getattr(request, "param", "openai")
    model = "claude-sonnet-4-6" if family == "anthropic" else _MODEL
    provider = {
        "providers": {
            "omp-test": {
                "kind": "gateway",
                "default": "omp",
                family: {
                    "base_url": f"{isolated_mock_llm_server_url}/v1",
                    "api_key": "mock-key",
                    **({"wire_api": "responses"} if family == "openai" else {}),
                    "models": {"default": model, "balanced": _OTHER_MODEL, "fast": _TITLE_MODEL},
                },
            }
        },
        "auto_open_conversation": False,
    }
    with isolated_host(
        tmp_path, live_server, config=provider, env={"TERM": "xterm-256color"}
    ) as host:
        host.wait_online(http_client)
        workspace = host.home / "workspace"
        workspace.mkdir()
        omp_settings = host.home / ".omp" / "agent"
        omp_settings.mkdir(parents=True)
        (omp_settings / "config.yml").write_text(
            'task:\n  perCallModel: "on"\ncompaction:\n  keepRecentTokens: 100\n'
        )
        bundle = bundle_files(
            {
                "omp-native-ui.yaml": b"""name: omp-native-ui
instructions: role.md
executor:
  harness: omp-native
  context_files: false
spawn: true
os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none
""",
                "role.md": b"Answer concisely. Use tools only when requested.",
            }
        )
        response = post_session_bundle(
            http_client.post,
            "/v1/sessions",
            bundle,
            metadata={
                "host_id": host.host_id,
                "workspace": str(workspace),
                "terminal_launch_args": [
                    "--no-title",
                    "--no-extensions",
                    "--no-skills",
                    "--no-rules",
                    "--no-context-files",
                    "--motion",
                    "none",
                    "--approval-mode",
                    "yolo",
                    "--smol",
                    _TITLE_MODEL,
                    "--slow",
                    _OTHER_MODEL,
                ],
                "labels": {"omnigent.ui": "terminal", "omnigent.wrapper": "omp-native-ui"},
            },
            timeout=60,
        )
        response.raise_for_status()
        sid = response.json()["session_id"]
        rig = OmpRig(
            http_client,
            live_server,
            isolated_mock_llm_server_url,
            tmp_path,
            host.home,
            sid,
            [sid],
            model=model,
        )
        try:
            poll_until(
                lambda: bool(rig.snapshot().get("external_session_id")),
                timeout_s=60,
                interval_s=0.3,
            )
            yield rig
        finally:
            with contextlib.suppress(httpx.HTTPError):
                (tmp_path / "omp-model-requests.json").write_text(json.dumps(rig.requests()))
            for session_id in reversed(rig.sessions):
                with contextlib.suppress(httpx.HTTPError, subprocess.SubprocessError):
                    (tmp_path / f"omp-{session_id}-snapshot.json").write_text(
                        json.dumps(rig.snapshot(session_id))
                    )
                    (tmp_path / f"omp-{session_id}-items.json").write_text(
                        json.dumps(rig.items(session_id))
                    )
                    (tmp_path / f"omp-{session_id}-terminal.txt").write_text(rig.pane(session_id))
                metadata = None
                with contextlib.suppress(httpx.HTTPError):
                    metadata = rig.terminal(session_id)
                with contextlib.suppress(httpx.HTTPError):
                    http_client.delete(f"/v1/sessions/{session_id}", timeout=30).raise_for_status()
                if metadata:
                    with contextlib.suppress(subprocess.SubprocessError):
                        _tmux(metadata, "kill-server")


@contextmanager
def _events(rig: OmpRig, final_answer: str) -> Iterator[list[tuple[str, dict]]]:
    events: list[tuple[str, dict]] = []
    errors: list[Exception] = []
    ready = threading.Event()
    stopping = threading.Event()
    active: list[httpx.Response] = []

    def read() -> None:
        try:
            with httpx.stream(
                "GET", f"{rig.server}/v1/sessions/{rig.session_id}/stream", timeout=70
            ) as response:
                active.append(response)
                response.raise_for_status()
                ready.set()
                name = ""
                for line in response.iter_lines():
                    if line.startswith("event:"):
                        name = line[6:].strip()
                    elif line.startswith("data:"):
                        data = json.loads(line[5:])
                        events.append((name, data))
                        if name == "response.output_item.done" and final_answer in line:
                            return
                    if stopping.is_set():
                        return
        except Exception as error:
            if not stopping.is_set():
                errors.append(error)
            ready.set()

    reader = threading.Thread(target=read, name="omp-native-events")
    reader.start()
    try:
        assert ready.wait(10), "OMP session stream did not connect"
        assert not errors, errors
        yield events
        reader.join(timeout=10)
        assert not reader.is_alive(), "OMP completion was not emitted on the session stream"
        assert not errors, errors
    finally:
        stopping.set()
        if reader.is_alive() and active:
            active[0].close()
        reader.join(timeout=5)


def test_api_and_terminal_roundtrip_streaming(omp_rig: OmpRig) -> None:
    rig = omp_rig
    rig.script(
        [
            {"text": _API_ANSWER, "stream": True, "chunk_delay": 0.02},
            {"text": _TUI_ANSWER, "stream": True, "chunk_delay": 0.02},
        ]
    )
    with _events(rig, _API_ANSWER) as events:
        rig.send(_API_PROMPT)
        rig.wait_answer(_API_ANSWER)
    assert sum(name == "response.output_text.delta" for name, _ in events) > 1
    assert "API input reached" in rig.pane()
    metadata = rig.terminal()
    _tmux(metadata, "send-keys", "-t", metadata["tmux_target"], "-l", _TUI_PROMPT)
    _tmux(metadata, "send-keys", "-t", metadata["tmux_target"], "Enter")
    items = rig.wait_answer(_TUI_ANSWER)
    for prompt in (_API_PROMPT, _TUI_PROMPT):
        assert (
            sum(item.get("role") == "user" and prompt in json.dumps(item) for item in items) == 1
        )
    assert "Terminal input reached" in rig.pane()
    assert len(rig.requests()) == 2


@pytest.mark.parametrize("phase", ["tool_call", "tool_result"])
@pytest.mark.parametrize("action", ["allow", "deny", "ask"])
def test_native_tool_policies(omp_rig: OmpRig, phase: str, action: str) -> None:
    rig = omp_rig
    secret = "OMP_PROTECTED_TOOL_OUTPUT"
    marker = rig.home / "workspace" / "policy-executed"
    command = f"printf {secret}; printf executed > {marker}"
    rig.script(
        [
            {
                "tool_calls": [
                    {
                        "call_id": "omp-policy-tool",
                        "name": "bash",
                        "arguments": json.dumps({"command": command}),
                    }
                ]
            },
            {"text": "OMP_POLICY_DONE", "stream": True},
        ]
    )
    policy = rig.client.post(
        f"/v1/sessions/{rig.session_id}/policies",
        json={
            "name": f"omp_{phase}_{action}",
            "type": "python",
            "handler": "omnigent.policies.builtins.cel.cel_policy",
            "factory_params": {
                "expression": (
                    f'event.type == "{phase}" ? '
                    f'{{"result": "{action.upper()}", "reason": "OMP policy test"}} '
                    ': {"result": "ALLOW"}'
                ),
            },
        },
    )
    policy.raise_for_status()
    with _events(rig, "OMP_POLICY_DONE") as events:
        rig.send("Use bash to execute the requested policy probe exactly once.")
        if action == "ask":
            poll_until(
                lambda: any(name == "response.elicitation_request" for name, _ in events),
                timeout_s=20,
                interval_s=0.1,
            )
            approval = next(
                data for name, data in events if name == "response.elicitation_request"
            )
            if phase == "tool_result":
                assert secret in json.dumps(approval), "the reviewer must see the withheld result"
            assert marker.exists() is (phase == "tool_result")
            assert not any(
                item.get("type") == "function_call_output" and secret in json.dumps(item)
                for item in rig.items()
            )
            rig.control("approval", elicitation_id=approval["elicitation_id"], action="accept")
        items = rig.wait_answer("OMP_POLICY_DONE")
    assert marker.exists() is (phase == "tool_result" or action != "deny")
    requests = rig.requests()
    assert len(requests) == 2, requests
    outputs = [
        item
        for item in requests[-1].get("input", [])
        if item.get("type") == "function_call_output"
    ]
    assert outputs, requests[-1]
    assert (secret in json.dumps(outputs)) is (action != "deny")
    if action == "deny":
        assert not any(
            item.get("type") == "function_call_output" and secret in json.dumps(item)
            for item in items
        )


def test_model_override_and_interrupt_recovery(omp_rig: OmpRig) -> None:
    rig = omp_rig
    options = rig.snapshot().get("model_options") or []
    poll_until(lambda: bool(rig.snapshot().get("model_options")), timeout_s=20, interval_s=0.2)
    options = rig.snapshot()["model_options"]
    other = next(option["id"] for option in options if option["id"].endswith("/" + _OTHER_MODEL))
    changed = rig.client.patch(f"/v1/sessions/{rig.session_id}", json={"model_override": other})
    changed.raise_for_status()
    rig.script(
        [
            {"text": "OMP_OVERRIDE_OK", "stream": True},
            {"text": "NEVER_COMPLETE", "block": True},
            {"text": "OMP_RECOVERED", "stream": True},
        ],
        model=_OTHER_MODEL,
    )
    rig.send("Reply briefly on the selected model.")
    rig.wait_answer("OMP_OVERRIDE_OK")
    assert rig.requests()[-1]["model"] == _OTHER_MODEL
    with _events(rig, "OMP_RECOVERED") as events:
        rig.send("Keep working until I interrupt this turn.")
        poll_until(lambda: len(rig.requests()) >= 2, timeout_s=20, interval_s=0.1)
        try:
            rig.control("interrupt")
            poll_until(
                lambda: any(name == "session.interrupted" for name, _ in events),
                timeout_s=15,
                interval_s=0.1,
            )
            rig.send("Reply briefly after interruption.")
            rig.wait_answer("OMP_RECOVERED")
        finally:
            release_mock_gate(rig.model_server)
    assert not any(
        item.get("role") == "assistant" and "NEVER_COMPLETE" in json.dumps(item)
        for item in rig.items()
    )


def test_cold_resume_and_fork_replay(omp_rig: OmpRig) -> None:
    rig = omp_rig
    token = "OMP_HISTORY_TOKEN_79"
    rig.script(
        [
            {"text": "OMP_SEED_SAVED", "stream": True},
            {"text": "OMP_RESUMED", "stream": True},
            {"text": "OMP_FORKED", "stream": True},
        ]
    )
    rig.send(f"Remember this token: {token}")
    rig.wait_answer("OMP_SEED_SAVED")
    original = rig.snapshot()
    terminal_url = f"/v1/sessions/{rig.session_id}/resources/terminals"
    rig.client.delete(f"{terminal_url}/terminal_omp_main", timeout=30).raise_for_status()
    # Force transcript reconstruction rather than merely reopening OMP's saved file.
    directory = (
        rig.home
        / ".omnigent"
        / "omp-native"
        / bridge_dir_for_session_id(rig.session_id).name
        / "sessions"
    )
    for session_file in directory.glob("*.jsonl"):
        session_file.unlink()
    rig.client.post(
        terminal_url,
        json={"terminal": "omp", "session_key": "main", "ensure_native_terminal": True},
        timeout=60,
    ).raise_for_status()
    rig.send("Recall the token from our earlier conversation.")
    rig.wait_answer("OMP_RESUMED")
    assert token in json.dumps(rig.requests()[-1]["input"])
    forked = rig.client.post(f"/v1/sessions/{rig.session_id}/fork", json={}, timeout=60)
    forked.raise_for_status()
    fork_id = forked.json()["id"]
    rig.sessions.append(fork_id)
    rig.client.patch(
        f"/v1/sessions/{fork_id}", json={"runner_id": original["runner_id"]}, timeout=60
    ).raise_for_status()
    rig.send("Recall the token in this fork.", fork_id)
    rig.wait_answer("OMP_FORKED", fork_id)
    assert token in json.dumps(rig.requests()[-1]["input"])
    assert rig.snapshot(fork_id)["external_session_id"] != rig.snapshot()["external_session_id"]


def test_rejects_superseded_native_generation(omp_rig: OmpRig) -> None:
    rig = omp_rig
    current = rig.snapshot()["external_session_id"]
    event = {
        "type": "external_conversation_item",
        "data": {
            "_omnigent_native_session_id": "superseded-omp-session",
            "response_id": "omp-generation-probe",
            "item_type": "message",
            "item_data": {
                "role": "assistant",
                "agent": "OMP",
                "content": [{"type": "output_text", "text": "STALE_NATIVE_TEXT"}],
            },
        },
    }
    for body in (event, [event]):
        response = rig.client.post(f"/v1/sessions/{rig.session_id}/events", json=body)
        assert response.status_code == 409, response.text
    assert "STALE_NATIVE_TEXT" not in json.dumps(rig.items())
    event["data"]["_omnigent_native_session_id"] = current
    accepted = rig.client.post(f"/v1/sessions/{rig.session_id}/events", json=event)
    assert accepted.status_code == 202, accepted.text
    assert "STALE_NATIVE_TEXT" in json.dumps(rig.items())


@pytest.mark.parametrize("phase", ["tool_call", "tool_result"])
@pytest.mark.parametrize("action", ["allow", "deny", "ask"])
def test_omnigent_mcp_policy_roundtrip(omp_rig: OmpRig, phase: str, action: str) -> None:
    rig = omp_rig
    rig.script(
        [
            {
                "tool_calls": [
                    {
                        "call_id": "omp-mcp-info",
                        "name": "sys_session_get_info",
                        "arguments": "{}",
                    }
                ]
            },
            {"text": "OMP_MCP_DONE", "stream": True},
        ]
    )
    policy = rig.client.post(
        f"/v1/sessions/{rig.session_id}/policies",
        json={
            "name": f"omp_mcp_{phase}_{action}",
            "type": "python",
            "handler": "omnigent.policies.builtins.cel.cel_policy",
            "factory_params": {
                "expression": (
                    f'event.type == "{phase}" ? '
                    f'{{"result": "{action.upper()}", "reason": "OMP MCP review"}} '
                    ': {"result": "ALLOW"}'
                ),
            },
        },
    )
    policy.raise_for_status()
    with _events(rig, "OMP_MCP_DONE") as events:
        rig.send("Call sys_session_get_info to inspect this session, then reply briefly.")
        if action == "ask":
            poll_until(
                lambda: any(name == "response.elicitation_request" for name, _ in events),
                timeout_s=20,
                interval_s=0.1,
            )
            approval = next(
                data for name, data in events if name == "response.elicitation_request"
            )
            rig.control("approval", elicitation_id=approval["elicitation_id"], action="accept")
        rig.wait_answer("OMP_MCP_DONE")
    if action == "ask":
        assert sum(name == "response.elicitation_request" for name, _ in events) == 1
    requests = rig.requests()
    assert len(requests) == 2, requests
    outputs = [
        item
        for item in requests[-1].get("input", [])
        if item.get("type") == "function_call_output"
    ]
    assert outputs, requests[-1]
    if action == "deny":
        assert (
            "suppressed" in json.dumps(outputs).lower() or "denied" in json.dumps(outputs).lower()
        )
    else:
        assert rig.session_id in json.dumps(outputs)


def test_native_compaction_and_clear(omp_rig: OmpRig) -> None:
    rig = omp_rig
    token = "OMP_CONTEXT_BEFORE_CLEAR"
    rig.script(
        [
            {"text": "OMP_CONTEXT_RECORDED", "stream": True},
            {"text": "OMP_READY_TO_COMPACT", "stream": True},
        ]
    )
    rig.send(f"Remember {token} for this conversation.")
    rig.wait_answer("OMP_CONTEXT_RECORDED")
    rig.send("Continue with this detailed context: " + "earlier history " * 200)
    rig.wait_answer("OMP_READY_TO_COMPACT")
    summary = [{"text": f"The conversation remembers {token}; continue the user's task."}]
    rig.script(summary)
    rig.script(summary, model=_OTHER_MODEL)
    rig.script(summary, model=_TITLE_MODEL)
    rig.control("compact")
    poll_until(
        lambda: any(item.get("type") == "compaction" for item in rig.items()),
        timeout_s=60,
        interval_s=0.2,
    )
    rig.script(
        [
            {"text": "OMP_AFTER_COMPACTION", "stream": True},
            {"text": "OMP_AFTER_CLEAR", "stream": True},
        ]
    )
    rig.send("Continue after the explicit compaction.")
    rig.wait_answer("OMP_AFTER_COMPACTION")
    assert token in json.dumps(rig.requests()[-1]["input"])
    before = rig.snapshot()["external_session_id"]
    rig.control("clear")
    poll_until(
        lambda: rig.snapshot()["external_session_id"] != before, timeout_s=60, interval_s=0.2
    )
    rig.send("Reply briefly in the cleared conversation.")
    rig.wait_answer("OMP_AFTER_CLEAR")
    assert token not in json.dumps(rig.requests()[-1]["input"])
    before = rig.snapshot()["external_session_id"]
    metadata = rig.terminal()
    _tmux(metadata, "send-keys", "-t", metadata["tmux_target"], "-l", "/new ")
    _tmux(metadata, "send-keys", "-t", metadata["tmux_target"], "Enter")
    poll_until(
        lambda: rig.snapshot()["external_session_id"] != before,
        timeout_s=30,
        interval_s=0.2,
    )
    rig.script([{"text": "OMP_AFTER_NATIVE_NEW", "stream": True}])
    rig.send("Answer after native /new.")
    rig.wait_answer("OMP_AFTER_NATIVE_NEW")
    assert "cleared conversation" not in json.dumps(rig.requests()[-1]["input"])


def test_native_children_keep_root_policies_and_separate_transcripts(omp_rig: OmpRig) -> None:
    rig = omp_rig
    marker = rig.home / "workspace" / "child-must-not-execute"
    rig.script(
        [
            {
                "tool_calls": [
                    {
                        "call_id": "omp-native-child",
                        "name": "task",
                        "arguments": json.dumps(
                            {
                                "context": "Native OMP integration test.",
                                "tasks": [
                                    {
                                        "name": "PolicyChild",
                                        "model": f"omnigent/{_OTHER_MODEL}",
                                        "solutionSpace": "One diagnostic task; no edits.",
                                        "task": (
                                            "OMP_CHILD_INPUT: run the requested bash diagnostic "
                                            "and report completion."
                                        ),
                                    }
                                ],
                            }
                        ),
                    }
                ]
            },
            *[{"text": "OMP_PARENT_FINISHED", "stream": True}] * 4,
        ]
    )
    rig.script(
        [
            {
                "tool_calls": [
                    {
                        "call_id": "omp-child-bash",
                        "name": "bash",
                        "arguments": json.dumps({"command": f"printf forbidden > {marker}"}),
                    }
                ]
            },
            {"text": "OMP_CHILD_FINISHED", "stream": True},
        ],
        model=_OTHER_MODEL,
    )
    rig.script([{"text": "Policy child"}], model=_TITLE_MODEL)
    rig.client.post(
        f"/v1/sessions/{rig.session_id}/policies",
        json={
            "name": "omp_root_blocks_child_bash",
            "type": "python",
            "handler": "omnigent.policies.builtins.cel.cel_policy",
            "factory_params": {
                "expression": (
                    'event.type == "tool_call" && event.target == "bash" ? '
                    '{"result": "DENY", "reason": "root protects descendants"} '
                    ': {"result": "ALLOW"}'
                )
            },
        },
    ).raise_for_status()
    rig.send("Delegate one diagnostic to a native task child.")
    rig.wait_answer("OMP_PARENT_FINISHED")
    children: list[dict] = []

    def child_ready() -> bool:
        nonlocal children
        response = rig.client.get(
            f"/v1/sessions/{rig.session_id}/child_sessions", params={"limit": 100}
        )
        response.raise_for_status()
        children = response.json()["data"]
        return any(
            child.get("labels", {}).get("omnigent.wrapper") == "omp-native-ui-subagent"
            for child in children
        )

    poll_until(child_ready, timeout_s=60, interval_s=0.2)
    child_id = next(
        child["id"]
        for child in children
        if child.get("labels", {}).get("omnigent.wrapper") == "omp-native-ui-subagent"
    )
    rig.sessions.append(child_id)
    rig.wait_answer("OMP_CHILD_FINISHED", child_id)
    assert not marker.exists()
    assert not any(
        item.get("role") == "assistant" and "OMP_CHILD_FINISHED" in json.dumps(item)
        for item in rig.items()
    )
    child_outputs = [
        item for item in rig.items(child_id) if item.get("type") == "function_call_output"
    ]
    assert any("root protects descendants" in json.dumps(item) for item in child_outputs)


@pytest.mark.parametrize("omp_rig", ["anthropic"], indirect=True)
def test_reasoning_usage_and_inline_image_delivery(omp_rig: OmpRig) -> None:
    rig = omp_rig
    thinking = "OMP reasons through the supplied image before answering this test request."
    rig.script(
        [
            {
                "text": "OMP_IMAGE_REASONING_DONE",
                "thinking": thinking,
                "chunk_delay": 0.01,
                "usage": {
                    "input_tokens": 42,
                    "cache_creation_input_tokens": 5,
                    "cache_read_input_tokens": 7,
                },
            }
        ]
    )
    rig.client.patch(
        f"/v1/sessions/{rig.session_id}",
        json={"reasoning_effort": "high"},
    ).raise_for_status()
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jH1sAAAAASUVORK5CYII="
    )
    with _events(rig, "OMP_IMAGE_REASONING_DONE") as events:
        rig.client.post(
            f"/v1/sessions/{rig.session_id}/events",
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "Inspect the supplied image and answer briefly.",
                        },
                        {
                            "type": "input_image",
                            "filename": "omp-image.png",
                            "image_url": f"data:image/png;base64,{base64.b64encode(png).decode()}",
                        },
                    ],
                },
            },
            timeout=30,
        ).raise_for_status()
        items = rig.wait_answer("OMP_IMAGE_REASONING_DONE")
    assert any(
        name.startswith("response.reasoning") and name.endswith(".delta") for name, _ in events
    )
    assert any(
        item.get("type") == "reasoning" and "OMP reasons" in json.dumps(item) for item in items
    )
    request = rig.requests()[-1]
    images = [
        block
        for message in request.get("messages", [])
        for block in message.get("content", [])
        if isinstance(block, dict) and block.get("type") == "image"
    ]
    assert len(images) == 1, request
    assert images[0]["source"]["media_type"].startswith("image/")
    # OMP resizes/re-encodes images; preserve the pixels, not its encoding policy.
    with Image.open(BytesIO(base64.b64decode(images[0]["source"]["data"]))) as delivered:
        assert delivered.convert("RGB").getextrema() == ((255, 255),) * 3
    assert request.get("thinking", {}).get("type") in {"enabled", "adaptive"}
    poll_until(
        lambda: bool(rig.snapshot().get("usage_by_model")),
        timeout_s=15,
        interval_s=0.1,
    )
    usage = rig.snapshot()["usage_by_model"][rig.model]
    # The API splits cache reads out; cache creation remains in the input count.
    assert usage["input_tokens"] == 47, rig.snapshot()["usage_by_model"]
    assert usage["cache_read_input_tokens"] == 7
    assert usage["output_tokens"] == len(thinking.split()) + 1
    assert usage["total_tokens"] == 54 + len(thinking.split()) + 1
    assert rig.snapshot()["total_cost_usd"] > 0


def test_busy_input_is_delivered_once_after_the_current_turn(omp_rig: OmpRig) -> None:
    rig = omp_rig
    rig.script(
        [
            {"text": "OMP_BUSY_FIRST_DONE", "block": True, "stream": True},
            {"text": "OMP_BUSY_FOLLOWUP_DONE", "stream": True},
        ]
    )
    try:
        rig.send("OMP_BUSY_ORIGINAL: begin a long analysis.")
        poll_until(lambda: len(rig.requests()) == 1, timeout_s=20, interval_s=0.1)
        rig.send("OMP_BUSY_FOLLOWUP: continue with this additional requirement.")

        def queued_in_tui() -> bool:
            pane = rig.pane()
            return "OMP_BUSY_FOLLOWUP" in pane and "Steering" in pane

        poll_until(queued_in_tui, timeout_s=20, interval_s=0.1)
        release_mock_gate(rig.model_server)
        items = rig.wait_answer("OMP_BUSY_FOLLOWUP_DONE")
    finally:
        release_mock_gate(rig.model_server)
    for marker in ("OMP_BUSY_ORIGINAL", "OMP_BUSY_FOLLOWUP"):
        assert (
            sum(item.get("role") == "user" and marker in json.dumps(item) for item in items) == 1
        )
        assert marker in json.dumps(rig.requests()[-1]["input"])


def test_native_html_form_answers_reach_the_model(omp_rig: OmpRig) -> None:
    rig = omp_rig
    form = """<form>
<label>Release note <textarea name="note">Default note</textarea></label>
<label>Priority <input type="number" name="priority" value="2" min="1" max="5"></label>
<fieldset><legend>Review</legend>
<label><input type="radio" name="review" value="ship" checked>Ship</label>
<label><input type="radio" name="review" value="hold">Hold</label></fieldset>
<fieldset><legend>Checks</legend>
<label><input type="checkbox" name="checks" value="unit" checked>Unit</label>
<label><input type="checkbox" name="checks" value="native">Native</label></fieldset>
</form>"""
    rig.script(
        [
            {
                "tool_calls": [
                    {"call_id": "omp-form", "name": "ask", "arguments": json.dumps({"html": form})}
                ]
            },
            {"text": "OMP_FORM_DONE", "stream": True},
        ]
    )
    with _events(rig, "OMP_FORM_DONE") as events:
        rig.send("Ask me for the release form, then confirm receipt.")
        poll_until(
            lambda: any(name == "response.elicitation_request" for name, _ in events),
            timeout_s=20,
            interval_s=0.1,
        )
        elicitation = next(data for name, data in events if name == "response.elicitation_request")
        rig.client.post(
            f"/v1/sessions/{rig.session_id}/elicitations/{elicitation['elicitation_id']}/resolve",
            json={
                "action": "accept",
                "content": {
                    "f0": "Reviewed native\nOMP integration",
                    "f1": 3,
                    "f2": "0",
                    "f3_0": True,
                    "f3_1": True,
                },
            },
        ).raise_for_status()
        rig.wait_answer("OMP_FORM_DONE")
    outputs = [
        item["output"]
        for item in rig.requests()[-1]["input"]
        if item.get("type") == "function_call_output"
    ]
    assert len(outputs) == 1
    answers = json.loads(outputs[0].removeprefix("User answers:\n").strip())
    assert answers == {
        "note": "Reviewed native\nOMP integration",
        "priority": 3,
        "review": "ship",
        "checks": ["unit", "native"],
    }
