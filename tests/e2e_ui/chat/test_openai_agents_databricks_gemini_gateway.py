"""UI journey: openai-agents reading two files on a Databricks-hosted Gemini model.

The agent spec pins ``databricks-gemini-3-8-flash`` with
``auth: {type: databricks, profile: <p>}`` and an ``os_env``; the user asks for two
file reads in the web chat. A local stand-in for the Databricks AI Gateway serves the
model (``_databricks_gemini_gateway_mock``), and ucode state supplies the gateway base
URL exactly as ``configure_agent_harness_with_ucode`` reads it. Each variant points
its profile at a differently configured gateway origin:

- ``codex-surface``: ucode's codex base URL, which only proxies GPT Responses.
- ``mlflow-surface``: base URL forced to ``/ai-gateway/mlflow/v1`` (serves Gemini).
- ``mlflow-lenient``: the same surface without the thought-signature check, so the
  tool loop caused by ``id == function name`` tool calls is reachable.

The fake Databricks CLI profiles and ucode state live in a scratch ``HOME``
that only a dedicated runner (spawned here against the shared server) sees, so the
developer's own Databricks and ucode configuration is never read or written.
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from omnigent.runner.identity import token_bound_runner_id
from tests.e2e_ui.chat._databricks_gemini_gateway_mock import (
    GeminiGatewayMock,
    write_gateway_home,
)
from tests.e2e_ui.conftest import (
    _HEALTH_POLL_INTERVAL_S,
    _HEALTH_TIMEOUT_S,
    _REPO_ROOT,
    _create_bundled_session,
)

MODEL = "databricks-gemini-3-8-flash"
FILES = {"notes/alpha.txt": "ALPHA-CONTENT-7f3a", "notes/bravo.txt": "BRAVO-CONTENT-9c1e"}
PROMPT = (
    "Read notes/alpha.txt and notes/bravo.txt with your file tool and tell me what each one says."
)
_WORKING = '[data-testid="working-indicator"]'
_TURN_TIMEOUT_S = 120.0
# A file requested this many times means the model never sees its earlier result.
_LOOP_REPEATS = 3

_VARIANTS: dict[str, tuple[str, bool]] = {
    "codex-surface": ("codex/v1", True),
    "mlflow-surface": ("mlflow/v1", True),
    "mlflow-lenient": ("mlflow/v1", False),
}

_AGENT_YAML = """\
spec_version: 1
name: gemini_gateway_probe_{slug}
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
    """Scratch HOME, one mock gateway per variant, and the runner that uses them."""

    home: Path
    workspace: Path
    mocks: dict[str, GeminiGatewayMock]
    runner_id: str

    def profile(self, variant: str) -> str:
        return f"omni-gemini-{variant}"


@dataclass
class Outcome:
    kind: str
    detail: str


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _spawn_runner(
    base_url: str, home: Path, log_path: Path
) -> tuple[subprocess.Popen[bytes], str]:
    """Start a runner whose ``HOME`` is *home* and wait until the server sees it online.

    Ambient Databricks/OpenAI credentials are dropped so the harness can only
    authenticate through the scratch profile; a broken profile fails loudly instead
    of silently falling back to a mock key.
    """
    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("DATABRICKS_", "OPENAI_"))
    }
    env.update(
        HOME=str(home),
        PYTHONPATH=f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        OMNIGENT_RUNNER_ID=runner_id,
        OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN=binding_token,
        OMNIGENT_RUNNER_PARENT_PID=str(os.getpid()),
        RUNNER_SERVER_URL=base_url,
    )
    with open(log_path, "w") as log_handle:
        proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
    deadline = time.monotonic() + _HEALTH_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"gateway runner exited early (code {proc.returncode}); log:\n"
                f"{log_path.read_text()[-3000:]}"
            )
        try:
            status = httpx.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
            if status.status_code == 200 and status.json().get("online") is True:
                return proc, runner_id
        except httpx.HTTPError:
            pass
        time.sleep(_HEALTH_POLL_INTERVAL_S)
    _terminate(proc)
    raise RuntimeError(
        f"gateway runner did not come online within {_HEALTH_TIMEOUT_S:.0f}s; log:\n"
        f"{log_path.read_text()[-3000:]}"
    )


@pytest.fixture(scope="session")
def gateway_home(
    live_server: str, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[GatewayHome]:
    home = tmp_path_factory.mktemp("gemini_gateway_home")
    workspace = home / "workspace"
    for rel, marker in FILES.items():
        target = workspace / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"{marker}\nfixture file for the two-file read journey\n")
    mocks: dict[str, GeminiGatewayMock] = {}
    profiles: dict[str, tuple[str, str]] = {}
    for variant, (surface, enforce) in _VARIANTS.items():
        mock = GeminiGatewayMock(files=dict(FILES), enforce_thought_signature=enforce)
        origin = mock.start()
        mocks[variant] = mock
        profiles[f"omni-gemini-{variant}"] = (origin, f"{origin}/ai-gateway/{surface}")
    write_gateway_home(home, profiles)
    try:
        runner, runner_id = _spawn_runner(live_server, home, home / "runner.log")
    except Exception:
        for mock in mocks.values():
            mock.stop()
        raise
    try:
        yield GatewayHome(home=home, workspace=workspace, mocks=mocks, runner_id=runner_id)
    finally:
        _terminate(runner)
        for mock in mocks.values():
            mock.stop()


@contextmanager
def _gemini_session(base_url: str, gateway: GatewayHome, variant: str) -> Iterator[str]:
    yaml_text = _AGENT_YAML.format(
        slug=variant.replace("-", "_"),
        model=MODEL,
        profile=gateway.profile(variant),
        workspace=gateway.workspace,
    )
    session_id = _create_bundled_session(base_url, gateway.runner_id, yaml_text)
    try:
        yield session_id
    finally:
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
        headline.click()
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
    items = httpx.get(f"{base_url}/v1/sessions/{session_id}/items", timeout=10.0).json()
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
@pytest.mark.parametrize("variant", list(_VARIANTS))
def test_databricks_gemini_two_file_read_completes(
    page: Page,
    live_server: str,
    gateway_home: GatewayHome,
    tmp_path: Path,
    variant: str,
) -> None:
    """Both files are read once each and the assistant answers, on every gateway variant."""
    mock = gateway_home.mocks[variant]
    with _gemini_session(live_server, gateway_home, variant) as session_id:
        page.goto(f"{live_server}/c/{session_id}")
        _send(page, PROMPT)
        outcome = _wait_for_outcome(page, mock)
        page.wait_for_timeout(3_000)
        page.screenshot(path=str(tmp_path / f"{variant}-outcome.png"), full_page=True)
        mock.dump(str(tmp_path / f"{variant}-gateway-journal.json"))
        _dump_ui_state(
            page, live_server, session_id, outcome, tmp_path / f"{variant}-ui-state.json"
        )

    assert outcome.kind == "answer", (
        f"[{variant}] turn ended with {outcome.kind}: {outcome.detail}\n"
        f"gateway journal:\n{_evidence(mock)}"
    )
    assert mock.decisions() == [*FILES, "<answer>"], (
        f"[{variant}] model saw an unexpected call sequence: {mock.decisions()}"
    )
    paths = {entry["path"] for entry in mock.journal}
    assert paths == {"/ai-gateway/mlflow/v1/chat/completions"}, (
        f"[{variant}] requests reached surfaces that do not serve Gemini: {sorted(paths)}"
    )
    expect(page.locator(_WORKING)).to_have_count(0, timeout=30_000)
