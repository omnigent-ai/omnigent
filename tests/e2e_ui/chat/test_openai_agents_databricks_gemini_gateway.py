"""Read two files through the browser, server, runner, and real OpenAI Agents SDK.

An isolated HOME supplies a Databricks profile and ucode codex URL; the strict
mock gateway requires Gemini to use MLflow and preserve tool thought signatures.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, expect

from omnigent.runner.identity import token_bound_runner_id
from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable
from tests._helpers.live_server import isolated_process_env, terminate_process
from tests.e2e_ui.chat._databricks_gemini_gateway_mock import (
    GeminiGatewayMock,
    write_gateway_home,
)
from tests.e2e_ui.conftest import (
    _HEALTH_POLL_INTERVAL_S,
    _HEALTH_TIMEOUT_S,
    _create_bundled_session,
)

MODEL = "databricks-gemini-3-8-flash"
PROFILE = "omni-gemini-codex-surface"
FILES = {"notes/alpha.txt": "ALPHA-CONTENT-7f3a", "notes/bravo.txt": "BRAVO-CONTENT-9c1e"}
PROMPT = (
    "Read notes/alpha.txt and notes/bravo.txt with your file tool and tell me what each one says."
)
_WORKING = '[data-testid="working-indicator"]'
_TURN_TIMEOUT_S = 120.0
# A file requested this many times means the model never sees its earlier result.
_LOOP_REPEATS = 3

_AGENT_YAML = """\
spec_version: 1
name: gemini_gateway_probe
prompt: |
  You are a file assistant. Use the sys_os_read tool to read each file the user
  names, one call per file, then tell the user what each file says.

executor:
  model: {model}
  auth:
    type: databricks
    profile: {profile}
  config:
    harness: openai-agents

os_env:
  type: caller_process
  cwd: {workspace}
  sandbox:
    type: none
"""


@dataclass
class GatewayHome:
    """One attempt's workspace, mock gateway, and isolated runner."""

    workspace: Path
    mock: GeminiGatewayMock
    runner_id: str


@dataclass
class Outcome:
    kind: str
    detail: str


@contextmanager
def _spawn_runner(base_url: str, home: Path, log_path: Path) -> Iterator[str]:
    """Start the selected runner with only the scratch Databricks/OpenAI credentials."""
    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)
    base_env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("DATABRICKS_", "OPENAI_"))
    }
    env = isolated_process_env(
        home,
        {
            "OMNIGENT_CONFIG_HOME": str(home / ".omnigent"),
            "OMNIGENT_RUNNER_ID": runner_id,
            "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
            "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
            "RUNNER_SERVER_URL": base_url,
        },
        base_env,
    )
    with open(log_path, "w") as log_handle:
        proc = subprocess.Popen(
            [runner_executable(), "-m", "omnigent.runner._entry"],
            env=apply_runner_env(env),
            cwd=compat_runner_cwd(),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
    try:
        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"gateway runner exited early (code {proc.returncode}); log:\n"
                    f"{log_path.read_text()[-3000:]}"
                )
            try:
                status = httpx.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
            except httpx.HTTPError:
                pass
            else:
                if status.status_code == 200 and status.json().get("online") is True:
                    yield runner_id
                    return
            time.sleep(_HEALTH_POLL_INTERVAL_S)
        raise RuntimeError(
            f"gateway runner did not come online within {_HEALTH_TIMEOUT_S:.0f}s; log:\n"
            f"{log_path.read_text()[-3000:]}"
        )
    finally:
        terminate_process(proc)


@pytest.fixture
def gateway_home(live_server: str, tmp_path: Path) -> Iterator[GatewayHome]:
    home = tmp_path / "home"
    workspace = home / "workspace"
    for rel, marker in FILES.items():
        target = workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{marker}\nfixture file for the two-file read journey\n")
    mock = GeminiGatewayMock(files=FILES)
    origin = mock.start()
    try:
        write_gateway_home(home, PROFILE, origin)
        with _spawn_runner(live_server, home, home / "runner.log") as runner_id:
            yield GatewayHome(workspace=workspace, mock=mock, runner_id=runner_id)
    finally:
        mock.stop()


@contextmanager
def _gemini_session(base_url: str, gateway: GatewayHome) -> Iterator[str]:
    yaml_text = _AGENT_YAML.format(
        model=MODEL,
        profile=PROFILE,
        workspace=gateway.workspace,
    )
    session_id = _create_bundled_session(
        base_url, gateway.runner_id, yaml_text, operator_authored=True
    )
    try:
        yield session_id
    finally:
        # Best-effort cleanup: after a hard failure the server may be gone, and
        # an error here would mask the test's own failure.
        with suppress(httpx.HTTPError):
            httpx.delete(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)


def _send(page: Page, text: str) -> None:
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _error_text(page: Page) -> str | None:
    pill = page.get_by_test_id("error-pill")
    if pill.count() == 0:
        return None
    first = pill.first
    headline = first.get_by_test_id("error-headline")
    text = headline.inner_text() if headline.count() else first.inner_text()
    content = first.get_by_test_id("error-message-content")
    if content.count() == 0 and headline.count():
        # Short, guarded click: this runs inside the outcome poll loop, where
        # Playwright's 30 s default would stall it on a detached headline.
        try:
            headline.click(timeout=2_000)
        except PlaywrightError:
            return text
        page.wait_for_timeout(300)
        content = first.get_by_test_id("error-message-content")
    if content.count():
        text = f"{text} :: {content.first.inner_text()}"
    return text


