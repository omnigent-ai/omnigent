"""Owner-bound credentials across native warm activation, wake, and teardown.

Kubernetes allocation/exec and the external cipher are test doubles. Managed
orchestration, activation payload construction, stores, and broker routes are real.
"""

from __future__ import annotations

import copy
import json
import sys
import types
import uuid
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnigent.connections.databricks import DatabricksConnectionStore
from omnigent.connections.github import GithubConnectionStore
from omnigent.db.utils import now_epoch
from omnigent.host.identity import MANAGED_HOST_TOKEN_HEADER
from omnigent.onboarding.sandboxes.agent_sandbox import WORKSPACE_SIZE_ENV_VAR
from omnigent.onboarding.sandboxes.agent_sandbox_warm_pool import (
    AgentSandboxWarmPoolLauncher,
    WarmPoolHandle,
)
from omnigent.onboarding.sandboxes.types import RepoWorkspace
from omnigent.server.databricks_app import DatabricksTokenSet
from omnigent.server.github_app import GitHubTokenSet
from omnigent.server.managed_hosts import (
    ManagedSandboxConfig,
    ManagedSandboxDeployment,
    launch_managed_host,
    resume_managed_host,
    terminate_managed_host,
)
from omnigent.server.routes.host_credentials import create_host_credentials_router
from omnigent.stores.host_store import HostStore


class _Pod:
    """The Pod fields consumed by launch readiness and profile validation."""

    def __init__(self, manifest: dict[str, Any]) -> None:
        self.manifest = manifest
        self.metadata = SimpleNamespace(**manifest["metadata"])
        self.status = SimpleNamespace(
            phase="Running",
            container_statuses=[
                SimpleNamespace(
                    name=item["name"],
                    ready=True,
                    state=SimpleNamespace(running=SimpleNamespace()),
                )
                for item in manifest["spec"]["containers"]
            ],
        )


