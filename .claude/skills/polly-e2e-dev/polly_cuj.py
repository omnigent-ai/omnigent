#!/usr/bin/env python3
"""Deterministic mock-LLM CUJ driver for the polly coding orchestrator.

This is the *reproducible loop* half of the ``polly-e2e-dev`` skill. It boots a
throwaway local Omnigent server from the current checkout (which carries
``omnigent.inner.nessie.policies`` — the module polly's guardrails resolve) plus
the repo's mock-LLM server, rewrites the ``examples/polly`` bundle to the
``openai-agents`` harness wired to the mock, then drives ``omnigent run`` turns
where the brain is *scripted* (text or tool calls). Because the brain is mocked,
the loop tests the **substrate / mechanics** of each critical user journey —
tool dispatch, the three runner-side guardrails, session persistence — not
polly's live judgment (that is the live recipe in ``SKILL.md``).

Each scenario prints one machine-readable ``SUMMARY {json}`` line and the driver
exits non-zero if any check failed (a ``skipped`` check never fails the run).

Run it (use the repo venv so subprocesses import the checkout, not a stale wheel)::

    .venv/bin/python .claude/skills/polly-e2e-dev/polly_cuj.py --scenario all
    .venv/bin/python .claude/skills/polly-e2e-dev/polly_cuj.py --list-scenarios
    .venv/bin/python .claude/skills/polly-e2e-dev/polly_cuj.py --scenario guardrail_purpose --keep

No credentials or network egress are required — the mock LLM stands in for every
provider. See ``SKILL.md`` for the live (real claude/codex/pi) recipe.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# ── Paths & constants ────────────────────────────────────────────────────────

# polly_cuj.py -> polly-e2e-dev -> skills -> .claude -> <repo root>
_REPO_DEFAULT = Path(__file__).resolve().parents[3]
_MOCK_SERVER_REL = Path("tests") / "server" / "integration" / "mock_llm_server.py"

_SERVER_BOOT_TIMEOUT_S = 90.0
_MOCK_BOOT_TIMEOUT_S = 15.0
_RUN_TIMEOUT_S = 180
_MIN_REPLY_CHARS = 12

# The mock routes /v1/responses by the request's ``model`` field; the polly
# brain spec is rewritten to send this exact key so we own its response queue.
_BRAIN_MODEL = "mock-polly-brain"

# Native harnesses that need a CLI binary on PATH; rewritten to ``openai-agents``
# (SDK-based, no binary) for the one scenario that actually dispatches workers.
_NATIVE_HARNESSES = frozenset(
    {
        "claude-native",
        "native-claude",
        "codex-native",
        "native-codex",
        "pi",
        "pi-native",
        "native-pi",
        "cursor-native",
        "native-cursor",
    }
)


# ── HTTP helpers (stdlib only) ───────────────────────────────────────────────


def _free_port() -> int:
    """Reserve an ephemeral loopback port."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _get_json(url: str, timeout: float = 10.0) -> object:
    """GET *url* and parse JSON."""
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _post_json(url: str, payload: dict, timeout: float = 10.0) -> object:
    """POST *payload* as JSON to *url* and parse the JSON reply."""
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={"content-type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _wait_for_http(url: str, deadline: float) -> None:
    """Block until *url* answers HTTP 200, or raise past *deadline*."""
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                if resp.status == 200:
                    return
        except (urllib.error.URLError, OSError) as err:
            last = err
        time.sleep(0.5)
    raise TimeoutError(f"{url} never became healthy: {last}")


# ── Mock LLM controls ────────────────────────────────────────────────────────


def _mock_reset(mock_url: str) -> None:
    _post_json(f"{mock_url}/mock/reset", {})


def _mock_configure(mock_url: str, responses: list[dict], *, key: str = "default") -> None:
    """Load a keyed response queue on the mock server."""
    _post_json(f"{mock_url}/mock/configure", {"key": key, "responses": responses})


def _mock_set_fallback(mock_url: str, key: str, text: str) -> None:
    """Set a non-resettable fallback response for *key* (drains stray child calls)."""
    _post_json(f"{mock_url}/mock/set_fallback", {"key": key, "text": text})


def _sys_session_send_call(
    agent: str, title: str, child_args: object, *, call_id: str = "call_1"
) -> dict:
    """Build a ``tool_calls`` entry for ``sys_session_send``.

    *child_args* may be a string (bare input) or a dict
    (``{"input": ..., "purpose": ...}``) — the latter is what
    ``headless_subagent_purpose_guard`` requires.
    """
    return {
        "call_id": call_id,
        "name": "sys_session_send",
        "arguments": json.dumps({"agent": agent, "title": title, "args": child_args}),
    }


def _sys_os_shell_call(command: str, *, call_id: str = "call_sh") -> dict:
    """Build a ``tool_calls`` entry for ``sys_os_shell``."""
    return {
        "call_id": call_id,
        "name": "sys_os_shell",
        "arguments": json.dumps({"command": command}),
    }


# ── Bundle rewrite (inlined from tests/e2e/test_polly_e2e.py) ─────────────────


def _mock_polly_bundle(tmp: Path, mock_url: str, *, rewrite_subagents: bool = False) -> Path:
    """Copy ``examples/polly`` into *tmp* and rewrite it to use the mock LLM.

    Switches the brain harness from ``claude-sdk`` to ``openai-agents``, pins the
    deterministic model key, and bakes ``auth`` + ``connection`` blocks at the
    mock so neither the brain nor the runner-side cost judge reaches a real
    provider. When *rewrite_subagents* is set, native sub-agent harnesses become
    ``openai-agents`` too (so a dispatch doesn't need claude/codex/pi on PATH).
    """
    src = (_repo() / "examples" / "polly").resolve()
    dst = tmp / "polly"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, symlinks=False)

    cfg_path = dst / "config.yaml"
    spec = yaml.safe_load(cfg_path.read_text())
    executor = spec.setdefault("executor", {})
    exec_cfg = executor.pop("config", {}) or {}
    exec_cfg["harness"] = "openai-agents"
    executor["config"] = exec_cfg
    executor["model"] = _BRAIN_MODEL
    executor["auth"] = {
        "type": "api_key",
        "api_key": "mock-key",
        "base_url": f"{mock_url}/v1",
    }
    executor["connection"] = {"base_url": f"{mock_url}/v1", "api_key": "mock-key"}
    cfg_path.write_text(yaml.safe_dump(spec, sort_keys=False))

    if rewrite_subagents:
        agents_dir = dst / "agents"
        for sub_cfg in agents_dir.glob("*/config.yaml") if agents_dir.is_dir() else []:
            sub = yaml.safe_load(sub_cfg.read_text())
            sub_exec = sub.get("executor") or {}
            sub_inner = sub_exec.get("config") or {}
            harness = sub_inner.get("harness") or sub_exec.get("type") or ""
            if harness in _NATIVE_HARNESSES:
                sub_inner["harness"] = "openai-agents"
                sub_exec["config"] = sub_inner
                sub["executor"] = sub_exec
                sub_cfg.write_text(yaml.safe_dump(sub, sort_keys=False))
    return dst


