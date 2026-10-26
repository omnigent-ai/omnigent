r"""Exercise runner token refresh through the ``databricks-cli`` profile.

The journeys use a real server, runner auth factory, tool relay, and
``UserPromptSubmit`` hook process with one long-lived factory, covering a
Homebrew-style CLI upgrade and an expired Databricks login.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import yaml

from tests._helpers.live_server import find_free_port
from tests._helpers.session import bundle_files, post_session_bundle

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Keep CI proxy settings out of loopback calls.
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

_EXTERNAL_SID = "11111111-2222-4333-8444-555566667777"

_TOKEN_TTL_S = 12
_EXPIRY_WAIT_S = _TOKEN_TTL_S + 6

_TOKEN_REFRESH_FAILURE = "Databricks token refresh returned no token"

# Databricks CLI v1.14.1 output for `auth token` once its refresh token is revoked.
_REVOKED_REFRESH_TOKEN_ERROR = (
    "Error: A new access token could not be retrieved because the refresh token is "
    "invalid. To reauthenticate, run the following command:\n"
    "  $ databricks auth login --profile DEFAULT\n"
)


def _localhost_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Build a worktree subprocess environment without proxy settings."""
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
    """Stop a spawned process."""
    if proc is None or proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _wait_http_ok(url: str, deadline: float) -> None:
    """Wait for a successful health check."""
    last = "not polled"
    while time.monotonic() < deadline:
        try:
            if _http.get(url, timeout=2.0).status_code == 200:
                return
        except httpx.HTTPError as exc:
            last = repr(exc)
        time.sleep(_POLL_S)
    raise AssertionError(f"server never became healthy at {url}; last: {last}")


def _install_fake_cli(brew: Path, version: str, *, revoked_marker: Path | None = None) -> Path:
    """Install a >1MB fake CLI that emits a short-lived OAuth token.

    While ``revoked_marker`` exists, the CLI fails like an expired login.
    """
    bin_dir = brew / "Cellar" / "databricks" / version / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    cli = bin_dir / "databricks"
    pad = "# " + "x" * 1022 + "\n"
    revoked_check = (
        f"if os.path.exists({str(revoked_marker)!r}):\n"
        f"    sys.stderr.write({_REVOKED_REFRESH_TOKEN_ERROR!r})\n"
        "    sys.exit(1)\n"
        if revoked_marker is not None
        else ""
    )
    script = (
        "#!/usr/bin/env python3\n"
        "import json\n"
        "import os\n"
        "import sys\n"
        "from datetime import datetime, timedelta\n"
        f"{revoked_check}"
        f"expiry = datetime.now() + timedelta(seconds={_TOKEN_TTL_S})\n"
        "expiry_text = expiry.strftime('%Y-%m-%dT%H:%M:%S')\n"
        f'print(json.dumps({{"access_token": "fake-cli-token-{version}", '
        '"token_type": "Bearer", "expiry": expiry_text}))\n' + pad * 1100
    )
    cli.write_text(script)
    cli.chmod(0o755)
    return cli


def _point_stable_symlink(brew: Path, version: str) -> None:
    """Point the stable CLI symlink at a Cellar version."""
    stable = brew / "bin" / "databricks"
    stable.parent.mkdir(parents=True, exist_ok=True)
    if stable.is_symlink() or stable.exists():
        stable.unlink()
    stable.symlink_to(Path("..") / "Cellar" / "databricks" / version / "bin" / "databricks")


def _start_server(tmp_path: Path) -> tuple[subprocess.Popen[bytes], str]:
    """Start a real server on loopback."""
    port = find_free_port()
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
    """Register a minimal agent and return its session ID."""
    cfg = {
        "name": "cli-upgrade-repro",
        "prompt": "You are a test agent.",
        "executor": {"harness": "openai-agents", "model": "gpt-4o-mini"},
    }
    data = yaml.safe_dump(cfg).encode()
    bundle_bytes = bundle_files({"cli-upgrade-repro.yaml": data})
    resp = post_session_bundle(_http.post, f"{base_url}/v1/sessions", bundle_bytes, timeout=30.0)
    resp.raise_for_status()
    return str(resp.json()["session_id"])


