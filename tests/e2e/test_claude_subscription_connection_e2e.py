"""Connect a Claude subscription and hand it to the owner's managed sandbox host.

A real server stores the pasted ``claude setup-token`` token through the KMS
credential cipher, backed by a loopback KMS stand-in, and vends it to the
launch token of the user's managed sandbox. The stub Kubernetes SDK captures
the sandbox's launch manifest, and the host's startup hook runs with that
container's environment against the real broker. No LLM or cluster is used.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import yaml

from omnigent.host.claude_credential import CLAUDE_OAUTH_TOKEN_ENV_VAR
from omnigent.host.identity import HOST_ID_ENV_VAR
from tests._helpers.live_server import find_free_port, isolated_process_env, terminate_process
from tests._helpers.polling import poll_until
from tests.e2e._k8s_stub_sdk import CAPTURE_ENV_VAR, STUB_FILES

_REPO_ROOT = Path(__file__).resolve().parents[2]
_KMS_KEY_ARN = "arn:aws:kms:us-east-1:111122223333:key/00000000-0000-0000-0000-000000000000"
_IMAGE_DEFAULT_TOKEN = "sk-ant-oat01-image-default"

# Server boot and manifest capture can exceed the repo-default timeout on a loaded box.
pytestmark = pytest.mark.timeout(600)


class _KmsHandler(BaseHTTPRequestHandler):
    """Answer KMS ``Encrypt`` and ``Decrypt`` with opaque, context-bound blobs."""

    server: _KmsServer

    def do_POST(self) -> None:
        action = self.headers["X-Amz-Target"].removeprefix("TrentService.")
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        context = body.get("EncryptionContext", {})
        self.server.actions.append(action)
        if action == "Encrypt":
            blob = os.urandom(32)
            self.server.blobs[blob] = (base64.b64decode(body["Plaintext"]), context)
            self._reply(200, {"CiphertextBlob": _b64(blob), "KeyId": body["KeyId"]})
            return
        if action == "Decrypt":
            plaintext, bound = self.server.blobs.get(
                base64.b64decode(body["CiphertextBlob"]), (None, None)
            )
            if plaintext is None or bound != context:
                self._reply(400, {"__type": "InvalidCiphertextException", "message": "mismatch"})
                return
            self._reply(200, {"Plaintext": _b64(plaintext), "KeyId": body["KeyId"]})
            return
        self._reply(400, {"__type": "UnsupportedOperationException", "message": action})

    def _reply(self, status: int, payload: dict[str, str]) -> None:
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/x-amz-json-1.1")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        return


class _KmsServer(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _KmsHandler)
        self.blobs: dict[bytes, tuple[bytes, dict[str, str]]] = {}
        self.actions: list[str] = []


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


@contextmanager
def _fake_kms() -> Iterator[_KmsServer]:
    kms = _KmsServer()
    thread = threading.Thread(target=kms.serve_forever, daemon=True)
    thread.start()
    try:
        yield kms
    finally:
        kms.shutdown()
        kms.server_close()


def _server_env(tmp_path: Path, kms: _KmsServer, capture_path: Path) -> dict[str, str]:
    """An isolated server env whose only AWS endpoint is the loopback KMS."""
    stub_root = tmp_path / "k8s_stub"
    for relative, source in STUB_FILES.items():
        target = stub_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source)
    home = tmp_path / "server-home"
    home.mkdir()
    ambient_aws: dict[str, str | None] = {
        name: None for name in os.environ if name.startswith("AWS_")
    }
    env = isolated_process_env(
        home,
        {
            **ambient_aws,
            "OMNIGENT_CLAUDE_CONNECT_ENABLED": "1",
            "OMNIGENT_CREDENTIAL_CIPHER": "kms",
            "OMNIGENT_CREDENTIAL_KMS_KEY_ID": _KMS_KEY_ARN,
            "AWS_ENDPOINT_URL_KMS": f"http://127.0.0.1:{kms.server_address[1]}",
            "AWS_ACCESS_KEY_ID": "e2e",
            "AWS_SECRET_ACCESS_KEY": "e2e",
            "AWS_DEFAULT_REGION": "us-east-1",
            "AWS_CONFIG_FILE": str(tmp_path / "no-aws-config"),
            "AWS_SHARED_CREDENTIALS_FILE": str(tmp_path / "no-aws-credentials"),
            "AWS_EC2_METADATA_DISABLED": "true",
            CAPTURE_ENV_VAR: str(capture_path),
            "OPENAI_API_KEY": "unused-no-turn-runs",
            "OMNIGENT_BUILTIN_AGENT_DIRS": str(
                _REPO_ROOT / "tests" / "resources" / "agents" / "sdk-chat-builtin.yaml"
            ),
        },
        None,
    )
    env["PYTHONPATH"] = os.pathsep.join([str(stub_root), env["PYTHONPATH"]])
    return env


def _write_server_config(tmp_path: Path, base_url: str) -> Path:
    config_path = tmp_path / "server-config.yaml"
    (tmp_path / "kubeconfig").write_text("")
    config_path.write_text(
        yaml.safe_dump(
            {
                "sandbox": {
                    "server_url": base_url,
                    "provider": "kubernetes",
                    "kubernetes": {
                        "image": "ghcr.io/omnigent-ai/omnigent-host:e2e",
                        "namespace": "omnigent-sandboxes",
                        "in_cluster": False,
                        "kubeconfig": str(tmp_path / "kubeconfig"),
                        "pod_ready_timeout_s": 600,
                    },
                }
            }
        )
    )
    return config_path


def _captured(capture_path: Path, call: str) -> dict:
    records = json.loads(capture_path.read_text()) if capture_path.exists() else []
    return next((record for record in records if record["call"] == call), {})


def _host_container_env(job: dict, secret: dict) -> dict[str, str]:
    """Resolve the sandbox host container's env as the kubelet would."""
    resolved: dict[str, str] = {}
    for entry in job["spec"]["template"]["spec"]["containers"][0]["env"]:
        if "value" in entry:
            resolved[entry["name"]] = str(entry["value"])
        elif "secretKeyRef" in entry.get("valueFrom", {}):
            key = entry["valueFrom"]["secretKeyRef"]["key"]
            resolved[entry["name"]] = secret["stringData"][key]
    return resolved


