"""E2E: harness readiness on a credential-less host.

Journey from the bug report: self-host ``omnigent host`` from an environment
whose image ships the harness CLIs but has **no** credentials configured, then
open the new-session picker against that host. The daemon's readiness map
(``configured_harnesses`` in its hello frame) drives the picker's warnings.

Unlike the stubbed availability tests in this directory, these tests register a
REAL ``omnigent host`` daemon (isolated ``HOME``, no provider keys or CLI
logins) against the live e2e server, so the daemon's actual readiness map — not
a hand-written wire body — is what the picker renders.

* ``test_sdk_harness_readiness_reflects_missing_credentials`` is the
  regression test for the headline facet: the daemon used to report the
  in-process SDK harnesses (``claude-sdk`` / ``openai-agents``) as ready no
  matter what, so the picker showed no warning and the failure only surfaced
  when the first turn died with an auth error. It fails whenever readiness for
  SDK harnesses claims ``true`` on a host with no resolvable credential.
* ``test_cli_harness_readiness_warns_without_credentials`` guards the already
  fixed facets of the same report on the real daemon path: ``pi`` reports
  ``needs-auth`` (not available) when its binary is present but no credential
  is configured, the picker warns for it, and ``claude-native`` is a readiness
  entry distinct from ``claude-sdk``.
* ``test_claude_sdk_expired_login_first_turn_fails_without_warning_or_fix``
  drives the expired-login variant: the host's only Claude credential is a
  claude.ai login whose refresh Claude Code can no longer complete. Readiness
  and the picker warning are recorded as observations; the protected behavior
  is the first turn failing with an error that names the login and how to fix
  it, not Claude Code's raw text under a generic error pill.

SDK-harness ``needs-auth`` is advisory: the daemon cannot see agent-level
credentials (``executor.auth``) and never blocks an SDK launch, so the picker
keeps the row selectable and warns via the under-composer notice. The SDK test
therefore drives the normal picker path (click the row, observe the notice,
launch). CLI-backed harnesses (pi) keep #7882's blocking treatment — their row
is disabled with a warning badge — so the pi test seeds the persisted last pick
(``omnigent:last-agent-id``) to keep the launch journey drivable.

The async-in-a-fresh-thread shape is inherited from
``start_session/test_start_session.py``: once a pytest-playwright sync test has
run in the session, pytest-asyncio can't start a loop on the main thread.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright, expect

from omnigent.inner.claude_sdk_executor import CLAUDE_AUTH_FAILED_CODE, CLAUDE_AUTH_FAILED_TITLE
from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR
from tests._helpers.async_thread import run_in_fresh_loop as _run_in_fresh_loop
from tests._helpers.browser import async_user_page
from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable
from tests._helpers.host import isolated_host
from tests._helpers.live_server import isolated_process_env, terminate_process
from tests.e2e_ui.chat._claude_oauth_refresh_proxy import (
    REFRESH_PATH,
    ClaudeOAuthRefreshRejectingProxy,
)
from tests.e2e_ui.conftest import _register_agent_yaml

_REPO_ROOT = Path(__file__).resolve().parents[3]

_HOST_ONLINE_TIMEOUT_S = 180.0
_FIRST_TURN_ERROR_TIMEOUT_MS = 240_000


def _fetch_host_row(base_url: str, host_name: str) -> dict[str, Any] | None:
    hosts = httpx.get(f"{base_url}/v1/hosts", timeout=10.0).json().get("hosts", [])
    return next(
        (h for h in hosts if h.get("name") == host_name and h.get("status") == "online"),
        None,
    )


@pytest.fixture(scope="module")
def credentialless_host(
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[dict[str, Any]]:
    """A real ``omnigent host`` daemon with harness CLIs on PATH but no credentials.

    Spawns the daemon with an isolated ``HOME`` (so no ``config.yaml``
    providers, no ``~/.claude`` / ``~/.codex`` / ``~/.pi`` logins) and an
    environment stripped of every ambient API key, then waits for the server's
    REST surface to report it online. Yields the host's ``/v1/hosts`` row plus
    a ``workspace`` directory sessions on it can use.
    """
    tmp = tmp_path_factory.mktemp("credentialless_host_home")
    workspace = tmp / "workspace"
    workspace.mkdir()
    # An explicit base replaces ambient credentials while retaining installed CLIs.
    base_env = {
        "PATH": os.environ["PATH"],
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }
    with (
        httpx.Client(base_url=live_server, trust_env=False) as client,
        isolated_host(
            tmp,
            live_server,
            base_env=base_env,
            name=f"credless-{uuid.uuid4().hex[:8]}",
            command=[
                runner_executable(),
                "-m",
                "omnigent",
                "host",
                "--server",
                live_server,
                "--non-interactive",
            ],
        ) as host,
    ):
        row = host.wait_online(client, timeout=_HOST_ONLINE_TIMEOUT_S, poll_interval=1)
        yield {**row, "workspace": str(workspace)}


def _register_harness_agent(base_url: str, name: str, harness: str, model: str) -> str:
    agent_id = _register_agent_yaml(
        base_url,
        (
            "spec_version: 1\n"
            f"name: {name}\n"
            "prompt: You are a test agent.\n"
            "executor:\n"
            "  config:\n"
            f"    model: {model}\n"
            f"    harness: {harness}\n"
        ),
    )
    assert agent_id is not None, f"agent {name} failed to register"
    return agent_id


async def _reveal_agent_row(page: Any, agent_id: str) -> Any:
    """Open the landing picker's agent menu and reveal *agent_id*'s row.

    Returns the row locator without clicking it: whether the row is enabled
    (advisory SDK ``needs-auth``) or disabled with a warning badge (blocking
    CLI ``needs-auth``, #7882) is itself part of what the tests observe.
    """
    picker = page.get_by_test_id("new-chat-landing-agent-select")
    await picker.click()
    await expect(page.get_by_role("menu").first).to_be_visible()
    row = page.get_by_test_id(f"new-chat-landing-agent-{agent_id}")
    if await row.count() == 0:
        # Session-registered agents fold into the "Other..." flyout.
        custom = page.get_by_test_id("new-chat-landing-custom-agents")
        if await custom.count() > 0:
            await custom.hover()
            with contextlib.suppress(Exception):
                await row.wait_for(state="visible", timeout=5_000)
    if await row.count() == 0:
        more = page.get_by_test_id("new-chat-landing-harness-more")
        if await more.count() > 0:
            await more.click()
    await row.wait_for(state="visible", timeout=10_000)
    return row


async def _dismiss_agent_menu(page: Any) -> None:
    """Close the landing picker's agent menu (and any open flyout)."""
    picker = page.get_by_test_id("new-chat-landing-agent-select")
    await page.keyboard.press("Escape")
    if await picker.get_attribute("aria-expanded") == "true":
        await page.keyboard.press("Escape")


async def _seed_last_agent(page: Any, agent_id: str) -> None:
    """Persist *agent_id* as the landing picker's last pick before page load.

    Used by the CLI (pi) journey: #7882's picker *disables* a not-ready CLI
    agent's row instead of merely warning about it, so that test cannot click
    the row to select the agent. Seeding the persisted last pick selects it
    the same way a returning user lands on their previous agent, keeping the
    launch journey (which warns but does not block) drivable. The SDK journey
    does NOT seed: SDK ``needs-auth`` is advisory, so the row stays clickable
    and the test exercises the normal picker path.
    """
    await page.add_init_script(
        f'window.localStorage.setItem("omnigent:last-agent-id", {json.dumps(agent_id)})'
    )


async def _seed_recent_workspace(page: Any, host_id: str, workspace: str) -> None:
    await page.add_init_script(
        "window.localStorage.setItem("
        '"omnigent:recent-workspaces", '
        f"JSON.stringify({json.dumps({host_id: [workspace]})}))"
    )


def test_sdk_harness_readiness_reflects_missing_credentials(
    live_server: str,
    credentialless_host: dict[str, Any],
) -> None:
    """A credential-less host must not present claude-sdk as ready with no warning.

    Reproduces the reported journey end to end: the picker offers the Claude
    SDK agent on the credential-less host without any readiness warning, the
    session launches, and the first turn dies with an auth error the user was
    never warned about. Passes once the host's readiness map (and therefore
    the picker) reflects that no credential is resolvable for the SDK harness.
    """
    _run_in_fresh_loop(_drive_sdk_readiness(live_server, credentialless_host))


async def _drive_sdk_readiness(base_url: str, host: dict[str, Any]) -> None:
    agent_id = _register_harness_agent(
        base_url, f"credless-claude-sdk-{uuid.uuid4().hex[:6]}", "claude-sdk", "claude-sonnet-4-5"
    )
    warned_before_launch = False
    first_turn_error: str | None = None
    session_id: str | None = None

    async with async_playwright() as pw:
        try:
            async with (
                await pw.chromium.launch() as browser,
                async_user_page(browser, headers={}) as page,
            ):
                await _seed_recent_workspace(page, host["host_id"], host["workspace"])
                await page.goto(f"{base_url}/")
                await page.get_by_test_id("new-chat-landing-input").wait_for(
                    state="visible", timeout=30_000
                )

                # SDK needs-auth is advisory: agent-level credentials are invisible
                # to the daemon. The row stays selectable without a persisted pick.
                row = await _reveal_agent_row(page, agent_id)
                assert await row.is_enabled(), (
                    "the needs-auth SDK agent's picker row must stay selectable "
                    "(advisory warning), not disabled"
                )
                await row.click()
                await _dismiss_agent_menu(page)

                # The selected SDK harness must show an advisory readiness warning
                # before launch, even though the picker row remains selectable.
                warning = page.get_by_test_id("new-chat-landing-harness-warning")
                await expect(warning).to_be_visible(timeout=15_000)
                warned_before_launch = True

                # Launch anyway and record the first-turn error as journey evidence;
                # the readiness warning itself is the regression assertion.
                await page.get_by_test_id("new-chat-landing-input").fill(
                    "hello from the readiness reproduction"
                )
                await page.get_by_test_id("new-chat-landing-submit").click()
                await page.wait_for_url(re.compile(r"/c/[0-9a-f]+"), timeout=60_000)
                match = re.search(r"/c/([0-9a-f]+)", page.url)
                session_id = match.group(1) if match else None
                error_pill = page.locator('[data-testid="error-pill"]').first
                try:
                    await error_pill.wait_for(
                        state="visible", timeout=_FIRST_TURN_ERROR_TIMEOUT_MS
                    )
                    first_turn_error = (await error_pill.inner_text()).strip()
                except PlaywrightTimeoutError:
                    first_turn_error = None
                if session_id is not None:
                    items = httpx.get(
                        f"{base_url}/v1/sessions/{session_id}/items", timeout=10.0
                    ).json()
                    for item in items.get("data", []):
                        if item.get("type") == "error":
                            first_turn_error = item.get("message") or first_turn_error
        finally:
            if session_id is not None:
                with contextlib.suppress(Exception):
                    httpx.delete(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)

    row = _fetch_host_row(base_url, host["name"])
    assert row is not None, "credential-less host dropped offline mid-test"
    availability = (row.get("configured_harnesses") or {}).get("claude-sdk")
    assert availability == "needs-auth", (
        "host with no credentials must report claude-sdk as 'needs-auth' "
        f"(configured_harnesses['claude-sdk'] == {availability!r}); "
        f"the launched session's first turn then failed with: {first_turn_error!r}"
    )
    assert warned_before_launch, (
        "picker showed no readiness warning for a claude-sdk agent on a "
        f"credential-less host; the first turn then failed with: {first_turn_error!r}"
    )


def test_cli_harness_readiness_warns_without_credentials(
    live_server: str,
    credentialless_host: dict[str, Any],
) -> None:
    """Installed-but-credential-less pi reports needs-auth and the picker warns.

    Guards the already-landed facets of the same report: pi's readiness is no
    longer binary presence (its ``pi`` CLI is on PATH here, yet the host
    reports ``needs-auth``), the picker surfaces that as a warning, and
    ``claude-native`` / ``claude-sdk`` are independent readiness entries.
    """
    if shutil.which("pi") is None:
        pytest.skip("pi CLI not installed on this machine")
    _run_in_fresh_loop(_drive_pi_readiness(live_server, credentialless_host))


async def _drive_pi_readiness(base_url: str, host: dict[str, Any]) -> None:
    agent_id = _register_harness_agent(
        base_url, f"credless-pi-{uuid.uuid4().hex[:6]}", "pi", "claude-sonnet-4-5"
    )

    row = _fetch_host_row(base_url, host["name"])
    assert row is not None, "credential-less host dropped offline mid-test"
    harnesses = row.get("configured_harnesses") or {}
    assert harnesses.get("pi") == "needs-auth", (
        "pi CLI is installed with no credential, so the host must report "
        f"needs-auth, got {harnesses.get('pi')!r}"
    )
    assert "claude-native" in harnesses and "claude-sdk" in harnesses, (
        "claude-native and claude-sdk must be distinct readiness entries, got "
        f"{sorted(harnesses)!r}"
    )

    async with async_playwright() as pw:
        async with (
            await pw.chromium.launch() as browser,
            async_user_page(browser, headers={}) as page,
        ):
            await _seed_recent_workspace(page, host["host_id"], host["workspace"])
            await _seed_last_agent(page, agent_id)
            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )
            # needs-auth renders the pi row disabled with a warning badge
            # (#7882); the seeded last pick keeps the agent selected so the
            # under-composer notice can render and name the host.
            row = await _reveal_agent_row(page, agent_id)
            if await row.is_enabled():
                await row.click()
            else:
                badge = page.get_by_test_id(f"new-chat-landing-agent-warning-{agent_id}")
                await expect(badge).to_be_visible()
            await _dismiss_agent_menu(page)
            warning = page.get_by_test_id("new-chat-landing-harness-warning")
            await expect(warning).to_be_visible(timeout=30_000)
            await expect(warning).to_contain_text(host["name"])


