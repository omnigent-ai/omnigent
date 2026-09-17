r"""E2E regression test: a persistent runner fails closed for the whole session
after the Homebrew ``databricks`` CLI is upgraded underneath it.

A long-lived runner using a ``databricks-cli`` auth profile holds one
Databricks SDK client for the session's lifetime.
``DatabricksCliTokenSource.__init__`` resolves the ``databricks`` executable
**once**, via ``PATH`` lookup + ``.resolve()``, to the
**versioned Homebrew realpath** (e.g.
``/opt/homebrew/Cellar/databricks/1.15.0/bin/databricks``) and bakes that
absolute path into ``self._cmd``. ``refresh()`` re-runs that exact command for
the life of the token source.

A ``brew upgrade databricks`` (1.15.0 -> 1.16.1) removes the old Cellar
directory, so every subsequent token refresh throws ``FileNotFoundError``. In
the runner that surfaces as ``_RunnerDatabricksAuth.auth_flow`` raising
``httpx.RequestError("Databricks token refresh returned no token")`` on the
relay's ``POST /policies/evaluate``; the claude-native ``UserPromptSubmit``
policy hook exhausts its retry budget and latches **fail-closed**, so every
prompt in the already-running session is blocked with::

    UserPromptSubmit operation blocked by hook:
    Omnigent policy evaluation unavailable (could not reach or authenticate to
    the Omnigent server); failing closed for this request. Detail: Databricks
    token refresh returned no token

A newly created session is unaffected (a fresh runner rebuilds the token source
and resolves the current binary via the stable ``bin/databricks`` symlink).

## What this test drives (the real journey, not a code-path poke)

It stands up the REAL components a claude-native prompt submit flows through:

* a Homebrew-like ``databricks`` CLI layout (versioned Cellar dir + stable
  ``bin`` symlink) and a ``databricks-cli`` auth profile pointed at it,
* the runner's own auth factory (``_make_auth_token_factory``) wrapped in the
  production ``_RunnerDatabricksAuth`` httpx auth and an ``open_server_client``
  policy client -- exactly how ``omnigent/runner/_entry.py`` builds them,
* a real ``omnigent server`` subprocess (the policy authority the relay proxies
  ``/policies/evaluate`` to),
* the production tool relay started as the runner starts it
  (``prepare_bridge_dir`` + ``start_tool_relay(policy_client=..., session_id=...)``),
* the REAL ``UserPromptSubmit`` hook subprocess Claude Code spawns on every
  prompt submit (``python -m omnigent.harnesses.claude_native.hook
  evaluate-policy``), whose ``decision`` output the Claude Code TUI renders.

## Polarity (fail now -> pass after the fix)

One long-lived auth factory + relay serve both prompts, modelling the
session-long runner. The first prompt (before the upgrade) is a control: it must
pass, proving the chain is healthy. Then the CLI is upgraded (old Cellar dir
removed, stable symlink repointed) and the cached token is allowed to expire, so
the next refresh re-runs the baked-and-now-missing versioned path.

The test asserts the DESIRED behaviour: the second prompt must **not** be
fail-closed-blocked merely because the baked CLI path vanished under a live
runner. On the buggy build the baked path 404s -> the refresh raises -> the hook
blocks -> **this test FAILS, reproducing the bug**. Once the runner re-resolves
the CLI path (or rebuilds the SDK client) on a refresh ``FileNotFoundError`` --
the ticket's suggested fix -- the next refresh finds the current binary via the
stable symlink and the prompt proceeds, so the test passes. A final control
proves a *fresh* factory resolves the upgraded binary fine, isolating the baked
versioned path (not the credential) as the cause.

Run::

    .venv/bin/python -m pytest \
        tests/e2e/test_databricks_cli_upgrade_token_refresh.py -v
"""

from __future__ import annotations

import io
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import threading
import time
from pathlib import Path

import httpx
import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Every HTTP call in this test targets loopback; CI shells often carry an egress
# proxy, so bypass proxy autodetection for the test's own client entirely.
_http = httpx.Client(trust_env=False)

