"""End-to-end test: a sub-agent's approval prompt surfaces on the parent.

The sub-agent-elicitation contract: when a sub-agent (child session) raises an
elicitation, the prompt must surface on the PARENT session (so a user
in the parent — e.g. polly — chat sees it) stamped with
``params.target_session_id`` (the child that owns the parked Future),
and resolving against that CHILD session id must release the worker.

This drives the REAL stack — a local ``omnigent server`` booted from
this working tree, a local runner + the native ``claude``/``codex``
CLIs spawned by ``omnigent run --server``, and a real LLM brain. The
``ask-mode-supervisor`` fixture agent's ``claude_code`` (claude-native,
``--permission-mode default``) and ``codex`` (codex-native, default
approval policy — no ``yolo``) sub-agents run in PROMPTING mode, so a
delegated shell command makes the worker raise a real approval that
must be forwarded and answered from the parent.

OPT-IN. Like ``test_polly_e2e.py`` this needs the dev-box toolset CI
runners lack (a logged-in ``oss`` Databricks OAuth profile + the
``claude``/``codex`` binaries), so it is gated behind
``OMNIGENT_E2E_SUBAGENT_ELICIT=1`` and is not collected by default::

    OMNIGENT_E2E_SUBAGENT_ELICIT=1 \\
    .venv/bin/python -m pytest \\
        tests/e2e/test_subagent_elicitation_forwarding_e2e.py \\
        --profile oss \\
        --llm-api-key "$(databricks auth token -p oss \\
            | python -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')" \\
        -v
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_PROFILE = "oss"

# ── Fixture agent bundle (materialized into a tmp dir by ``agent_dir``) ──
# A polly-style supervisor whose claude_code and codex sub-agents run in
# PROMPTING (ask) permission mode — NOT bypass/yolo — so a worker raises a
# real approval the parent must surface and resolve. Inlined
# here rather than checked in under tests/resources so the fixture lives
# and dies with this test.
_SUPERVISOR_CONFIG_YAML = """\
spec_version: 1
name: ask-mode-supervisor
description: >-
  E2E fixture: a polly-style supervisor whose claude_code and codex
  sub-agents run in PROMPTING (ask) permission mode — they are NOT in
  bypass / yolo. Used to e2e-verify that a sub-agent's approval prompt
  surfaces on the parent session and is answerable by resolving against
  the child session id (PR 2272 — sub-agent elicitation routing).

# Orchestrator brain: claude-sdk (in-process), like polly. It only
# delegates; the substantive command-running work is done by the native
# sub-agents, which run in their own terminal and prompt for approval.
executor:
  type: omnigent
  config:
    harness: claude-sdk

prompt: |
  You are a coding orchestrator. You do not do substantive work
  yourself — you delegate to one of your two sub-agents and report
  back what they did.

  You have exactly two sub-agents, both real CLI coding harnesses that
  run in their own terminal:
  - `claude_code` — Claude Code (claude-native harness).
  - `codex` — Codex (codex-native harness).

  Delegate work via `sys_session_send(agent=<name>, title=<short label>,
  args=<task>)`. Each runs autonomously and notifies you via the inbox
  when done; collect results with `sys_read_inbox`. Do not busy-poll —
  end your turn while a worker runs and you will be woken when it
  finishes.

  When the user asks you to run a shell command, delegate it verbatim to
  the requested sub-agent and tell it to actually execute the command in
  its terminal. After it finishes, report the command output.

async: true
cancellable: true
timers: true

os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none

tools:
  agents:
    - claude_code
    - codex
"""

_CLAUDE_CODE_CONFIG_YAML = """\
spec_version: 1
name: claude_code
description: Claude Code coding sub-agent in PROMPTING mode (asks before running commands).

executor:
  type: omnigent
  config:
    harness: claude-native
    # PROMPTING (not bypass): the server translates this into
    # ``--permission-mode default`` so Claude Code asks before running
    # Bash/Edit/Write. This is the whole point of the fixture — a worker
    # that raises an approval the parent must surface and resolve.
    permission_mode: default

