"""Real Codex turns honor model capabilities through the server and runner.

Only Responses replies are mocked. The installed Codex binary supplies the
catalog, runs its app-server and TUI, and sends the recorded HTTP requests.
Run with ``uv run --no-sync pytest tests/e2e/test_codex_native_supported_efforts_e2e.py -v -s``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import tomllib

from omnigent.harnesses.codex_native.app_server import client_for_transport
from omnigent.harnesses.codex_native.bridge import read_bridge_state
from tests._helpers.live_server import terminate_process
from tests._helpers.native_session import create_native_session
from tests._helpers.server_runner import ServerRunner, server_runner

_REPO_ROOT = Path(__file__).resolve().parents[2]
pytestmark = [
    pytest.mark.posix_only,
    pytest.mark.timeout(240),
    pytest.mark.skipif(
        shutil.which("codex") is None or shutil.which("tmux") is None,
        reason="requires real Codex and tmux binaries",
    ),
]


def _json(response: httpx.Response) -> Any:
    response.raise_for_status()
    return response.json()


@dataclass
class _Rig:
    stack: ServerRunner
    api: httpx.Client
    model: httpx.Client
    source_home: Path
    catalog: dict[str, Any]
    runner_env: dict[str, str]

    def capabilities(self, model: str) -> set[str]:
        row = next((row for row in self.catalog["models"] if row["slug"] == model), None)
        if row is None:
            pytest.skip(f"installed Codex does not advertise {model}")
        return {level["effort"] for level in row["supported_reasoning_levels"]}


@pytest.fixture(scope="module")
def codex_effort_rig(
    tmp_path_factory: pytest.TempPathFactory, mock_llm_server_url: str
) -> Iterator[_Rig]:
    """Own a real server, runner, and private Codex home without ambient credentials."""
    root = tmp_path_factory.mktemp("codex-supported-efforts")
    source = root / "codex-source"
    config = root / "config"
    source.mkdir()
    config.mkdir()
    (config / "config.yaml").write_text(
        json.dumps(
            {
                "runner": {"idle_timeout_s": 0},
                "providers": {
                    "effort-test": {
                        "kind": "key",
                        "default": ["openai"],
                        "openai": {
                            "base_url": f"{mock_llm_server_url}/v1",
                            "api_key": "mock-key",
                            "wire_api": "responses",
                            "models": {"default": "gpt-5.4"},
                        },
                    }
                },
            }
        )
    )
    base_env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    }
    env = {
        "OMNIGENT_CONFIG_HOME": str(config),
        "CODEX_HOME": str(source),
        "TERM": "xterm-256color",
    }
    catalog = json.loads(
        subprocess.run(
            ["codex", "debug", "models", "--bundled"],
            env={**base_env, **env, "HOME": str(root)},
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        ).stdout
    )
    (root / "bundled-catalog.json").write_text(json.dumps(catalog, indent=2))
    version = subprocess.check_output(["codex", "--version"], text=True).strip()
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=_REPO_ROOT, text=True)
    print(f"Real harness: {version}; checkout: {revision.strip()}; evidence: {root}", flush=True)
    with (
        server_runner(root, base_env=base_env, server_env=env, server_cwd=_REPO_ROOT) as stack,
        httpx.Client(base_url=stack.base_url, timeout=30, trust_env=False) as api,
        httpx.Client(base_url=mock_llm_server_url, timeout=10, trust_env=False) as model,
    ):
        stack.start_runner(cwd=_REPO_ROOT, env=env)
        yield _Rig(stack, api, model, source, catalog, env)


@dataclass
class _Session:
    id: str
    bridge: Path
    terminal: dict[str, Any]

    def ensure_terminal(self, rig: _Rig) -> None:
        self.terminal = _json(
            rig.api.post(
                f"/v1/sessions/{self.id}/resources/terminals",
                json={"terminal": "codex", "session_key": "main", "ensure_native_terminal": True},
                timeout=90,
            )
        )
        deadline = time.monotonic() + 60
        pane = ""
        while time.monotonic() < deadline:
            pane = self.tmux("capture-pane", "-p")
            # Ultra uses a double chevron; the final composer follows earlier user turns.
            composers = [
                line.lstrip()[1:].strip()
                for line in pane.splitlines()
                if line.lstrip().startswith(("›", "»"))
            ]
            disabled = {"Input disabled.", "Shutting down...", "Answer the questions to continue."}
            if read_bridge_state(self.bridge) and composers and composers[-1] not in disabled:
                return
            time.sleep(0.2)
        pytest.fail(f"Codex terminal never became interactive.\n{pane}\n{rig.stack.log_tail()}")

    def tmux(self, *args: str) -> str:
        metadata = self.terminal["metadata"]
        return subprocess.run(
            [
                "tmux",
                "-S",
                metadata["tmux_socket"],
                args[0],
                "-t",
                metadata["tmux_target"],
                *args[1:],
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout


@contextmanager
def _session(rig: _Rig, model: str, effort: str, *, inherited: bool = False) -> Iterator[_Session]:
    source_effort = effort if inherited else "medium"
    source_config = f'model = "{model}"\nmodel_reasoning_effort = "{source_effort}"\n'
    # Record the user's choice to keep the model instead of accepting an upgrade.
    migrations = {
        row["slug"]: row["upgrade"]["model"]
        for row in rig.catalog["models"]
        if isinstance(row.get("upgrade"), dict) and row["upgrade"].get("model")
    }
    source_config += "\n[notice.model_migrations]\n" + "".join(
        f"{json.dumps(old)} = {json.dumps(new)}\n" for old, new in migrations.items()
    )
    (rig.source_home / "config.toml").write_text(source_config)
    metadata: dict[str, Any] = {"workspace": str(rig.stack.workspace)}
    if not inherited:
        metadata["reasoning_effort"] = effort
    created = create_native_session(
        rig.api, rig.stack.base_url, harness="codex", model=model, metadata=metadata
    )
    session_id = created["session_id"]
    bridge_id = hashlib.sha256(session_id.encode()).hexdigest()[:32]
    bridge = rig.stack.runner_home / ".omnigent" / "codex-native" / bridge_id
    try:
        _json(rig.api.patch(f"/v1/sessions/{session_id}", json={"runner_id": rig.stack.runner_id}))
        session = _Session(session_id, bridge, {})
        session.ensure_terminal(rig)
        yield session
        assert (rig.source_home / "config.toml").read_text() == source_config
    finally:
        # The transcript is diagnostic only; deleting the session must still happen.
        with suppress(httpx.HTTPError, OSError):
            transcript = rig.api.get(f"/v1/sessions/{session_id}/items", params={"limit": 100})
            (rig.stack.root / f"{session_id}-transcript.json").write_text(transcript.text)
        rig.api.delete(f"/v1/sessions/{session_id}")


async def _native_settings(session: _Session, model: str) -> dict[str, Any]:
    state = read_bridge_state(session.bridge)
    assert state is not None
    client = client_for_transport(state.socket_path, client_name="effort-e2e-observer")
    await client.connect()
    try:
        cursor = None
        while True:
            response = await client.request(
                "model/list", {"includeHidden": True, "cursor": cursor, "limit": 100}
            )
            result = response["result"]
            for row in result["data"]:
                if model in (row.get("id"), row.get("model")):
                    thread = await client.request(
                        "thread/read", {"threadId": state.thread_id, "includeTurns": False}
                    )
                    return {"model": row, "thread": thread["result"]["thread"]}
            cursor = result.get("nextCursor")
            assert cursor, f"Real Codex model/list did not advertise {model}"
    finally:
        await client.close()


def _assert_turn(
    rig: _Rig, session: _Session, model: str, expected: str, *, terminal: bool = False
) -> None:
    marker = f"EFFORT_OK_{uuid.uuid4().hex}"
    prompt = f"Reply with {marker}"
    # Background title requests share the prompt; they must not exhaust the turn's reply.
    _json(
        rig.model.post("/mock/configure", json={"key": marker, "responses": [], "match": marker})
    )
    _json(rig.model.post("/mock/set_fallback", json={"key": marker, "text": marker}))
    before = len(_json(rig.model.get("/mock/requests"))["requests"])
    if terminal:
        metadata = session.terminal["metadata"]
        subprocess.run(
            [
                "tmux",
                "-S",
                metadata["tmux_socket"],
                "send-keys",
                "-t",
                metadata["tmux_target"],
                "-l",
                "--",
                prompt,
            ],
            check=True,
            timeout=10,
        )
        # Let Codex finish classifying the burst as pasted text before Enter.
        time.sleep(0.3)
        session.tmux("send-keys", "Enter")
        # A slow TUI can still be absorbing the paste when Enter lands, which drops
        # the submit; resend it until the turn's request reaches the model.
        for _ in range(10):
            time.sleep(1)
            sent = _json(rig.model.get("/mock/requests"))["requests"][before:]
            if any(marker in json.dumps(request) for request in sent):
                break
            session.tmux("send-keys", "Enter")
    else:
        _json(
            rig.api.post(
                f"/v1/sessions/{session.id}/events",
                json={
                    "type": "message",
                    "data": {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
                },
            )
        )
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        items = _json(rig.api.get(f"/v1/sessions/{session.id}/items", params={"limit": 100}))[
            "data"
        ]
        snapshot = _json(rig.api.get(f"/v1/sessions/{session.id}"))
        assert snapshot.get("status") != "failed", snapshot.get("status_error")
        state = read_bridge_state(session.bridge)
        replied = any(
            item.get("role") == "assistant" and marker in json.dumps(item.get("content"))
            for item in items
        )
        if replied and state is not None and state.active_turn_id is None:
            break
        time.sleep(0.2)
    else:
        pytest.fail(
            f"No completed Codex reply.\n{session.tmux('capture-pane', '-p')}\n"
            f"{rig.stack.log_tail()}"
        )
    requests = _json(rig.model.get("/mock/requests"))["requests"][before:]
    requests = [request for request in requests if marker in json.dumps(request.get("input"))]
    (rig.stack.root / f"{marker}-all-requests.json").write_text(json.dumps(requests, indent=2))
    assert state is not None
    # Codex's background title threads echo the same prompt but can use another model.
    # Select the user thread by identity, leaving model and effort assertions unfiltered.
    background_requests = sum(
        request.get("prompt_cache_key") != state.thread_id for request in requests
    )
    requests = [
        request for request in requests if request.get("prompt_cache_key") == state.thread_id
    ]
    (rig.stack.root / f"{marker}-requests.json").write_text(json.dumps(requests, indent=2))
    assert requests, "The real Codex process must send a Responses request for this turn"
    assert {request["model"] for request in requests} == {model}
    efforts = [request.get("reasoning", {}).get("effort") for request in requests]
    settings = asyncio.run(_native_settings(session, model))
    thread = settings["thread"]
    if "reasoningEffort" in thread:
        native_effort = thread["reasoningEffort"]
        effort_source = "thread/read"
    else:
        # Older Codex exposes this setting in its rollout instead of thread/read.
        contexts = [
            row["payload"]
            for line in Path(thread["path"]).read_text().splitlines()
            if (row := json.loads(line)).get("type") == "turn_context"
        ]
        assert contexts, "Codex must persist the completed turn's context"
        native_effort = contexts[-1]["effort"]
        effort_source = "rollout"
    state = read_bridge_state(session.bridge)
    assert state is not None
    config = tomllib.loads((Path(state.codex_home) / "config.toml").read_text())
    evidence = {
        "thread_id": state.thread_id,
        "background_requests": background_requests,
        "model": model,
        "expected": expected,
        "wire_efforts": efforts,
        "config_effort": config.get("model_reasoning_effort"),
        "native_effort": native_effort,
        "native_effort_source": effort_source,
        "advertised": settings["model"]["supportedReasoningEfforts"],
    }
    (rig.stack.root / f"{marker}.json").write_text(json.dumps(evidence, indent=2))
    print(json.dumps(evidence), flush=True)
    # Codex preserves ultra internally but serializes it as max on Responses.
    assert set(efforts) == {"max" if expected == "ultra" else expected}, evidence
    assert config.get("model_reasoning_effort") == expected, evidence
    assert native_effort == expected, evidence
    assert expected in {
        level["reasoningEffort"] for level in settings["model"]["supportedReasoningEfforts"]
    }, evidence
    deadline = time.monotonic() + 10
    while snapshot.get("reasoning_effort") != expected and time.monotonic() < deadline:
        time.sleep(0.2)
        snapshot = _json(rig.api.get(f"/v1/sessions/{session.id}"))
    assert snapshot.get("reasoning_effort") == expected, snapshot


@pytest.mark.parametrize("entry_point", ["launch", "inherited", "existing"])
@pytest.mark.parametrize(
    ("model", "requested", "expected"),
    [
        ("gpt-5.4", "minimal", "low"),
        ("gpt-5.4", "max", "xhigh"),
        ("gpt-5.6-sol", "minimal", "low"),
    ],
)
def test_codex_clamps_unsupported_effort(
    codex_effort_rig: _Rig, entry_point: str, model: str, requested: str, expected: str
) -> None:
    """Fresh and existing native sessions send only the model's advertised efforts."""
    rig = codex_effort_rig
    offered = rig.capabilities(model)
    assert requested not in offered and expected in offered, offered
    # The picker must be corrected even when clamping leaves the native effort unchanged.
    initial = expected if entry_point == "existing" else requested
    with _session(rig, model, initial, inherited=entry_point == "inherited") as session:
        if entry_point == "existing":
            _assert_turn(rig, session, model, expected)
            _json(
                rig.api.patch(f"/v1/sessions/{session.id}", json={"reasoning_effort": requested})
            )
        # Launch goes through the TUI so turn-dispatch clamping cannot mask a launch bug.
        _assert_turn(rig, session, model, expected, terminal=entry_point != "existing")


