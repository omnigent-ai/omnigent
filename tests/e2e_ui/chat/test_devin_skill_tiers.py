"""E2E: a Devin session must offer the skills Devin itself loads.

Devin runs on the generic ACP harness (the builtin ``devin`` row spawning
``devin acp``). Devin loads user-invocable skills from its own directories
(workspace ``.devin/skills``, user ``~/.config/devin/skills``, plus
``.claude/skills`` / ``.agents/skills`` for compatibility) and offers them as
slash commands. Omnigent's composer menu resolves a session's skills through
the generic host walk, which only knows the ``.claude`` / ``.agents`` tiers —
so a skill living where Devin keeps it never appears in the menu, and typing
its slash command sends plaintext instead of the skill's instructions.

This drives both halves of that journey against a hermetic ``devin`` CLI stub
(serving both ``devin acp`` and ``devin skills list --json --trigger user``)
on a dedicated runner:

* the composer ``/`` menu must list the ``.devin/skills`` skill alongside the
  ``.claude/skills`` compat one;
* sending the skill's slash command must deliver the SKILL.md body to the
  agent — the stub's reply says "delivered" only when the ``session/prompt``
  text contains the body marker.
"""

from __future__ import annotations

import contextlib
import gzip
import io
import json
import os
import signal
import subprocess
import sys
import tarfile
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _HEALTH_POLL_INTERVAL_S,
    _HEALTH_TIMEOUT_S,
    _REPO_ROOT,
    _server_state,
)

_DEVIN_SKILL_NAME = "grill-me"
_COMPAT_SKILL_NAME = "compat-skill"
_SKILL_BODY_MARKER = "SKILL-BODY-DELIVERY-MARKER"
_DELIVERED_TEXT = "SKILL INSTRUCTIONS DELIVERED: the agent received the skill body"
_MISSING_PREFIX = "SKILL INSTRUCTIONS MISSING"

# The ``devin`` CLI stub: ``devin acp`` speaks the Agent Client Protocol over
# stdio and probes each prompt for the skill body marker; ``devin skills list``
# reports the seeded skills so a discovery fix that shells out to the CLI (the
# way the Devin terminal sources its own menu) finds the same catalog the
# filesystem tiers carry. Stdlib only.
_DEVIN_STUB_TEMPLATE = r"""#!{python}
import sys, json

SKILLS = {skills_json}
MARKER = "SKILL-BODY-DELIVERY-MARKER"


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


if len(sys.argv) > 1 and sys.argv[1] == "skills":
    print(json.dumps(SKILLS))
    raise SystemExit(0)

if len(sys.argv) > 1 and sys.argv[1] == "acp":
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        mid, method = msg.get("id"), msg.get("method")
        if method == "initialize":
            send({{"jsonrpc": "2.0", "id": mid, "result": {{
                "protocolVersion": 1,
                "agentCapabilities": {{"promptCapabilities": {{"image": False}}}},
            }}}})
        elif method == "session/new":
            send({{"jsonrpc": "2.0", "id": mid,
                  "result": {{"sessionId": "devin-stub-session-1"}}}})
        elif method == "session/prompt":
            sid = msg["params"]["sessionId"]
            text = "\n".join(
                b.get("text", "") for b in msg["params"].get("prompt", [])
                if isinstance(b, dict) and b.get("type") == "text"
            )
            if MARKER in text:
                reply = "SKILL INSTRUCTIONS DELIVERED: the agent received the skill body"
            else:
                tail = text[-200:].replace("\n", " ")
                reply = "SKILL INSTRUCTIONS MISSING: agent saw only: ..." + tail
            send({{"jsonrpc": "2.0", "method": "session/update",
                  "params": {{"sessionId": sid, "update": {{
                      "sessionUpdate": "agent_message_chunk",
                      "content": {{"type": "text", "text": reply}}}}}}}})
            send({{"jsonrpc": "2.0", "id": mid,
                  "result": {{"stopReason": "end_turn"}}}})
    raise SystemExit(0)

print("devin stub 0.0.1")
"""