# ── Expired Claude Code login (OAuth refresh fails) ───────────────────────────
# The login still carries a refresh token, so readiness counts it as usable,
# while a loopback proxy rejects Claude Code's refresh at turn time.

_CLAUDE_SDK_FIRST_MESSAGE = "Can you read this message and reply?"
_CODEX_CONTROL_MESSAGE = "Reply with the single word pong."
_CODEX_CONTROL_MODEL = "gpt-4o"
_CLAUDE_LOGIN_COMMAND = "`claude auth login`"


def _write_expired_claude_login(home: Path) -> None:
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    creds = home / ".claude" / ".credentials.json"
    creds.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "expired-access-token-placeholder",
                    "refreshToken": "stale-refresh-token-placeholder",
                    "expiresAt": int((time.time() - 30 * 86400) * 1000),
                    "scopes": ["user:inference", "user:profile"],
                    "subscriptionType": "max",
                }
            }
        )
    )
    creds.chmod(0o600)


@dataclass
class _ExpiredLoginHost:
    """A real host daemon with an expired Claude login; restartable with the same HOME."""

    root: Path
    base_url: str
    host_id: str
    name: str
    workspace: Path
    proxy: ClaudeOAuthRefreshRejectingProxy
    proc: subprocess.Popen[bytes] | None = None
    readiness_seen: list[dict[str, Any]] = field(default_factory=list)

    @property
    def home(self) -> Path:
        return self.root / "home"

    @property
    def log_path(self) -> Path:
        return self.root / "host.log"

    def start(self) -> dict[str, Any]:
        assert self.proc is None or self.proc.poll() is not None
        base_env = {
            "PATH": os.environ["PATH"],
            "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
        }
        env = isolated_process_env(self.home, {}, base_env)
        env.update(
            OMNIGENT_CONFIG_HOME=str(self.home / ".omnigent"),
            OMNIGENT_HOST_ID=self.host_id,
            OMNIGENT_HOST_NAME=self.name,
            OMNIGENT_RUNNER_ENV_PASSTHROUGH="HTTPS_PROXY,NO_PROXY,NODE_EXTRA_CA_CERTS",
            **self.proxy.env(),
        )
        env[PROCESS_LOG_FILE_ENV_VAR] = str(self.log_path)
        with self.log_path.open("ab") as log:
            self.proc = subprocess.Popen(
                [
                    runner_executable(),
                    "-m",
                    "omnigent",
                    "host",
                    "--server",
                    self.base_url,
                    "--non-interactive",
                ],
                env=apply_runner_env(env),
                cwd=compat_runner_cwd(),
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        deadline = time.monotonic() + _HOST_ONLINE_TIMEOUT_S
        while time.monotonic() < deadline and self.proc.poll() is None:
            row = _fetch_host_row(self.base_url, self.name)
            if row is not None and row.get("host_id") == self.host_id:
                return row
            time.sleep(1)
        raise AssertionError(
            f"host {self.name} never came online (exit={self.proc.poll()})\n"
            f"{self.log_path.read_text(errors='replace')[-4000:]}"
        )

    def stop(self) -> None:
        terminate_process(self.proc)

    def restart(self) -> dict[str, Any]:
        self.stop()
        # The server can keep reporting the old connection online for a moment.
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and _fetch_host_row(self.base_url, self.name):
            time.sleep(0.5)
        if _fetch_host_row(self.base_url, self.name):
            raise AssertionError(f"host {self.name} still reported online after stop()")
        return self.start()


@pytest.fixture(scope="module")
def expired_claude_login_host(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[_ExpiredLoginHost]:
    """A real ``omnigent host`` whose only Claude credential is an expired claude.ai login.

    Codex is served by an openai-family provider aimed at the mock model server.
    ``isolated_host`` owns ``config.yaml`` as identity-only, so the daemon is spawned here.
    """
    root = tmp_path_factory.mktemp("expired_claude_login_host")
    proxy = ClaudeOAuthRefreshRejectingProxy(root / "proxy")
    host = _ExpiredLoginHost(
        root=root,
        base_url=live_server,
        host_id=uuid.uuid4().hex,
        name=f"expired-claude-{uuid.uuid4().hex[:8]}",
        workspace=root / "workspace",
        proxy=proxy,
    )
    try:
        host.workspace.mkdir()
        (host.home / ".omnigent").mkdir(parents=True)
        (host.home / ".omnigent" / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    "host": {"host_id": host.host_id, "name": host.name},
                    "providers": {
                        "codex-mock": {
                            "kind": "key",
                            "default": ["openai"],
                            "openai": {
                                "base_url": f"{mock_llm_server_url}/v1",
                                "api_key": "mock-key",
                                "wire_api": "responses",
                                "models": {"default": _CODEX_CONTROL_MODEL},
                            },
                        }
                    },
                }
            )
        )
        _write_expired_claude_login(host.home)
        host.start()
        yield host
    finally:
        host.stop()
        proxy.close()
        if record_dir := os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
            for src in (host.log_path, proxy.log_path):
                if src.exists():
                    shutil.copy(src, Path(record_dir) / src.name)