_PYTHONPATH = os.pathsep.join(
    [
        str(_REPO_ROOT),
        str(_REPO_ROOT / "sdks" / "python-client"),
        str(_REPO_ROOT / "sdks" / "ui"),
        os.environ.get("PYTHONPATH", ""),
    ]
)

_HEALTH_TIMEOUT_S = 120.0
_POLL_S = 0.5
_HOOK_TIMEOUT_S = 180.0

# The prior Claude session id the hook stamps onto its evaluation request.
_EXTERNAL_SID = "11111111-2222-4333-8444-555566667777"

# Short-lived fake tokens so the cached one expires within the test. The SDK
# treats a token as expired once its lifespan is negative, forcing a blocking
# refresh that re-runs the baked CLI command.
_TOKEN_TTL_S = 12
_EXPIRY_WAIT_S = _TOKEN_TTL_S + 6

# The reason substring the fail-closed hook surfaces for this specific defect.
_TOKEN_REFRESH_FAILURE = "Databricks token refresh returned no token"


def _find_free_port() -> int:
    """Grab an ephemeral loopback port."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _localhost_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Subprocess env with worktree imports and no ambient proxy in the way."""
    env = {
        **os.environ,
        "PYTHONPATH": _PYTHONPATH,
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(name, None)
    env.update(extra or {})
    return env


def _terminate(proc: subprocess.Popen[bytes] | None) -> None:
    """Best-effort SIGTERM -> SIGKILL teardown for a spawned process."""
    if proc is None or proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _wait_http_ok(url: str, deadline: float) -> None:
    """Poll *url* until it returns 200 or *deadline* (monotonic) passes."""
    last = "not polled"
    while time.monotonic() < deadline:
        try:
            if _http.get(url, timeout=2.0).status_code == 200:
                return
        except httpx.HTTPError as exc:
            last = repr(exc)
        time.sleep(_POLL_S)
    raise AssertionError(f"server never became healthy at {url}; last: {last}")


# ---------------------------------------------------------------------------
# The Homebrew-like databricks CLI layout (Cellar/<version> + stable symlink).
# ---------------------------------------------------------------------------
def _install_fake_cli(brew: Path, version: str) -> Path:
    """Install a fake >1MB ``databricks`` CLI at ``Cellar/<version>/bin``.

    The SDK's ``_find_executable`` rejects binaries under 1MB (its heuristic for
    the modern single-binary CLI), so the fake must exceed that. It prints a
    short-lived OAuth token JSON in the shape the SDK's ``CliTokenSource``
    parses (``access_token`` / ``token_type`` / ``expiry``).
    """
    bin_dir = brew / "Cellar" / "databricks" / version / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    cli = bin_dir / "databricks"
    pad = "# " + "x" * 1022 + "\n"
    script = (
        "#!/usr/bin/env bash\n"
        f'exp="$(date -d "+{_TOKEN_TTL_S} seconds" \'+%Y-%m-%dT%H:%M:%S\')"\n'
        'printf \'{"access_token":"fake-cli-token-'
        + version
        + '","token_type":"Bearer","expiry":"%s"}\\n\' "$exp"\n'
        "exit 0\n" + pad * 1100
    )
    cli.write_text(script)
    cli.chmod(0o755)
    return cli


def _point_stable_symlink(brew: Path, version: str) -> None:
    """Point ``bin/databricks`` at ``Cellar/<version>/bin/databricks`` (like brew)."""
    stable = brew / "bin" / "databricks"
    stable.parent.mkdir(parents=True, exist_ok=True)
    if stable.is_symlink() or stable.exists():
        stable.unlink()
    stable.symlink_to(Path("..") / "Cellar" / "databricks" / version / "bin" / "databricks")


# ---------------------------------------------------------------------------
def _start_server(tmp_path: Path) -> tuple[subprocess.Popen[bytes], str]:
    """Spawn a real ``omnigent server`` on a free loopback port; return it + base URL."""
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    log = (tmp_path / "server.log").open("w")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from omnigent.cli import main; main()",
            "server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--database-uri",
            f"sqlite:///{tmp_path / 'db.sqlite'}",
            "--artifact-location",
            str(tmp_path / "artifacts"),
        ],
        env=_localhost_env(),
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    _wait_http_ok(f"{base_url}/health", time.monotonic() + _HEALTH_TIMEOUT_S)
    return proc, base_url