_DEVIN_AGENT_YAML = """\
name: devin-skills-repro
prompt: You are a skills-delivery probe agent.

executor:
  harness: devin
"""


def _skill_md(name: str, description: str) -> str:
    return (
        f"---\nname: {name}\ndescription: {description}\n---\n\n"
        f"# {name}\n\nWhen this skill is invoked, interrogate the plan ruthlessly.\n\n"
        f"{_SKILL_BODY_MARKER}\n"
    )


def _bundle() -> bytes:
    """Gzipped tarball of the builtin-Devin agent spec."""
    buf = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w") as tar,
    ):
        data = _DEVIN_AGENT_YAML.encode()
        info = tarfile.TarInfo(name="devin-skills-repro.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _runner_pids() -> list[int]:
    result = subprocess.run(
        ["pgrep", "-f", "omnigent.runner._entry"],
        capture_output=True,
        text=True,
    )
    return [int(pid) for pid in result.stdout.split() if pid.strip()]


def _runner_online(base_url: str, runner_id: str) -> bool:
    try:
        resp = httpx.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
    except httpx.HTTPError:
        return False
    return resp.status_code == 200 and resp.json().get("online") is True


def _respawn_runner_with_devin_stub(
    base_url: str,
    stub_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> subprocess.Popen[bytes]:
    """Replace the shared runner with one that can see the ``devin`` stub.

    The stub must be visible to the RUNNER process (skill discovery and the
    ``devin acp`` spawn both happen there), and a runner's environment is
    fixed at spawn — so the shared runner is stopped and an identically bound
    one is spawned with the stub's dir on PATH and ``OMNIGENT_DEVIN_PATH``
    pinned. Later tests recover via ``_ensure_runner_online``.
    """
    runner_id = str(_server_state["runner_id"])
    binding_token = str(_server_state["binding_token"])
    mock_url = str(_server_state.get("mock_llm_url", ""))

    for pid in _runner_pids():
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + _HEALTH_TIMEOUT_S
    while time.monotonic() < deadline and _runner_online(base_url, runner_id):
        time.sleep(_HEALTH_POLL_INTERVAL_S)
    for pid in _runner_pids():
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)

    runner_tmp = tmp_path_factory.mktemp("devin_stub_runner")
    log_path = runner_tmp / "runner.log"
    env = {
        **os.environ,
        "PATH": f"{stub_path.parent}{os.pathsep}{os.environ.get('PATH', '')}",
        "OMNIGENT_DEVIN_PATH": str(stub_path),
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": base_url,
        **(
            {"OPENAI_BASE_URL": f"{mock_url}/v1", "OPENAI_API_KEY": "mock-key"} if mock_url else {}
        ),
    }
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
                f"devin-stub runner exited early (code {proc.returncode}); "
                f"log:\n{log_path.read_text()[-3000:]}"
            )
        if _runner_online(base_url, runner_id):
            _server_state["runner_pid"] = proc.pid
            return proc
        time.sleep(_HEALTH_POLL_INTERVAL_S)
    proc.terminate()
    raise RuntimeError(f"devin-stub runner did not register within {_HEALTH_TIMEOUT_S:.0f}s")