@pytest.mark.parametrize(
    ("model", "effort"), [("gpt-5.4", "xhigh"), ("gpt-6-sol", "max"), ("gpt-6-sol", "ultra")]
)
def test_codex_preserves_supported_effort(codex_effort_rig: _Rig, model: str, effort: str) -> None:
    """Supported high levels survive both launch and subsequent settings changes."""
    rig = codex_effort_rig
    assert effort in rig.capabilities(model)
    with _session(rig, model, effort) as session:
        _assert_turn(rig, session, model, effort, terminal=True)
        _json(rig.api.patch(f"/v1/sessions/{session.id}", json={"reasoning_effort": effort}))
        _assert_turn(rig, session, model, effort)


def test_codex_model_switch_clamps_inherited_effort(codex_effort_rig: _Rig) -> None:
    """Changing only the model revalidates the existing session's supported max effort."""
    rig = codex_effort_rig
    assert "max" in rig.capabilities("gpt-6-sol")
    assert "max" not in rig.capabilities("gpt-5.4")
    with _session(rig, "gpt-6-sol", "max") as session:
        _assert_turn(rig, session, "gpt-6-sol", "max")
        _json(rig.api.patch(f"/v1/sessions/{session.id}", json={"model_override": "gpt-5.4"}))
        _assert_turn(rig, session, "gpt-5.4", "xhigh")