# ── Subprocess env ───────────────────────────────────────────────────────────

_CREDENTIAL_VARS = (
    "DATABRICKS_TOKEN",
    "DATABRICKS_HOST",
    "DATABRICKS_CLIENT_ID",
    "DATABRICKS_CLIENT_SECRET",
    "DATABRICKS_CONFIG_PROFILE",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE",
    "CLAUDECODE",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "CODEX",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GITHUB_TOKEN",
    "GH_TOKEN",
)


def _run_env(mock_url: str) -> dict[str, str]:
    """Env for the ``omnigent run`` subprocess: isolated config, mock provider."""
    env = dict(os.environ)
    env["OMNIGENT_SKIP_ONBOARD"] = "1"
    env["OMNIGENT_NO_UPDATE_CHECK"] = "1"
    config_home = Path(tempfile.mkdtemp(prefix="polly-cuj-config-"))
    (config_home / "config.yaml").write_text("", encoding="utf-8")
    env["OMNIGENT_CONFIG_HOME"] = str(config_home)
    for stale in _CREDENTIAL_VARS:
        env.pop(stale, None)
    env["OPENAI_BASE_URL"] = f"{mock_url}/v1"
    env["OPENAI_API_KEY"] = "mock-key"
    return env


# ── Server lifecycle ─────────────────────────────────────────────────────────