prompt: |
  You are Claude Code, a coding sub-agent. Do exactly the one task the
  orchestrator delegates — when asked to run a shell command, run it in
  your terminal with the Bash tool, then report the output. Do not
  refactor or wander.

os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none
"""

_CODEX_CONFIG_YAML = """\
spec_version: 1
name: codex
description: Codex coding sub-agent in PROMPTING mode (asks before running commands).

executor:
  type: omnigent
  config:
    harness: codex-native
    # PROMPTING (not yolo): with ``yolo`` omitted the server adds NO
    # bypass flag, so Codex launches in its default approval policy and
    # asks before running a command. The whole point of the fixture is a
    # worker that raises an approval the parent must surface and resolve.

prompt: |
  You are Codex, a coding sub-agent. Do exactly the one task the
  orchestrator delegates — when asked to run a shell command, run it in
  your terminal, then report the output. Do not refactor or wander.

os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none
"""
# Brain model for the claude-sdk orchestrator. The agent pins no model;
# on a workspace whose default is a non-Claude model the claude-sdk brain
# 400s, so name a Claude model here (mirrors test_polly_e2e / the driver).
_BRAIN_MODEL = "databricks-claude-opus-4-8"
_SERVER_BOOT_TIMEOUT_SEC = 90
_POLL_INTERVAL_SEC = 4.0
# Bounded, fail-fast phases so a flaky/stalled real-LLM brain surfaces the run
# log quickly instead of polling a single long budget. Sized for a loaded box.
_PARENT_DISCOVER_TIMEOUT_SEC = 120
_CHILD_SPAWN_TIMEOUT_SEC = 180
_ELICIT_TIMEOUT_SEC = 180

pytestmark = pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_SUBAGENT_ELICIT") != "1",
    reason=(
        "sub-agent elicitation e2e needs the dev-box toolset (oss OAuth + "
        "claude/codex CLIs) absent on CI — set "
        "OMNIGENT_E2E_SUBAGENT_ELICIT=1 to opt in."
    ),
)


def _clean_env(profile: str = _PROFILE) -> dict[str, str]:
    """
    Build a child env with leaked agent credentials/PYTHONPATH stripped.

    The native harnesses resolve the profile's OAuth via the global
    config's ``auth:`` block, written into an isolated
    ``OMNIGENT_CONFIG_HOME`` here (the supported replacement for the
    removed ``--profile`` CLI flag); a stray ``DATABRICKS_TOKEN`` /
    ``ANTHROPIC_API_KEY`` / ``CLAUDE_CODE`` from the outer coding-agent
    process would shadow it.
    ``PYTHONPATH`` is dropped so the child imports omnigent from
    ``--code-dir`` (this worktree), not a sibling editable install.

    :param profile: Databricks profile for the auth block, e.g. ``"oss"``.
    :returns: A sanitized copy of ``os.environ``.
    """
    env = dict(os.environ)
    env["OMNIGENT_SKIP_ONBOARD"] = "1"
    env["OMNIGENT_NO_UPDATE_CHECK"] = "1"
    for stale in (
        "DATABRICKS_TOKEN",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "CLAUDE_CODE",
        "CLAUDECODE",
        "CODEX",
        "PYTHONPATH",
    ):
        env.pop(stale, None)
    config_home = Path(tempfile.mkdtemp(prefix="omnigent-elicit-config-"))
    (config_home / "config.yaml").write_text(
        f"auth:\n  type: databricks\n  profile: {profile}\n",
        encoding="utf-8",
    )
    env["OMNIGENT_CONFIG_HOME"] = str(config_home)
    env["DATABRICKS_CONFIG_PROFILE"] = profile
    return env


def _free_port() -> int:
    """:returns: An ephemeral localhost port the OS just confirmed free."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(base: str, path: str, token: str | None) -> dict:
    """
    GET a JSON object from the local server.

    :param base: Server base URL, e.g. ``"http://127.0.0.1:8811"``.
    :param path: API path, e.g. ``"/v1/sessions/conv_x"``.
    :param token: Bearer token, or ``None`` for the open local server.
    :returns: Parsed JSON dict, or ``{}`` on any transport/parse error.
    """
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    req = urllib.request.Request(f"{base}{path}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.load(r)
    except (urllib.error.URLError, OSError, ValueError):
        return {}


def _post(base: str, path: str, body: dict, token: str | None) -> tuple[int, str]:
    """
    POST a JSON body to the local server.

    :param base: Server base URL.
    :param path: API path, e.g.
        ``"/v1/sessions/conv_child/elicitations/elicit_x/resolve"``.
    :param body: JSON-serializable request body, e.g. ``{"action": "accept"}``.
    :param token: Bearer token, or ``None`` for the open local server.
    :returns: ``(status_code, response_text)``; ``(-1, err)`` on transport error.
    """
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"{base}{path}", data=json.dumps(body).encode(), method="POST", headers=headers
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as e:
        return -1, str(e)


def _descendants(pid: int) -> list[int]:
    """
    Recursively collect descendant pids of ``pid``.

    :param pid: Root process id.
    :returns: All descendant pids (children, grandchildren, ...).
    """
    try:
        kids = subprocess.check_output(["ps", "--ppid", str(pid), "-o", "pid="]).split()
    except subprocess.CalledProcessError:
        return []
    out: list[int] = []
    for k in kids:
        ki = int(k)
        out.append(ki)
        out += _descendants(ki)
    return out


def _kill_tree(pid: int, conv_ids: set[str]) -> None:
    """
    SIGTERM-then-SIGKILL a run subprocess tree + leaked harness runners.

    ``omnigent run`` spawns a detached daemon → runner → per-conversation
    harness chain plus (for claude-native) a tmux server, which outlive the
    run process. We additionally kill any ``harnesses._runner`` whose
    cmdline names one of this run's conversation ids.

    :param pid: The ``omnigent run`` subprocess pid.
    :param conv_ids: Conversation ids of this run (parent + sub-agents) used
        to find leaked harness runners.
    """
    pids = set(_descendants(pid)) | {pid}
    conv_ids = {c for c in conv_ids if c}
    if conv_ids:
        try:
            ps = subprocess.check_output(["ps", "-eo", "pid=,args="]).decode()
        except subprocess.CalledProcessError:
            ps = ""
        for line in ps.splitlines():
            if "omnigent.runtime.harnesses._runner" in line and any(c in line for c in conv_ids):
                toks = line.split()
                with contextlib.suppress(ValueError, IndexError):
                    pids.add(int(toks[0]))
                    if "--parent-pid" in toks:
                        pids.add(int(toks[toks.index("--parent-pid") + 1]))
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for p in pids:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(p, sig)
        time.sleep(1.0)


def _kill_native_terminals(conv_ids: set[str]) -> None:
    """
    Kill leaked native-worker tmux panes for a run's conversation ids.

    claude-native / codex-native sub-agents launch their CLI inside a tmux
    server (``/tmp/omnigent-terminal-*/tmux.sock``). When the parent's
    one-shot ``omnigent run`` exits, the detached worker's pane can outlive
    it. The pane's ``new-session`` command line embeds the session URL
    (``/c/<conv_id>``) and the worker's bridge dir, so match on the conv id
    and SIGKILL the owning processes. Best-effort; failures are ignored.

    :param conv_ids: Conversation ids of this run (parent + sub-agents).
    """
    conv_ids = {c for c in conv_ids if c}
    if not conv_ids:
        return
    try:
        ps = subprocess.check_output(["ps", "-eo", "pid=,args="]).decode()
    except subprocess.CalledProcessError:
        return
    victims: set[int] = set()
    for line in ps.splitlines():
        if "tmux" not in line and "harnesses._runner" not in line:
            continue
        if any(c in line for c in conv_ids):
            with contextlib.suppress(ValueError, IndexError):
                victims.add(int(line.split(None, 1)[0]))
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for p in victims:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(p, sig)
        time.sleep(0.5)


@pytest.fixture(scope="module")
def local_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """
    Boot a throwaway ``omnigent server`` from this working tree.

    A bare ``omnigent run`` would route to the developer's configured
    default (shared) server, which need not carry this branch's code; a
    local server from ``_REPO`` carries it. The server uses a throwaway
    sqlite DB + artifact dir under a temp path and is killed on teardown.

    :param tmp_path_factory: Pytest temp path factory for DB/artifacts/log.
    :returns: The server base URL, e.g. ``"http://127.0.0.1:8811"``.
    :raises RuntimeError: If the server never passes health.
    """
    workdir = tmp_path_factory.mktemp("subagent_elicit_e2e")
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    log = open(workdir / "server.log", "w")  # noqa: SIM115 — lives for the Popen lifetime
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "omnigent",
            "server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--database-uri",
            f"sqlite:///{workdir / 'e2e.db'}",
            "--artifact-location",
            str(workdir / "artifacts"),
        ],
        cwd=str(_REPO),
        env=_clean_env(),
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + _SERVER_BOOT_TIMEOUT_SEC
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"local server exited early (rc={proc.returncode}); "
                    f"log: {(workdir / 'server.log').read_text()[-3000:]}"
                )
            try:
                with urllib.request.urlopen(f"{base}/", timeout=5) as r:
                    if r.status == 200:
                        break
            except (urllib.error.URLError, OSError):
                time.sleep(1)
        else:
            raise RuntimeError(f"local server at {base} never became healthy")
        yield base
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        log.close()