@pytest.fixture
def devin_session(
    live_server: str,
    runner_id: str,
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """A builtin-Devin session in a workspace with both skill tiers seeded."""
    workspace = tmp_path / "workspace"
    devin_dir = workspace / ".devin" / "skills" / _DEVIN_SKILL_NAME
    devin_dir.mkdir(parents=True)
    (devin_dir / "SKILL.md").write_text(_skill_md(_DEVIN_SKILL_NAME, "Devin-tier repro skill."))
    compat_dir = workspace / ".claude" / "skills" / _COMPAT_SKILL_NAME
    compat_dir.mkdir(parents=True)
    (compat_dir / "SKILL.md").write_text(
        _skill_md(_COMPAT_SKILL_NAME, "Claude-compat-tier repro skill.")
    )

    stub_dir = tmp_path / "devin-stub-bin"
    stub_dir.mkdir()
    stub_path = stub_dir / "devin"
    stub_path.write_text(
        _DEVIN_STUB_TEMPLATE.format(
            python=sys.executable,
            skills_json=json.dumps(
                [
                    {
                        "name": _DEVIN_SKILL_NAME,
                        "description": "Devin-tier repro skill.",
                        "path": str(devin_dir / "SKILL.md"),
                    },
                    {
                        "name": _COMPAT_SKILL_NAME,
                        "description": "Claude-compat-tier repro skill.",
                        "path": str(compat_dir / "SKILL.md"),
                    },
                ]
            ),
        )
    )
    stub_path.chmod(0o755)

    runner_proc = _respawn_runner_with_devin_stub(live_server, stub_path, tmp_path_factory)

    create_resp = httpx.post(
        f"{live_server}/v1/sessions",
        data={"metadata": json.dumps({"workspace": str(workspace)})},
        files={"bundle": ("agent.tar.gz", _bundle(), "application/gzip")},
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]

    try:
        patch_resp = httpx.patch(
            f"{live_server}/v1/sessions/{session_id}",
            json={"runner_id": runner_id},
            timeout=10.0,
        )
        patch_resp.raise_for_status()
        yield (live_server, session_id)
    finally:
        try:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        finally:
            runner_proc.terminate()
            try:
                runner_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                runner_proc.kill()
                runner_proc.wait(timeout=5)


def test_devin_menu_lists_devins_own_skill_tier(
    page: Page,
    devin_session: tuple[str, str],
) -> None:
    """The composer ``/`` menu must list a skill living in ``.devin/skills``.

    The ``.claude/skills`` compat skill is the control: it shows via the
    generic host walk, proving discovery ran — while the skill in Devin's own
    tier is the one the walk never finds (the bug this test pins).
    """
    base_url, session_id = devin_session
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)

    composer.fill("/")
    expect(page.get_by_test_id(f"slash-menu-item-{_COMPAT_SKILL_NAME}")).to_be_visible(
        timeout=30_000
    )
    expect(page.get_by_test_id(f"slash-menu-item-{_DEVIN_SKILL_NAME}")).to_be_visible(
        timeout=10_000
    )
    # Hold the settled menu on screen so a recording ends on the outcome.
    page.wait_for_timeout(1_500)


def test_devin_skill_invocation_delivers_instructions(
    page: Page,
    devin_session: tuple[str, str],
) -> None:
    """Sending the Devin-tier skill's slash command must deliver its body.

    While the skill is undiscovered the composer treats ``/grill-me`` as
    plaintext, so the agent receives the literal command and no instructions —
    the stub then replies with the MISSING probe text this test rejects.
    """
    base_url, session_id = devin_session
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)

    composer.fill(f"/{_DEVIN_SKILL_NAME} review this plan")
    composer.press("Enter")

    delivered = page.get_by_text(_DELIVERED_TEXT)
    missing = page.get_by_text(_MISSING_PREFIX)
    error_pill = page.locator('[data-testid="error-pill"][data-level="error"]')

    expect(delivered.or_(missing.first).or_(error_pill.first).first).to_be_visible(timeout=90_000)
    # Hold the settled turn on screen so a recording ends on the outcome.
    page.wait_for_timeout(1_500)

    if error_pill.count() > 0:
        error_pill.first.click()
        detail = page.get_by_test_id("error-message-content").first
        expect(detail).to_be_visible(timeout=5_000)
        pytest.fail(f"skill invocation failed with an error: {detail.inner_text()}")
    if missing.count() > 0:
        pytest.fail(f"skill instructions never reached the agent: {missing.first.inner_text()}")

    expect(delivered).to_be_visible()