@pytest.fixture
def fake_kubernetes_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep this default-CI integration test independent of the optional SDK."""

    class ApiClient:
        def __enter__(self) -> ApiClient:
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def sanitize_for_serialization(self, pod: _Pod) -> dict[str, Any]:
            value = copy.deepcopy(pod.manifest)
            value["metadata"]["uid"] = pod.metadata.uid
            return value

    client = types.ModuleType("kubernetes.client")
    monkeypatch.setattr(client, "ApiClient", ApiClient, raising=False)
    monkeypatch.setattr(client, "V1DeleteOptions", SimpleNamespace, raising=False)
    monkeypatch.setattr(client, "V1Preconditions", SimpleNamespace, raising=False)
    rest = types.ModuleType("kubernetes.client.rest")
    monkeypatch.setattr(
        rest, "ApiException", type("ApiException", (Exception,), {}), raising=False
    )
    package = types.ModuleType("kubernetes")
    monkeypatch.setattr(package, "client", client, raising=False)
    for name, module in (
        ("kubernetes", package),
        ("kubernetes.client", client),
        ("kubernetes.client.rest", rest),
    ):
        monkeypatch.setitem(sys.modules, name, module)


class _MemoryCipher:
    """Stand in for the external encryption service while retaining row binding."""

    def __init__(self) -> None:
        self.values: dict[str, tuple[str, dict[str, str]]] = {}

    def encrypt(self, plaintext: str, *, context: Mapping[str, str]) -> str:
        ciphertext = uuid.uuid4().hex
        self.values[ciphertext] = (plaintext, dict(context))
        return ciphertext

    def decrypt(self, ciphertext: str, *, context: Mapping[str, str]) -> str | None:
        plaintext, expected = self.values[ciphertext]
        return plaintext if expected == dict(context) else None


class _WarmCluster:
    """Mock infrastructure beneath the production launch/activation methods."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, host_store: HostStore) -> None:
        self.monkeypatch = monkeypatch
        self.host_store = host_store
        self.members: dict[str, dict[str, Any]] = {}
        self.activations: list[dict[str, Any]] = []
        self.client: TestClient
        self.custom = Mock()
        self.custom.delete_namespaced_custom_object.side_effect = self._delete_claim
        self.core = Mock()
        self.core.delete_namespaced_pod.side_effect = self._delete_pod

    def launcher(self) -> AgentSandboxWarmPoolLauncher:
        launcher = AgentSandboxWarmPoolLauncher(
            warm_pool="shared-hosts-v1",
            namespace="pool-test",
            env=[],
            image="example.com/omnigent-host:pool-test",
        )
        self.monkeypatch.setattr(launcher, "prepare", lambda: None)
        self.monkeypatch.setattr(launcher, "provision", lambda name: self._provision(launcher))
        self.monkeypatch.setattr(
            launcher, "_allocation", lambda handle: self.members[handle.claim_name]["sandbox"]
        )
        self.monkeypatch.setattr(
            launcher, "_pod", lambda handle, sandbox: self.members[handle.claim_name]["pod"]
        )
        self.monkeypatch.setattr(launcher, "_load_custom", lambda: self.custom)
        self.monkeypatch.setattr(launcher, "_load_core", lambda: self.core)
        self.monkeypatch.setattr(launcher, "_patch_deadline", lambda handle, boot: None)
        self.monkeypatch.setattr(launcher, "_exec", self._exec)
        return launcher

    def _provision(self, launcher: AgentSandboxWarmPoolLauncher) -> str:
        index = len(self.members) + 1
        handle = WarmPoolHandle(
            "pool-test",
            f"claim-{index}",
            str(uuid.uuid4()),
            f"sandbox-{index}",
            str(uuid.uuid4()),
        )
        profile = launcher.template_spec()
        pod = copy.deepcopy(profile["podTemplate"])
        pod["metadata"].update(
            {
                "name": handle.sandbox_name,
                "namespace": handle.namespace,
                "uid": str(uuid.uuid4()),
                "ownerReferences": [
                    {
                        "apiVersion": "agents.x-k8s.io/v1beta1",
                        "kind": "Sandbox",
                        "name": handle.sandbox_name,
                        "uid": handle.sandbox_uid,
                        "controller": True,
                    }
                ],
            }
        )
        self.members[handle.claim_name] = {
            "handle": handle,
            "pod": _Pod(pod),
            "payload": None,
            "sandbox": {
                "metadata": {"name": handle.sandbox_name, "uid": handle.sandbox_uid},
                "spec": profile,
            },
        }
        return handle.encode()

    def _exec(
        self,
        handle: WarmPoolHandle,
        pod_name: str,
        mode: str,
        payload: dict[str, Any] | None = None,
    ) -> str:
        member = self.members[handle.claim_name]
        assert pod_name == member["pod"].metadata.name
        if mode == "status":
            accepted = member["payload"]
            return json.dumps(
                {
                    "stage": "prepared" if accepted else "waiting",
                    "generation": accepted["generation"] if accepted else None,
                }
            )
        assert mode == "activate" and payload is not None
        assert member["payload"] is None
        assert payload["pod_uid"] == member["pod"].metadata.uid
        host = self.host_store.resolve_launch_token(payload["host_id"], payload["token"])
        assert host is not None and host.sandbox_id == handle.encode()
        # The actual payload must already authenticate to both brokers at delivery.
        for provider in ("github", "databricks"):
            response = self.credential(payload, provider)
            assert response.status_code == 200
            assert response.json()["owner"] == host.user_id
            assert response.json()["connected"] is True
            assert response.headers["cache-control"] == "no-store"
        member["payload"] = copy.deepcopy(payload)
        self.activations.append(copy.deepcopy(payload))
        self.host_store.upsert_on_connect(
            host_id=host.host_id,
            name=host.name,
            user_id=host.user_id,
        )
        return ""

    def credential(self, payload: dict[str, Any], provider: str):
        return self.client.get(
            f"/v1/hosts/{payload['host_id']}/credentials/{provider}",
            headers={MANAGED_HOST_TOKEN_HEADER: payload["token"]},
        )

    def _delete_pod(self, name: str, namespace: str, *, body: Any, **kwargs: Any) -> None:
        member = next(item for item in self.members.values() if item["pod"].metadata.name == name)
        assert namespace == member["handle"].namespace
        assert body.preconditions.uid == member["pod"].metadata.uid
        member["pod"].metadata.uid = str(uuid.uuid4())
        member["payload"] = None

    def _delete_claim(
        self,
        group: str,
        version: str,
        namespace: str,
        plural: str,
        name: str,
        *,
        body: dict[str, Any],
        **kwargs: Any,
    ) -> None:
        member = self.members[name]
        assert body["preconditions"]["uid"] == member["handle"].claim_uid
        del self.members[name]