async def _start_session_from_landing(
    page: Any, base_url: str, host: _ExpiredLoginHost, agent_id: str, message: str
) -> tuple[str, str | None]:
    """Pick *agent_id* on the landing page, send *message*; return (session_id, warning text)."""
    await _seed_recent_workspace(page, host.host_id, str(host.workspace))
    await page.goto(f"{base_url}/")
    await page.get_by_test_id("new-chat-landing-input").wait_for(state="visible", timeout=30_000)
    host_row = await asyncio.to_thread(_fetch_host_row, base_url, host.name) or {}
    host.readiness_seen.append(dict(host_row.get("configured_harnesses") or {}))
    row = await _reveal_agent_row(page, agent_id)
    assert await row.is_enabled(), "the agent's picker row must stay selectable"
    await row.click()
    await _dismiss_agent_menu(page)
    warning = page.get_by_test_id("new-chat-landing-harness-warning")
    warned: str | None = None
    with contextlib.suppress(PlaywrightTimeoutError):
        await warning.wait_for(state="visible", timeout=5_000)
        warned = (await warning.inner_text()).strip()
    await page.get_by_test_id("new-chat-landing-input").fill(message)
    await page.get_by_test_id("new-chat-landing-submit").click()
    await page.wait_for_url(re.compile(r"/c/[0-9a-f]+"), timeout=60_000)
    match = re.search(r"/c/([0-9a-f]+)", page.url)
    assert match is not None, page.url
    return match.group(1), warned