_REPO_HOLDER: dict[str, Path] = {}


def _repo() -> Path:
    """The repo root the driver operates on (set in :func:`main`)."""
    return _REPO_HOLDER["repo"]


def _runner_pids() -> set[int]:
    """PIDs of runner/harness subprocesses spawned by *this* interpreter.

    Scoped to ``sys.executable`` so a sweep can never touch another worktree's
    server or a real ``omnigent`` session running under a different venv.
    """
    pids: set[int] = set()
    for module in (
        "omnigent.host._daemon_entry",
        "omnigent.runner._entry",
        "omnigent.runtime.harnesses._runner",
    ):
        try:
            out = subprocess.run(
                ["pgrep", "-f", f"{sys.executable} -m {module}"],
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            return pids  # no pgrep — skip the sweep rather than guess
        pids |= {int(x) for x in out.stdout.split() if x.isdigit()}
    return pids


def _kill(pids: set[int]) -> None:
    """SIGTERM then SIGKILL a set of PIDs, tolerating already-dead ones."""
    for pid in pids:
        with suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGTERM)
    if not pids:
        return
    time.sleep(2)
    for pid in pids:
        with suppress(ProcessLookupError, PermissionError):
            os.kill(pid, signal.SIGKILL)


@dataclass
class _Servers:
    """Handles for the mock LLM + local Omnigent server."""

    mock_url: str
    server_url: str
    _mock_proc: subprocess.Popen
    _server_proc: subprocess.Popen
    _logdir: Path


@contextmanager
def _servers(tmp: Path) -> Iterator[_Servers]:
    """Start the mock LLM and a throwaway local Omnigent server; reap both.

    ``omni run`` turns make the server spawn per-conversation runner/harness
    subprocesses that a plain server SIGTERM does not reap. We snapshot runner
    PIDs before boot and, on teardown, sweep any that appeared during the run
    (scoped to this interpreter) so nothing leaks.
    """
    repo = _repo()
    logdir = tmp / "logs"
    logdir.mkdir(parents=True, exist_ok=True)
    baseline_pids = _runner_pids()

    mock_port = _free_port()
    mock_url = f"http://127.0.0.1:{mock_port}"
    mock_log = open(logdir / "mock_llm.log", "w")  # noqa: SIM115
    mock_proc = subprocess.Popen(
        [sys.executable, str(repo / _MOCK_SERVER_REL), str(mock_port)],
        env={**os.environ, "PYTHONPATH": str(repo)},
        stdout=mock_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    server_port = _free_port()
    server_url = f"http://127.0.0.1:{server_port}"
    server_log = open(logdir / "server.log", "w")  # noqa: SIM115
    server_proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "omnigent",
            "server",
            "--host",
            "127.0.0.1",
            "--port",
            str(server_port),
            "--database-uri",
            f"sqlite:///{tmp / 'polly_cuj.db'}",
            "--artifact-location",
            str(tmp / "artifacts"),
        ],
        cwd=str(repo),
        env={**os.environ, "OMNIGENT_SKIP_ONBOARD": "1", "OMNIGENT_NO_UPDATE_CHECK": "1"},
        stdout=server_log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    try:
        _wait_for_http(f"{mock_url}/stats", time.monotonic() + _MOCK_BOOT_TIMEOUT_S)
        _wait_for_http(f"{server_url}/", time.monotonic() + _SERVER_BOOT_TIMEOUT_S)
        yield _Servers(mock_url, server_url, mock_proc, server_proc, logdir)
    finally:
        for proc in (server_proc, mock_proc):
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
        # Reap runner/harness subprocesses that appeared during this run.
        _kill(_runner_pids() - baseline_pids)
        mock_log.close()
        server_log.close()


def _run_polly(
    bundle: Path, server_url: str, prompt: str, mock_url: str
) -> subprocess.CompletedProcess:
    """``omnigent run <bundle> --server <url> -p <prompt>`` against the mock."""
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "omnigent",
            "run",
            str(bundle),
            "--server",
            server_url,
            "-p",
            prompt,
        ],
        cwd=str(_repo()),
        env=_run_env(mock_url),
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_S,
    )