def _force_codex_prompting(config_path: Path) -> str | None:
    """
    Rewrite a codex ``config.toml`` to a PROMPTING approval policy in place.

    The codex-native launch bridges config from the developer's real
    ``~/.codex/config.toml`` (the detached runner daemon does not inherit a
    test-set ``CODEX_HOME``, so an isolated home does not reach the
    app-server). Dev boxes commonly run unattended with
    ``approval_policy = "never"`` + ``sandbox_mode = "danger-full-access"``,
    which makes a codex worker auto-run everything — so it never raises an
    approval to forward. This flips those two keys to ``"untrusted"`` /
    ``"workspace-write"`` so a write outside the workspace prompts.

    The caller MUST restore the file with :func:`_restore_file` in a
    ``finally``. This whole test is gated behind
    ``OMNIGENT_E2E_SUBAGENT_ELICIT=1`` (a deliberate, human-invoked opt-in,
    never CI/unattended), so briefly toggling the invoking developer's own
    codex approval policy is acceptable; the restore bounds the window.

    :param config_path: Path to the codex ``config.toml`` to edit in place,
        e.g. ``~/.codex/config.toml``.
    :returns: The original file contents (pass to :func:`_restore_file`), or
        ``None`` when the file does not exist (nothing was changed).
    """
    if not config_path.is_file():
        return None
    original = config_path.read_text()
    out: list[str] = []
    seen_approval = seen_sandbox = False
    for line in original.splitlines():
        # Only rewrite top-level keys (no leading whitespace => not inside a
        # ``[table]``), so a nested ``approval_policy`` under some provider
        # table is left alone.
        if line.startswith("approval_policy") and "=" in line:
            out.append('approval_policy = "untrusted"')
            seen_approval = True
        elif line.startswith("sandbox_mode") and "=" in line:
            out.append('sandbox_mode = "workspace-write"')
            seen_sandbox = True
        else:
            out.append(line)
    prefix: list[str] = []
    if not seen_approval:
        prefix.append('approval_policy = "untrusted"')
    if not seen_sandbox:
        prefix.append('sandbox_mode = "workspace-write"')
    config_path.write_text("\n".join([*prefix, *out]) + "\n")
    return original