def _stored_error(base_url: str, session_id: str) -> dict[str, Any] | None:
    items = httpx.get(f"{base_url}/v1/sessions/{session_id}/items", timeout=10.0).json()
    for item in items.get("data", []):
        if item.get("type") == "error":
            return {k: item.get(k) for k in ("message", "code", "source", "remediation", "cause")}
    return None


async def _await_first_turn(page: Any, base_url: str, session_id: str) -> dict[str, Any]:
    """Wait for the turn to settle; return what the user sees plus the stored error.

    A failing turn can stream error text as assistant content before the pill
    lands, so a reply counts only when no error follows it.
    """
    assistant = page.locator('[data-testid="message-bubble"][data-role="assistant"]').first
    error_pill = page.locator('[data-testid="error-pill"]').first
    outcome: dict[str, Any] = {
        "reply": None,
        "headline": None,
        "message": None,
        "stored_error": None,
    }
    deadline = time.monotonic() + _FIRST_TURN_ERROR_TIMEOUT_MS / 1000
    settle_by: float | None = None
    while time.monotonic() < deadline:
        if await error_pill.count():
            try:
                await error_pill.click(timeout=5_000)
            except PlaywrightError:
                # The pill can re-render while the turn is still streaming; poll again.
                await page.wait_for_timeout(500)
                continue
            headline = page.get_by_test_id("error-headline").first
            body = page.get_by_test_id("error-message-content").first
            await expect(headline).to_be_visible(timeout=10_000)
            outcome["headline"] = (await headline.inner_text()).strip()
            with contextlib.suppress(PlaywrightTimeoutError):
                await body.wait_for(state="visible", timeout=10_000)
                outcome["message"] = (await body.inner_text()).strip()
            break
        if settle_by is None and await assistant.count():
            text = (await assistant.inner_text()).strip()
            if text:
                outcome["reply"] = text
                settle_by = time.monotonic() + 10
        if settle_by is not None and time.monotonic() > settle_by:
            break
        await page.wait_for_timeout(500)
    outcome["stored_error"] = await asyncio.to_thread(_stored_error, base_url, session_id)
    if outcome["stored_error"] is not None or outcome["headline"] is not None:
        outcome["reply"] = None
    return outcome


