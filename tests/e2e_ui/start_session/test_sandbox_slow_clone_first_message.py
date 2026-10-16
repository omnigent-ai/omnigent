"""Browser e2e: a sandbox session's first message survives a repository clone that outlives
the rendezvous budget. Drives a ``localexec`` stand-in (the reported Lakebox provider is
internal) with a loopback git server that throttles one repository's clone."""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread

import httpx
import pytest
import yaml
from playwright.sync_api import Locator, Page, Response, expect
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from tests._helpers.compat import (
    COMPAT_SERVER_VERSION_ENV,
    apply_server_env,
    meets_min_server_version,
    server_executable,
)
from tests.e2e_ui.conftest import (
    _BUILD_OUTPUT,
    _REPO_ROOT,
    _TEST_AGENT_YAML,
    _find_free_port,
    set_fallback_mock_llm,
)

_FIXTURE_ROOT = _REPO_ROOT / "tests" / "e2e_ui" / "_fixtures" / "localexec_sandbox"
# The sandbox's git config rewrites this host onto the loopback git server, so
# the user types an ordinary https:// repository URL.
_MIRROR_HOST = "https://monorepo.test/"
_MONOREPO = "asana-monorepo"
_SMALL_REPO = "small-service"
_MONOREPO_PAYLOAD_MIB = 12
_CLONE_BYTES_PER_SECOND = 256 * 1024
# Lowered from the product's 240 s so the test stays short; the throttled clone still outlives it.
_RENDEZVOUS_BUDGET_S = int(os.environ.get("OMNIGENT_E2E_RENDEZVOUS_BUDGET_S", "20"))
# The per-stage rendezvous shipped in 0.18.0; older servers fail this module by design.
_MIN_SERVER_VERSION = "0.18.0"
_PROMPT = "Summarize this repository's layout."
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'