def _prompt_submit_payload(bridge_dir: Path, prompt: str) -> bytes:
    return json.dumps(
        {
            "hook_event_name": "UserPromptSubmit",
            "prompt": prompt,
            "session_id": _EXTERNAL_SID,
            "cwd": str(bridge_dir),
        }
    ).encode()


def _run_installed_prompt_policy_hook(
    bridge: _PolicyHookBridge, prompt: str
) -> subprocess.CompletedProcess[bytes]:
    """Run the ``UserPromptSubmit`` policy command Claude Code is launched with."""
    from omnigent.harnesses.claude_native.bridge import build_hook_settings

    bridge_dir = bridge.bridge_dir
    settings = build_hook_settings(bridge_dir, ap_server_url=bridge.base_url)
    [command] = [
        hook["command"]
        for entry in settings["hooks"]["UserPromptSubmit"]
        for hook in entry["hooks"]
        if "evaluate-policy" in str(hook.get("command", ""))
    ]
    return subprocess.run(
        ["/bin/sh", "-c", str(command)],
        input=_prompt_submit_payload(bridge_dir, prompt),
        capture_output=True,
        timeout=_HOOK_TIMEOUT_S,
        env=_localhost_env(),
    )


def _run_prompt_submit_hook(bridge_dir: Path, prompt: str) -> subprocess.CompletedProcess[bytes]:
    """Run the production ``UserPromptSubmit`` hook subprocess."""
    payload = _prompt_submit_payload(bridge_dir, prompt)
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


def _stage_cli_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Stage an isolated ``databricks-cli`` profile and return the Homebrew root."""
    import databricks.sdk.config as sdk_config

    # Newer SDKs fetch host metadata while resolving the profile; older ones do not.
    if hasattr(sdk_config, "get_host_metadata"):
        from databricks.sdk.oauth import HostMetadata

        def _offline_host_metadata(_host: str) -> HostMetadata:
            return HostMetadata(oidc_endpoint="")

        monkeypatch.setattr(sdk_config, "get_host_metadata", _offline_host_metadata)

    fake_home = tmp_path / "home"
    brew = tmp_path / "homebrew"
    fake_home.mkdir(parents=True)
    (fake_home / ".databrickscfg").write_text(
        "[DEFAULT]\n"
        "host = https://adb-1111222233334444.15.azuredatabricks.net\n"
        "auth_type = databricks-cli\n"
    )
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("PATH", f"{brew / 'bin'}{os.pathsep}{os.environ['PATH']}")
    # Isolate the staged profile from ambient and delegated credentials.
    for name in list(os.environ):
        if name.startswith(("DATABRICKS", "OMNIGENT_RUNNER")):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("RUNNER_SERVER_URL", raising=False)
    return brew


@dataclass
class _PolicyHookBridge:
    base_url: str
    bridge_dir: Path
    factory: Callable[[], str | None]


@contextlib.contextmanager
def _policy_hook_bridge(tmp_path: Path) -> Iterator[_PolicyHookBridge]:
    """Serve hook policy calls through one long-lived runner auth factory.

    Stage the fake CLI before entering: the SDK resolves its executable here.
    """
    from omnigent.cli_auth import open_server_client
    from omnigent.harnesses.claude_native.bridge import (
        prepare_bridge_dir,
        start_tool_relay,
        write_active_session_id,
    )
    from omnigent.runner._entry import _make_auth_token_factory, _RunnerDatabricksAuth

    server_proc: subprocess.Popen[bytes] | None = None
    relay = None
    policy_client = None
    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()
    try:
        server_proc, base_url = _start_server(tmp_path)
        session_id = _create_session(base_url)

        factory = _make_auth_token_factory(base_url)
        assert factory is not None, (
            "runner auth factory did not resolve the databricks-cli profile"
        )
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
        yield _PolicyHookBridge(base_url=base_url, bridge_dir=bridge_dir, factory=factory)
    finally:
        if relay is not None:
            relay.close()
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=5)
        if policy_client is not None:
            asyncio.run(policy_client.aclose())
        _terminate(server_proc)


def _assert_prompt_allowed(result: subprocess.CompletedProcess[bytes], leg: str) -> None:
    """Fail unless the hook let the prompt through."""
    assert result.returncode == 0 and not result.stdout.strip(), (
        f"{leg}: the hook blocked the prompt. returncode={result.returncode} "
        f"stdout={result.stdout.decode()!r} stderr={result.stderr.decode().strip()!r}"
    )


def test_prompt_survives_databricks_cli_upgrade_mid_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A prompt survives a CLI upgrade under a live runner."""
    from omnigent.runner._entry import _make_auth_token_factory

    brew = _stage_cli_profile(tmp_path, monkeypatch)
    _install_fake_cli(brew, "1.15.0")
    _point_stable_symlink(brew, "1.15.0")

    with _policy_hook_bridge(tmp_path) as bridge:
        first_token = bridge.factory()
        assert first_token and "1.15.0" in first_token, (
            f"expected a token minted by the 1.15.0 CLI, got {first_token!r}"
        )

        # Verify the full policy path before changing the CLI.
        control = _run_prompt_submit_hook(bridge.bridge_dir, "hello before the upgrade")
        _assert_prompt_allowed(control, "control leg before the CLI upgrade")

        _install_fake_cli(brew, "1.16.1")
        _point_stable_symlink(brew, "1.16.1")
        shutil.rmtree(brew / "Cellar" / "databricks" / "1.15.0")
        assert (brew / "bin" / "databricks").resolve().is_file(), (
            "stable symlink should resolve to the upgraded binary"
        )

        # Force a refresh after removing the old versioned path.
        time.sleep(_EXPIRY_WAIT_S)

        after = _run_prompt_submit_hook(bridge.bridge_dir, "hello after the upgrade")
        # Confirm a fresh factory resolves the upgraded executable.
        fresh_factory = _make_auth_token_factory(bridge.base_url)
        fresh_token = fresh_factory() if fresh_factory is not None else None
        assert fresh_token and "1.16.1" in fresh_token, (
            f"a fresh runner should mint a token from the upgraded 1.16.1 CLI; got {fresh_token!r}"
        )

        assert after.returncode == 0 and not after.stdout.strip(), (
            "Bug reproduced: after a `brew upgrade databricks` under a live runner, the "
            "Databricks SDK re-ran the versioned CLI realpath it baked at construction -- "
            "now deleted by the upgrade -- so every token refresh 404s and the "
            f"claude-native UserPromptSubmit hook latches fail-closed ({_TOKEN_REFRESH_FAILURE}). "
            "A fresh runner resolves the current binary fine, so only the baked path broke. "
            f"hook returncode={after.returncode} stdout={after.stdout.decode()!r}\n"
            f"hook stderr={after.stderr.decode().strip()!r}"
        )


