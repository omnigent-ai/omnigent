"""Own the lifetime of an isolated local reproduction server, runner and model.

Runs independently of pytest. The supervisor has a fixed lease, stops all its
process groups on exit, and preserves logs and the database for inspection.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from .transport import Relay


def write_json(path: Path, value: dict) -> None:
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as handle:
        json.dump(value, handle, indent=2)
        handle.write("\n")
        temp = Path(handle.name)
    temp.replace(path)


_logger = logging.getLogger(__name__)


def isolated_env(environ: dict[str, str], output: Path) -> dict[str, str]:
    """Keep proxy/tool plumbing while removing parent session and model state."""
    remove = {
        "RUNNER_SERVER_URL",
        "OMNIGENT_REMOTE_AUTH_TOKEN",
        "LLM_API_KEY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_MODEL",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "OPENAI_MODEL",
        "PYTEST_ADDOPTS",
        "OMNIGENT_CONFIG_HOME",
        "OMNIGENT_AUTH_ENABLED",
        "OMNIGENT_AUTH_PROVIDER",
    }
    env = {
        key: value
        for key, value in environ.items()
        if key not in remove
        and not key.startswith(("OMNIGENT_RUNNER_", "OMNIGENT_HOST_", "OMNIGENT_COMPAT_"))
    }
    env.update(
        {
            "OMNIGENT_CONFIG_HOME": str(output / "config"),
            "CLAUDE_CONFIG_DIR": str(output / "claude-config"),
            "CODEX_HOME": str(output / "codex-config"),
            "OMNIGENT_DATA_DIR": str(output / "data"),
            "OMNIGENT_AUTH_PROVIDER": "header",
            "OMNIGENT_LOCAL_SINGLE_USER": "1",
            "OMNIGENT_DISABLE_CATALOG_LOOKUP": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        }
    )
    for key in ("NO_PROXY", "no_proxy"):
        env[key] = ",".join(filter(None, (env.get(key), "localhost,127.0.0.1,::1")))
    return env


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def write_model_config(
    config_home: Path, mock_url: str, claude_model: str, codex_model: str
) -> None:
    config_home.mkdir(parents=True, exist_ok=True)
    config = {
        "providers": {
            "repro-claude": {
                "kind": "key",
                "default": ["anthropic"],
                "anthropic": {
                    "base_url": mock_url,
                    "api_key": "mock-key",
                    "models": {"default": claude_model},
                },
            },
            "repro-openai": {
                "kind": "key",
                "default": ["openai"],
                "openai": {
                    "base_url": f"{mock_url}/v1",
                    "api_key": "mock-key",
                    "wire_api": "responses",
                    "models": {"default": codex_model},
                },
            },
        }
    }
    write_json(config_home / "config.yaml", config)


def supervise(output: Path) -> None:
    from omnigent.runner.identity import token_bound_runner_id

    state = json.loads((output / "environment.json").read_text())
    root = Path(state["workspace"])
    children = []
    logs = []
    relays = []
    stopping = False

    def on_signal(*_args):
        nonlocal stopping
        stopping = True

    old_term = signal.signal(signal.SIGTERM, on_signal)
    old_int = signal.signal(signal.SIGINT, on_signal)

    def cancelled():
        return stopping or (output / "stop").exists() or time.time() >= state["expires_at"]

    def spawn(name, command, env):
        handle = (output / f"{name}.log").open("w")
        logs.append(handle)
        process = subprocess.Popen(
            command,
            cwd=root,
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        children.append(process)
        return process

    def ready(client, url, predicate, timeout=90):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cancelled():
                raise InterruptedError("environment stopped during startup")
            if any(child.poll() is not None for child in children):
                raise RuntimeError("environment process exited; inspect process logs")
            try:
                response = client.get(url)
                if response.status_code == 200 and predicate(response):
                    return
            except httpx.TransportError:
                # Startup can refuse connections until the service binds; retry until the deadline.
                pass
            time.sleep(0.2)
        raise TimeoutError(f"not ready: {url}")

    try:
        env = dict(os.environ)
        claude_dir = output / "claude-config"
        claude_dir.mkdir(exist_ok=True)
        write_json(
            claude_dir / ".claude.json",
            {
                "hasCompletedOnboarding": True,
                "projects": {str(root): {"hasTrustDialogAccepted": True}},
            },
        )
        mock_port = _port()
        mock_url = f"http://127.0.0.1:{mock_port}"
        spawn(
            "model",
            [
                sys.executable,
                str(root / "tests/server/integration/mock_llm_server.py"),
                str(mock_port),
            ],
            env,
        )
        with httpx.Client(trust_env=False, timeout=2) as client:
            ready(client, f"{mock_url}/stats", lambda _: True, timeout=20)
            models = json.loads((root / "tests/server/integration/repro_models.json").read_text())
            write_model_config(
                output / "config", mock_url, models["claude-native"], models["codex-native"]
            )
            response = client.post(
                f"{mock_url}/mock/set_fallback",
                json={"key": "_policy_llm_", "text": '{"action":"allow","reason":""}'},
            )
            response.raise_for_status()
            port = _port()
            base_url = f"http://127.0.0.1:{port}"
            token = secrets.token_urlsafe(32)
            runner_id = token_bound_runner_id(token)
            env.update(
                OPENAI_BASE_URL=f"{mock_url}/v1",
                OPENAI_API_KEY="mock-key",
                OMNIGENT_WEB_UI_DIST=str(root / "omnigent/server/static/web-ui"),
            )
            server_env = {**env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": token}
            spawn(
                "server",
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
                    f"sqlite:///{output / 'sessions.db'}",
                    "--artifact-location",
                    str(output / "artifacts"),
                ],
                server_env,
            )
            runner_env = {
                **env,
                "RUNNER_SERVER_URL": base_url,
                "OMNIGENT_RUNNER_ID": runner_id,
                "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": token,
                "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
            }
            spawn("runner", [sys.executable, "-m", "omnigent.runner._entry"], runner_env)
            ready(
                client,
                f"{base_url}/v1/runners/{runner_id}/status",
                lambda r: r.json().get("online") is True,
            )
        for name, url in (("server", base_url), ("model", mock_url)):
            relay = Relay(
                unix_listener=output / f"{name}.sock",
                tcp_target=("127.0.0.1", int(url.rsplit(":", 1)[1])),
            )
            relay.__enter__()
            relays.append(relay)
        state.update(
            status="ready",
            base_url=base_url,
            mock_url=mock_url,
            runner_id=runner_id,
            config_home=str(output / "config"),
            database=str(output / "sessions.db"),
        )
        write_json(output / "environment.json", state)
        while not cancelled():
            if any(child.poll() is not None for child in children):
                raise RuntimeError("environment process exited; inspect process logs")
            time.sleep(0.2)
        state["status"] = "stopped"
    except InterruptedError:
        state["status"] = "stopped"
    except Exception as exc:
        _logger.exception("Reproduction environment failed")
        state.update(status="failed", error=f"{type(exc).__name__}: {exc}")
    finally:
        # Capture model requests even when the authored journey failed.
        if "mock_url" in locals():
            for filename, endpoint in (
                ("model-stats.json", "/stats"),
                ("model-requests.json", "/mock/requests"),
            ):
                with (
                    contextlib.suppress(Exception),
                    httpx.Client(trust_env=False, timeout=2) as client,
                ):
                    response = client.get(f"{mock_url}{endpoint}")
                    response.raise_for_status()
                    write_json(output / filename, response.json())
        for relay in reversed(relays):
            relay.__exit__(None, None, None)
        # Signal groups, including descendants even if their direct parent exited.
        for child in reversed(children):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGTERM)
        deadline = time.monotonic() + 10
        for child in reversed(children):
            with contextlib.suppress(subprocess.TimeoutExpired):
                child.wait(timeout=max(0.01, deadline - time.monotonic()))
        for child in reversed(children):
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
            child.wait()
        for handle in logs:
            handle.close()
        write_json(output / "environment.json", state)
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)


def serve(output: Path, lease_seconds: int) -> int:
    """Run in the workflow's persistent sandbox, in the foreground."""
    root = Path.cwd().resolve()
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (output / "environment.json").exists():
        raise ValueError(
            "Reproduction output already contains an attempt; use a fresh --output directory"
        )
    if output.stat().st_mode & 0o077:
        raise ValueError("Reproduction output must be an owner-only directory (mode 0700)")
    if not (root / "omnigent/server/static/web-ui/index.html").is_file():
        raise RuntimeError("Build the SPA before provisioning the reproduction environment")
    if not 60 <= lease_seconds <= 21600:
        raise ValueError("lease_seconds must be 60..21600")
    write_json(
        output / "environment.json",
        {
            "status": "starting",
            "workspace": str(root),
            "model_backend": "mock",
            "expires_at": time.time() + lease_seconds,
        },
    )
    env = isolated_env(dict(os.environ), output)
    env["PYTHONPATH"] = os.pathsep.join(
        (str(root), str(root / "sdks/python-client"), str(root / "sdks/ui"))
    )
    os.environ.clear()
    os.environ.update(env)
    supervise(output)
    return 1 if json.loads((output / "environment.json").read_text())["status"] == "failed" else 0