def _force_claude_prompting(settings_path: Path) -> str | None:
    """
    Rewrite a claude ``settings.json`` so Claude Code prompts on tool use.

    Claude Code in ``--permission-mode default`` still won't prompt for a
    tool that an explicit ``permissions.allow`` rule pre-approves. A dev box
    commonly allows ``Bash(*)`` / ``Edit(*)`` / ``Write(*)`` with
    ``defaultMode: "dontAsk"``. This strips the broad allow rules for the
    tools the worker will use and sets ``defaultMode`` to ``"default"`` so
    Claude raises a ``PermissionRequest`` (which the native hook forwards).

    The caller MUST restore via :func:`_restore_file` in a ``finally``.
    Gated behind the opt-in env var like :func:`_force_codex_prompting`.

    :param settings_path: Path to the claude ``settings.json`` to edit in
        place, e.g. ``~/.claude/settings.json``.
    :returns: The original file contents (pass to :func:`_restore_file`), or
        ``None`` when the file does not exist.
    """
    if not settings_path.is_file():
        return None
    original = settings_path.read_text()
    data = json.loads(original)
    perms = data.setdefault("permissions", {})
    allow = perms.get("allow")
    if isinstance(allow, list):
        # Drop the broad pre-approvals for the tools the worker uses so the
        # default-mode permission prompt actually fires.
        blocked_prefixes = ("Bash(", "Edit(", "Write(")
        perms["allow"] = [
            rule
            for rule in allow
            if not (isinstance(rule, str) and rule.startswith(blocked_prefixes))
        ]
    perms["defaultMode"] = "default"
    settings_path.write_text(json.dumps(data, indent=2))
    return original


