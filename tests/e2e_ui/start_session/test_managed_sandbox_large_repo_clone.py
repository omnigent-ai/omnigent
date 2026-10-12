"""Browser journey: a New Chat on a managed sandbox whose repository has a large history.

A real server, the production managed-launch pipeline and the real ``omnigent host`` run
against a storage-bounded local sandbox (see ``_local_exec_sandbox_server``); the remotes
are generated repositories behind ``https://`` aliases. No browser, API or SSE traffic is mocked.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from playwright.async_api import Page, async_playwright, expect

from tests._helpers.async_thread import run_in_fresh_loop
from tests.e2e_ui.conftest import _find_free_port, configure_mock_llm, set_fallback_mock_llm
from tests.e2e_ui.start_session._local_exec_sandbox_server import COMMANDS_LOG
from tests.e2e_ui.start_session._managed_replica_server import terminate

_REPO_ROOT = Path(__file__).resolve().parents[3]
_WEB_UI_INDEX = _REPO_ROOT / "omnigent" / "server" / "static" / "web-ui" / "index.html"

# The sandbox can hold the small repository, the monorepo's current tree and the
# host's own files, but not the monorepo's complete history.
SANDBOX_FILE_SIZE_KIB = 12 * 1024
MONOREPO_REVISIONS = 8
MONOREPO_REVISION_BYTES = 4 * 1024 * 1024
MONOREPO_SMALL_FILES = 1500

MONOREPO_URL = "https://monorepo.test/asana/monorepo.git"
SMALL_REPO_URL = "https://monorepo.test/asana/small-repo.git"
AGENT_NAME = "sandbox_probe"
LAUNCH_TIMEOUT_S = 240


@dataclass(frozen=True)
class BoundedSandboxServer:
    url: str
    root: Path
    evidence: Path

    @property
    def commands(self) -> list[tuple[str, str]]:
        log = self.root / "sandboxes" / COMMANDS_LOG
        if not log.exists():
            return []
        rows = [line.split("\t", 1) for line in log.read_text().splitlines()]
        return [(sandbox_id, command) for sandbox_id, command in rows]


def _git(*args: str, cwd: Path) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="Fixture",
        GIT_AUTHOR_EMAIL="fixture@example.test",
        GIT_COMMITTER_NAME="Fixture",
        GIT_COMMITTER_EMAIL="fixture@example.test",
    )
    return subprocess.run(
        ["git", *args], cwd=cwd, env=env, text=True, capture_output=True, check=True
    ).stdout.strip()


def make_bare_remote(bare: Path, *, revisions: int, revision_bytes: int, small_files: int) -> Path:
    """Create a bare repository whose history holds *revisions* incompressible blobs."""
    bare = bare.resolve()
    work = bare.with_suffix(".work")
    work.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=work)
    for index in range(small_files):
        path = work / "services" / f"svc_{index % 40:02d}" / f"module_{index:04d}.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"VALUE = {index}\n")
    for revision in range(revisions):
        (work / "data.bin").write_bytes(os.urandom(revision_bytes))
        _git("add", "-A", cwd=work)
        _git("commit", "-q", "-m", f"revision {revision}", cwd=work)
    _git("clone", "-q", "--bare", str(work), str(bare), cwd=work.parent)
    _git("config", "uploadpack.allowFilter", "true", cwd=bare)
    shutil.rmtree(work)
    return bare


def write_remote_aliases(path: Path, aliases: dict[str, Path]) -> Path:
    path.write_text(
        "".join(f'[url "{bare.as_uri()}"]\n\tinsteadOf = {url}\n' for url, bare in aliases.items())
    )
    return path


@pytest.fixture
def bounded_sandbox_server(
    tmp_path: Path, mock_llm_server_url: str
) -> Iterator[BoundedSandboxServer]:
    """Generate the remotes and start the product server on the bounded local sandbox."""
    if not _WEB_UI_INDEX.is_file():
        pytest.skip("web SPA bundle is not built")
    remotes = tmp_path / "remotes"
    aliases = {
        MONOREPO_URL: make_bare_remote(
            remotes / "monorepo.git",
            revisions=MONOREPO_REVISIONS,
            revision_bytes=MONOREPO_REVISION_BYTES,
            small_files=MONOREPO_SMALL_FILES,
        ),
        SMALL_REPO_URL: make_bare_remote(
            remotes / "small-repo.git", revisions=1, revision_bytes=4096, small_files=5
        ),
    }
    git_config = write_remote_aliases(tmp_path / "sandbox-gitconfig", aliases)
    config = tmp_path / "config"
    config.mkdir()
    agent_yaml = tmp_path / f"{AGENT_NAME}.yaml"
    agent_yaml.write_text(
        f"name: {AGENT_NAME}\nprompt: Answer the user's question in one sentence.\n"
        "executor:\n  model: gpt-4o-mini\n  harness: openai-agents\n"
        "os_env:\n  type: caller_process\n  cwd: .\n  sandbox:\n    type: none\n"
    )
    port = _find_free_port()
    url = f"http://127.0.0.1:{port}"
    root = tmp_path / "server"
    root.mkdir()
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    env = {
        **{
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("OMNIGENT_RUNNER_", "OMNIGENT_HOST_"))
        },
        "PYTHONPATH": str(_REPO_ROOT),
        "OMNIGENT_CONFIG_HOME": str(config),
        "OMNIGENT_CONFIG": "",
        "OMNIGENT_BUILTIN_AGENT_DIRS": str(agent_yaml),
        "OMNIGENT_DATA_DIR": str(tmp_path / "data"),
        "OMNIGENT_SKIP_ONBOARD": "1",
        "OMNIGENT_LOCAL_SINGLE_USER": "1",
        "OMNIGENT_INTERNAL_WS_ORIGIN": "",
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "OPENAI_API_KEY": "mock-key",
        "ANTHROPIC_API_KEY": "",
    }
    set_fallback_mock_llm(mock_llm_server_url, "_policy_llm_", '{"action": "allow", "reason": ""}')
    log_path = evidence / "server.log"
    with log_path.open("w") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "tests.e2e_ui.start_session._local_exec_sandbox_server",
                "--port",
                str(port),
                "--root",
                str(root),
                "--file-size-kib",
                str(SANDBOX_FILE_SIZE_KIB),
                "--git-config",
                str(git_config),
            ],
            cwd=_REPO_ROOT,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    try:
        with httpx.Client(timeout=1, trust_env=False) as client:
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                assert process.poll() is None, log_path.read_text()[-4000:]
                with contextlib.suppress(httpx.HTTPError):
                    if client.get(f"{url}/health").status_code == 200:
                        break
                time.sleep(0.1)
            else:
                pytest.fail(f"server did not start:\n{log_path.read_text()[-4000:]}")
        yield BoundedSandboxServer(url, root, evidence)
    finally:
        with contextlib.suppress(httpx.HTTPError):
            httpx.post(f"{url}/__test/hosts/stop", timeout=20, trust_env=False)
        terminate(process)


async def _create_sandbox_session(
    page: Page, rig: BoundedSandboxServer, repo_url: str, prompt: str
) -> str:
    """Drive New Chat: agent, sandbox host, repository, first message. Returns the session id."""
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        agents = (await client.get(f"{rig.url}/v1/agents")).json()["data"]
    agent_id = next(agent["id"] for agent in agents if agent["name"] == AGENT_NAME)
    repo_name = repo_url.rsplit("/", 1)[-1].removesuffix(".git")

    await page.goto(rig.url)
    await expect(page.get_by_test_id("new-chat-landing-input")).to_be_visible(timeout=30_000)
    await page.get_by_test_id("new-chat-landing-agent-select").click()
    await page.get_by_test_id(f"new-chat-landing-agent-{agent_id}").click()
    host_chip = page.get_by_test_id("new-chat-landing-host-chip")
    await host_chip.click()
    sandbox_option = page.get_by_test_id("new-chat-landing-sandbox-option")
    await expect(sandbox_option).to_be_visible(timeout=15_000)
    await sandbox_option.click()
    await expect(host_chip).to_have_attribute("aria-label", re.compile(r"^Host: .*Sandbox"))
    await expect(page.get_by_test_id("new-chat-landing-host-menu")).to_be_hidden()

    controls = page.get_by_test_id("new-chat-landing-workspace-controls")
    repository = controls.get_by_test_id("new-chat-landing-repo-chip")
    await repository.click()
    repo_input = page.get_by_test_id("new-chat-landing-repo-input")
    await repo_input.fill(repo_url)
    await repo_input.press("Enter")
    await expect(repository).to_have_attribute("aria-label", f"Sandbox repositories: {repo_name}")
    await page.keyboard.press("Escape")
    await page.screenshot(path=str(rig.evidence / f"{repo_name}-new-chat.png"))

    await page.get_by_test_id("new-chat-landing-input").fill(prompt)
    await expect(page.get_by_test_id("new-chat-landing-submit")).to_be_enabled()
    await page.get_by_test_id("new-chat-landing-submit").click()
    await expect(page).to_have_url(re.compile(r"/c/[a-f0-9]+"), timeout=30_000)
    return page.url.rsplit("/", 1)[-1]


async def _await_launch_outcome(
    page: Page, rig: BoundedSandboxServer, session_id: str, reply: str
) -> tuple[str, list[str]]:
    """Watch the launch band until the sandbox fails or the agent replies."""
    tag = session_id[:8]
    failed = page.get_by_test_id("sandbox-failed-indicator")
    starting = page.get_by_test_id("runner-starting-indicator")
    assistant = page.locator('[data-testid="message-bubble"][data-role="assistant"]').filter(
        has_text=reply
    )
    stages: list[str] = []
    deadline = time.monotonic() + LAUNCH_TIMEOUT_S
    async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
        while time.monotonic() < deadline:
            if await starting.count():
                label = (await starting.first.inner_text()).strip()
                if label and (not stages or stages[-1] != label):
                    stages.append(label)
                    await page.screenshot(path=str(rig.evidence / f"{tag}-{len(stages)}.png"))
            if await failed.count() and await failed.first.is_visible():
                await expect(failed.first).to_contain_text("Sandbox launch failed")
                await page.screenshot(path=str(rig.evidence / f"{tag}-failed.png"))
                snapshot = (await client.get(f"{rig.url}/v1/sessions/{session_id}")).json()
                (rig.evidence / f"{tag}-session.json").write_text(json.dumps(snapshot, indent=2))
                # Hold the terminal state so a recording of the journey stays readable.
                await asyncio.sleep(3)
                return (await failed.first.inner_text()).strip(), stages
            if await assistant.count() and await assistant.first.is_visible():
                await page.screenshot(path=str(rig.evidence / f"{tag}-reply.png"))
                await asyncio.sleep(3)
                return "", stages
            await asyncio.sleep(0.5)
    await page.screenshot(path=str(rig.evidence / f"{tag}-timeout.png"))
    raise AssertionError(
        f"sandbox launch neither failed nor produced a reply within {LAUNCH_TIMEOUT_S}s; "
        f"stages seen: {stages}"
    )


async def _drive(rig: BoundedSandboxServer, repo_url: str, prompt: str, reply: str) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        context = await browser.new_context(viewport={"width": 1024, "height": 680})
        page = await context.new_page()
        try:
            session_id = await _create_sandbox_session(page, rig, repo_url, prompt)
            failure, stages = await _await_launch_outcome(page, rig, session_id, reply)
            clones = [command for _, command in rig.commands if command.startswith("git clone")]
            (rig.evidence / "clone-commands.txt").write_text("\n".join(clones) + "\n")
            (rig.evidence / f"{session_id[:8]}-stages.txt").write_text(
                "\n".join([*stages, f"failure: {failure or '-'}"]) + "\n"
            )
            assert clones, "the managed spin-up never ran git clone"
            assert failure == "", (
                f"sandbox launch for {repo_url} failed after stages {stages}: {failure}\n"
                f"clone command: {clones[-1]}"
            )
            await expect(page.get_by_test_id("runner-starting-indicator")).to_have_count(0)
        finally:
            await context.close()
            await browser.close()


@pytest.mark.timeout(600)
def test_managed_sandbox_launch_clones_small_repository(
    bounded_sandbox_server: BoundedSandboxServer, mock_llm_server_url: str
) -> None:
    prompt = "Confirm the small repository sandbox is ready."
    reply = "The small repository sandbox is ready."
    configure_mock_llm(
        mock_llm_server_url, [{"text": reply}], key="small-repo-sandbox", match=prompt
    )
    run_in_fresh_loop(_drive(bounded_sandbox_server, SMALL_REPO_URL, prompt, reply))


@pytest.mark.timeout(600)
def test_managed_sandbox_launch_clones_large_monorepo(
    bounded_sandbox_server: BoundedSandboxServer, mock_llm_server_url: str
) -> None:
    prompt = "Confirm the monorepo sandbox is ready."
    reply = "The monorepo sandbox is ready."
    configure_mock_llm(
        mock_llm_server_url, [{"text": reply}], key="monorepo-sandbox", match=prompt
    )
    run_in_fresh_loop(_drive(bounded_sandbox_server, MONOREPO_URL, prompt, reply))