def test_prompt_block_after_expired_databricks_login_names_recovery_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An expired login fails closed with the re-login step, then recovers in place."""
    brew = _stage_cli_profile(tmp_path, monkeypatch)
    revoked_marker = tmp_path / "refresh-token-revoked"
    _install_fake_cli(brew, "1.15.0", revoked_marker=revoked_marker)
    _point_stable_symlink(brew, "1.15.0")

    with _policy_hook_bridge(tmp_path) as bridge:
        control = _run_installed_prompt_policy_hook(bridge, "hello before the login expires")
        _assert_prompt_allowed(control, "control leg before the login expires")

        revoked_marker.touch()
        time.sleep(_EXPIRY_WAIT_S)
        blocked = _run_installed_prompt_policy_hook(bridge, "try again")

        revoked_marker.unlink()
        after_login = _run_installed_prompt_policy_hook(bridge, "hello after re-login")

    block_output = blocked.stdout.decode()
    assert _TOKEN_REFRESH_FAILURE in block_output, (
        "expected the expired login to fail the prompt closed with the token refresh "
        f"failure; returncode={blocked.returncode} stdout={block_output!r} "
        f"stderr={blocked.stderr.decode().strip()!r}"
    )
    _assert_prompt_allowed(after_login, "prompt after the login was restored")
    assert "databricks auth login" in block_output, (
        "The prompt blocked by an expired Databricks login does not tell the user to run "
        "`databricks auth login`; the CLI error naming that step is dropped. "
        f"hook stdout={block_output!r}"
    )