def test_connected_subscription_is_exported_by_the_owners_sandbox_host(tmp_path: Path) -> None:
    pasted_token = f"sk-ant-oat01-e2e-{uuid.uuid4().hex}"
    port = find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    capture_path = tmp_path / "submitted.json"
    log_path = tmp_path / "server.log"
    with _fake_kms() as kms, log_path.open("w") as log:
        proc = subprocess.Popen(
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
                f"sqlite:///{tmp_path / 'e2e.db'}",
                "--artifact-location",
                str(tmp_path / "artifacts"),
                "--config",
                str(_write_server_config(tmp_path, base_url)),
            ],
            env=_server_env(tmp_path, kms, capture_path),
            cwd=str(_REPO_ROOT),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            client = httpx.Client(base_url=base_url, timeout=30.0, trust_env=False)

            def healthy() -> bool:
                assert proc.poll() is None, log_path.read_text()[-3000:]
                try:
                    return client.get("/health").status_code == 200
                except httpx.HTTPError:
                    return False

            poll_until(healthy, timeout_s=180.0)
            assert "claude" in client.get("/v1/info").json()["enabled_connections"]

            # The user pastes their setup token in Settings.
            connected = client.put("/v1/connections/claude/token", json={"token": pasted_token})
            assert connected.status_code == 200, connected.text
            assert connected.json()["connected"] is True
            assert pasted_token not in connected.text
            assert kms.actions.count("Encrypt") == 1
            assert pasted_token.encode() not in (tmp_path / "e2e.db").read_bytes()

            # The user starts a managed sandbox session.
            agent_id = client.get("/v1/agents").json()["data"][0]["id"]
            created = client.post(
                "/v1/sessions", json={"agent_id": agent_id, "host_type": "managed"}, timeout=120.0
            )
            assert created.status_code == 201, created.text
            poll_until(
                lambda: (
                    _captured(capture_path, "create_namespaced_job")
                    and _captured(capture_path, "create_namespaced_secret")
                ),
                timeout_s=120.0,
            )
            job = _captured(capture_path, "create_namespaced_job")["manifest"]
            secret = _captured(capture_path, "create_namespaced_secret")["manifest"]
            host_env = _host_container_env(job, secret)

            # The sandbox host starts with the image's shared default and its own launch env.
            hook = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "import json, os, sys\n"
                    "from omnigent.host.claude_credential import configure_host_claude\n"
                    "applied = configure_host_claude(sys.argv[1], os.environ[sys.argv[2]])\n"
                    "print(json.dumps([applied, os.environ.get(sys.argv[3])]))\n",
                    base_url,
                    HOST_ID_ENV_VAR,
                    CLAUDE_OAUTH_TOKEN_ENV_VAR,
                ],
                env={
                    **host_env,
                    "IS_SANDBOX": "1",
                    CLAUDE_OAUTH_TOKEN_ENV_VAR: _IMAGE_DEFAULT_TOKEN,
                    "HOME": str(tmp_path / "pod-home"),
                    "PATH": os.environ["PATH"],
                    "PYTHONPATH": os.pathsep.join(
                        [str(_REPO_ROOT), str(_REPO_ROOT / "sdks" / "python-client")]
                    ),
                    "NO_PROXY": "127.0.0.1,localhost",
                },
                capture_output=True,
                text=True,
                timeout=60.0,
            )
            assert hook.returncode == 0, hook.stderr
            assert json.loads(hook.stdout.strip().splitlines()[-1]) == [True, pasted_token]
            assert "Decrypt" in kms.actions
        finally:
            terminate_process(proc)