def test_codex_effort_reset_survives_next_turn(codex_effort_rig: _Rig) -> None:
    """Clearing the picker uses the real model default without restoring the saved effort."""
    rig = codex_effort_rig
    assert "xhigh" in rig.capabilities("gpt-5.4")
    with _session(rig, "gpt-5.4", "xhigh") as session:
        _assert_turn(rig, session, "gpt-5.4", "xhigh")
        settings = asyncio.run(_native_settings(session, "gpt-5.4"))
        default = settings["model"]["defaultReasoningEffort"]
        assert default != "xhigh", "Reset must select a different effort than the prior setting"
        # REST's default selection forwards an explicit effort:null to the native runner.
        _json(rig.api.patch(f"/v1/sessions/{session.id}", json={"reasoning_effort": "default"}))
        _assert_turn(rig, session, "gpt-5.4", default, terminal=True)
        _assert_turn(rig, session, "gpt-5.4", default)


def test_codex_combined_model_and_reset_uses_target_default(codex_effort_rig: _Rig) -> None:
    """A combined public update selects the new model's real advertised default."""
    rig = codex_effort_rig
    supported = rig.capabilities("gpt-5.6-sol")
    with _session(rig, "gpt-5.4", "xhigh") as session:
        _assert_turn(rig, session, "gpt-5.4", "xhigh")
        previous = asyncio.run(_native_settings(session, "gpt-5.4"))["model"]
        target = asyncio.run(_native_settings(session, "gpt-5.6-sol"))["model"]
        default = target["defaultReasoningEffort"]
        assert previous["defaultReasoningEffort"] != default
        assert previous["defaultReasoningEffort"] in supported
        _json(
            rig.api.patch(
                f"/v1/sessions/{session.id}",
                json={"model_override": "gpt-5.6-sol", "reasoning_effort": "default"},
            )
        )
        _assert_turn(rig, session, "gpt-5.6-sol", default, terminal=True)
        _assert_turn(rig, session, "gpt-5.6-sol", default)


