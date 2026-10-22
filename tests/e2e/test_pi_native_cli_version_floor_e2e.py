"""Real Pi installations through CLI and server-native launch, with local model replies.

Set OMNIGENT_E2E_PI_OLD_BINARY and OMNIGENT_E2E_PI_CURRENT_BINARY to actual
npm Pi entry points. Each case owns its server, runner, home, and terminals.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pexpect
import pytest
import yaml
from packaging.version import Version

from omnigent.harnesses.pi_native.main import _materialize_pi_agent_spec
from omnigent.onboarding.harness_install import PI_KEY, harness_install_spec
from tests._helpers.live_server import local_server_env
from tests._helpers.server_runner import server_runner
from tests._helpers.session import bundle_files, post_session_bundle
from tests.e2e.conftest import configure_mock_llm, get_mock_requests

_ROOT = Path(__file__).resolve().parents[2]
_MODEL = "pi-version-journey"
_OBSERVER = """const fs = require('fs');
module.exports = function() {
  fs.writeFileSync(require('path').join(process.cwd(), 'pi-started.json'),
    JSON.stringify({pid: process.pid, argv: process.argv}));
};
"""


def _wait(check: Callable[[], Any], what: str, timeout: float = 90) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if result := check():
            return result
        time.sleep(0.2)
    raise AssertionError(f"Timed out waiting for {what}")


def _pi_installation(kind: str) -> tuple[str, str]:
    key = f"OMNIGENT_E2E_PI_{kind.upper()}_BINARY"
    binary = os.environ.get(key)
    if not binary:
        pytest.skip(f"Set {key} to a real npm Pi installation")
    entry = Path(binary).resolve()
    packages = [parent / "package.json" for parent in entry.parents]
    metadata = next(
        json.loads(package.read_text())
        for package in packages
        if package.is_file()
        and json.loads(package.read_text()).get("name") == "@earendil-works/pi-coding-agent"
    )
    assert entry.name == "cli.js"
    version = subprocess.check_output([binary, "--version"], text=True, timeout=20).strip()
    assert version == metadata["version"]
    spec = harness_install_spec(PI_KEY)
    assert spec is not None and spec.min_version is not None
    assert (Version(version) < Version(spec.min_version)) == (kind == "old")
    return binary, version


class _Recording:
    """Save terminal output as text and a replayable asciicast."""

    def __init__(self, root: Path) -> None:
        self.started = time.monotonic()
        self.text = (root / "terminal.txt").open("w")
        self.cast = (root / "terminal.cast").open("w")
        self.cast.write(json.dumps({"version": 2, "width": 160, "height": 45}) + "\n")
        self.parts: list[str] = []

    def write(self, text: str) -> None:
        self.parts.append(text)
        self.text.write(text)
        self.cast.write(json.dumps([time.monotonic() - self.started, "o", text]) + "\n")
        self.flush()

    def flush(self) -> None:
        self.text.flush()
        self.cast.flush()

    def close(self) -> None:
        self.text.close()
        self.cast.close()


@pytest.mark.parametrize("surface", ["server", "cli"])
@pytest.mark.parametrize("version_kind", ["old", "current"])
@pytest.mark.min_runner_version("0.18.0")
@pytest.mark.timeout(240)
def test_real_pi_cli_version_floor(
    tmp_path: Path,
    isolated_mock_llm_server_url: str,
    surface: str,
    version_kind: str,
) -> None:
    """An old CLI gives a remedy; a supported real Pi answers native and web input."""
    if shutil.which("tmux") is None or shutil.which("node") is None:
        pytest.skip("Real Pi launch requires tmux and node")
    binary, version = _pi_installation(version_kind)
    clean_env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(
            ("OMNIGENT_", "RUNNER_", "PI_", "DATABRICKS_", "ANTHROPIC_", "OPENAI_")
        )
        and key not in {"TMUX", "HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"}
    }
    runtime_env = {
        "OMNIGENT_PI_PATH": binary,
        "OMNIGENT_NO_UPDATE_CHECK": "1",
        "OMNIGENT_SKIP_ONBOARD": "1",
        "PI_OFFLINE": "1",
        "PI_TELEMETRY": "0",
        "TERM": "xterm-256color",
        "PATH": str(Path(binary).parent) + os.pathsep + clean_env["PATH"],
    }
    configure_mock_llm(
        isolated_mock_llm_server_url,
        [{"text": "PI_FIRST_TURN_OK"}, {"text": "PI_SECOND_TURN_OK"}],
        key=_MODEL,
    )
    evidence: dict[str, Any] = {
        "surface": surface,
        "test_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "source_diff_sha256": hashlib.sha256(
            subprocess.check_output(["git", "diff", "--binary", "HEAD"], cwd=_ROOT)
        ).hexdigest(),
        "pi_binary": binary,
        "pi_version": version,
        "head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=_ROOT, text=True
        ).strip(),
        "feature": "feature-map/native-harnesses.md",
        "entry_point": "launch-cli" if surface == "cli" else "launch-web/server-native-route",
    }
    with server_runner(tmp_path / "stack", base_env=clean_env, server_cwd=_ROOT) as stack:
        config_dir = stack.runner_home / ".omnigent"
        config_dir.mkdir(exist_ok=True)
        (config_dir / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    "providers": {
                        "test": {
                            "kind": "gateway",
                            "default": ["pi"],
                            "openai": {
                                "base_url": isolated_mock_llm_server_url + "/v1",
                                "api_key": "mock-key",
                                "wire_api": "chat",
                                "models": {"default": _MODEL},
                            },
                        }
                    },
                }
            )
        )
        extensions = stack.runner_home / ".pi/agent/extensions"
        extensions.mkdir(parents=True)
        observer = extensions / "launch-observer.js"
        observer.write_text(_OBSERVER)
        (extensions.parent / "settings.json").write_text(
            json.dumps({"extensions": [str(observer)]})
        )
        runtime_env["OMNIGENT_CONFIG_HOME"] = str(config_dir)
        child = None
        recording = _Recording(tmp_path)
        drain_stop = threading.Event()
        drain_thread = None
        with httpx.Client(base_url=stack.base_url, trust_env=False, timeout=120) as client:
            try:
                if surface == "server":
                    stack.start_runner(env=runtime_env, cwd=stack.workspace)
                    spec_dir = tmp_path / "spec"
                    spec_dir.mkdir()
                    spec = _materialize_pi_agent_spec(spec_dir)
                    created = post_session_bundle(
                        client.post,
                        "/v1/sessions",
                        bundle_files({spec.name: spec.read_bytes()}),
                        metadata={
                            "labels": {
                                "omnigent.ui": "terminal",
                                "omnigent.wrapper": "pi-native-ui",
                            },
                        },
                    )
                    evidence["create"] = {"status": created.status_code, "body": created.text}
                    assert created.status_code == 201, created.text
                    session_id = created.json()["session_id"]
                    bound = client.patch(
                        f"/v1/sessions/{session_id}", json={"runner_id": stack.runner_id}
                    )
                    bound.raise_for_status()
                    response = client.post(
                        f"/v1/sessions/{session_id}/resources/terminals",
                        json={
                            "terminal": "pi",
                            "session_key": "main",
                            "ensure_native_terminal": True,
                        },
                    )
                    evidence["ensure"] = {"status": response.status_code, "body": response.text}
                else:
                    cli_env = local_server_env(
                        {
                            **runtime_env,
                            "HOME": str(stack.runner_home),
                            "OMNIGENT_DATA_DIR": str(config_dir),
                        },
                        base_env=clean_env,
                    )
                    command = ["-m", "omnigent.cli", "pi", "--server", stack.base_url]
                    evidence["command"] = [sys.executable, *command]
                    child = pexpect.spawn(
                        sys.executable,
                        command,
                        cwd=str(stack.workspace),
                        env=cli_env,
                        encoding="utf-8",
                        dimensions=(45, 160),
                    )
                    child.logfile_read = recording

                    def drain() -> None:
                        while not drain_stop.is_set():
                            try:
                                child.read_nonblocking(8192, timeout=0.2)
                            except pexpect.TIMEOUT:
                                continue
                            except (pexpect.EOF, OSError):
                                return

                    drain_thread = threading.Thread(target=drain, daemon=True)
                    drain_thread.start()

                    def created_session() -> str | None:
                        data = (
                            client.get("/v1/sessions", params={"visibility": "all"})
                            .json()
                            .get("data", [])
                        )
                        return next(
                            (
                                row["id"]
                                for row in data
                                if row.get("labels", {}).get("omnigent.wrapper") == "pi-native-ui"
                            ),
                            None,
                        )

                    session_id = _wait(created_session, "CLI-created Pi session")
                evidence["session_id"] = session_id
                if version_kind == "old":
                    if child is not None:
                        _wait(lambda: not child.isalive(), "CLI startup failure")
                        child.close()
                        evidence["cli_exit"] = child.exitstatus
                    snapshot = client.get(f"/v1/sessions/{session_id}").json()
                    evidence["snapshot"] = snapshot
                    terminal = client.get(
                        f"/v1/sessions/{session_id}/resources/terminals/terminal_pi_main"
                    )
                    evidence["terminal_resource"] = {
                        "status": terminal.status_code,
                        "body": terminal.text,
                    }
                    rendered = json.dumps(evidence) + "".join(recording.parts)
                    assert f"installed Pi CLI ({version})" in rendered
                    assert "Upgrade with:" in rendered
                    assert "@earendil-works/pi-coding-agent" in rendered
                    assert not (stack.workspace / "pi-started.json").exists(), (
                        "Old Pi reached terminal launch"
                    )
                    assert terminal.status_code == (409 if surface == "cli" else 404), (
                        terminal.text
                    )
                    if surface == "cli":
                        assert snapshot.get("runner_id") is None
                else:
                    started = stack.workspace / "pi-started.json"
                    _wait(started.exists, "real Pi extension initialization")
                    evidence["pi_process"] = json.loads(started.read_text())
                    assert str(Path(evidence["pi_process"]["argv"][1]).resolve()) == str(
                        Path(binary).resolve()
                    )

                    def answered(marker: str) -> bool:
                        items = (
                            client.get(f"/v1/sessions/{session_id}/items").json().get("data", [])
                        )
                        evidence["items"] = items
                        return any(
                            marker in json.dumps(item)
                            and item.get("data", item).get("role") == "assistant"
                            for item in items
                        )

                    if child is not None:
                        _wait(lambda: _MODEL in "".join(recording.parts), "attached Pi prompt")
                        child.send("Reply with PI_FIRST_TURN_OK.\r")
                    else:
                        sent = client.post(
                            f"/v1/sessions/{session_id}/events",
                            json={
                                "type": "message",
                                "data": {
                                    "role": "user",
                                    "content": [
                                        {
                                            "type": "input_text",
                                            "text": "Reply with PI_FIRST_TURN_OK.",
                                        }
                                    ],
                                },
                            },
                        )
                        sent.raise_for_status()
                    _wait(lambda: answered("PI_FIRST_TURN_OK"), "Pi's first mirrored answer")
                    if child is not None:
                        sent = client.post(
                            f"/v1/sessions/{session_id}/events",
                            json={
                                "type": "message",
                                "data": {
                                    "role": "user",
                                    "content": [
                                        {
                                            "type": "input_text",
                                            "text": "Reply with PI_SECOND_TURN_OK.",
                                        }
                                    ],
                                },
                            },
                        )
                        sent.raise_for_status()
                        _wait(
                            lambda: answered("PI_SECOND_TURN_OK"),
                            "web input through the real Pi bridge",
                        )
                    evidence["model_requests"] = get_mock_requests(
                        isolated_mock_llm_server_url, key=_MODEL
                    )
                    terminal = client.get(
                        f"/v1/sessions/{session_id}/resources/terminals/terminal_pi_main"
                    ).json()
                    evidence["terminal_resource"] = terminal
                    metadata = terminal["metadata"]
                    captured = subprocess.check_output(
                        [
                            "tmux",
                            "-S",
                            metadata["tmux_socket"],
                            "capture-pane",
                            "-pt",
                            metadata["tmux_target"],
                            "-S",
                            "-80",
                        ],
                        text=True,
                    )
                    (tmp_path / "pane.txt").write_text(captured)
            finally:
                if child is not None:
                    child.terminate(force=True)
                drain_stop.set()
                if drain_thread is not None:
                    drain_thread.join(timeout=3)
                recording.close()
                (tmp_path / "evidence.json").write_text(json.dumps(evidence, indent=2))
                if "session_id" in evidence:
                    with contextlib.suppress(httpx.HTTPError):
                        client.delete(f"/v1/sessions/{evidence['session_id']}", timeout=15)