def test_claude_sdk_expired_login_first_turn_fails_without_warning_or_fix(
    live_server: str,
    mock_llm_server_url: str,
    expired_claude_login_host: _ExpiredLoginHost,
) -> None:
    """An expired Claude login must fail the first turn with an error that names the fix.

    The proxy must have rejected a refresh, so a passing turn means the fault
    never reached Claude Code. Codex on the same host is the control; the
    session after a host restart must carry the same classified error. The
    picker's advisory ``needs-auth`` warning and the host's readiness map are
    recorded, not required: readiness is advisory by design and its reading of
    the expired login varies between runs.
    """
    outcomes: dict[str, Any] = {}
    try:
        _run_in_fresh_loop(
            _drive_expired_login(
                live_server, mock_llm_server_url, expired_claude_login_host, outcomes
            )
        )
    finally:
        for session_id in outcomes.get("session_ids", []):
            with contextlib.suppress(Exception):
                httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
    requests = expired_claude_login_host.proxy.snapshot()
    assert any(r["path"].startswith(REFRESH_PATH) and r["status"] == "400" for r in requests), (
        f"the proxy never rejected a Claude Code token refresh: {requests!r}"
    )
    assert outcomes["codex"]["reply"], (
        f"Codex control on the same host did not answer: {outcomes['codex']!r}"
    )
    for leg in ("claude_sdk", "claude_sdk_after_restart"):
        outcome = outcomes[leg]
        assert outcome["reply"] is None, f"{leg}: answered despite the expired login: {outcome!r}"
        stored = outcome["stored_error"] or {}
        assert stored.get("code") == CLAUDE_AUTH_FAILED_CODE, f"{leg}: {outcome!r}"
        assert _CLAUDE_LOGIN_COMMAND in (stored.get("remediation") or ""), f"{leg}: {outcome!r}"
        assert outcome["headline"] == CLAUDE_AUTH_FAILED_TITLE, f"{leg}: {outcome!r}"
        shown = outcome["message"] or ""
        assert "Try this:" in shown and _CLAUDE_LOGIN_COMMAND in shown, f"{leg}: {outcome!r}"