def test_codex_resume_clamps_persisted_effort(codex_effort_rig: _Rig) -> None:
    """Restart the real runner, resume the same Codex thread, and send from its TUI."""
    rig = codex_effort_rig
    assert "minimal" not in rig.capabilities("gpt-5.4")
    with _session(rig, "gpt-5.4", "medium") as session:
        _assert_turn(rig, session, "gpt-5.4", "medium")
        original = read_bridge_state(session.bridge)
        assert original is not None
        terminate_process(rig.stack.runner)
        rig.stack.runner = None
        deadline = time.monotonic() + 15
        status = _json(rig.api.get(f"/v1/runners/{rig.stack.runner_id}/status"))
        while status["online"] and time.monotonic() < deadline:
            time.sleep(0.2)
            status = _json(rig.api.get(f"/v1/runners/{rig.stack.runner_id}/status"))
        assert not status["online"], "The old runner must be offline before changing saved effort"
        _json(rig.api.patch(f"/v1/sessions/{session.id}", json={"reasoning_effort": "minimal"}))
        saved = _json(rig.api.get(f"/v1/sessions/{session.id}"))
        assert saved["reasoning_effort"] == "minimal", "Resume must load an unsupported setting"
        rig.stack.start_runner(cwd=_REPO_ROOT, env=rig.runner_env)
        session.ensure_terminal(rig)
        resumed = read_bridge_state(session.bridge)
        assert resumed is not None
        assert resumed.socket_path != original.socket_path, "A new app-server must own the resume"
        assert resumed.thread_id == original.thread_id, "Resume must retain the existing thread"
        _assert_turn(rig, session, "gpt-5.4", "low", terminal=True)