def _create_session(base_url: str) -> str:
    """Register a minimal inline agent and return its session id.

    A session row is all the relay's ``/policies/evaluate`` proxy needs: it is
    the sole enforcement point the ``UserPromptSubmit`` hook reaches, so the
    journey is exercised without booting a Claude CLI process.
    """
    cfg = {
        "name": "cli-upgrade-repro",
        "prompt": "You are a test agent.",
        "executor": {"harness": "openai-agents", "model": "gpt-4o-mini"},
    }
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml.safe_dump(cfg).encode()
        info = tarfile.TarInfo("cli-upgrade-repro.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    resp = _http.post(
        f"{base_url}/v1/sessions",
        data={"metadata": "{}"},
        files={"bundle": ("agent.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    resp.raise_for_status()
    return str(resp.json()["session_id"])


def _run_prompt_submit_hook(bridge_dir: Path, prompt: str) -> subprocess.CompletedProcess[bytes]:
    """Run the REAL ``UserPromptSubmit`` hook subprocess Claude Code spawns.

    Its JSON stdout is the harness verdict the Claude Code TUI renders; a
    ``{"decision": "block", ...}`` is the fail-closed message the user sees.
    """
    payload = json.dumps(
        {
            "hook_event_name": "UserPromptSubmit",
            "prompt": prompt,
            "session_id": _EXTERNAL_SID,
            "cwd": str(bridge_dir),
        }
    ).encode()
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "omnigent.harnesses.claude_native.hook",
            "evaluate-policy",
            "--bridge-dir",
            str(bridge_dir),
        ],
        input=payload,
        capture_output=True,
        timeout=_HOOK_TIMEOUT_S,
        env=_localhost_env(),
    )


def _decision(hook_stdout: bytes) -> dict[str, object]:
    """Parse the hook's JSON stdout ("" = no opinion / allow)."""
    text = hook_stdout.decode().strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"_raw": text}
    return parsed if isinstance(parsed, dict) else {"_raw": text}