async def test_warm_activation_keeps_owner_brokers_across_refresh_wake_and_delete(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    fake_kubernetes_sdk: None,
) -> None:
    monkeypatch.delenv(WORKSPACE_SIZE_ENV_VAR, raising=False)
    host_store = HostStore(db_uri)
    cipher = _MemoryCipher()
    github_store = GithubConnectionStore(db_uri, cipher)
    databricks_store = DatabricksConnectionStore(db_uri, cipher)
    expiry = now_epoch() + 3600
    for index, owner in enumerate(("alice", "bob")):
        github_store.upsert(
            owner,
            github_login=owner,
            github_user_id=index,
            tokens=GitHubTokenSet(f"gh-{owner}", f"gh-refresh-{owner}", expiry, None, "repo"),
        )
        databricks_store.upsert(
            owner,
            workspace_host=f"https://{owner}.cloud.databricks.com",
            databricks_user=f"{owner}@example.com",
            databricks_user_id=str(index),
            tokens=DatabricksTokenSet(
                f"db-{owner}", f"db-refresh-{owner}", expiry, None, "all-apis"
            ),
        )
    github_client = SimpleNamespace(
        refresh_token=AsyncMock(
            return_value=GitHubTokenSet("gh-alice-rotated", "gh-new-refresh", expiry, None, "repo")
        )
    )
    databricks_client = SimpleNamespace(
        refresh_token=AsyncMock(
            return_value=DatabricksTokenSet(
                "db-alice-rotated", "db-new-refresh", expiry, None, "all-apis"
            )
        )
    )
    app = FastAPI()
    app.state.github_store, app.state.github_client = github_store, github_client
    app.state.databricks_store, app.state.databricks_client = databricks_store, databricks_client
    app.include_router(create_host_credentials_router(host_store), prefix="/v1")
    cluster = _WarmCluster(monkeypatch, host_store)
    deployment = ManagedSandboxDeployment.single(
        ManagedSandboxConfig(
            provider="agent_sandbox",
            server_url="http://testserver",
            launcher_factory=cluster.launcher,
            token_ttl_s=3600,
        )
    )
    repo = RepoWorkspace(
        url="https://github.com/example/private.git", repo_name="private", branch="main"
    )

    with TestClient(app) as client:
        cluster.client = client
        alice_launch = await launch_managed_host(
            config=deployment,
            owner="alice",
            host_store=host_store,
            repos=[repo],
        )
        await launch_managed_host(
            config=deployment,
            owner="bob",
            host_store=host_store,
            repos=[repo],
        )
        alice, bob = cluster.activations
        assert alice["host_id"] != bob["host_id"] and alice["pod_uid"] != bob["pod_uid"]
        assert alice_launch.workspace.endswith("/private")
        for payload, owner in ((alice, "alice"), (bob, "bob")):
            assert cluster.credential(payload, "github").json()["token"] == f"gh-{owner}"
            databricks = cluster.credential(payload, "databricks").json()
            assert databricks["token"] == f"db-{owner}"
            assert databricks["workspace_host"] == f"https://{owner}.cloud.databricks.com"
            command = json.dumps(payload["prepare_command"])
            assert "configure_clone_credentials" in command
            assert payload["token"] not in command
            assert f"gh-{owner}" not in json.dumps(payload)
            assert f"db-{owner}" not in json.dumps(payload)
        for provider in ("github", "databricks"):
            assert (
                cluster.credential({**alice, "token": bob["token"]}, provider).status_code == 401
            )

        # Existing warm hosts fetch provider rotations through the same stores.
        github_store.update_tokens(
            "alice",
            GitHubTokenSet(
                "expired-gh",
                "gh-refresh-alice",
                now_epoch() - 1,
                None,
                "repo",
            ),
        )
        databricks_store.update_tokens(
            "alice",
            DatabricksTokenSet(
                "expired-db",
                "db-refresh-alice",
                now_epoch() - 1,
                None,
                "all-apis",
            ),
        )
        assert cluster.credential(alice, "github").json()["token"] == "gh-alice-rotated"
        assert cluster.credential(alice, "databricks").json()["token"] == "db-alice-rotated"
        github_client.refresh_token.assert_awaited_once_with("gh-refresh-alice")
        databricks_client.refresh_token.assert_awaited_once_with(
            "https://alice.cloud.databricks.com",
            "db-refresh-alice",
        )

        host = host_store.get_host(alice_launch.host_id)
        assert host is not None
        assigned_handle = host.sandbox_id
        await resume_managed_host(
            alice_launch.host_id, host_store, deployment, repos=[repo], force=True
        )
        resumed = cluster.activations[-1]
        assert resumed["host_id"] == alice["host_id"]
        assert resumed["token"] != alice["token"] and resumed["pod_uid"] != alice["pod_uid"]
        host = host_store.get_host(alice_launch.host_id)
        assert host is not None and host.sandbox_id == assigned_handle
        for provider in ("github", "databricks"):
            assert cluster.credential(alice, provider).status_code == 401
            assert cluster.credential(resumed, provider).json()["owner"] == "alice"

        await terminate_managed_host(host, host_store, deployment)
        assert host_store.get_host(alice_launch.host_id) is None
        assert len(cluster.members) == 1
        for provider in ("github", "databricks"):
            assert cluster.credential(resumed, provider).status_code == 401
            assert cluster.credential(bob, provider).json()["owner"] == "bob"