# ── Session observation ──────────────────────────────────────────────────────


def _latest_session_id(server_url: str) -> str | None:
    """Newest top-level session id, or None."""
    try:
        page = _get_json(f"{server_url}/v1/sessions?kind=default&order=desc&limit=5")
    except (urllib.error.URLError, OSError):
        return None
    data = page.get("data", []) if isinstance(page, dict) else []
    for row in data:
        for key in ("id", "session_id", "conversation_id"):
            if isinstance(row, dict) and isinstance(row.get(key), str):
                return row[key]
    return None


def _session_items(server_url: str, session_id: str) -> list[dict]:
    """All items in a session, chronological."""
    page = _get_json(f"{server_url}/v1/sessions/{session_id}/items?order=asc&limit=300")
    data = page.get("data", []) if isinstance(page, dict) else []
    return [item for item in data if isinstance(item, dict)]


def _tool_outputs(items: list[dict]) -> list[str]:
    """Every ``function_call_output`` payload, stringified."""
    outs: list[str] = []
    for item in items:
        if item.get("type") == "function_call_output":
            out = item.get("output")
            outs.append(out if isinstance(out, str) else json.dumps(out))
    return outs


def _assistant_text(items: list[dict]) -> str:
    """Concatenate assistant message text blocks."""
    parts: list[str] = []
    for item in items:
        if item.get("type") == "message" and item.get("role") == "assistant":
            for block in item.get("content", []) or []:
                if isinstance(block, dict) and block.get("text"):
                    parts.append(str(block["text"]))
    return "\n".join(parts)


# ── Scenario framework ───────────────────────────────────────────────────────


@dataclass
class Result:
    """One scenario's outcome."""

    scenario: str
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append((name, ok, detail))

    def skip(self, name: str, detail: str) -> None:
        # A skip is recorded as a note + a passing "skipped" marker so it never
        # fails the run but is visible in the SUMMARY.
        self.notes.append(f"SKIP {name}: {detail}")

    @property
    def ok(self) -> bool:
        return all(ok for _, ok, _ in self.checks)

    def summary(self) -> dict:
        return {
            "scenario": self.scenario,
            "ok": self.ok,
            "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in self.checks],
            "notes": self.notes,
        }


@dataclass
class Ctx:
    """Shared scenario context."""

    servers: _Servers
    tmp: Path


def _add_exit_check(res: Result, proc: subprocess.CompletedProcess) -> None:
    """Record the standard exit-0 check, keeping trailing stderr for context."""
    detail = f"rc={proc.returncode}; stderr={proc.stderr[-300:]}"
    res.add("exit_zero", proc.returncode == 0, detail)


# ── Scenarios ────────────────────────────────────────────────────────────────


def scenario_boot(ctx: Ctx) -> Result:
    """Bundle loads, server-side policies resolve, a turn streams back."""
    res = Result("boot")
    s = ctx.servers
    _mock_reset(s.mock_url)
    _mock_configure(
        s.mock_url,
        [{"text": "I am polly: I plan a coding task and delegate it to sub-agents."}],
        key=_BRAIN_MODEL,
    )
    bundle = _mock_polly_bundle(ctx.tmp / "boot", s.mock_url)
    proc = _run_polly(bundle, s.server_url, "In one sentence, what are you?", s.mock_url)
    _add_exit_check(res, proc)
    reply = proc.stdout.strip()
    res.add("non_empty_reply", len(reply) >= _MIN_REPLY_CHARS, f"{len(reply)} chars")
    return res