def _restore_file(path: Path, original: str | None) -> None:
    """
    Restore a file's original contents captured before an in-place edit.

    :param path: The file that was edited, e.g. ``~/.codex/config.toml``.
    :param original: The contents returned by the force-prompting helpers;
        ``None`` is a no-op (the file never existed / was not changed).
    """
    if original is not None:
        path.write_text(original)


@pytest.fixture(scope="module")
def agent_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """
    Materialize the ask-mode-supervisor agent bundle into a tmp dir.

    Writes the inlined supervisor + sub-agent specs in the on-disk bundle
    layout ``omnigent run`` expects (``config.yaml`` at the root,
    ``agents/<name>/config.yaml`` per sub-agent).

    :param tmp_path_factory: Pytest tmp-dir factory (module-scoped so all
        parametrized cases share one bundle).
    :returns: Path of the bundle root to pass to ``omnigent run``.
    """
    root = tmp_path_factory.mktemp("ask-mode-supervisor")
    (root / "config.yaml").write_text(_SUPERVISOR_CONFIG_YAML)
    for name, body in (
        ("claude_code", _CLAUDE_CODE_CONFIG_YAML),
        ("codex", _CODEX_CONFIG_YAML),
    ):
        sub = root / "agents" / name
        sub.mkdir(parents=True)
        (sub / "config.yaml").write_text(body)
    return root


def _llm_token(request: pytest.FixtureRequest) -> str:
    """
    Resolve the LLM bearer token for the run subprocess.

    :param request: Pytest request — reads the ``--llm-api-key`` option.
    :returns: The token string.
    :raises pytest.UsageError: If ``--llm-api-key`` was not supplied.
    """
    key = request.config.getoption("--llm-api-key")
    if not key:
        raise pytest.UsageError("this e2e requires --llm-api-key (an `oss` Databricks PAT).")
    return str(key)


