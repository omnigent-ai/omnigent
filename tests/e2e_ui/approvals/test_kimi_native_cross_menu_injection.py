"""E2E (UI): a kimi-native web permission verdict must stay with ITS menu.

kimi-native mirrors each TUI permission menu to a web ``ApprovalCard`` via the
``PermissionRequest`` hook, which long-polls the server for the web verdict and
types the answer back into the TUI (``inject_approval_keystroke``). Two defects
make that unsafe:

1. **No terminal-side resolution** — answering the menu in the terminal never
   resolves the parked elicitation, so the web card stays pending forever
   (qwen/hermes/goose/cursor post ``external_elicitation_resolved``; kimi
   posts nothing).
2. **Presence-only injection** — the injector checks only that *a* permission
   menu is on screen, never that it is the menu the verdict was raised for, so
   a verdict answered on the stale card is typed into a *different, later*
   menu — approving or denying the wrong tool call.

Both tests drive the real product path end to end: a live server + runner, the
real ``kimi`` TUI in the runner-owned tmux pane (pointed at the mock LLM via a
Kimi Code custom provider), the real Omnigent hooks, and the real web UI.

Requires ``kimi`` + ``tmux`` on PATH (no Kimi login needed — the fixture
provisions a mock custom provider). Skips otherwise.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sysconfig
import tarfile
import tempfile
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _REPO_ROOT,
    _bind_session_runner,
    _ensure_runner_online,
    _server_state,
    configure_mock_llm,
    reset_mock_llm,
)
from tests.e2e_ui.messages.test_message_render_parity import (
    _ensure_chat_view,
    _select_view_mode,
    _send,
)

_PENDING_CARD = '[data-testid="approval-card"][data-state="pending"]'
_TERMINAL_VIEW = '[data-testid="terminal-view"]'
_XTERM_INPUT = ".xterm-helper-textarea"

# Kimi's permission menu chrome (tool-independent) — the same marker the
# injector keys on, which is exactly why a later menu is indistinguishable.
_MENU_MARKER = "Approve once"

# Cold kimi boot + hook round-trip before the first card shows.
_CARD_TIMEOUT_MS = 120_000
_TERMINAL_READY_TIMEOUT_MS = 60_000


def _kimi_unavailable_reason() -> str | None:
    """Skip reason when the kimi-native prerequisites are absent.

    Unlike the cursor/hermes suites no vendor login is required: the fixture
    writes a Kimi Code custom-provider config pointing at the mock LLM, which
    the real ``kimi`` CLI accepts as a fully configured install.
    """
    if shutil.which("kimi") is None:
        return "kimi-native approval test needs the `kimi` CLI on PATH."
    if shutil.which("tmux") is None:
        return "kimi-native approval test needs `tmux` on PATH (runner-owned TUI pane)."
    return None


pytestmark = pytest.mark.skipif(
    _kimi_unavailable_reason() is not None,
    reason=_kimi_unavailable_reason() or "",
)


@pytest.fixture(autouse=True)
def _hook_import_shim() -> Iterator[None]:
    """Make ``omnigent`` importable under ``python -I`` for the kimi hook.

    The kimi-native ``PermissionRequest`` hook (which mirrors the TUI menu to a
    web ApprovalCard) is launched as ``python -I -m omnigent.kimi_native_hook``;
    ``-I`` (isolated mode) drops ``PYTHONPATH``, the cwd, and user-site from
    ``sys.path``, so a *pip-installed* omnigent still imports but this checkout's
    *editable* install does not: its ``__editable__`` finder maps ``omnigent`` to
    a sibling worktree path that does not exist here, so ``-I`` fails with
    ``ModuleNotFoundError`` and the hook subprocess dies before it can POST the
    card. That is a CI editable-install artifact, not the product; in a shipped
    (pip-installed) omnigent the hook imports fine and the card appears.

    Drop a plain ``.pth`` naming this worktree's repo root into site-packages so
    ``-I`` resolves the *same* omnigent the spawned server/runner already run
    (they inject ``_REPO_ROOT`` on ``PYTHONPATH``). Removed on teardown.
    """
    site_packages = Path(sysconfig.get_paths()["purelib"])
    shim = site_packages / "zzz_omnigent_kimi_repro_isolated_import.pth"
    created = False
    if not shim.exists():
        shim.write_text(f"{_REPO_ROOT}\n", encoding="utf-8")
        created = True
    try:
        yield
    finally:
        if created:
            shim.unlink(missing_ok=True)


def _wait_for(
    predicate: Callable[[], bool], *, timeout_s: float = 30.0, interval_s: float = 0.5
) -> bool:
    """Poll *predicate* until truthy or the deadline passes; return the result."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval_s)
    return False