def _final_answer_visible(page: Page) -> bool:
    return page.get_by_text("notes/bravo.txt says", exact=False).count() > 0


def _wait_for_outcome(page: Page, mock: GeminiGatewayMock) -> Outcome:
    deadline = time.monotonic() + _TURN_TIMEOUT_S
    while time.monotonic() < deadline:
        if _final_answer_visible(page):
            return Outcome("answer", "; ".join(mock.decisions()))
        error = _error_text(page)
        if error is not None:
            return Outcome("error", error)
        decisions = mock.decisions()
        repeats = {path: decisions.count(path) for path in FILES}
        if max(repeats.values()) >= _LOOP_REPEATS:
            stop = "stopped via Interrupt"
            try:
                page.get_by_role("button", name="Interrupt", exact=True).click(timeout=10_000)
            except Exception as exc:
                stop = f"Interrupt click failed: {exc}"
            return Outcome(
                "loop", f"{len(decisions)} model calls, reads per file {repeats}; {stop}"
            )
        page.wait_for_timeout(500)
    return Outcome("timeout", "; ".join(mock.decisions()))


def _dump_ui_state(
    page: Page, base_url: str, session_id: str, outcome: Outcome, path: Path
) -> None:
    # Diagnostics only: a failed fetch must not replace the outcome assertion.
    try:
        items = httpx.get(f"{base_url}/v1/sessions/{session_id}/items", timeout=10.0).json()
    except (httpx.HTTPError, ValueError) as exc:
        items = {"data": [], "fetch_error": repr(exc)}
    rows = items.get("data", items) if isinstance(items, dict) else items
    kinds: dict[str, int] = {}
    for row in rows:
        key = f"{row.get('type')}:{row.get('name') or row.get('role') or ''}"
        kinds[key] = kinds.get(key, 0) + 1
    state = {
        "session_id": session_id,
        "outcome": outcome.__dict__,
        "visible_read_mentions": {
            rel: page.get_by_text(rel, exact=False).count() for rel in FILES
        },
        "visible_bubbles": page.locator('[data-testid="message-bubble"]').all_inner_texts(),
        "visible_fold_rows": page.locator('[data-slot="collapsible-trigger"]').all_inner_texts(),
        "error_pill": _error_text(page),
        "transcript_item_counts": kinds,
        "video": page.video.path() if page.video is not None else None,
    }
    path.write_text(json.dumps(state, indent=2))


def _evidence(mock: GeminiGatewayMock) -> str:
    rows = [
        f"{e['seq']:>4} {e['status']} {e['path']} :: {e['note']}"
        for e in mock.journal[:8] + mock.journal[-4:]
    ]
    return f"{len(mock.journal)} gateway requests\n" + "\n".join(dict.fromkeys(rows))


@pytest.mark.timeout(420)
def test_databricks_gemini_two_file_read_completes(
    page: Page,
    live_server: str,
    gateway_home: GatewayHome,
    tmp_path: Path,
) -> None:
    """The codex URL resolves to MLflow; signed, distinct reads produce the browser answer."""
    mock = gateway_home.mock
    with _gemini_session(live_server, gateway_home) as session_id:
        page.goto(f"{live_server}/c/{session_id}")
        _send(page, PROMPT)
        outcome = _wait_for_outcome(page, mock)
        page.wait_for_timeout(3_000)
        page.screenshot(path=str(tmp_path / "codex-surface-outcome.png"), full_page=True)
        mock.dump(str(tmp_path / "codex-surface-gateway-journal.json"))
        _dump_ui_state(
            page, live_server, session_id, outcome, tmp_path / "codex-surface-ui-state.json"
        )

        assert outcome.kind == "answer", (
            f"turn ended with {outcome.kind}: {outcome.detail}\n"
            f"gateway journal:\n{_evidence(mock)}"
        )
        for path, marker in FILES.items():
            expect(page.get_by_text(f"{path} says {marker}", exact=False)).to_be_visible()
        expect(page.locator(_WORKING)).to_have_count(0, timeout=30_000)

    assert mock.decisions() == [*FILES, "<answer>"], (
        f"model saw an unexpected call sequence: {mock.decisions()}"
    )
    paths = {entry["path"] for entry in mock.journal}
    assert paths == {"/ai-gateway/mlflow/v1/chat/completions"}, (
        f"requests reached surfaces that do not serve Gemini: {sorted(paths)}"
    )
    messages = mock.journal[-1]["body"]["messages"]
    calls = [
        call
        for message in messages
        if message["role"] == "assistant"
        for call in message.get("tool_calls") or []
    ]
    assert [call["function"]["name"] for call in calls] == ["sys_os_read", "sys_os_read"]
    assert [json.loads(call["function"]["arguments"])["path"] for call in calls] == list(FILES)
    call_ids = [call["id"] for call in calls]
    assert len(set(call_ids)) == 2
    assert all(call_ids) and "sys_os_read" not in call_ids
    assert [call["thoughtSignature"] for call in calls] == [
        "thought-signature-1",
        "thought-signature-2",
    ]
    results = [message for message in messages if message["role"] == "tool"]
    assert [result["tool_call_id"] for result in results] == call_ids
    for result, marker in zip(results, FILES.values(), strict=True):
        assert marker in json.dumps(result["content"])
