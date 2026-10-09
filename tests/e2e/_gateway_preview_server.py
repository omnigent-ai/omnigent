"""Local sandbox transport for the gateway-preview launch journey.

Only cloud allocation and the user catalog are substituted. Registration,
managed launch, host/runner tunnels, session storage, and native Codex are real.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from omnigent.host.identity import HOST_ID_ENV_VAR, HOST_NAME_ENV_VAR, HOST_TOKEN_ENV_VAR
from omnigent.onboarding.sandboxes.base import SandboxHostLauncher
from omnigent.server.managed_hosts import ManagedSandboxConfig, ManagedSandboxDeployment
from tests._helpers.live_server import terminate_process

MODEL = "system.ai.gpt-6-astra"


class _LocalSandbox(SandboxHostLauncher):
    provider = "test-gateway"

    def __init__(self, root: Path) -> None:
        self.root = root
        self.host: subprocess.Popen | None = None

    def prepare(self) -> None:
        pass

    def provision(self, name: str) -> str:
        (self.root / "provisioned").write_text(name)
        (self.root / "workspace").mkdir(exist_ok=True)
        return name

    def start_host(
        self,
        sandbox_id: str,
        *,
        token: str,
        host_id: str,
        host_name: str,
        server_url: str,
        **kwargs,
    ) -> str:
        workspace = self.root / "workspace"
        env = {
            **os.environ,
            HOST_ID_ENV_VAR: host_id,
            HOST_NAME_ENV_VAR: host_name,
            HOST_TOKEN_ENV_VAR: token,
            "OMNIGENT_DATA_DIR": str(self.root / "host-data"),
        }
        with (self.root / "host.log").open("ab") as log:
            self.host = subprocess.Popen(
                [sys.executable, "-m", "omnigent.host._daemon_entry", "--server", server_url],
                env=env,
                cwd=workspace,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        return str(workspace)

    def terminate(self, sandbox_id: str) -> None:
        terminate_process(self.host)


async def _models(harness: str, user_id: str | None) -> list[dict]:
    if harness != "codex-native":
        return []
    return [
        {
            "id": MODEL,
            "displayName": "Astra 6",
            "supportedReasoningEfforts": [{"reasoningEffort": "max"}],
        }
    ]


def main() -> None:
    import omnigent.server.app as server_app
    from omnigent.cli import main as cli_main

    root = Path(os.environ["OMNIGENT_E2E_GATEWAY_ROOT"])
    launcher = _LocalSandbox(root)
    port = sys.argv[sys.argv.index("--port") + 1]
    deployment = ManagedSandboxDeployment.single(
        ManagedSandboxConfig(
            server_url=f"http://127.0.0.1:{port}",
            launcher_factory=lambda: launcher,
            provider=launcher.provider,
            token_ttl_s=3600,
            gateway_model_options=_models,
        )
    )
    real_create_app = server_app.create_app

    def create_app(*args, **kwargs):
        kwargs["sandbox_config"] = deployment
        return real_create_app(*args, **kwargs)

    server_app.create_app = create_app
    try:
        cli_main()
    finally:
        launcher.terminate("test")


if __name__ == "__main__":
    main()
