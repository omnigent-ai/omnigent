"""Browser e2e: a new-worktree session survives a checkout slower than the
host's short metadata-command timeout.

A sleeping smudge filter makes a tiny repo's checkout outlast that short
bound, standing in for a very large monorepo.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest
import yaml
from playwright.sync_api import Page, Response, expect
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from omnigent.host.git_worktree import _GIT_TIMEOUT_S
from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BRANCH = "repro/large-repo-worktree"
_CHECKOUT_S = int(_GIT_TIMEOUT_S) + 15
_SETTLE_TIMEOUT_S = _CHECKOUT_S + 90
_AGENT_PREFERENCE = ("claude-native-ui", "codex-native-ui", "hello_world")
_SESSION_PATH = re.compile(r"/c/[0-9a-f]{32}$")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def _init_slow_checkout_repo(repo: Path, checkout_s: int) -> None:
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "e2e@example.com")
    _git(repo, "config", "user.name", "e2e")
    (repo / ".gitattributes").write_text("slow.bin filter=slow\n")
    (repo / "README.md").write_text("Stand-in for a very large repository.\n")
    pkg = repo / "src" / "pkg"
    pkg.mkdir(parents=True)
    for i in range(200):
        (pkg / f"mod_{i}.py").write_text(f"VALUE = {i}\n")
    (repo / "slow.bin").write_text("payload\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial commit")
    # The filter inherits git's stderr pipe; detach it so a killed git cannot hang its caller.
    _git(
        repo, "config", "filter.slow.smudge", f"sh -c 'exec 2>/dev/null; sleep {checkout_s}; cat'"
    )
    _git(repo, "config", "filter.slow.clean", "cat")
    _git(repo, "config", "filter.slow.required", "true")


def _spawn_host(
    home: Path, base_url: str, mock_llm_url: str
) -> tuple[subprocess.Popen[bytes], str, Path]:
    config = home / ".omnigent" / "config.yaml"
    if config.exists():
        host_id = yaml.safe_load(config.read_text())["host"]["host_id"]
    else:
        config.parent.mkdir(parents=True, exist_ok=True)
        host_id = uuid.uuid4().hex
        config.write_text(
            yaml.safe_dump({"host": {"host_id": host_id, "name": f"slow-checkout-{host_id[:8]}"}})
        )
    log = home / "host-daemon.log"
    env = {
        **os.environ,
        "HOME": str(home),
        "OMNIGENT_RUNNER_ZYGOTE": "0",
        "OMNIGENT_HOST_NO_OPEN": "1",
        "OPENAI_BASE_URL": f"{mock_llm_url}/v1",
        "OPENAI_API_KEY": "mock-key",
        "PYTHONPATH": os.pathsep.join(
            p for p in (str(_REPO_ROOT), os.environ.get("PYTHONPATH", "")) if p
        ),
        PROCESS_LOG_FILE_ENV_VAR: str(log),
    }
    with open(log, "wb") as fh:
        proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.host._daemon_entry", "--server", base_url],
            env=env,
            cwd=str(_REPO_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=fh,
        )
    return proc, host_id, log


def _wait_host_online(base_url: str, host_id: str, timeout: float = 90.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(f"{base_url}/v1/hosts", timeout=5.0)
            if resp.status_code == 200 and any(
                h["host_id"] == host_id and h["status"] == "online"
                for h in resp.json().get("hosts", [])
            ):
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise AssertionError(f"host {host_id} never came online at {base_url}")


def _pick_agent_id(base_url: str) -> str:
    payload = httpx.get(f"{base_url}/v1/agents", timeout=10.0).json()
    agents = payload.get("data", payload) if isinstance(payload, dict) else payload
    assert agents, f"no agents registered at {base_url}; cannot drive the journey"
    by_name = {a["name"]: a["id"] for a in agents}
    for name in _AGENT_PREFERENCE:
        if name in by_name:
            return by_name[name]
    return agents[0]["id"]


def _dismiss_first_run_dialog(page: Page) -> None:
    dialog = page.locator("[role=dialog]")
    try:
        dialog.first.wait_for(state="visible", timeout=5_000)
    except PlaywrightTimeoutError:
        return
    page.keyboard.press("Escape")
    expect(dialog).to_have_count(0)


def _choose_agent(page: Page, agent_id: str) -> None:
    trigger = page.get_by_test_id("new-chat-landing-agent-select")
    expect(trigger).to_be_enabled(timeout=60_000)
    trigger.click()
    option = page.get_by_test_id(f"new-chat-landing-agent-{agent_id}")
    expect(option).to_be_visible(timeout=60_000)
    if option.get_attribute("data-active") != "true":
        option.click()
    if trigger.get_attribute("aria-expanded") == "true":
        page.keyboard.press("Escape")
    expect(trigger).to_have_attribute("aria-expanded", "false")
    expect(page.locator("[data-radix-popper-content-wrapper]")).to_have_count(0)


def _choose_host(page: Page, host_id: str) -> None:
    page.get_by_test_id("new-chat-landing-host-chip").click()
    option = page.get_by_test_id(f"new-chat-landing-host-{host_id}")
    expect(option).to_be_visible(timeout=30_000)
    option.click()
    expect(page.get_by_test_id("new-chat-landing-host-menu")).to_be_hidden()


def _choose_directory(page: Page, directory: Path) -> None:
    page.get_by_test_id("new-chat-landing-workspace-chip").click()
    page.get_by_test_id("new-chat-landing-workspace-open-folder").click()
    expect(page.get_by_test_id("workspace-picker")).to_be_visible()
    crumbs = page.get_by_test_id("workspace-picker-breadcrumbs")
    crumbs.get_by_role("button").last.click()
    path_input = page.get_by_test_id("workspace-picker-path-input")
    path_input.fill(str(directory))
    path_input.press("Enter")
    expect(crumbs).to_contain_text(directory.name, timeout=30_000)
    page.get_by_test_id("workspace-picker-select").click()
    expect(page.get_by_test_id("workspace-picker")).to_be_hidden()
    expect(page.get_by_test_id("new-chat-landing-workspace-chip")).to_contain_text(directory.name)


def _request_new_worktree(page: Page, branch: str) -> None:
    chip = page.get_by_test_id("new-chat-landing-branch-chip")
    expect(chip).to_be_enabled(timeout=60_000)
    chip.click()
    branch_input = page.get_by_test_id("new-chat-landing-branch-input")
    expect(branch_input).to_be_visible()
    branch_input.fill(branch)
    page.keyboard.press("Escape")
    expect(branch_input).to_be_hidden()
    expect(chip).to_have_attribute("title", re.compile(re.escape(branch)))


def _composer(page: Page):
    composer = page.get_by_test_id("new-chat-landing-composer")
    expect(composer).to_be_visible()
    if composer.evaluate("el => el.tagName") == "TEXTAREA":
        return composer
    return composer.get_by_role("textbox").first


def _page_text(page: Page) -> str:
    try:
        return re.sub(r"\s+", " ", page.locator("body").inner_text())[:300]
    except Exception:
        return ""


@pytest.mark.timeout(900)
def test_new_session_worktree_survives_slow_checkout(
    request: pytest.FixtureRequest,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    # A prepared repo/host home can be supplied to replay the journey on a known environment.
    repo = Path(os.environ.get("OMNIGENT_E2E_SLOW_REPO", tmp_path / "large-repo"))
    if not repo.exists():
        _init_slow_checkout_repo(repo, _CHECKOUT_S)
    evidence = Path(os.environ.get("OMNIGENT_E2E_EVIDENCE_DIR", tmp_path / "evidence"))
    evidence.mkdir(parents=True, exist_ok=True)
    host_home = Path(os.environ.get("OMNIGENT_E2E_HOST_HOME", tmp_path / "host-home"))
    host, host_id, host_log = _spawn_host(host_home, live_server, mock_llm_server_url)
    try:
        _wait_host_online(live_server, host_id)
        agent_id = _pick_agent_id(live_server)

        page: Page = request.getfixturevalue("page")
        creates: list[Response] = []
        page.on(
            "response",
            lambda r: (
                creates.append(r)
                if r.request.method == "POST" and r.url.rstrip("/").endswith("/v1/sessions")
                else None
            ),
        )
        page.goto(live_server)
        expect(page.get_by_test_id("new-chat-landing")).to_be_visible(timeout=60_000)
        _dismiss_first_run_dialog(page)
        _choose_agent(page, agent_id)
        _choose_host(page, host_id)
        _choose_directory(page, repo)
        _request_new_worktree(page, _BRANCH)
        _composer(page).fill("Start work in a fresh worktree of this large repository.")
        submit = page.get_by_test_id("new-chat-landing-submit")
        expect(submit).to_be_enabled()
        page.screenshot(path=str(evidence / "before-start.png"))

        submit.click()
        started = time.monotonic()
        timeline: list[dict[str, object]] = []
        pending_shots = [10, 60, 110]
        while time.monotonic() - started < _SETTLE_TIMEOUT_S:
            elapsed = time.monotonic() - started
            sample: dict[str, object] = {
                "t": round(elapsed, 1),
                "path": urlparse(page.url).path,
                "landing": page.get_by_test_id("new-chat-landing").count() > 0,
                "toasts": page.locator("[data-sonner-toast]").all_inner_texts(),
                "landing_error": page.get_by_test_id("new-chat-landing-error").all_inner_texts(),
                "text": _page_text(page),
            }
            timeline.append(sample)
            if pending_shots and elapsed >= pending_shots[0]:
                page.screenshot(path=str(evidence / f"t{pending_shots.pop(0)}s.png"))
            if creates and (
                sample["toasts"]
                or sample["landing_error"]
                or _SESSION_PATH.search(str(sample["path"]))
            ):
                break
            page.wait_for_timeout(2_000)
        page.wait_for_timeout(1_000)
        page.screenshot(path=str(evidence / "settled.png"))

        create = creates[0] if creates else None
        try:
            create_body = create.text()[:1000] if create else None
        except Exception:
            # The body may be unretrievable (e.g. after navigation); don't let
            # evidence collection pre-empt the assertions below.
            create_body = "<body unavailable>"
        record = {
            "repo": str(repo),
            "branch": _BRANCH,
            "host_id": host_id,
            "checkout_s": _CHECKOUT_S,
            "host_git_timeout_s": _GIT_TIMEOUT_S,
            "create_status": create.status if create else None,
            "create_body": create_body,
            "settled_after_s": round(time.monotonic() - started, 1),
            "timeline": timeline,
            "worktree_list": _git(repo, "worktree", "list", "--porcelain"),
            "branches": _git(repo, "branch", "--list"),
            "host_log_tail": host_log.read_text(errors="replace")[-4000:],
        }
        (evidence / "journey.json").write_text(json.dumps(record, indent=2))

        assert create is not None, f"POST /v1/sessions did not settle within {_SETTLE_TIMEOUT_S}s"
        assert create.ok, (
            f"starting a session with a new worktree failed after {record['settled_after_s']}s: "
            f"HTTP {create.status} {create_body}; the page ended on "
            f"{timeline[-1]['path']} with toasts {timeline[-1]['toasts']}"
        )
        # Guard against a disabled slow filter silently passing the test: the
        # create only exercises the regression if its checkout outlasted the
        # short metadata bound.
        assert record["settled_after_s"] > _GIT_TIMEOUT_S, (
            f"create settled in {record['settled_after_s']}s, within the "
            f"{_GIT_TIMEOUT_S}s metadata bound; the slow checkout did not run"
        )
        expect(page).to_have_url(_SESSION_PATH, timeout=60_000)
        worktree = repo.parent / f"{repo.name}-worktrees" / _BRANCH.replace("/", "-")
        deadline = time.monotonic() + _CHECKOUT_S
        while not (worktree / "slow.bin").exists() and time.monotonic() < deadline:
            time.sleep(2)
        assert (worktree / "slow.bin").exists(), (
            f"worktree checkout did not materialize {worktree / 'slow.bin'} within {_CHECKOUT_S}s"
        )
        assert (worktree / "slow.bin").read_text() == "payload\n"
    finally:
        host.send_signal(signal.SIGTERM)
        try:
            host.wait(timeout=20)
        except subprocess.TimeoutExpired:
            host.kill()