async def _drive_expired_login(
    base_url: str, mock_url: str, host: _ExpiredLoginHost, outcomes: dict[str, Any]
) -> None:
    from tests.e2e_ui.conftest import configure_mock_llm, set_fallback_mock_llm

    sdk_agent = _register_harness_agent(
        base_url, f"expired-claude-sdk-{uuid.uuid4().hex[:6]}", "claude-sdk", "claude-sonnet-4-5"
    )
    codex_agent = _register_harness_agent(
        base_url, f"expired-codex-{uuid.uuid4().hex[:6]}", "codex", _CODEX_CONTROL_MODEL
    )
    set_fallback_mock_llm(mock_url, _CODEX_CONTROL_MODEL, "pong")
    configure_mock_llm(mock_url, [{"text": "pong"}] * 3, key=_CODEX_CONTROL_MODEL)
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    context_options: dict[str, Any] = {}
    if record_dir:
        context_options = {
            "record_video_dir": record_dir,
            "record_video_size": {"width": 1280, "height": 800},
            "viewport": {"width": 1280, "height": 800},
        }
    session_ids: list[str] = outcomes.setdefault("session_ids", [])
    marks: list[tuple[str, float]] = []
    async with async_playwright() as pw:
        async with (
            await pw.chromium.launch() as browser,
            async_user_page(browser, headers={}, **context_options) as page,
        ):
            t0 = time.monotonic()

            def mark(label: str) -> None:
                marks.append((label, round(time.monotonic() - t0, 1)))

            mark("claude-sdk: landing")
            sid, warned = await _start_session_from_landing(
                page, base_url, host, sdk_agent, _CLAUDE_SDK_FIRST_MESSAGE
            )
            session_ids.append(sid)
            mark("claude-sdk: message sent")
            outcomes["claude_sdk"] = {
                "session_id": sid,
                "warned": warned,
                **(await _await_first_turn(page, base_url, sid)),
            }
            mark("claude-sdk: turn settled")
            if record_dir:
                await page.wait_for_timeout(4_000)
                await page.screenshot(path=str(Path(record_dir) / "claude-sdk-first-turn.png"))

            mark("codex: landing")
            sid, warned = await _start_session_from_landing(
                page, base_url, host, codex_agent, _CODEX_CONTROL_MESSAGE
            )
            session_ids.append(sid)
            mark("codex: message sent")
            outcomes["codex"] = {
                "session_id": sid,
                "warned": warned,
                **(await _await_first_turn(page, base_url, sid)),
            }
            mark("codex: turn settled")
            if record_dir:
                await page.wait_for_timeout(3_000)

            mark("host restart")
            await asyncio.to_thread(host.restart)
            mark("claude-sdk after restart: landing")
            sid, warned = await _start_session_from_landing(
                page, base_url, host, sdk_agent, _CLAUDE_SDK_FIRST_MESSAGE
            )
            session_ids.append(sid)
            mark("claude-sdk after restart: message sent")
            outcomes["claude_sdk_after_restart"] = {
                "session_id": sid,
                "warned": warned,
                **(await _await_first_turn(page, base_url, sid)),
            }
            mark("claude-sdk after restart: turn settled")
            if record_dir:
                await page.wait_for_timeout(4_000)
                outcomes["video"] = str(await page.video.path()) if page.video else None
    outcomes["marks"] = marks
    outcomes["host_readiness_at_each_landing"] = [
        {k: v for k, v in seen.items() if k in ("claude-sdk", "claude-native", "codex")}
        for seen in host.readiness_seen
    ]
    if record_dir:
        (Path(record_dir) / "outcomes.json").write_text(json.dumps(outcomes, indent=2))