def scenario_tool_dispatch(ctx: Ctx) -> Result:
    """Brain emits a benign ``sys_os_shell``; it runs and touches disk."""
    res = Result("tool_dispatch")
    s = ctx.servers
    sentinel = ctx.tmp / "tool_dispatch_sentinel.txt"
    sentinel.unlink(missing_ok=True)
    token = "polly-tool-dispatch-ok"
    _mock_reset(s.mock_url)
    _mock_configure(
        s.mock_url,
        [
            {"tool_calls": [_sys_os_shell_call(f"printf '{token}' > {sentinel}")]},
            {"text": "Wrote the sentinel file."},
        ],
        key=_BRAIN_MODEL,
    )
    bundle = _mock_polly_bundle(ctx.tmp / "tool", s.mock_url)
    proc = _run_polly(bundle, s.server_url, "Write the sentinel via shell.", s.mock_url)
    _add_exit_check(res, proc)
    wrote = sentinel.exists() and token in sentinel.read_text()
    res.add("shell_touched_disk", wrote, f"sentinel={sentinel} exists={sentinel.exists()}")
    return res


# Common marker both deny formats share — ``[Denied by policy: <name>] {json}``
# for SDK function tools and ``{"error": "Denied by policy: <reason>"}`` for the
# bridged ``sys_*`` tools the orchestrator uses.
_DENY_MARKER = "Denied by policy:"


def _guardrail_scenario(
    ctx: Ctx,
    name: str,
    responses: list[dict],
    *,
    check_name: str,
    expect: str,
    prompt: str,
    rewrite_subagents: bool = False,
) -> Result:
    """Script the brain into a tool call the policy must refuse, then prove it.

    A pass requires BOTH the generic deny marker and *expect* (a reason fragment
    unique to the target policy) in the tool outputs — so the check proves the
    *right* guardrail fired, not merely that something was refused.
    """
    res = Result(name)
    s = ctx.servers
    _mock_reset(s.mock_url)
    # Drain any stray sub-agent child LLM calls with a trivial fallback.
    _mock_set_fallback(s.mock_url, "default", "ok")
    _mock_configure(s.mock_url, responses, key=_BRAIN_MODEL)
    bundle = _mock_polly_bundle(ctx.tmp / name, s.mock_url, rewrite_subagents=rewrite_subagents)
    proc = _run_polly(bundle, s.server_url, prompt, s.mock_url)
    _add_exit_check(res, proc)

    sid = _latest_session_id(s.server_url)
    if sid is None:
        res.add(check_name, False, "no session found to inspect")
        return res
    outs = _tool_outputs(_session_items(s.server_url, sid))
    combined = "\n".join(outs)
    denied = _DENY_MARKER in combined and expect in combined
    detail = f"denied={denied} ({len(outs)} tool outputs)"
    if not denied:
        detail += f" | outputs={combined[:600]!r}"
    res.add(check_name, denied, detail)
    res.notes.append(f"session={sid}")
    return res


def scenario_guardrail_purpose(ctx: Ctx) -> Result:
    """A ``sys_session_send`` with no ``args.purpose`` is denied at the gate."""
    return _guardrail_scenario(
        ctx,
        "guardrail_purpose",
        responses=[
            # args is a bare string -> no object purpose -> DENY.
            {"tool_calls": [_sys_session_send_call("pi", "no-purpose", "investigate the bug")]},
            {"text": "acknowledged the denial"},
        ],
        check_name="headless_subagent_purpose_guard_denied",
        expect="must declare what kind of work it is",
        prompt="Dispatch a worker (the driver scripts a malformed dispatch).",
    )


def scenario_guardrail_blast_radius(ctx: Ctx) -> Result:
    """A catastrophic ``sys_os_shell`` (rm -rf /etc) is denied."""
    return _guardrail_scenario(
        ctx,
        "guardrail_blast_radius",
        responses=[
            # Force-push is catastrophic (DENY) but harmless if it ever runs:
            # no remote/auth in the sandbox, so a missed gate fails closed.
            {"tool_calls": [_sys_os_shell_call("git push --force origin main")]},
            {"text": "acknowledged the denial"},
        ],
        check_name="blast_radius_denied",
        expect="blast-radius policy",
        prompt="Run a destructive command (the driver scripts it).",
    )


