#!/usr/bin/env python
"""Reproduce a Codex-native session blocked on a startup prompt.

Boots a throwaway local Omnigent server + runner and creates a real
``codex-native`` session, then makes the detached Codex TUI park its startup on
an interactive box a human must answer — a directory-trust / hook-review screen
(the reliable ``--trigger trust-box`` default) or a ``TERM`` "Continue anyway?"
gate (``--trigger term-dumb``). This is the state that used to make the session
hang for 30s and then die with::

    Codex native thread never started: Codex app-server never started a thread
    (startup timed out: TimeoutError). ...

- BEFORE the readiness fix: the turn hangs, then fails with the "never started"
  error and the terminal vanishes.
- AFTER the fix: the session stays alive, shows an "answer it in the Terminal"
  banner (with the web UI built) / records an actionable notice, and recovers
  once you attach the pane and answer the box.

Requirements: the ``codex`` CLI on PATH and a usable Codex credential in your
environment (a configured provider or an ``auth.json`` login), so the startup
box is the *only* thing blocking startup. Uses your real ``$HOME`` /
``$CODEX_HOME`` so the credential is picked up; everything else (config, state,
DB) is a temp dir wiped on exit.

To see the banner visually the web UI must be built into
``omnigent/server/static/web-ui`` (``cd web && npm ci --legacy-peer-deps &&
npm run build``); otherwise use the UI-free checks the script prints.

Usage::

    python dev/repro_codex_startup_prompt.py                    # reliable trust box
    python dev/repro_codex_startup_prompt.py --trigger term-dumb # TERM box (flaky)
    python dev/repro_codex_startup_prompt.py --trigger none      # clean control run

Ctrl-C to tear the stack down.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

import httpx

_REPO_ROOT = Path(__file__).resolve().parents[1]
_HEALTH_TIMEOUT_S = 60.0


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _create_codex_session(base_url: str, runner_id: str) -> str:
    """Register the ``omnigent codex`` wrapper spec and bind its session."""
    from omnigent._wrapper_labels import (
        CODEX_NATIVE_WRAPPER_VALUE,
        UI_MODE_LABEL_KEY,
        UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY,
    )
    from omnigent.harnesses.codex_native.main import _materialize_codex_agent_spec

    with tempfile.TemporaryDirectory() as tmp:
        spec_path = _materialize_codex_agent_spec(Path(tmp), model=None)
        yaml_text = spec_path.read_text()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        info = tarfile.TarInfo("codex-native-ui.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    metadata = {
        "labels": {
            UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
            WRAPPER_LABEL_KEY: CODEX_NATIVE_WRAPPER_VALUE,
        },
        # Runner-owned codex terminals hard-require a workspace.
        "workspace": str(_REPO_ROOT),
    }
    create = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps(metadata)},
        files={"bundle": ("codex-native-ui.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = str(create.json()["session_id"])
    patch = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        timeout=10.0,
    )
    patch.raise_for_status()
    return session_id


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--trigger",
        choices=("trust-box", "term-dumb", "none"),
        default="trust-box",
        help="How to make the pane park on a startup box. 'trust-box' (default, "
        "reliable): force Codex's real directory-trust/hook-review box by "
        "disabling the runner's auto-acknowledgements. 'term-dumb': set the "
        "runner TERM to dumb (only bites if the browser terminal is attached "
        "during codex boot — not reliable for the detached auto-create pane). "
        "'none': clean control run.",
    )
    args = parser.parse_args()

    if shutil.which("codex") is None:
        print("error: the `codex` CLI must be on PATH for this repro.", file=sys.stderr)
        return 2

    work = Path(tempfile.mkdtemp(prefix="codex-startup-repro-"))
    config_home = work / "config-home"
    state_dir = work / "codex-native-state"
    artifacts = work / "artifacts"
    for path in (config_home, state_dir, artifacts):
        path.mkdir(parents=True, exist_ok=True)

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    binding_token = secrets.token_urlsafe(32)

    from omnigent.runner.identity import token_bound_runner_id

    runner_id = token_bound_runner_id(binding_token)

    # Inherit the real environment (so $HOME / $CODEX_HOME credentials are found)
    # and pin the isolated config/state dirs on top.
    shared_env = {
        **os.environ,
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "OMNIGENT_CODEX_NATIVE_STATE_DIR": str(state_dir),
    }
    server_env = {**shared_env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token}
    runner_env = {
        **shared_env,
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": base_url,
    }
    if args.trigger == "trust-box":
        # Force Codex's real trust/hook-review box in the detached pane, so the
        # thread never starts and the recovery path engages regardless of
        # whether a browser is attached (see _force_startup_trust_prompt).
        runner_env["OMNIGENT_CODEX_FORCE_STARTUP_TRUST_PROMPT"] = "1"
    elif args.trigger == "term-dumb":
        # A non-interactive TERM. Only trips the box if the browser terminal is
        # attached during codex boot; the detached auto-create pane usually
        # starts clean, so this is the less-reliable trigger.
        runner_env["TERM"] = "dumb"

    server_log = work / "server.log"
    runner_log = work / "runner.log"
    server_handle = server_log.open("w")
    runner_handle = runner_log.open("w")
    server_proc: subprocess.Popen[bytes] | None = None
    runner_proc: subprocess.Popen[bytes] | None = None
    session_id: str | None = None
    client = httpx.Client()
    try:
        server_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "omnigent.cli",
                "server",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--database-uri",
                f"sqlite:///{work}/repro.db",
                "--artifact-location",
                str(artifacts),
            ],
            env=server_env,
            stdout=server_handle,
            stderr=subprocess.STDOUT,
            cwd=str(_REPO_ROOT),
        )
        runner_proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
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
            try:
                if client.get(f"{base_url}/health", timeout=2).status_code == 200:
                    status = client.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
                    if status.status_code == 200 and status.json().get("online"):
                        online = True
                        break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        if not online:
            print(
                "stack did not come online:\n"
                f"  server log: {server_log}\n  runner log: {runner_log}",
                file=sys.stderr,
            )
            return 1

        session_id = _create_codex_session(base_url, runner_id)
        url = f"{base_url}/c/{session_id}"
        snapshot = f"{base_url}/v1/sessions/{session_id}"
        # The server serves the SPA only when it is built into
        # omnigent/server/static/web-ui; a fresh worktree has none, so the URL
        # would render the API-only landing ("web UI isn't installed"). Detect
        # that and steer the user to the build step or the UI-free checks.
        web_ui_built = (_REPO_ROOT / "omnigent/server/static/web-ui/index.html").is_file()
        print("\n" + "=" * 72)
        print(f"  trigger={args.trigger!r}  codex-native session is up.")
        print(f"  Session: {session_id}")
        if web_ui_built:
            print(f"  Open in browser:  {url}")
            print("  Send a chat message; the 'answer it in the Terminal' banner")
            print("  appears while the pane is blocked and clears once you answer it.")
        else:
            print("  The web UI is NOT built, so the browser URL shows the API-only")
            print("  landing. To see the banner visually, build it once:")
            print("      (cd web && npm ci --legacy-peer-deps && npm run build)")
            print("  then re-run this script. Or verify without the UI below.")
        print("")
        print("  --- verify WITHOUT the browser UI ---")
        print("  1) Readiness fix (runner keeps the session alive):")
        print(f"     grep -E 'parked on|never started' {runner_log}")
        print("     Fixed  -> 'parked on a terminal-compatibility prompt ...'")
        print("     Broken -> 'never started a thread (startup timed out)'")
        print("  2) GUI banner state (server snapshot field):")
        print(f"     curl -s {snapshot} | python -m json.tool | grep codex_startup_prompt")
        print("     Blocked -> a prompt string; recovered/clear -> null")
        print("  3) Turn behaviour (fails fast with guidance, not a 30s hang):")
        print(f"     curl -s -XPOST {snapshot}/events \\")
        print('       -H "content-type: application/json" \\')
        print(
            '       -d \'{"type":"message","data":{"role":"user",'
            '"content":[{"type":"input_text","text":"hi"}]}}\''
        )
        print("     then re-open the session or GET its items and read the turn error.")
        print("")
        print("  To clear the box and confirm recovery: attach the pane and answer it:")
        print(f"     tmux -S <socket> attach   (socket/target are in {runner_log})")
        print("  Ctrl-C to tear down.")
        print("=" * 72 + "\n")
        signal.pause()
        return 0
    except KeyboardInterrupt:
        print("\ntearing down...")
        return 0
    finally:
        if session_id is not None:
            with httpx.Client() as c, contextlib.suppress(httpx.HTTPError):
                c.delete(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
        for proc in (runner_proc, server_proc):
            if proc is not None and proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
        for proc in (runner_proc, server_proc):
            if proc is not None:
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
        client.close()
        server_handle.close()
        runner_handle.close()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