def _pending_elicitations(base_url: str, session_id: str) -> list[dict]:
    """Return the session snapshot's pending elicitation events (owner view)."""
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    resp.raise_for_status()
    return resp.json().get("pending_elicitations") or []


def _pane_text(session_id: str) -> str:
    """Capture the kimi TUI pane via the bridge-advertised tmux target."""
    from omnigent.kimi_native_bridge import bridge_dir_for_session_id, read_tmux_info

    info = read_tmux_info(bridge_dir_for_session_id(session_id))
    if not info:
        return ""
    out = subprocess.run(
        ["tmux", "-S", info["socket_path"], "capture-pane", "-p", "-t", info["tmux_target"]],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return out.stdout if out.returncode == 0 else ""


def _tool_result_seen(mock_url: str, call_id: str) -> bool:
    """True once kimi reported a tool result for *call_id* back to the LLM."""
    resp = httpx.get(f"{mock_url}/mock/requests", timeout=10.0)
    resp.raise_for_status()
    for req in resp.json()["requests"]:
        for message in req.get("messages") or []:
            if (
                isinstance(message, dict)
                and message.get("role") == "tool"
                and message.get("tool_call_id") == call_id
            ):
                return True
    return False


def _bash_tool_call(call_id: str, command: str) -> dict:
    """A scripted mock-LLM response that makes kimi gate a Bash command."""
    return {
        "tool_calls": [
            {"call_id": call_id, "name": "Bash", "arguments": json.dumps({"command": command})}
        ]
    }


def _diag(session_id: str, mock_url: str, base_url: str) -> str:
    """A one-shot dump of the live state, for debugging a stalled journey."""
    lines: list[str] = []
    try:
        reqs = httpx.get(f"{mock_url}/mock/requests", timeout=10.0).json()["requests"]
        lines.append(f"MOCK saw {len(reqs)} request(s)")
        if reqs:
            last = reqs[-1]
            lines.append(f"  last.model={last.get('model')!r}")
            for m in (last.get("messages") or [])[-4:]:
                if isinstance(m, dict):
                    lines.append(
                        f"  msg role={m.get('role')!r} "
                        f"content={str(m.get('content'))[:120]!r} "
                        f"tool_calls={bool(m.get('tool_calls'))}"
                    )
    except Exception as exc:
        lines.append(f"MOCK requests read failed: {exc!r}")
    try:
        pend = _pending_elicitations(base_url, session_id)
        lines.append(f"SERVER pending_elicitations={len(pend)}")
    except Exception as exc:
        lines.append(f"pending read failed: {exc!r}")
    lines.append("TMUX PANE >>>")
    lines.append(_pane_text(session_id) or "(pane empty / bridge not advertised)")
    lines.append("<<< TMUX PANE")
    return "\n".join(lines)


def _tmux_pane(sock: Path, target: str) -> str:
    """Capture a pane on a private (non-runner) tmux socket."""
    out = subprocess.run(
        ["tmux", "-S", str(sock), "capture-pane", "-p", "-t", target],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return out.stdout if out.returncode == 0 else ""


def _pretrust_workspace(kimi_home: Path, workspace: Path) -> None:
    """Record folder trust for *workspace* by driving kimi's real trust dialog once.

    Kimi 0.41+ prompts "Trust this folder?" on first launch in a folder and
    remembers the answer under ``$KIMI_CODE_HOME/workspace-trust/``. The
    runner's session-scoped home symlinks that directory, so trusting here
    keeps the auto-launched TUI from blocking on the modal.
    """
    sock = Path(tempfile.mkdtemp(prefix="kimi-trust-")) / "sock"
    env = {**os.environ, "KIMI_CODE_HOME": str(kimi_home)}
    subprocess.run(
        [
            "tmux",
            "-S",
            str(sock),
            "new-session",
            "-d",
            "-s",
            "trust",
            "-x",
            "200",
            "-y",
            "50",
            "-c",
            str(workspace),
            "kimi",
        ],
        env=env,
        check=True,
        timeout=30,
    )
    try:
        if not _wait_for(lambda: "Trust this folder" in _tmux_pane(sock, "trust"), timeout_s=60):
            raise RuntimeError(
                f"kimi never showed its trust dialog for {workspace}:\n{_tmux_pane(sock, 'trust')}"
            )
        subprocess.run(
            ["tmux", "-S", str(sock), "send-keys", "-t", "trust", "Enter"],
            check=True,
            timeout=15,
        )
        if not _wait_for(lambda: "context:" in _tmux_pane(sock, "trust"), timeout_s=60):
            raise RuntimeError(
                f"kimi composer never rendered after trusting {workspace}:\n"
                f"{_tmux_pane(sock, 'trust')}"
            )
    finally:
        subprocess.run(["tmux", "-S", str(sock), "kill-server"], capture_output=True)


@pytest.fixture
def kimi_mock_home(mock_llm_server_url: str) -> Iterator[Path]:
    """A global ``~/.kimi-code`` whose custom provider is the mock LLM server.

    The runner builds each session's ``KIMI_CODE_HOME`` from the user's global
    home, so this is how the auto-launched TUI reaches the mock. Any real
    ``~/.kimi-code`` is moved aside and restored on teardown.
    """
    home = Path.home() / ".kimi-code"
    backup: Path | None = None
    if home.exists():
        backup = home.with_name(f".kimi-code.e2e-backup-{os.getpid()}")
        home.rename(backup)
    home.mkdir(parents=True)
    (home / "config.toml").write_text(
        'default_model = "mock-model"\n\n'
        "[providers.mock]\n"
        'type = "openai"\n'
        f'base_url = "{mock_llm_server_url}/v1"\n'
        'api_key = "sk-mock-e2e"\n\n'
        '[models."mock-model"]\n'
        'provider = "mock"\n'
        'model = "mock-model"\n'
        "max_context_size = 128000\n"
    )
    try:
        yield home
    finally:
        shutil.rmtree(home, ignore_errors=True)
        if backup is not None:
            backup.rename(home)


def _create_native_kimi_session(base_url: str, runner_id: str, workspace: Path) -> str:
    """Create a runner-bound ``kimi-native`` session rooted at *workspace*."""
    from omnigent._wrapper_labels import (
        KIMI_NATIVE_WRAPPER_VALUE,
        UI_MODE_LABEL_KEY,
        UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY,
    )
    from omnigent.kimi_native import _materialize_kimi_agent_spec

    with tempfile.TemporaryDirectory() as tmp:
        yaml_text = _materialize_kimi_agent_spec(Path(tmp)).read_text()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        info = tarfile.TarInfo("kimi-native-ui.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    metadata = {
        "labels": {
            UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
            WRAPPER_LABEL_KEY: KIMI_NATIVE_WRAPPER_VALUE,
        },
        "workspace": str(workspace),
    }
    create = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps(metadata)},
        files={"bundle": ("kimi-native-ui.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = str(create.json()["session_id"])
    _bind_session_runner(base_url, session_id, runner_id)
    return session_id


@pytest.fixture
def native_kimi_session(
    live_server: str,
    kimi_mock_home: Path,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str, Path]]:
    """A runner-bound kimi-native session in a pre-trusted temp workspace.

    :returns: ``(base_url, session_id, workspace)``.
    """
    workspace = Path(tempfile.mkdtemp(prefix="kimi-e2e-ws-"))
    _pretrust_workspace(kimi_mock_home, workspace)
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    session_id = _create_native_kimi_session(live_server, runner_id, workspace)
    try:
        yield (live_server, session_id, workspace)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        shutil.rmtree(workspace, ignore_errors=True)
        if respawned is not None:
            respawned.terminate()
            try:
                respawned.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned.kill()
                respawned.wait(timeout=5)


def _open_terminal_view(page: Page) -> None:
    """Switch to the Terminal view and wait for the xterm to attach."""
    _select_view_mode(page, "Terminal")
    terminal = page.locator(_TERMINAL_VIEW).last
    expect(terminal).to_have_attribute(
        "data-state", "connected", timeout=_TERMINAL_READY_TIMEOUT_MS
    )


def _type_into_tui(page: Page, keys: list[str]) -> None:
    """Send *keys* (Playwright key names) into the embedded kimi TUI."""
    xterm_input = page.locator(_TERMINAL_VIEW).last.locator(_XTERM_INPUT)
    expect(xterm_input).to_be_attached(timeout=30_000)
    xterm_input.focus()
    for key in keys:
        page.keyboard.press(key)
        page.wait_for_timeout(300)


def _type_prompt_into_tui(page: Page, text: str) -> None:
    """Type a prompt into the kimi TUI composer and submit with Enter."""
    xterm_input = page.locator(_TERMINAL_VIEW).last.locator(_XTERM_INPUT)
    expect(xterm_input).to_be_attached(timeout=30_000)
    xterm_input.focus()
    page.keyboard.type(text, delay=15)
    page.wait_for_timeout(1000)
    page.keyboard.press("Enter")


def _raise_menu_a_and_answer_in_tui(
    page: Page, base_url: str, session_id: str, workspace: Path, mock_url: str
) -> None:
    """Steps 1-3 of the reported journey, shared by both tests.

    Send a prompt whose scripted reply is a gated Bash call → kimi raises
    permission menu A and the hook parks web card A → the user answers menu A
    in the terminal (1 = Approve once) → the tool runs and the turn completes.
    """
    reset_mock_llm(mock_url)
    configure_mock_llm(
        mock_url,
        [
            _bash_tool_call("call-menu-a", "touch marker_a.txt"),
            {"text": "Tool A completed."},
        ],
    )

    page.goto(f"{base_url}/c/{session_id}")
    _ensure_chat_view(page)
    _send(page, "Run the first marker command.")

    # The PermissionRequest hook parks card A while kimi shows menu A.
    card = page.locator(_PENDING_CARD).first
    try:
        expect(card).to_be_visible(timeout=_CARD_TIMEOUT_MS)
    except AssertionError:
        raise AssertionError(
            "card A never appeared; live diagnostics:\n" + _diag(session_id, mock_url, base_url)
        ) from None
    assert _pending_elicitations(base_url, session_id), "server has no parked elicitation"
    assert _wait_for(lambda: _MENU_MARKER in _pane_text(session_id), timeout_s=60), (
        f"kimi permission menu A never rendered:\n{_pane_text(session_id)}"
    )

    # The user answers menu A in the terminal: 1 = Approve once, ↵ confirms.
    _open_terminal_view(page)
    _type_into_tui(page, ["1", "Enter"])

    assert _wait_for(lambda: (workspace / "marker_a.txt").exists(), timeout_s=60), (
        f"tool A never ran after the TUI approval:\n{_pane_text(session_id)}"
    )
    assert _wait_for(lambda: _tool_result_seen(mock_url, "call-menu-a"), timeout_s=60), (
        "kimi never reported tool A's result back to the LLM"
    )
    # Menu A is gone from the TUI; the turn wrapped up.
    assert _wait_for(lambda: _MENU_MARKER not in _pane_text(session_id), timeout_s=30), (
        f"menu A still on screen after the TUI approval:\n{_pane_text(session_id)}"
    )


@pytest.mark.timeout(600)
def test_menu_answered_in_tui_releases_the_web_card(
    page: Page,
    native_kimi_session: tuple[str, str, Path],
) -> None:
    """Facet 1: answering the menu in the terminal must clear the parked web card.

    kimi posts no ``external_elicitation_resolved`` when its permission menu is
    answered in the TUI, so the web ApprovalCard for menu A stays pending
    forever — a stale live verdict waiting to hit whatever menu shows next.
    """
    base_url, session_id, workspace = native_kimi_session
    mock_url = str(_server_state["mock_llm_url"])

    _raise_menu_a_and_answer_in_tui(page, base_url, session_id, workspace, mock_url)

    # The menu was answered in the terminal and the tool already ran, so the
    # parked elicitation must drain (the terminal answer is authoritative).
    released = _wait_for(lambda: not _pending_elicitations(base_url, session_id), timeout_s=30)
    assert released, (
        "BUG: the permission menu was answered in the terminal and the tool "
        "already ran, but the web approval card is still parked — kimi-native "
        "never resolves an elicitation from the terminal side, so the stale "
        "card keeps a live verdict aimed at any future menu."
    )
    _ensure_chat_view(page)
    expect(page.locator(_PENDING_CARD)).to_have_count(0, timeout=10_000)


@pytest.mark.timeout(600)
def test_stale_web_verdict_must_not_answer_a_later_menu(
    page: Page,
    native_kimi_session: tuple[str, str, Path],
) -> None:
    """Facet 2: a verdict on the stale card must not resolve a DIFFERENT menu.

    With card A still parked after the TUI answered menu A, a second gated
    command raises menu B (and card B). Rejecting the stale card A must not
    touch menu B — but the injector only checks that *some* permission menu is
    on screen, so the deny keystroke lands in menu B and rejects the wrong
    tool call.
    """
    base_url, session_id, workspace = native_kimi_session
    mock_url = str(_server_state["mock_llm_url"])

    _raise_menu_a_and_answer_in_tui(page, base_url, session_id, workspace, mock_url)

    if not _pending_elicitations(base_url, session_id):
        # Terminal-side resolution landed (facet 1 fixed): the stale card is
        # gone, so the dangerous state cannot arise. Nothing left to inject.
        return

    # Step 4: a different gated command raises menu B (+ card B). The prompt is
    # typed in the TUI, as a terminal-first user would with a card still parked.
    configure_mock_llm(
        mock_url,
        [
            _bash_tool_call("call-menu-b", "touch marker_b.txt"),
            {"text": "Tool B completed."},
        ],
    )
    _open_terminal_view(page)
    _type_prompt_into_tui(page, "Run the second marker command.")

    assert _wait_for(
        lambda: (
            "marker_b.txt" in _pane_text(session_id) and _MENU_MARKER in _pane_text(session_id)
        ),
        timeout_s=90,
    ), f"kimi permission menu B never rendered:\n{_pane_text(session_id)}"

    _ensure_chat_view(page)
    cards = page.locator(_PENDING_CARD)
    expect(cards).to_have_count(2, timeout=_CARD_TIMEOUT_MS)

    # Step 5: the user rejects the STALE card A (its menu was already answered
    # in the terminal; the web verdict has nowhere legitimate to go).
    cards.first.get_by_role("button", name="Reject", exact=True).click()

    # Step 6 — BUG: hook A's deny is typed into menu B (the only menu on
    # screen), rejecting tool B, which the user never ruled on. On a correct
    # build menu B stays up, still waiting for ITS verdict.
    menu_b_answered = _wait_for(lambda: _MENU_MARKER not in _pane_text(session_id), timeout_s=20)
    assert not menu_b_answered, (
        "BUG: rejecting the STALE approval card (menu A, already answered in "
        "the terminal) resolved kimi's LATER permission menu B — the web "
        "verdict was injected into a different tool call's menu. "
        f"kimi reported tool B rejected to the LLM: "
        f"{_tool_result_seen(mock_url, 'call-menu-b')}; pane:\n"
        f"{_pane_text(session_id)}"
    )
    assert not (workspace / "marker_b.txt").exists(), (
        "tool B ran even though nobody approved it — the stale web verdict "
        "was injected into menu B as an approval"
    )