def test_prompt_survives_databricks_cli_upgrade_mid_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prompt submit must survive a ``brew upgrade databricks`` under a live runner.

    Fails on the buggy build (the SDK baked the versioned CLI realpath once, so
    the refresh after the upgrade 404s and the hook blocks fail-closed); passes
    once the runner re-resolves the CLI path / rebuilds the SDK client on a
    refresh ``FileNotFoundError``.
    """
    from omnigent.cli_auth import open_server_client
    from omnigent.harnesses.claude_native.bridge import (
        prepare_bridge_dir,
        start_tool_relay,
        write_active_session_id,
    )
    from omnigent.runner._entry import _make_auth_token_factory, _RunnerDatabricksAuth

    # A databricks-cli auth profile pointed at the Homebrew-like layout. HOME +
    # PATH must be set before the runner's auth factory resolves the SDK, since
    # the token source bakes the versioned CLI realpath at construction.
    fake_home = tmp_path / "home"
    brew = tmp_path / "homebrew"
    fake_home.mkdir(parents=True)
    (fake_home / ".databrickscfg").write_text(
        "[DEFAULT]\n"
        "host = https://adb-1111222233334444.15.azuredatabricks.net\n"
        "auth_type = databricks-cli\n"
    )
    _install_fake_cli(brew, "1.15.0")
    _point_stable_symlink(brew, "1.15.0")
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("PATH", f"{brew / 'bin'}{os.pathsep}{os.environ['PATH']}")
    # Ambient PAT/host or a delegated-mint binding would pre-empt the
    # databricks-cli resolution this test exercises; clear them so the SDK
    # falls through to the staged ``databricks-cli`` profile deterministically.
    for name in list(os.environ):
        if name.startswith(("DATABRICKS", "OMNIGENT_RUNNER")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("RUNNER_SERVER_URL", raising=False)

    import asyncio

    server_proc: subprocess.Popen[bytes] | None = None
    relay = None
    policy_client = None
    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()
    try:
        server_proc, base_url = _start_server(tmp_path)
        session_id = _create_session(base_url)

        # The runner's own auth factory + per-request auth, built exactly as
        # ``omnigent/runner/_entry.py`` builds them for the server client. One
        # factory serves the whole session -> the SDK client (and its baked CLI
        # path) is long-lived, which is the crux of the bug.
        factory = _make_auth_token_factory(base_url)
        assert factory is not None, (
            "runner auth factory did not resolve the databricks-cli profile"
        )
        first_token = factory()
        assert first_token and "1.15.0" in first_token, (
            f"expected a token minted by the 1.15.0 CLI, got {first_token!r}"
        )

        # The exact server client the runner hands the relay to proxy
        # ``/policies/evaluate`` (see ``runner/app.py`` _ensure_comment_relay_started).
        policy_client = open_server_client(
            base_url,
            auth=_RunnerDatabricksAuth(factory, server_url=base_url),
            timeout=httpx.Timeout(5.0, read=None),
            follow_redirects=False,
        )

        bridge_dir = prepare_bridge_dir(session_id, workspace=tmp_path)

        async def _noop_tool(name: str, arguments: dict[str, object]) -> dict[str, object]:
            del name, arguments
            return {}

        relay = start_tool_relay(
            bridge_dir=bridge_dir,
            tools=[],
            tool_executor=_noop_tool,
            loop=loop,
            policy_client=policy_client,
            session_id=session_id,
        )
        write_active_session_id(bridge_dir, session_id)

        # --- Control leg: session is healthy before the upgrade. ---
        control = _run_prompt_submit_hook(bridge_dir, "hello before the upgrade")
        control_decision = _decision(control.stdout)
        assert control_decision.get("decision") != "block", (
            "Control leg (before the CLI upgrade) unexpectedly blocked the prompt -- the "
            "server/relay/auth chain is unhealthy, so the post-upgrade leg would prove "
            f"nothing. hook stdout={control.stdout.decode()!r} "
            f"stderr={control.stderr.decode()!r}"
        )

        # --- brew upgrade databricks: 1.15.0 -> 1.16.1 (old Cellar dir removed). ---
        _install_fake_cli(brew, "1.16.1")
        _point_stable_symlink(brew, "1.16.1")
        shutil.rmtree(brew / "Cellar" / "databricks" / "1.15.0")
        assert (brew / "bin" / "databricks").resolve().is_file(), (
            "stable symlink should resolve to the upgraded binary"
        )

        # Let the cached token lapse so the next refresh re-runs the baked
        # (now-missing) versioned CLI path.
        time.sleep(_EXPIRY_WAIT_S)

        # --- The bug leg: same session, same long-lived relay + auth factory. ---
        after = _run_prompt_submit_hook(bridge_dir, "hello after the upgrade")
        after_decision = _decision(after.stdout)

        # A *fresh* runner rebuilds the token source and resolves the upgraded
        # binary via the stable symlink -- proving the credential is valid and
        # only the baked versioned path broke ("new sessions unaffected").
        fresh_factory = _make_auth_token_factory(base_url)
        fresh_token = fresh_factory() if fresh_factory is not None else None
        assert fresh_token and "1.16.1" in fresh_token, (
            f"a fresh runner should mint a token from the upgraded 1.16.1 CLI; got {fresh_token!r}"
        )

        assert after_decision.get("decision") != "block", (
            "Bug reproduced: after a `brew upgrade databricks` under a live runner, the "
            "Databricks SDK re-ran the versioned CLI realpath it baked at construction -- "
            "now deleted by the upgrade -- so every token refresh 404s and the "
            f"claude-native UserPromptSubmit hook latches fail-closed ({_TOKEN_REFRESH_FAILURE}). "
            "A fresh runner resolves the current binary fine, so only the baked path broke. "
            f"hook decision={after_decision!r}\n"
            f"hook stderr={after.stderr.decode().strip()!r}"
        )
    finally:
        if relay is not None:
            relay.close()
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=5)
        if policy_client is not None:
            asyncio.run(policy_client.aclose())
        _terminate(server_proc)