def scenario_fanout_dispatch(ctx: Ctx) -> Result:
    """Six-wide fan-out: many dispatch handles are created in one turn.

    Hard check: the fan-out *substrate* works — emitting N ``sys_session_send``
    calls in one response creates N sub-agent dispatch handles. The
    ``spawn_bounds`` per-turn cap (max 5) is reported as a non-failing
    *finding*: it is a stateful counter, but the server rebuilds the policy
    engine per ``tools/call`` (``_build_policy_engine_from_spec``), so the
    counter resets each call and the cap does not trip in this local
    server-side path. See SKILL.md "Known sharp edges". Verify the cap live.
    """
    res = Result("fanout_dispatch")
    s = ctx.servers
    _mock_reset(s.mock_url)
    _mock_set_fallback(s.mock_url, "default", "ok")
    calls = [
        _sys_session_send_call(
            "pi",
            f"probe-{i}",
            {"input": "noop", "purpose": "explore"},
            call_id=f"call_{i}",
        )
        for i in range(1, 7)
    ]
    _mock_configure(
        s.mock_url,
        [{"tool_calls": calls}, {"text": "dispatched a wave"}],
        key=_BRAIN_MODEL,
    )
    bundle = _mock_polly_bundle(ctx.tmp / "fanout", s.mock_url, rewrite_subagents=True)
    proc = _run_polly(bundle, s.server_url, "Fan out a wave of workers.", s.mock_url)
    _add_exit_check(res, proc)

    sid = _latest_session_id(s.server_url)
    if sid is None:
        res.add("fanout_dispatched", False, "no session found to inspect")
        return res
    outs = _tool_outputs(_session_items(s.server_url, sid))
    combined = "\n".join(outs)
    handles = sum(1 for o in outs if '"kind": "sub_agent"' in o or '"status": "launching"' in o)
    res.add("fanout_dispatched", handles >= 2, f"{handles} handles / {len(outs)} outputs")
    cap_fired = "worker dispatches this turn" in combined
    res.notes.append(
        f"finding: spawn_bounds per-turn cap fired={cap_fired} "
        "(expected False in this server-side path; verify the cap live)"
    )
    res.notes.append(f"session={sid}")
    return res


_SCENARIOS: dict[str, Callable[[Ctx], Result]] = {
    "boot": scenario_boot,
    "tool_dispatch": scenario_tool_dispatch,
    "guardrail_purpose": scenario_guardrail_purpose,
    "guardrail_blast_radius": scenario_guardrail_blast_radius,
    "fanout_dispatch": scenario_fanout_dispatch,
}


# ── Entrypoint ───────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        default="all",
        help="Scenario to run, or 'all' (default). See --list-scenarios.",
    )
    parser.add_argument("--list-scenarios", action="store_true", help="Print scenarios and exit.")
    parser.add_argument("--repo", type=Path, default=_REPO_DEFAULT, help="Repo root to test.")
    parser.add_argument("--keep", action="store_true", help="Keep the sandbox temp dir.")
    args = parser.parse_args(argv)

    if args.list_scenarios:
        for name in _SCENARIOS:
            print(name)
        return 0

    _REPO_HOLDER["repo"] = args.repo.resolve()
    polly_dir = _repo() / "examples" / "polly" / "config.yaml"
    if not polly_dir.exists():
        print(f"error: {polly_dir} not found — is --repo correct?", file=sys.stderr)
        return 2

    if args.scenario == "all":
        chosen = list(_SCENARIOS)
    elif args.scenario in _SCENARIOS:
        chosen = [args.scenario]
    else:
        print(f"error: unknown scenario {args.scenario!r}; try --list-scenarios", file=sys.stderr)
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="polly-cuj-"))
    all_ok = True
    try:
        with _servers(tmp) as servers:
            ctx = Ctx(servers=servers, tmp=tmp)
            for name in chosen:
                try:
                    res = _SCENARIOS[name](ctx)
                except Exception as exc:  # noqa: BLE001 — report, don't crash the suite
                    res = Result(name)
                    res.add("ran", False, f"{type(exc).__name__}: {exc}")
                all_ok = all_ok and res.ok
                print("SUMMARY " + json.dumps(res.summary()))
    finally:
        if args.keep:
            print(f"[kept sandbox] {tmp}", file=sys.stderr)
        else:
            shutil.rmtree(tmp, ignore_errors=True)

    print("SUMMARY " + json.dumps({"scenario": "ALL", "ok": all_ok, "ran": chosen}))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