class _GitSmartHttpHandler(BaseHTTPRequestHandler):
    """Serve bare repositories via ``git http-backend``, throttling one of them."""

    project_root: Path
    throttled_repo: str
    bytes_per_second: int

    def log_message(self, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        self._serve(b"")

    def do_POST(self) -> None:
        self._serve(self.rfile.read(int(self.headers.get("Content-Length") or 0)))

    def _serve(self, body: bytes) -> None:
        path, _, query = self.path.partition("?")
        env = {
            "PATH": os.environ["PATH"],
            "GIT_PROJECT_ROOT": str(self.project_root),
            "GIT_HTTP_EXPORT_ALL": "1",
            "REQUEST_METHOD": self.command,
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "REMOTE_ADDR": self.client_address[0],
            "CONTENT_TYPE": self.headers.get("Content-Type", ""),
            "CONTENT_LENGTH": str(len(body)),
        }
        for header, variable in (
            ("Git-Protocol", "GIT_PROTOCOL"),
            ("Content-Encoding", "HTTP_CONTENT_ENCODING"),
        ):
            if value := self.headers.get(header):
                env[variable] = value
        completed = subprocess.run(
            ["git", "http-backend"], input=body, env=env, capture_output=True, check=False
        )
        if completed.returncode != 0:
            self.send_error(500, explain=completed.stderr.decode(errors="replace")[:500])
            return
        raw_headers, _, payload = completed.stdout.partition(b"\r\n\r\n")
        status = 200
        headers: list[tuple[str, str]] = []
        for line in raw_headers.decode().splitlines():
            name, _, value = line.partition(":")
            if name.lower() == "status":
                status = int(value.split()[0])
            else:
                headers.append((name, value.strip()))
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command == "POST" and path.startswith(f"/{self.throttled_repo}/"):
            self._write_throttled(payload)
        else:
            self.wfile.write(payload)

    def _write_throttled(self, payload: bytes) -> None:
        chunk = 16 * 1024
        started = time.monotonic()
        # A clone the sandbox gave up on closes the socket mid-transfer.
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            for offset in range(0, len(payload), chunk):
                self.wfile.write(payload[offset : offset + chunk])
                self.wfile.flush()
                delay = started + (offset + chunk) / self.bytes_per_second - time.monotonic()
                if delay > 0:
                    time.sleep(delay)


def _bare_repo(scratch: Path, name: str, *, payload_mib: int) -> None:
    work = scratch / f"{name}-work"
    work.mkdir()
    git = ["git", "-c", "user.name=E2E", "-c", "user.email=e2e@example.test"]
    subprocess.run([*git, "init", "-q", "-b", "main", str(work)], check=True)
    (work / "README.md").write_text(f"# {name}\n")
    for index in range(payload_mib):
        (work / f"blob-{index:02d}.bin").write_bytes(os.urandom(1024 * 1024))
    subprocess.run([*git, "-C", str(work), "add", "."], check=True)
    subprocess.run([*git, "-C", str(work), "commit", "-q", "-m", "initial"], check=True)
    subprocess.run(
        ["git", "clone", "-q", "--bare", str(work), str(scratch / "git" / f"{name}.git")],
        check=True,
    )


@dataclass(frozen=True)
class _Deployment:
    base_url: str
    sandbox_root: Path
    agent_id: str


def _wait_for_sandbox_deployment(
    server: subprocess.Popen[bytes], base_url: str, log: Path
) -> None:
    deadline = time.monotonic() + 90
    last_error = "not polled yet"
    while time.monotonic() < deadline:
        if server.poll() is not None:
            break
        try:
            info = httpx.get(f"{base_url}/v1/info", timeout=2)
            if info.status_code == 200 and info.json().get("sandbox_providers") == ["localexec"]:
                return
            last_error = f"/v1/info HTTP {info.status_code}: {info.text[:200]}"
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(0.5)
    raise RuntimeError(
        f"sandbox test server did not become ready on {base_url} ({last_error}).\n"
        f"{log.read_text()[-3000:]}"
    )


@pytest.fixture(scope="module")
def deployment(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[_Deployment]:
    """Spawn a server whose only execution target is the ``localexec`` sandbox."""
    # Gate here rather than with the marker: the marker's skip runs only after
    # this module-scoped server has been spawned.
    pinned = os.environ.get(COMPAT_SERVER_VERSION_ENV)
    if pinned and not meets_min_server_version(pinned, _MIN_SERVER_VERSION):
        pytest.skip(f"requires server >= {_MIN_SERVER_VERSION}; compat run pins {pinned}")
    scratch = tmp_path_factory.mktemp("monorepo_sandbox")
    (scratch / "git").mkdir()
    _bare_repo(scratch, _SMALL_REPO, payload_mib=0)
    _bare_repo(scratch, _MONOREPO, payload_mib=_MONOREPO_PAYLOAD_MIB)
    handler = type(
        "Handler",
        (_GitSmartHttpHandler,),
        {
            "project_root": scratch / "git",
            "throttled_repo": f"{_MONOREPO}.git",
            "bytes_per_second": _CLONE_BYTES_PER_SECOND,
        },
    )
    git_server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    Thread(target=git_server.serve_forever, daemon=True).start()
    gitconfig = scratch / "gitconfig"
    gitconfig.write_text(
        f'[url "http://127.0.0.1:{git_server.server_port}/"]\n\tinsteadOf = {_MIRROR_HOST}\n'
    )

    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    sandbox_root = scratch / "sandboxes"
    agent_yaml = scratch / "hello_world.yaml"
    agent_yaml.write_text(_TEST_AGENT_YAML)
    (scratch / "artifacts").mkdir()
    server_config = scratch / "server-config.yaml"
    server_config.write_text(
        yaml.safe_dump(
            {
                "sandbox": {
                    "provider": "localexec",
                    "server_url": base_url,
                    "localexec": {
                        "root": str(sandbox_root),
                        "env": {
                            "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
                            "OPENAI_API_KEY": "mock-key",
                            "OMNIGENT_HOST_NO_OPEN": "1",
                            "GIT_CONFIG_GLOBAL": str(gitconfig),
                            "GIT_TERMINAL_PROMPT": "0",
                            "PYTHONPATH": str(_REPO_ROOT),
                        },
                    },
                }
            }
        )
    )
    env = {
        **os.environ,
        "OMNIGENT_CONFIG": str(server_config),
        # Isolate the server's state under the test's scratch directory.
        "OMNIGENT_DATA_DIR": str(scratch / "data"),
        "OMNIGENT_WEB_UI_DIST": str(_BUILD_OUTPUT),
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "OPENAI_API_KEY": "mock-key",
        "ANTHROPIC_API_KEY": "",
    }
    apply_server_env(env, _REPO_ROOT)
    env["PYTHONPATH"] = os.pathsep.join(
        path for path in (str(_FIXTURE_ROOT), env.get("PYTHONPATH", "")) if path
    )
    server_log = scratch / "server.log"
    with open(server_log, "wb") as log:
        server = subprocess.Popen(
            [
                server_executable(),
                "-c",
                "import omnigent.server.managed_hosts as m; "
                # A bare read first: a renamed constant fails here instead of
                # silently leaving the default budget in place.
                "m.MANAGED_LAUNCH_RENDEZVOUS_TIMEOUT_S; "
                f"m.MANAGED_LAUNCH_RENDEZVOUS_TIMEOUT_S = {_RENDEZVOUS_BUDGET_S}; "
                "from omnigent.cli import main; main()",
                "server",
                "--config",
                str(server_config),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--database-uri",
                f"sqlite:///{scratch / 'test.db'}",
                "--artifact-location",
                str(scratch / "artifacts"),
                "--agent",
                str(agent_yaml),
            ],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    try:
        _wait_for_sandbox_deployment(server, base_url, server_log)
        set_fallback_mock_llm(
            mock_llm_server_url, "_policy_llm_", '{"action": "allow", "reason": ""}'
        )
        set_fallback_mock_llm(mock_llm_server_url, "gpt-4o-mini", "Mock LLM response.")
        agents = httpx.get(f"{base_url}/v1/agents", timeout=10).json()["data"]
        agent_id = next((a["id"] for a in agents if a["name"] == "hello_world"), None)
        assert agent_id is not None, (
            f"agent 'hello_world' not registered; agents: {agents}\n"
            f"{server_log.read_text()[-3000:]}"
        )
        yield _Deployment(base_url=base_url, sandbox_root=sandbox_root, agent_id=agent_id)
    finally:
        server.send_signal(signal.SIGTERM)
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)
        for pgid_file in sandbox_root.glob("*/.omnigent-host.pgid"):
            with contextlib.suppress(ProcessLookupError, PermissionError, ValueError):
                os.killpg(int(pgid_file.read_text()), signal.SIGKILL)
        git_server.shutdown()
        git_server.server_close()


def _select_agent(page: Page, agent_id: str) -> None:
    """Pick the agent behind the picker's custom-agents submenu before switching to the
    sandbox host, whose model-catalog polling re-renders the menu and detaches a submenu
    hover mid-open."""
    picker = page.get_by_test_id("new-chat-landing-agent-select")
    picker.click()
    expect(page.get_by_role("menu").first).to_be_visible()
    row = page.get_by_test_id(f"new-chat-landing-agent-{agent_id}")
    if row.count() == 0:
        submenu = page.get_by_test_id("new-chat-landing-custom-agents")
        expect(submenu).to_be_visible()
        for _ in range(20):
            with contextlib.suppress(PlaywrightTimeoutError):
                submenu.hover(timeout=1000)
            if row.count():
                break
            page.wait_for_timeout(250)
    expect(row).to_be_visible()
    row.click()
    expect(picker).to_have_attribute("aria-expanded", "false")


def _select_sandbox_repo(page: Page, repo: str) -> None:
    page.get_by_test_id("new-chat-landing-host-chip").click()
    page.get_by_test_id("new-chat-landing-sandbox-option").click()
    repo_chip = page.get_by_test_id("new-chat-landing-repo-chip")
    repo_chip.click()
    repo_input = page.get_by_test_id("new-chat-landing-repo-input")
    expect(repo_input).to_be_visible()
    repo_input.fill(f"{_MIRROR_HOST}{repo}.git")
    repo_input.press("Enter")
    # The chip label is the durable signal that the repo was added; the popover
    # may already have closed on its own by the time its row could be checked.
    expect(repo_chip).to_have_attribute("aria-label", f"Sandbox repositories: {repo}")
    if repo_input.is_visible():
        page.keyboard.press("Escape")
    expect(repo_input).to_have_count(0)


@dataclass(frozen=True)
class _StartedSession:
    session_id: str
    sent_at: float
    failed_message_posts: list[str]


@dataclass(frozen=True)
class _LaunchOutcome:
    since_send_s: float
    indicator: str
    error_pill: str | None


def _watch_failed_message_posts(page: Page) -> list[str]:
    failures: list[str] = []

    def record(response: Response) -> None:
        request = response.request
        if request.method == "POST" and "/events" in response.url and response.status >= 400:
            try:
                body = response.text()
            except Exception:  # the body may be gone once the page navigates
                body = "<body unavailable>"
            failures.append(f"HTTP {response.status} {response.url}: {body[:300]}")

    page.on("response", record)
    return failures


def _start_sandbox_session(page: Page, base_url: str, repo: str, agent_id: str) -> _StartedSession:
    failed_message_posts = _watch_failed_message_posts(page)
    page.goto(f"{base_url}/")
    prompt = page.get_by_test_id("new-chat-landing-input")
    expect(prompt).to_be_visible(timeout=30_000)
    _select_agent(page, agent_id)
    _select_sandbox_repo(page, repo)
    prompt.fill(_PROMPT)
    page.get_by_test_id("new-chat-landing-submit").click()
    sent_at = time.monotonic()
    expect(page).to_have_url(re.compile(r"/c/[0-9a-f]{32}$"), timeout=30_000)
    return _StartedSession(
        session_id=page.url.rsplit("/", 1)[-1],
        sent_at=sent_at,
        failed_message_posts=failed_message_posts,
    )


def _wait_for_launch_outcome(
    page: Page, launching: Locator, started: _StartedSession, output_path: Path
) -> _LaunchOutcome:
    """Poll until the launch indicator clears or an error pill appears, whichever is first."""
    error_pill = page.get_by_test_id("error-pill")
    while True:
        indicator = "".join(launching.all_inner_texts()[:1])
        pills = error_pill.all_inner_texts()
        since_send_s = time.monotonic() - started.sent_at
        if pills:
            output_path.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(output_path / "error-pill-appeared.png"))
            return _LaunchOutcome(since_send_s, indicator, pills[0])
        if not indicator:
            return _LaunchOutcome(since_send_s, indicator, None)
        assert since_send_s < 180, f"launch indicator still shows {indicator!r} after 180s"
        page.wait_for_timeout(500)


def test_first_message_survives_slow_monorepo_clone(
    page: Page, deployment: _Deployment, output_path: str
) -> None:
    """A large-repo sandbox session must not fail its first message while cloning: the
    throttled clone outlives the lowered rendezvous budget, and the parked message must
    wait it out instead of surfacing a runner-unavailable error pill."""
    started = _start_sandbox_session(page, deployment.base_url, _MONOREPO, deployment.agent_id)
    launching = page.get_by_test_id("runner-starting-indicator")
    expect(launching).to_contain_text("Cloning repository", timeout=60_000)

    outcome = _wait_for_launch_outcome(page, launching, started, Path(output_path))
    # The launch itself completes either way; the parked message must have ridden it out.
    expect(launching).to_have_count(0, timeout=180_000)
    assert outcome.error_pill is None, (
        f"session {started.session_id}: error pill {outcome.error_pill!r} appeared "
        f"{outcome.since_send_s:.0f}s after send while the launch indicator read "
        f"{outcome.indicator!r} (rendezvous budget {_RENDEZVOUS_BUDGET_S}s); "
        f"failed message POSTs: {started.failed_message_posts}"
    )
    expect(page.get_by_test_id("error-pill")).to_have_count(0)
    expect(page.locator(_ASSISTANT)).to_have_count(1)
    # Without this the test could pass vacuously if the throttle stopped biting.
    assert outcome.since_send_s > _RENDEZVOUS_BUDGET_S, (
        f"the clone finished {outcome.since_send_s:.0f}s after send, inside the "
        f"{_RENDEZVOUS_BUDGET_S}s rendezvous budget, so the regression was not exercised"
    )


def test_small_repository_first_message_is_answered(page: Page, deployment: _Deployment) -> None:
    """Control: the rig answers a fast clone, so a monorepo failure is not a fixture failure."""
    _start_sandbox_session(page, deployment.base_url, _SMALL_REPO, deployment.agent_id)
    expect(page.locator(_ASSISTANT)).to_have_count(1, timeout=180_000)
    expect(page.get_by_test_id("error-pill")).to_have_count(0)
