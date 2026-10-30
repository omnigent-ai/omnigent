"""The host resolver and a real Pi session publish the same curated catalog."""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path

import httpx

from tests._helpers.host import isolated_host
from tests._helpers.session import bundle_files, post_session_bundle

_ENABLED_MODEL = "anthropic/claude-sonnet-4-5"


def test_host_and_session_pickers_honor_enabled_models(
    live_server: str, http_client: httpx.Client, tmp_path: Path
) -> None:
    agent = tmp_path / "home" / ".pi" / "agent"
    agent.mkdir(parents=True)
    catalogs = {
        "anthropic": ["claude-sonnet-4-5", "claude-opus-4-6"],
        "openrouter": ["ai21/jamba-large-1.7"],
    }
    (agent / "auth.json").write_text(
        json.dumps({provider: {"type": "api_key", "key": "test-only"} for provider in catalogs})
    )
    (agent / "models-store.json").write_text(
        json.dumps(
            {
                provider: {"models": [{"id": model} for model in models]}
                for provider, models in catalogs.items()
            }
        )
    )
    # Brace matching must agree across the host's resolver and the running CLI.
    (agent / "settings.json").write_text(
        json.dumps({"enabledModels": ["anthropic/claude-{sonnet-4-5,not-an-installed-*}"]})
    )
    with isolated_host(
        tmp_path, live_server, env={"OMNIGENT_PI_PATH": os.environ.get("OMNIGENT_PI_PATH")}
    ) as host:
        host.wait_online(http_client)
        response = http_client.get(
            f"/v1/hosts/{host.host_id}/harnesses/pi-native/model-options", timeout=30
        )
        response.raise_for_status()
        assert [option["id"] for option in response.json()["models"]] == [_ENABLED_MODEL]

        workspace = host.home / "workspace"
        workspace.mkdir()
        bundle = bundle_files(
            {
                "pi-native-ui.yaml": f"""
name: pi-native-ui
prompt: Pi is running in the session terminal.
executor:
  harness: pi-native
  model: {_ENABLED_MODEL}
spawn: true
os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none
""".encode()
            }
        )
        create = post_session_bundle(
            http_client.post,
            "/v1/sessions",
            bundle,
            metadata={
                "host_id": host.host_id,
                "workspace": str(workspace),
                "labels": {"omnigent.ui": "terminal", "omnigent.wrapper": "pi-native-ui"},
            },
            timeout=60,
        )
        create.raise_for_status()
        session_id = create.json()["session_id"]
        try:
            deadline = time.monotonic() + 150
            options = []
            while time.monotonic() < deadline and host.proc.poll() is None:
                snapshot = http_client.get(f"/v1/sessions/{session_id}", timeout=30)
                snapshot.raise_for_status()
                options = snapshot.json().get("model_options") or []
                if options:
                    break
                time.sleep(0.5)
            assert [option["id"] for option in options] == [_ENABLED_MODEL], (
                host.log_path.read_text(errors="replace")[-4000:]
            )
        finally:
            with contextlib.suppress(httpx.HTTPError):
                http_client.delete(f"/v1/sessions/{session_id}", timeout=10)
