"""E2E: a present-but-crashing Claude Code CLI must not be misdiagnosed as "missing".

Reported failure: a claude-native session's terminal launches the real Claude
Code CLI, the CLI boots and runs (it self-updates, prints its
``Resume this session with:`` banner), then a stray ``--model`` token reaches
the shell (``zsh:2: command not found: --model``) and the pane dies with exit
status 127. The runner classifies *every* terminal that exits 127 as a missing
binary, so the web session fails with a card that reads::

    Agent command not found
    The host couldn't find the agent's CLI on its PATH, so the terminal exited
    before the session could start.
    Try this: Install the harness on the host (e.g. run `omnigent setup`).

That diagnosis is wrong: the captured pane output proves the CLI was present and
running. Telling the user to install a harness they already have sends them down
the wrong path. The honest diagnosis for a 127 exit whose captured output does
not name the launched command as missing is the generic terminal-exit message.

What this test drives
---------------------
The rig launches the session's Claude Code terminal through a stub
(``OMNIGENT_CLAUDE_PATH``, the documented harness-command override) that stands
in for a present CLI crashing late: it prints boot output and the reported
resume banner (so ``capture-pane`` frames prove the binary was present and
running), prints the reported ``zsh:2: command not found: --model`` line, then
exits 127. So the terminal is a *present* executable that runs and then dies 127
with captured output naming ``--model`` — not ``claude`` — as the unknown token,
exactly the reported signal.

The journey (the reported one): open a fresh Claude Code (claude-native)
session, whose required terminal auto-launches on bind, and read the failure
card the web UI shows once that terminal dies.

* Buggy build (``exit_code == 127`` alone means "missing binary"): the card
  reads "Agent command not found" and tells the user to run ``omnigent setup``,
  contradicting the pane output that proves the CLI ran. This test FAILS.
* Fixed build (a 127 exit whose output does not name the launched command as
  missing is not a missing-binary): the card reads the honest generic
  "The agent's terminal exited unexpectedly…" and never suggests installing the
  harness. This test PASSES.

The runner logs the full, untruncated failure message to its process log (its
persisted card copy is truncated for preview), so the terminal's captured pane
output — the proof the CLI was present and ran — is asserted from that log; the
user-visible misdiagnosis is asserted from the card itself.

The rig mirrors ``test_claude_native_slow_ready_first_prompt``: a dedicated
server + runner pair (own ``HOME`` / ``OMNIGENT_CONFIG_HOME``) so the stub
binary and temp config cannot leak into other tests. No real ``claude`` and no
live model are needed — the stub crashes at startup, before contacting a model.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import sysconfig
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _create_native_claude_session

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Boot budget for the spawned server + runner pair.
_HEALTH_TIMEOUT_S = 60.0
# claude-native auto-launch of the (crashing) terminal + the failure reaching
# the SPA: bind → stub boots and dies 127 → session.status failed → card.
_CARD_TIMEOUT_MS = 120_000

_ERROR_PILL = '[data-testid="error-pill"]'

# Model baked into the rig's mock anthropic provider config (matches
# conftest._CLAUDE_MOCK_MODEL); the stub never actually contacts it.
_CLAUDE_MOCK_MODEL = "claude-sonnet-4-20250514"

# The reported crash: the CLI runs (prints its resume banner) then a stray arg
# hits the shell and the pane dies 127. The stray line names ``--model`` — NOT
# ``claude`` — as the unknown command, so a correct diagnosis cannot read it as
# a missing harness.
_CRASH_EXIT_CODE = 127
_RESUME_MARKER = "Resume this session with:"
_STRAY_ARG_LINE = "zsh:2: command not found: --model"

# The misdiagnosis this test rejects, and the honest diagnosis it requires.
_MISDIAGNOSIS_HEADLINE = "Agent command not found"
_MISDIAGNOSIS_REMEDIATION = "Install the harness"
_HONEST_HEADLINE = "The agent's terminal exited unexpectedly"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# Proxy-blind client: CI forces an egress proxy via HTTP(S)_PROXY env vars that
# must not intercept loopback requests to the spawned server.
_client = httpx.Client(trust_env=False)

for _var in ("NO_PROXY", "no_proxy"):
    os.environ[_var] = ",".join(filter(None, [os.environ.get(_var, ""), "127.0.0.1,localhost"]))


def _no_proxy_env() -> dict[str, str]:
    """Ambient env with loopback excluded from any forced HTTP(S) proxy."""
    env = os.environ.copy()
    for var in ("NO_PROXY", "no_proxy"):
        existing = env.get(var, "")
        env[var] = ",".join(filter(None, [existing, "127.0.0.1,localhost"]))
    return env


@pytest.fixture
def browser_context_args(browser_context_args: dict[str, Any]) -> dict[str, Any]:
    """Record the sync ``page`` journey to ``OMNIGENT_E2E_RECORD_DIR`` when set.

    The conftest ``_record_video`` fixture only patches the async Browser; the
    pytest-playwright sync ``page`` fixture this test uses builds its context
    from ``browser_context_args``, so recording is enabled by injecting
    ``record_video_dir`` here. Playwright finalizes the ``.webm`` on context
    close, regardless of test outcome. No-op when the env var is unset.
    """
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    if not record_dir:
        return browser_context_args
    Path(record_dir).mkdir(parents=True, exist_ok=True)
    return {**browser_context_args, "record_video_dir": record_dir}


def _write_crashing_claude_stub(bin_dir: Path) -> Path:
    """Write a ``claude`` executable that runs, then dies with exit 127.

    Stands in for the reported present-but-crashing Claude Code CLI: it prints
    boot output and the CLI's own ``Resume this session with:`` banner (so the
    captured pane proves the binary was present and running), prints the
    reported stray ``--model`` shell error, then exits 127. It never contacts a
    model — the crash is at startup.

    :param bin_dir: Directory to write the stub into.
    :returns: The absolute path of the stub executable, named ``claude``.
    """
    stub = bin_dir / "claude"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        "# Rig: a present Claude Code CLI that boots, runs, then crashes 127.\n"
        'echo "Claude Code (native session)"\n'
        "sleep 0.3\n"
        'echo "· Checking for updates…"\n'
        "sleep 0.3\n"
        'echo "· Loading MCP servers…"\n'
        "sleep 0.3\n"
        f'echo "{_RESUME_MARKER} claude --resume $(date +%s)"\n'
        "sleep 0.3\n"
        # A stray argument reaches the shell after the CLI hands back control;
        # it names ``--model`` (not ``claude``) as the unknown command.
        f'echo "{_STRAY_ARG_LINE}"\n'
        f"exit {_CRASH_EXIT_CODE}\n"
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return stub


def _rig_python(work: Path) -> str:
    """Build an interpreter whose isolated mode can import this checkout.

    The claude-native launch spawns helper subprocesses as
    ``<runner python> -I …``; ``-I`` drops ``PYTHONPATH``, so when this checkout
    is importable only via ``PYTHONPATH`` (the CI worktree layout) those helpers
    would die with ``ModuleNotFoundError``. A dedicated venv whose
    ``site-packages`` carries a ``.pth`` naming the checkout (plus the parent
    environment's site-packages for dependencies) survives ``-I``.

    :param work: The rig's scratch directory.
    :returns: Absolute path of the rig venv's ``python``.
    """
    venv_dir = work / "rig-venv"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(venv_dir)],
        check=True,
        capture_output=True,
    )
    site_packages = next((venv_dir / "lib").glob("python*/site-packages"))
    parent_purelib = sysconfig.get_paths()["purelib"]
    # The sibling SDK packages resolve to their real source roots: an editable
    # install in the parent venv is a ``.pth`` finder hook, and site only runs
    # ``.pth`` hooks from real site dirs.
    roots = [str(_REPO_ROOT)]
    for pkg in ("omnigent_client", "omnigent_ui_sdk"):
        spec = importlib.util.find_spec(pkg)
        if spec is not None and spec.origin:
            root = str(Path(spec.origin).resolve().parents[1])
            if root not in roots:
                roots.append(root)
    (site_packages / "omnigent_rig.pth").write_text("\n".join([*roots, parent_purelib]) + "\n")
    return str(venv_dir / "bin" / "python")


def _await_log_contains(log_path: Path, needles: list[str], *, timeout_s: float = 30.0) -> str:
    """Poll *log_path* until it holds every needle, then return its text.

    :param log_path: File the runner streams its logs to.
    :param needles: Substrings that must all be present.
    :param timeout_s: How long to keep polling before returning what is there.
    :returns: The log text read on the final poll.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        text = log_path.read_text(errors="replace") if log_path.exists() else ""
        if all(needle in text for needle in needles) or time.monotonic() >= deadline:
            return text
        time.sleep(0.5)


@pytest.mark.timeout(30)
def test_crashing_claude_stub_exits_127_after_running(tmp_path: Path) -> None:
    """The fault-injection stub must present as a CLI that ran, then died 127."""
    stub = _write_crashing_claude_stub(tmp_path)
    result = subprocess.run([str(stub)], capture_output=True, text=True)
    assert result.returncode == _CRASH_EXIT_CODE
    assert _RESUME_MARKER in result.stdout, result.stdout
    assert _STRAY_ARG_LINE in result.stdout, result.stdout


@pytest.fixture
def crashing_claude_session(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str, Path]]:
    """A claude-native session whose present CLI crashes with exit 127 at boot.

    Spawns a dedicated server + runner whose claude-native harness command
    (``OMNIGENT_CLAUDE_PATH``) is the crashing stub, with an isolated ``HOME`` /
    ``OMNIGENT_CONFIG_HOME`` carrying a mock anthropic provider (so the launch
    derives gateway auth and reaches the terminal-spawn step without live
    credentials), then creates and binds the same claude-native wrapper session
    ``omnigent claude`` ships. Binding auto-launches the required terminal,
    which runs the stub and dies 127.

    :returns: ``(base_url, session_id, runner_process_log_path)``.
    """
    if shutil.which("tmux") is None:
        pytest.skip("tmux is required for the claude-native terminal rig")

    work = tmp_path_factory.mktemp("claude_crash_127")
    config_home = work / "config-home"
    home_dir = work / "home"
    stub_bin = work / "stub-bin"
    artifacts = work / "artifacts"
    for path in (config_home, home_dir, stub_bin, artifacts):
        path.mkdir(parents=True, exist_ok=True)
    stub = _write_crashing_claude_stub(stub_bin)
    rig_python = _rig_python(work)

    # Mock anthropic provider so the launch derives gateway auth and reaches the
    # terminal-spawn step without live credentials — mirrors the slow-ready rig.
    (config_home / "config.yaml").write_text(
        "providers:\n"
        "  mock-claude:\n"
        "    kind: key\n"
        "    default: [anthropic]\n"
        "    anthropic:\n"
        f'      base_url: "{mock_llm_server_url}"\n'
        '      api_key: "mock-key"\n'
        "      models:\n"
        f"        default: {_CLAUDE_MOCK_MODEL}\n"
    )

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    binding_token = secrets.token_urlsafe(32)

    from omnigent.runner.identity import token_bound_runner_id

    runner_id = token_bound_runner_id(binding_token)

    shared_env = {
        **_no_proxy_env(),
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "HOME": str(home_dir),
        # Force the mock provider even if CI carries a real LLM_API_KEY.
        "LLM_API_KEY": "",
    }
    server_env = {**shared_env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token}
    # The runner logs the full, untruncated failure message to its process log
    # file (stderr is not a TTY here, so nothing mirrors to the redirect).
    process_log = work / "runner-process.log"
    runner_env = {
        **shared_env,
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": base_url,
        "OMNIGENT_PROCESS_LOG_FILE": str(process_log),
        # The fault injection: the claude-native terminal launches a present
        # CLI that runs and then crashes 127 instead of the real Claude Code.
        "OMNIGENT_CLAUDE_PATH": str(stub),
    }

    server_log = work / "server.log"
    runner_log = work / "runner.log"
    server_handle = server_log.open("w")
    runner_handle = runner_log.open("w")
    server_proc: subprocess.Popen[bytes] | None = None
    runner_proc: subprocess.Popen[bytes] | None = None
    session_id: str | None = None
    try:
        server_proc = subprocess.Popen(
            [
                rig_python,
                "-m",
                "omnigent.cli",
                "server",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--database-uri",
                f"sqlite:///{work}/test.db",
                "--artifact-location",
                str(artifacts),
            ],
            env=server_env,
            stdout=server_handle,
            stderr=subprocess.STDOUT,
            cwd=str(_REPO_ROOT),
        )
        runner_proc = subprocess.Popen(
            [rig_python, "-m", "omnigent.runner._entry"],
            env=runner_env,
            stdout=runner_handle,
            stderr=subprocess.STDOUT,
            cwd=str(_REPO_ROOT),
        )

        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        online = False
        while time.monotonic() < deadline:
            if server_proc.poll() is not None or runner_proc.poll() is not None:
                break
            with contextlib.suppress(httpx.HTTPError):
                if _client.get(f"{base_url}/health", timeout=2).status_code == 200:
                    status = _client.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
                    if status.status_code == 200 and status.json().get("online"):
                        online = True
                        break
            time.sleep(0.5)
        if not online:
            raise RuntimeError(
                "crashing claude rig did not come online within "
                f"{_HEALTH_TIMEOUT_S:.0f}s.\nServer log:\n{server_log.read_text()[-3000:]}\n"
                f"Runner log:\n{runner_log.read_text()[-3000:]}"
            )

        session_id = _create_native_claude_session(base_url, runner_id)
        yield (base_url, session_id, process_log)
    finally:
        if session_id is not None:
            with contextlib.suppress(httpx.HTTPError):
                _client.delete(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
        for proc in (runner_proc, server_proc):
            if proc is not None and proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
        for proc in (runner_proc, server_proc):
            if proc is not None:
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        server_handle.close()
        runner_handle.close()


def _reveal_failure_card(page: Page):
    """Bring the failed session's error card on screen and expand it.

    A terminal-first (native) session defaults to the terminal view; the error
    pill renders in the chat transcript, so switch to chat first, then expand
    the pill and open its diagnostics so the card shows its full state.

    :param page: The Playwright page, navigated to the session.
    :returns: The error-pill locator.
    """
    toggle = page.get_by_test_id("view-mode-toggle")
    pill = page.locator(_ERROR_PILL).first
    expect(toggle.or_(pill).first).to_be_visible(timeout=_CARD_TIMEOUT_MS)
    if toggle.count() > 0:
        with contextlib.suppress(Exception):
            page.get_by_test_id("view-mode-chat").click(timeout=30_000)
    expect(pill).to_be_visible(timeout=_CARD_TIMEOUT_MS)
    if pill.get_by_test_id("error-message-content").count() == 0:
        pill.get_by_test_id("error-headline").click()
    expect(pill.get_by_test_id("error-message-content")).to_be_visible(timeout=15_000)
    diag_btn = pill.get_by_role("button", name="View diagnostics")
    if diag_btn.count() > 0:
        with contextlib.suppress(Exception):
            diag_btn.first.click()
    return pill


@pytest.mark.timeout(300)
def test_present_cli_crash_is_not_misdiagnosed_as_missing(
    page: Page,
    crashing_claude_session: tuple[str, str, Path],
) -> None:
    """A present CLI that crashes 127 must not be diagnosed as a missing harness.

    Journey (the reported one): open a fresh Claude Code (claude-native)
    session whose required terminal auto-launches on bind, runs the present
    CLI, and dies with exit 127 after the CLI's own output. Read the failure
    card the web UI shows.

    While the bug is live the card reads "Agent command not found" and tells the
    user to run ``omnigent setup`` — even though the captured pane output proves
    the CLI was present and running. This test rejects that misdiagnosis: the
    card must instead read the honest generic terminal-exit message and must not
    suggest installing a harness the host already has.
    """
    base_url, session_id, process_log = crashing_claude_session

    page.goto(f"{base_url}/c/{session_id}")
    pill = _reveal_failure_card(page)

    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    if record_dir:
        with contextlib.suppress(Exception):
            page.screenshot(path=str(Path(record_dir) / "failure-card.png"))
    page.wait_for_timeout(1500)

    card_text = pill.inner_text()

    # Fidelity: the runner logs the full, untruncated failure message. It must
    # show the terminal ran the present CLI (its resume banner), captured the
    # stray ``--model`` line (not ``claude``) as the unknown token, and died
    # 127 — the present-CLI precondition the misdiagnosis ignores. These hold
    # on any build; only the classification below changes when the bug is fixed.
    log_text = _await_log_contains(process_log, [_RESUME_MARKER, _STRAY_ARG_LINE])
    assert _RESUME_MARKER in log_text, (
        "The captured terminal output lacks the CLI's resume banner; the CLI-present "
        "precondition is unproven (a genuinely missing binary would produce no such output)."
    )
    assert _STRAY_ARG_LINE in log_text, (
        "The captured terminal output lacks the stray --model line naming the real unknown token; "
        "the reported crash signal is unproven."
    )
    assert f"exited with status {_CRASH_EXIT_CODE}" in log_text, (
        f"The terminal did not exit with the reported status {_CRASH_EXIT_CODE}; "
        "the reported 127 crash is unproven."
    )

    # The bug: a present CLI's 127 crash is diagnosed as a missing harness.
    assert _MISDIAGNOSIS_HEADLINE not in card_text, (
        f"Misdiagnosis reproduced: the failure card reads {_MISDIAGNOSIS_HEADLINE!r} for a "
        "present Claude Code CLI that ran (its 'Resume this session with:' banner is in the "
        "captured pane) and then crashed with exit 127. The card contradicts the pane output.\n"
        f"Card text:\n{card_text}"
    )
    assert _MISDIAGNOSIS_REMEDIATION.lower() not in card_text.lower(), (
        "Misdiagnosis reproduced: the failure card tells the user to install the harness "
        f"(remediation {_MISDIAGNOSIS_REMEDIATION!r}) though the CLI was already present.\n"
        f"Card text:\n{card_text}"
    )
    expect(pill).to_contain_text(_HONEST_HEADLINE, timeout=15_000)