@pytest.mark.parametrize(
    "sub_agent,scenario",
    [
        # A gated shell command (file write) — claude-native prompts via its
        # permission hook; codex prompts via its approval policy.
        ("claude_code", "command"),
        ("codex", "command"),
        # Claude's built-in AskUserQuestion — the interactive question card.
        # claude-native only; fires regardless of bypass mode so it needs no
        # prompting-config toggle. (codex has no AskUserQuestion equivalent.)
        ("claude_code", "ask_user_question"),
    ],
)
def test_subagent_prompt_surfaces_on_parent_and_resolves_via_child(
    local_server: str,
    agent_dir: Path,
    request: pytest.FixtureRequest,
    sub_agent: str,
    scenario: str,
    using_mock_llm: bool,
) -> None:
    """
    A prompting sub-agent's approval is forwarded to the parent and answered there.

    Drives ``omnigent run ask-mode-supervisor --server <local> -p "..."``
    telling the orchestrator to delegate a shell command to ``sub_agent``
    (claude_code or codex). That worker runs in prompting mode, so it
    raises an approval before executing. The test then asserts the
    sub-agent-elicitation contract over the AP API:

    1. A child (sub-agent) session of the expected vendor appears.
    2. The PARENT snapshot's ``pending_elicitations`` surfaces that
       child's prompt, stamped with ``params.target_session_id`` == the
       child id — the "actionable from the parent UI" guarantee.
    3. Resolving via the CHILD session's resolve endpoint (what the
       parent UI does with ``target_session_id``) clears the child's
       pending prompt — proving the verdict reached the parked Future.

    A mirroring regression fails at step 2 (no targeted prompt on the
    parent); a resolve-routing regression fails at step 3 (child stays
    parked).

    :param local_server: Base URL of the booted local server.
    :param request: Pytest request (for ``--llm-api-key`` / ``--profile``).
    :param sub_agent: Which native sub-agent to delegate to, e.g.
        ``"claude_code"``.
    :param scenario: Which elicitation the worker raises — ``"command"``
        (a gated shell command) or ``"ask_user_question"`` (Claude's
        built-in interactive question tool).
    :param using_mock_llm: Whether mock LLM mode is active.
    """
    if using_mock_llm:
        pytest.skip(
            "sub-agent elicitation forwarding e2e requires real native CLI "
            "harnesses (claude/codex) with OAuth authentication; not feasible "
            "under mock LLM"
        )
    from tests.e2e._harness_probes import cli_unavailable_reason

    # The delegated worker is a real CLI (claude / codex); skip the row if its
    # binary isn't installed/runnable. ``_harness_probes`` keys its skip helper
    # on the wrapped-harness names (claude-sdk/codex/...), not the *-native
    # ones, so probe the binary directly here.
    _cli_binary = "claude" if sub_agent == "claude_code" else "codex"
    _cli_reason = cli_unavailable_reason(_cli_binary)
    if _cli_reason is not None:
        pytest.skip(
            f"{sub_agent} sub-agent requires a runnable {_cli_binary!r} CLI; {_cli_reason}"
        )

    token = _llm_token(request)
    env = _clean_env(request.config.getoption("--profile") or _PROFILE)
    env["OPENAI_API_KEY"] = token

    # Paths to the developer's native-CLI config we may toggle to PROMPTING.
    # Both CLIs read their on-disk config at launch; a dev box configured for
    # unattended use (codex ``approval_policy = "never"`` / claude ``Bash(*)``
    # allow-list) would auto-run the command and never prompt. We flip the
    # relevant config to a prompting policy for the run and ALWAYS restore it
    # in ``finally`` (the toggle is the first statement in the ``try`` so a
    # failure anywhere after it still restores). Safe because this whole test
    # is opt-in (OMNIGENT_E2E_SUBAGENT_ELICIT=1), never CI.
    codex_config = (
        Path(os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")) / "config.toml"
    )
    claude_settings = Path.home() / ".claude" / "settings.json"
    codex_config_original: str | None = None
    claude_settings_original: str | None = None

    # Per-scenario task for the worker:
    # - "command": take an action the worker's prompting permission mode gates
    #   (claude-native --permission-mode default prompts on Bash; codex's
    #   default approval policy prompts before a file write).
    # - "ask_user_question": call Claude's built-in AskUserQuestion tool, which
    #   raises an interactive-question elicitation regardless of permission mode.
    if scenario == "ask_user_question":
        task = (
            "Use the AskUserQuestion tool to ask me exactly one multiple-choice "
            "question (with 2-3 options) about my color preference. Do not answer "
            "it yourself."
        )
    else:
        task = (
            f"Run this shell command in your terminal and report the output: "
            f"echo forwarding-probe > /tmp/elicit_probe_{sub_agent}.txt"
        )
    prompt = (
        f"Immediately call sys_session_send with agent='{sub_agent}', "
        f"title='elicit-probe', and args set to exactly: '{task}'. Do this as "
        f"your very first action — do not analyze, plan, or ask questions "
        f"first, and do not do the task yourself. After the sub-agent reports "
        f"back via your inbox, summarize its result in one sentence."
    )
    run_log = Path(local_server.replace("http://", "").replace(":", "_"))
    log_path = f"/tmp/subagent_elicit_run_{sub_agent}_{scenario}_{run_log}.log"

    parent_id: str | None = None
    child_id: str | None = None
    elicitation_id: str | None = None
    log = None
    run_proc: subprocess.Popen[str] | None = None
    try:
        # Toggle the prompting config FIRST so the ``finally`` restore covers
        # every subsequent failure (Popen error, assert, timeout, exception).
        # AskUserQuestion fires regardless of permission mode, so the "command"
        # scenario is the only one that needs the prompting toggle.
        if scenario == "command":
            if sub_agent == "codex":
                codex_config_original = _force_codex_prompting(codex_config)
            else:
                claude_settings_original = _force_claude_prompting(claude_settings)

        baseline = {
            s.get("id")
            for s in _get(local_server, "/v1/sessions?order=desc&limit=60", token).get("data", [])
        }
        log = open(log_path, "w")  # noqa: SIM115 — lives for the run subprocess lifetime
        run_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "omnigent",
                "run",
                str(agent_dir),
                "--server",
                local_server,
                "--model",
                _BRAIN_MODEL,
                "-p",
                prompt,
            ],
            cwd=str(_REPO),
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        # The phases below poll a live, out-of-process server (and a detached
        # native-worker tree) over HTTP, driven by a real LLM. There is no
        # in-process event to await across that boundary, so each phase is a
        # bounded ``time.sleep``-paced poll on a monotonic deadline — the
        # standard shape for this repo's real-server e2e tests
        # (cf. ``test_polly_e2e.py``).

        # 1) Discover the parent session this run created (bounded — a brain
        #    that never connects fails fast with the run log rather than
        #    polling the full turn budget).
        parent_deadline = time.monotonic() + _PARENT_DISCOVER_TIMEOUT_SEC
        while time.monotonic() < parent_deadline and parent_id is None:
            for s in _get(local_server, "/v1/sessions?order=desc&limit=60", token).get("data", []):
                if s.get("id") not in baseline and (
                    (s.get("agent_name") or "").lower() == "ask-mode-supervisor"
                ):
                    parent_id = s["id"]
                    break
            if parent_id is None:
                time.sleep(_POLL_INTERVAL_SEC)
        assert parent_id is not None, (
            f"run never created an ask-mode-supervisor session within "
            f"{_PARENT_DISCOVER_TIMEOUT_SEC}s; log tail:\n"
            f"{Path(log_path).read_text()[-2000:]}"
        )

        # 2) Wait for the worker child to spawn (bounded, fail-fast). A brain
        #    that ends its turn without dispatching — or stalls under load —
        #    fails here with the run log instead of hanging on step 3.
        child_deadline = time.monotonic() + _CHILD_SPAWN_TIMEOUT_SEC
        while time.monotonic() < child_deadline and child_id is None:
            for c in _get(
                local_server, f"/v1/sessions/{parent_id}/child_sessions?limit=50", token
            ).get("data", []):
                title = c.get("title") or ""
                vendor = c.get("tool") or (title.split(":", 1)[0] if ":" in title else "")
                if vendor == sub_agent:
                    child_id = c["id"]
                    break
            if child_id is None:
                time.sleep(_POLL_INTERVAL_SEC)
        assert child_id is not None, (
            f"the orchestrator never spawned a `{sub_agent}` child within "
            f"{_CHILD_SPAWN_TIMEOUT_SEC}s (brain may have stalled or refused to "
            f"delegate). run log tail:\n{Path(log_path).read_text()[-2000:]}"
        )

        # 3) Wait for the child's approval to surface on the PARENT snapshot,
        #    stamped with target_session_id. The worker raises it once it
        #    decides to run the gated command (the run process may have already
        #    exited — the native worker keeps running detached).
        targeted_prompt: dict | None = None
        elicit_deadline = time.monotonic() + _ELICIT_TIMEOUT_SEC
        while time.monotonic() < elicit_deadline and targeted_prompt is None:
            parent_snap = _get(local_server, f"/v1/sessions/{parent_id}", token)
            for prompt_event in parent_snap.get("pending_elicitations", []) or []:
                params = prompt_event.get("params") or {}
                if params.get("target_session_id") == child_id:
                    targeted_prompt = prompt_event
                    break
            if targeted_prompt is None:
                time.sleep(_POLL_INTERVAL_SEC)
        if targeted_prompt is None:
            # Dump child + parent state to distinguish "worker never prompted"
            # (permission mode / safe command auto-approved) from "prompted but
            # the mirror dropped it".
            child_snap = _get(local_server, f"/v1/sessions/{child_id}", token)
            child_items = _get(
                local_server, f"/v1/sessions/{child_id}/items?order=asc&limit=50", token
            ).get("data", [])
            child_pending = child_snap.get("pending_elicitations", [])
            last_msgs = [
                "".join(
                    b.get("text", "")
                    for b in (it.get("data", {}).get("content") or it.get("content") or [])
                    if isinstance(b, dict)
                )
                for it in child_items
                if it.get("type") == "message"
            ]
            raise AssertionError(
                "the worker's approval never surfaced on the PARENT snapshot with "
                f"target_session_id={child_id!r}.\n"
                f"child status={child_snap.get('status')!r} busy={child_snap.get('busy')!r}\n"
                f"child OWN pending_elicitations={child_pending!r}\n"
                f"child last messages={last_msgs[-4:]!r}\n"
                f"run log tail:\n{Path(log_path).read_text()[-2000:]}"
            )
        elicitation_id = targeted_prompt.get("elicitation_id")
        assert isinstance(elicitation_id, str) and elicitation_id, targeted_prompt
        assert targeted_prompt["params"]["target_session_id"] == child_id

        # 4) Resolve via the CHILD session id (what the parent UI does with
        #    the mirrored target_session_id) and confirm the child's parked
        #    prompt clears — proving the verdict reached the parked Future.
        status, text = _post(
            local_server,
            f"/v1/sessions/{child_id}/elicitations/{elicitation_id}/resolve",
            {"action": "accept"},
            token,
        )
        assert status == 202, f"resolve via child failed: {status} {text}"

        cleared = False
        clear_deadline = time.monotonic() + 60.0
        while time.monotonic() < clear_deadline:
            child_snap = _get(local_server, f"/v1/sessions/{child_id}", token)
            pending_ids = {
                p.get("elicitation_id") for p in child_snap.get("pending_elicitations", []) or []
            }
            if elicitation_id not in pending_ids:
                cleared = True
                break
            time.sleep(2.0)
        assert cleared, (
            f"child {child_id!r} still shows elicitation {elicitation_id!r} after the "
            f"verdict — the resolve did not reach the child's parked Future"
        )
    finally:
        # Restore the developer's native-CLI config FIRST (before any slow
        # teardown) to bound the prompting-policy window as tightly as
        # possible for any concurrent agents. ``_restore_file`` is a no-op
        # when the toggle never ran (original is ``None``).
        _restore_file(codex_config, codex_config_original)
        _restore_file(claude_settings, claude_settings_original)
        # ``omnigent run`` exits after the parent's one-shot turn, but its
        # detached daemon/worker tree (and any native tmux pane) keeps
        # running, so always sweep leaked workers by conv id regardless of
        # whether the run process itself is still alive.
        if run_proc is not None:
            _kill_tree(run_proc.pid, {parent_id or "", child_id or ""})
        _kill_native_terminals({parent_id or "", child_id or ""})
        if log is not None:
            log.close()
