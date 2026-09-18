"""Native claim allocation, activation, and durable workspace lifecycle tests."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import sys
import time
import types
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import click
import pytest
import yaml

from omnigent.host.identity import HOST_ID_ENV_VAR, HOST_NAME_ENV_VAR, HOST_TOKEN_ENV_VAR
from omnigent.onboarding.sandboxes import agent_sandbox_warm_pool as warm
from omnigent.onboarding.sandboxes import kubernetes as k8s
from omnigent.onboarding.sandboxes.agent_sandbox import (
    STORAGE_CLASS_ENV_VAR,
    WORKSPACE_SIZE_ENV_VAR,
    AgentSandboxLauncher,
)
from omnigent.onboarding.sandboxes.base import SandboxGoneError
from omnigent.onboarding.sandboxes.types import RepoWorkspace

_NAMESPACE = "original-runners"
_CLAIM_UID = "377e70bc-61e5-4a4c-b79e-4913d561f496"
_SANDBOX_UID = "822161cd-35bc-430e-a3fc-7bb8006d80b4"
_POD_UID = "f95a2a89-ed5a-40b5-b6c3-d5d6295a56c3"
_OTHER_UID = "dcb5bd52-bfc3-469e-937b-f830e4e56337"
_TOKEN = "test-host-token-never-in-argv"
_GENERATION = hashlib.sha256(_TOKEN.encode()).hexdigest()[:32]
_HANDLE = warm.WarmPoolHandle(_NAMESPACE, "warm-claim", _CLAIM_UID, "pooled-sandbox", _SANDBOX_UID)
_START_ARGS = {
    "token": _TOKEN,
    "host_id": "01b57a70573748edba1f599dd76ebf94",
    "host_name": "managed-test",
    "server_url": "https://omnigent.example",
}


class _ApiError(Exception):
    def __init__(self, status: int, reason: str = "API failed") -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


class _ApiClient:
    def __init__(self, configuration: Any = None) -> None:
        self.configuration = configuration

    def __enter__(self) -> _ApiClient:
        return self

    def __exit__(self, *_args: Any) -> None:
        pass

    def sanitize_for_serialization(self, value: Any) -> Any:
        return value.raw if isinstance(value, SimpleNamespace) else value


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith(("OMNIGENT_KUBERNETES_", "OMNIGENT_AGENT_SANDBOX_")):
            monkeypatch.delenv(name)


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Keep unit coverage available without installing the optional Kubernetes SDK."""
    client = types.ModuleType("kubernetes.client")
    client.ApiClient = _ApiClient  # type: ignore[attr-defined]
    client.V1DeleteOptions = SimpleNamespace  # type: ignore[attr-defined]
    client.V1Preconditions = SimpleNamespace  # type: ignore[attr-defined]
    client.CoreV1Api = MagicMock()  # type: ignore[attr-defined]
    rest = types.ModuleType("kubernetes.client.rest")
    rest.ApiException = _ApiError  # type: ignore[attr-defined]
    stream = types.ModuleType("kubernetes.stream")
    stream.stream = MagicMock()  # type: ignore[attr-defined]
    package = types.ModuleType("kubernetes")
    package.client = client  # type: ignore[attr-defined]
    for name, module in (
        ("kubernetes", package),
        ("kubernetes.client", client),
        ("kubernetes.client.rest", rest),
        ("kubernetes.stream", stream),
    ):
        monkeypatch.setitem(sys.modules, name, module)
    return SimpleNamespace(client=client, stream=stream.stream)


def _launcher(**kwargs: Any) -> warm.AgentSandboxWarmPoolLauncher:
    options: dict[str, Any] = {
        "warm_pool": "warm-pool-v1",
        "image": "host-image:test",
        "namespace": _NAMESPACE,
        "service_account": "sandbox-runner",
        "env": (),
        "in_cluster": True,
        **kwargs,
    }
    return warm.AgentSandboxWarmPoolLauncher(**options)


def _pod(spec: dict[str, Any], *, uid: str = _POD_UID) -> SimpleNamespace:
    raw = copy.deepcopy(spec["podTemplate"])
    raw["metadata"].update({"name": "pooled-sandbox-pod", "uid": uid})
    metadata = SimpleNamespace(
        name="pooled-sandbox-pod",
        uid=uid,
        deletion_timestamp=None,
        owner_references=[SimpleNamespace(uid=_SANDBOX_UID, controller=True)],
    )
    containers = [
        SimpleNamespace(
            name=item["name"],
            ready=True,
            state=SimpleNamespace(running=SimpleNamespace(), waiting=None, terminated=None),
        )
        for item in raw["spec"]["containers"]
    ]
    return SimpleNamespace(
        metadata=metadata,
        status=SimpleNamespace(
            phase="Running", container_statuses=containers, init_container_statuses=[]
        ),
        raw=raw,
    )


@dataclass
class _Harness:
    launcher: warm.AgentSandboxWarmPoolLauncher
    custom: MagicMock
    core: MagicMock
    closed: MagicMock
    resources: dict[str, dict[str, Any]]
    pod: SimpleNamespace

    @property
    def claim(self) -> dict[str, Any]:
        return self.resources[warm.CLAIMS]

    @property
    def sandbox(self) -> dict[str, Any]:
        return self.resources[warm.SANDBOX_PLURAL]

    @property
    def template(self) -> dict[str, Any]:
        return self.resources[warm.TEMPLATES]


@pytest.fixture
def harness(sdk: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> _Harness:
    launcher = _launcher()
    spec = launcher.template_spec()
    resources = {
        warm.POOLS: {"spec": {"sandboxTemplateRef": {"name": "template-v1"}}},
        warm.TEMPLATES: {"spec": copy.deepcopy(spec)},
        warm.CLAIMS: {
            "metadata": {"name": _HANDLE.claim_name, "uid": _CLAIM_UID},
            "spec": {},
            "status": {"sandbox": {"name": _HANDLE.sandbox_name}},
        },
        warm.SANDBOX_PLURAL: {
            "metadata": {
                "name": _HANDLE.sandbox_name,
                "uid": _SANDBOX_UID,
                "ownerReferences": [{"uid": _CLAIM_UID, "controller": True}],
                "annotations": {"agents.x-k8s.io/pod-name": "pooled-sandbox-pod"},
            },
            "spec": copy.deepcopy(spec),
        },
    }
    custom = MagicMock()

    def get(group: str, version: str, namespace: str, plural: str, name: str, **_: Any) -> Any:
        assert namespace == _NAMESPACE
        assert version == warm.API_VERSION
        return copy.deepcopy(resources[plural])

    def create(
        group: str, version: str, namespace: str, plural: str, body: dict[str, Any], **_: Any
    ) -> Any:
        assert plural == warm.CLAIMS
        resources[plural]["spec"] = copy.deepcopy(body["spec"])
        return copy.deepcopy(resources[plural])

    custom.get_namespaced_custom_object.side_effect = get
    custom.create_namespaced_custom_object.side_effect = create
    core = MagicMock()
    pod = _pod(spec)
    core.read_namespaced_pod.return_value = pod
    closed = MagicMock()
    monkeypatch.setattr(launcher, "_load_custom", lambda: custom)
    monkeypatch.setattr(launcher, "_load_core", lambda: core)
    monkeypatch.setattr(launcher, "_close_clients", closed)
    monkeypatch.setattr(warm, "_new_pod_name", lambda name: _HANDLE.claim_name)
    monkeypatch.setattr(
        warm, "time", SimpleNamespace(monotonic=time.monotonic, sleep=lambda _: None)
    )
    return _Harness(launcher, custom, core, closed, resources, pod)


def _exec_states(harness: _Harness, monkeypatch: pytest.MonkeyPatch, *stages: str) -> MagicMock:
    states = iter(stages)

    def execute(handle: Any, pod_name: str, mode: str, payload: Any = None) -> str:
        if mode == "activate":
            return ""
        stage = next(states)
        return json.dumps(
            {"stage": stage, "generation": None if stage == "waiting" else _GENERATION}
        )

    result = MagicMock(side_effect=execute)
    monkeypatch.setattr(harness.launcher, "_exec", result)
    return result


def _set_profile(harness: _Harness, profile: dict[str, Any]) -> None:
    harness.template["spec"] = copy.deepcopy(profile)
    harness.sandbox["spec"] = copy.deepcopy(profile)
    harness.pod.raw = _pod(profile).raw


def test_template_is_unassigned_and_preserves_hardening() -> None:
    spec = _launcher().template_spec()
    pod = spec["podTemplate"]["spec"]
    assert not {"shutdownTime", "shutdownPolicy", "operatingMode"} & spec.keys()
    assert "initContainers" not in pod
    assert pod["automountServiceAccountToken"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    assert pod["serviceAccountName"] == "sandbox-runner"
    for container in pod["containers"]:
        env = {item["name"]: item for item in container["env"]}
        assert not {HOST_ID_ENV_VAR, HOST_NAME_ENV_VAR, HOST_TOKEN_ENV_VAR} & env.keys()
        assert env[warm.POD_UID_ENV_VAR]["valueFrom"]["fieldRef"]["fieldPath"] == "metadata.uid"
        assert container["securityContext"]["allowPrivilegeEscalation"] is False
        assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}
        assert container["readinessProbe"]["exec"]["command"] == [
            "python3",
            "-m",
            "omnigent.host.warm_bootstrap",
            "ready",
        ]
        assert container["command"] == [
            "python3",
            "-m",
            "omnigent.host.warm_bootstrap",
            "prepare" if container["name"] == "bootstrap" else "host",
        ]
    assert {item["name"] for item in pod["containers"]} == {"bootstrap", "host"}
    activation = next(item for item in pod["volumes"] if item["name"] == "activation")
    assert activation["emptyDir"]["medium"] == "Memory"
    bootstrap, host = pod["containers"]
    assert not next(m for m in bootstrap["volumeMounts"] if m["name"] == "activation").get(
        "readOnly"
    )
    assert next(m for m in host["volumeMounts"] if m["name"] == "activation")["readOnly"] is True


def test_template_preserves_storage_mount_separation_and_agent_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(WORKSPACE_SIZE_ENV_VAR, "20Gi")
    monkeypatch.setenv(STORAGE_CLASS_ENV_VAR, "fast-storage")
    resources = {
        "requests": {"cpu": "2", "memory": "4Gi"},
        "limits": {"cpu": "4", "memory": "8Gi"},
    }
    launcher = _launcher(
        resources=resources,
        secret_name="harness-credentials",
        pvc_mounts=[{"claim_name": "dataset", "mount_path": "/data", "read_only": True}],
        secret_mounts=[{"secret_name": "runtime-secret", "mount_path": "/secrets/runtime"}],
        runtime_class="kata",
        node_selector={"kubernetes.io/arch": "arm64"},
        tolerations=[{"key": "sandbox", "operator": "Exists"}],
    )
    spec = launcher.template_spec(agent_name="analysis-agent")
    pod = spec["podTemplate"]["spec"]
    assert spec["podTemplate"]["metadata"]["labels"][k8s._AGENT_LABEL] == "analysis-agent"
    assert pod["runtimeClassName"] == "kata"
    assert pod["nodeSelector"] == {"kubernetes.io/arch": "arm64"}
    assert pod["tolerations"] == [{"key": "sandbox", "operator": "Exists"}]
    bootstrap, host = pod["containers"]
    assert bootstrap["resources"] == host["resources"] == resources
    assert {m["name"] for m in bootstrap["volumeMounts"]} == {"home", "activation"}
    assert {m["name"] for m in host["volumeMounts"]} == {"home", "activation", "pvc-0", "secret-0"}
    assert (
        bootstrap["envFrom"] == host["envFrom"] == [{"secretRef": {"name": "harness-credentials"}}]
    )
    (claim,) = spec["volumeClaimTemplates"]
    assert claim["metadata"]["name"] == "home"
    assert claim["spec"]["storageClassName"] == "fast-storage"
    assert claim["spec"]["resources"]["requests"]["storage"] == "20Gi"


def test_template_profile_is_deterministic_and_changes_with_configuration() -> None:
    baseline = _launcher().template_spec()
    assert baseline == _launcher().template_spec(host_config={"profile": "oss"})
    annotation = baseline["podTemplate"]["metadata"]["annotations"][warm.PROFILE_ANNOTATION]
    changed = _launcher(image="host-image:new-version").template_spec()
    assert changed["podTemplate"]["metadata"]["annotations"][warm.PROFILE_ANNOTATION] != annotation


def test_shared_template_is_unclassified_and_has_a_distinct_profile() -> None:
    launcher = _launcher()
    legacy = launcher.template_spec()["podTemplate"]["metadata"]
    shared = launcher.template_spec(shared=True)
    metadata = shared["podTemplate"]["metadata"]
    assert warm.SHARED_POOL_ANNOTATION not in legacy["annotations"]
    assert metadata["annotations"][warm.SHARED_POOL_ANNOTATION] == "true"
    assert k8s._AGENT_LABEL not in metadata["labels"]
    assert (
        metadata["annotations"][warm.PROFILE_ANNOTATION]
        != legacy["annotations"][warm.PROFILE_ANNOTATION]
    )
    assert shared == launcher.template_spec(shared=True)


def test_pool_generator_emits_explicit_shared_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = tmp_path / "server.yaml"
    config.write_text(
        json.dumps(
            {
                "sandbox": {
                    "provider": "agent_sandbox",
                    "server_url": "https://omnigent.example",
                    "kubernetes": {"image": "host-image:test", "namespace": _NAMESPACE},
                }
            }
        )
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["warm-pool", "--config", str(config), "--name", "shared-v1", "--shared"],
    )
    warm.main()
    template, pool = yaml.safe_load_all(capsys.readouterr().out)
    assert template["kind"] == "SandboxTemplate"
    metadata = template["spec"]["podTemplate"]["metadata"]
    assert metadata["annotations"][warm.SHARED_POOL_ANNOTATION] == "true"
    assert k8s._AGENT_LABEL not in metadata["labels"]
    assert pool["spec"]["sandboxTemplateRef"] == {"name": template["metadata"]["name"]}


def test_pool_generator_rejects_shared_agent_classifier(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "warm-pool",
            "--config",
            "unused.yaml",
            "--name",
            "invalid-v1",
            "--shared",
            "--agent-name",
            "privileged-agent",
        ],
    )
    with pytest.raises(SystemExit) as raised:
        warm.main()
    assert raised.value.code == 2
    assert "not allowed with" in capsys.readouterr().err


@pytest.mark.parametrize("agent_name", [None, "claude-native-ui", "codex-native-ui"])
def test_shared_pool_claims_and_activates_for_different_agents(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, agent_name: str | None
) -> None:
    _set_profile(harness, harness.launcher.template_spec(shared=True))
    harness.launcher.prepare_for_launch(agent_name=agent_name)
    execute = _exec_states(harness, monkeypatch, "waiting", "prepared")
    handle = harness.launcher.provision("managed-test")
    assert handle == _HANDLE.encode()
    repo = RepoWorkspace("https://github.com/example/private.git", None, "private")
    assert (
        harness.launcher.start_host(handle, **_START_ARGS, agent_name=agent_name, repos=[repo])
        == "/home/omnigent/workspace/private"
    )
    payload = next(call.args[3] for call in execute.call_args_list if call.args[2] == "activate")
    assert payload["host_id"] == _START_ARGS["host_id"]
    assert payload["token"] == _TOKEN
    assert "omnigent.git_credential_github" in payload["prepare_command"][-1]
    assert _TOKEN not in json.dumps(payload["prepare_command"])
    assert k8s._AGENT_LABEL not in harness.pod.raw["metadata"]["labels"]


@pytest.mark.parametrize("agent_name", [None, "different-agent", "privileged-agent"])
def test_classified_pool_still_requires_its_exact_agent(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, agent_name: str | None
) -> None:
    _set_profile(harness, harness.launcher.template_spec(agent_name="privileged-agent"))
    harness.launcher.prepare_for_launch(agent_name=agent_name)
    direct = MagicMock(return_value="direct-sandbox")
    monkeypatch.setattr(AgentSandboxLauncher, "provision", direct)
    if agent_name == "privileged-agent":
        assert harness.launcher.provision("managed-test") == _HANDLE.encode()
        execute = _exec_states(harness, monkeypatch, "waiting", "prepared")
        harness.launcher.start_host(_HANDLE.encode(), **_START_ARGS, agent_name=agent_name)
        assert any(call.args[2] == "activate" for call in execute.call_args_list)
        direct.assert_not_called()
    else:
        assert harness.launcher.provision("managed-test") == "direct-sandbox"
        harness.custom.create_namespaced_custom_object.assert_not_called()
        direct.assert_called_once_with("managed-test")
    assert harness.pod.raw["metadata"]["labels"][k8s._AGENT_LABEL] == "privileged-agent"


@pytest.mark.parametrize("marker", ["false", "TRUE", ""])
def test_unknown_shared_marker_is_rejected_before_claiming(harness: _Harness, marker: str) -> None:
    metadata = harness.template["spec"]["podTemplate"]["metadata"]
    metadata["annotations"][warm.SHARED_POOL_ANNOTATION] = marker
    with pytest.raises(click.ClickException):
        harness.launcher.provision("managed-test")
    harness.custom.create_namespaced_custom_object.assert_not_called()


@pytest.mark.parametrize("classifier", ["privileged-agent", ""])
def test_shared_template_with_classifier_is_rejected_before_claiming(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, classifier: str
) -> None:
    _set_profile(harness, harness.launcher.template_spec(shared=True))
    harness.template["spec"]["podTemplate"]["metadata"]["labels"][k8s._AGENT_LABEL] = classifier
    direct = MagicMock()
    monkeypatch.setattr(AgentSandboxLauncher, "provision", direct)
    with pytest.raises(click.ClickException):
        harness.launcher.provision("managed-test")
    harness.custom.create_namespaced_custom_object.assert_not_called()
    direct.assert_not_called()


@pytest.mark.parametrize("pool_name", [None, "replacement-pool"])
@pytest.mark.parametrize("agent_name", [None, "codex-native-ui"])
def test_shared_allocation_wakes_with_new_agent_and_pool_configuration(
    harness: _Harness,
    monkeypatch: pytest.MonkeyPatch,
    pool_name: str | None,
    agent_name: str | None,
) -> None:
    _set_profile(harness, harness.launcher.template_spec(shared=True))
    harness.launcher.prepare_for_launch(agent_name="claude-native-ui")
    assert harness.launcher.provision("managed-test") == _HANDLE.encode()
    harness.custom.reset_mock()
    del harness.resources[warm.POOLS]
    del harness.resources[warm.TEMPLATES]
    harness.launcher = _launcher(warm_pool=pool_name)
    monkeypatch.setattr(harness.launcher, "_load_custom", lambda: harness.custom)
    monkeypatch.setattr(harness.launcher, "_load_core", lambda: harness.core)
    monkeypatch.setattr(harness.launcher, "_close_clients", harness.closed)
    harness.launcher.prepare_for_launch(agent_name=agent_name)
    harness.launcher.resume(_HANDLE.encode())
    harness.core.read_namespaced_pod.return_value = _pod(harness.sandbox["spec"], uid=_OTHER_UID)
    execute = _exec_states(harness, monkeypatch, "waiting", "prepared")
    assert (
        harness.launcher.start_host(_HANDLE.encode(), **_START_ARGS, agent_name=agent_name)
        == "/home/omnigent/workspace"
    )
    payload = next(call.args[3] for call in execute.call_args_list if call.args[2] == "activate")
    assert payload["pod_uid"] == _OTHER_UID
    assert payload["host_id"] == _START_ARGS["host_id"]
    harness.custom.create_namespaced_custom_object.assert_not_called()
    harness.custom.delete_namespaced_custom_object.assert_not_called()


@pytest.mark.parametrize(
    "change", ["classifier", "empty_classifier", "missing_marker", "unknown_marker", "fingerprint"]
)
def test_shared_pod_identity_changes_are_rejected_before_delivering_credentials(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    _set_profile(harness, harness.launcher.template_spec(shared=True))
    metadata = harness.pod.raw["metadata"]
    if change in {"classifier", "empty_classifier"}:
        metadata["labels"][k8s._AGENT_LABEL] = "privileged-agent" if change == "classifier" else ""
    elif change == "missing_marker":
        del metadata["annotations"][warm.SHARED_POOL_ANNOTATION]
    elif change == "unknown_marker":
        metadata["annotations"][warm.SHARED_POOL_ANNOTATION] = "false"
    else:
        metadata["annotations"][warm.PROFILE_ANNOTATION] = "different-profile"
    execute = MagicMock()
    monkeypatch.setattr(harness.launcher, "_exec", execute)
    with pytest.raises(click.ClickException):
        harness.launcher.start_host(_HANDLE.encode(), **_START_ARGS, agent_name="claude-native-ui")
    execute.assert_not_called()
    harness.core.delete_namespaced_persistent_volume_claim.assert_not_called()


@pytest.mark.parametrize("change", ["added_marker", "missing_marker", "unknown_marker"])
def test_allocation_sharing_policy_cannot_change_without_its_profile(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    _set_profile(harness, harness.launcher.template_spec(shared=change != "added_marker"))
    annotations = harness.sandbox["spec"]["podTemplate"]["metadata"]["annotations"]
    if change == "added_marker":
        annotations[warm.SHARED_POOL_ANNOTATION] = "true"
    elif change == "missing_marker":
        del annotations[warm.SHARED_POOL_ANNOTATION]
    else:
        annotations[warm.SHARED_POOL_ANNOTATION] = "false"
    execute = MagicMock()
    monkeypatch.setattr(harness.launcher, "_exec", execute)
    with pytest.raises(click.ClickException):
        harness.launcher.start_host(_HANDLE.encode(), **_START_ARGS)
    execute.assert_not_called()
    harness.custom.patch_namespaced_custom_object.assert_not_called()
    harness.custom.delete_namespaced_custom_object.assert_not_called()


def test_handle_round_trip_pins_namespace_and_both_resource_uids() -> None:
    assert warm.WarmPoolHandle.parse(_HANDLE.encode()) == _HANDLE
    assert len(_HANDLE.encode()) <= 256


@pytest.mark.parametrize(
    "value", ["legacy-id", "wp2:a:b:c:d:e", "wp1::b:c:d:e", "wp1:a:b:bad:d:bad"]
)
def test_malformed_handle_fails_closed(value: str) -> None:
    with pytest.raises(click.ClickException, match="Invalid warm-pool"):
        warm.WarmPoolHandle.parse(value)


def test_handle_cannot_exceed_database_identifier_limit() -> None:
    handle = warm.WarmPoolHandle("n" * 63, "c" * 63, _CLAIM_UID, "s" * 63, _SANDBOX_UID)
    with pytest.raises(click.ClickException, match="host ID limit"):
        handle.encode()


def test_provision_adopts_claim_without_cold_creation_overrides(harness: _Harness) -> None:
    assert harness.launcher.provision("new-session") == _HANDLE.encode()
    call = harness.custom.create_namespaced_custom_object.call_args
    assert call.args[:4] == (warm.EXTENSION_GROUP, warm.API_VERSION, _NAMESPACE, warm.CLAIMS)
    spec = call.args[4]["spec"]
    assert spec["warmPoolRef"] == {"name": "warm-pool-v1"}
    assert "env" not in spec and "volumeClaimTemplates" not in spec
    assert spec["lifecycle"]["shutdownPolicy"] == "Delete"
    assert spec["lifecycle"]["shutdownTime"]
    harness.custom.delete_namespaced_custom_object.assert_not_called()
    harness.closed.assert_called_once()


def test_agent_classifier_mismatch_falls_back_before_claiming(harness: _Harness) -> None:
    harness.launcher.prepare_for_launch(agent_name="different-agent")
    result = harness.launcher.provision("new-session")
    assert not result.startswith("wp1:")
    harness.custom.create_namespaced_custom_object.assert_not_called()


def test_stale_template_is_rejected_before_claiming(harness: _Harness) -> None:
    harness.template["spec"]["podTemplate"]["spec"]["containers"][0]["image"] = "old-image"
    with pytest.raises(click.ClickException, match="template does not match"):
        harness.launcher.provision("new-session")
    harness.custom.create_namespaced_custom_object.assert_not_called()


def test_failed_adoption_cleans_up_only_the_created_claim(harness: _Harness) -> None:
    harness.sandbox["metadata"]["ownerReferences"] = []
    with pytest.raises(SandboxGoneError):
        harness.launcher.provision("new-session")
    deletion = harness.custom.delete_namespaced_custom_object.call_args
    assert deletion.args[3:5] == (warm.CLAIMS, _HANDLE.claim_name)
    assert deletion.kwargs["body"]["preconditions"] == {"uid": _CLAIM_UID}
    harness.core.delete_namespaced_pod.assert_not_called()


def test_create_failure_does_not_delete_an_unknown_claim(harness: _Harness) -> None:
    harness.custom.create_namespaced_custom_object.side_effect = _ApiError(409, "Already exists")
    with pytest.raises(click.ClickException, match="Already exists"):
        harness.launcher.provision("new-session")
    harness.custom.delete_namespaced_custom_object.assert_not_called()


def test_unallocated_claim_times_out_and_is_deleted(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.claim["status"] = {}
    ticks = iter((0.0, 0.0, 10000.0))
    monkeypatch.setattr(warm.time, "monotonic", lambda: next(ticks))
    with pytest.raises(click.ClickException, match="Timed out"):
        harness.launcher.provision("new-session")
    harness.custom.delete_namespaced_custom_object.assert_called_once()


@pytest.mark.parametrize("case", ["claim_uid", "sandbox_uid", "assignment", "owner", "deleting"])
def test_controller_repair_or_missing_ownership_cannot_replace_workspace(
    harness: _Harness, case: str
) -> None:
    if case == "claim_uid":
        harness.claim["metadata"]["uid"] = _OTHER_UID
    elif case == "sandbox_uid":
        harness.sandbox["metadata"]["uid"] = _OTHER_UID
    elif case == "assignment":
        harness.claim["status"]["sandbox"]["name"] = "replacement-sandbox"
    elif case == "owner":
        harness.sandbox["metadata"]["ownerReferences"][0]["controller"] = False
    else:
        harness.sandbox["metadata"]["deletionTimestamp"] = "2026-09-17T00:00:00Z"
    with pytest.raises(SandboxGoneError):
        harness.launcher.resume(_HANDLE.encode())
    harness.core.delete_namespaced_pod.assert_not_called()


def test_start_gates_on_preparation_and_preserves_broker_configuration(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    execute = _exec_states(harness, monkeypatch, "waiting", "bound", "preparing", "prepared")
    render = MagicMock(wraps=warm._render_workspace_prep_command)
    monkeypatch.setattr(warm, "_render_workspace_prep_command", render)
    stages: list[str] = []
    repo = RepoWorkspace("https://github.com/example/repository.git", None, "repository")
    result = harness.launcher.start_host(
        _HANDLE.encode(),
        **_START_ARGS,
        repos=[repo],
        host_config={"profile": "oss"},
        on_stage=stages.append,
    )
    assert result == "/home/omnigent/workspace/repository"
    assert stages == ["starting", "cloning"]
    activation = [call for call in execute.call_args_list if call.args[2] == "activate"]
    assert len(activation) == 1
    payload = activation[0].args[3]
    assert payload["pod_uid"] == _POD_UID
    assert payload["token"] == _TOKEN
    assert payload["generation"] == _GENERATION
    assert payload["host_id"] == _START_ARGS["host_id"]
    assert _TOKEN not in json.dumps(payload["prepare_command"])
    assert "omnigent.git_credential_github" in payload["prepare_command"][-1]
    assert HOST_TOKEN_ENV_VAR in payload["prepare_command"][-1]
    assert render.call_args.args[-1] == {"profile": "oss"}
    assert [call.args[2] for call in execute.call_args_list].count("status") == 4


def test_deadline_is_armed_before_claim_expiry_is_cleared(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _exec_states(harness, monkeypatch, "prepared")
    harness.launcher.start_host(_HANDLE.encode(), **_START_ARGS)
    patches = harness.custom.patch_namespaced_custom_object.call_args_list
    assert [call.args[3] for call in patches] == [warm.SANDBOX_PLURAL, warm.CLAIMS]
    sandbox = patches[0].args[5]
    assert sandbox["metadata"] == {"uid": _SANDBOX_UID}
    assert sandbox["spec"]["shutdownPolicy"] == "Retain"
    assert sandbox["spec"]["operatingMode"] == "Running"
    assert sandbox["spec"]["shutdownTime"]
    assert patches[1].args[5] == {"metadata": {"uid": _CLAIM_UID}, "spec": {"lifecycle": None}}


def test_start_rejects_host_config_outside_shared_home_before_activation(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", "/private-container-config")
    harness.launcher._env_names = ("OMNIGENT_CONFIG_HOME",)
    execute = MagicMock()
    monkeypatch.setattr(harness.launcher, "_exec", execute)
    with pytest.raises(ValueError, match="must resolve under"):
        harness.launcher.start_host(
            _HANDLE.encode(), **_START_ARGS, host_config={"profile": "oss"}
        )
    execute.assert_not_called()
    harness.custom.patch_namespaced_custom_object.assert_not_called()


def test_failed_preparation_is_reported_even_when_readiness_turns_false(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    unready = copy.deepcopy(harness.pod)
    for container in unready.status.container_statuses:
        container.ready = False
    harness.core.read_namespaced_pod.side_effect = [harness.pod, unready]
    _exec_states(harness, monkeypatch, "waiting", "failed")
    with pytest.raises(click.ClickException, match="workspace preparation failed"):
        harness.launcher.start_host(_HANDLE.encode(), **_START_ARGS)


def test_activation_cannot_follow_a_replacement_pod(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    replacement = _pod(harness.sandbox["spec"], uid=_OTHER_UID)
    harness.core.read_namespaced_pod.side_effect = [harness.pod, replacement]
    execute = _exec_states(harness, monkeypatch, "waiting")
    with pytest.raises(click.ClickException, match="Pod changed during activation"):
        harness.launcher.start_host(_HANDLE.encode(), **_START_ARGS)
    assert len([call for call in execute.call_args_list if call.args[2] == "activate"]) == 1


def test_another_generation_is_never_activated(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    execute = MagicMock(return_value=json.dumps({"stage": "prepared", "generation": "another"}))
    monkeypatch.setattr(harness.launcher, "_exec", execute)
    with pytest.raises(click.ClickException, match="another host generation"):
        harness.launcher.start_host(_HANDLE.encode(), **_START_ARGS)
    assert all(call.args[2] == "status" for call in execute.call_args_list)


def test_resume_deletes_only_the_pinned_pod_and_defers_wake(harness: _Harness) -> None:
    harness.launcher.resume(_HANDLE.encode())
    deletion = harness.core.delete_namespaced_pod.call_args
    assert deletion.args == (harness.pod.metadata.name, _NAMESPACE)
    assert deletion.kwargs["body"].preconditions.uid == _POD_UID
    harness.custom.delete_namespaced_custom_object.assert_not_called()
    harness.custom.patch_namespaced_custom_object.assert_not_called()


def test_missing_suspended_pod_does_not_destroy_allocation(harness: _Harness) -> None:
    harness.core.read_namespaced_pod.side_effect = _ApiError(404)
    harness.launcher.resume(_HANDLE.encode())
    harness.core.delete_namespaced_pod.assert_not_called()
    harness.custom.delete_namespaced_custom_object.assert_not_called()


def test_resume_tolerates_pod_removed_between_read_and_delete(harness: _Harness) -> None:
    harness.core.delete_namespaced_pod.side_effect = _ApiError(404)

    harness.launcher.resume(_HANDLE.encode())

    harness.core.read_namespaced_pod.assert_called_once()
    harness.core.delete_namespaced_pod.assert_called_once()
    deletion = harness.core.delete_namespaced_pod.call_args
    assert deletion.args == (harness.pod.metadata.name, _NAMESPACE)
    assert deletion.kwargs["body"].preconditions.uid == _POD_UID
    harness.custom.delete_namespaced_custom_object.assert_not_called()
    harness.custom.patch_namespaced_custom_object.assert_not_called()
    harness.closed.assert_called_once()


@pytest.mark.parametrize("status", [403, 409])
def test_resume_propagates_pod_delete_failures(harness: _Harness, status: int) -> None:
    failure = _ApiError(status)
    harness.core.delete_namespaced_pod.side_effect = failure

    with pytest.raises(_ApiError) as raised:
        harness.launcher.resume(_HANDLE.encode())

    assert raised.value is failure
    deletion = harness.core.delete_namespaced_pod.call_args
    assert deletion.kwargs["body"].preconditions.uid == _POD_UID
    harness.custom.delete_namespaced_custom_object.assert_not_called()
    harness.custom.patch_namespaced_custom_object.assert_not_called()
    harness.closed.assert_called_once()


def test_missing_sandbox_surfaces_gone_instead_of_reallocation(harness: _Harness) -> None:
    harness.custom.get_namespaced_custom_object.side_effect = _ApiError(404)
    with pytest.raises(SandboxGoneError):
        harness.launcher.resume(_HANDLE.encode())
    harness.custom.create_namespaced_custom_object.assert_not_called()


def test_termination_uses_claim_uid_and_recorded_namespace(harness: _Harness) -> None:
    harness.launcher._namespace = "new-server-namespace"
    harness.launcher._warm_pool = None
    harness.launcher.terminate(_HANDLE.encode())
    deletion = harness.custom.delete_namespaced_custom_object.call_args
    assert deletion.args[2:5] == (_NAMESPACE, warm.CLAIMS, _HANDLE.claim_name)
    assert deletion.kwargs["body"]["preconditions"] == {"uid": _CLAIM_UID}
    assert deletion.kwargs["body"]["propagationPolicy"] == "Foreground"
    harness.core.delete_namespaced_pod.assert_not_called()
    harness.core.delete_namespaced_secret.assert_not_called()


@pytest.mark.parametrize("status", [404, 409])
def test_termination_tolerates_missing_or_replaced_claim(harness: _Harness, status: int) -> None:
    harness.custom.delete_namespaced_custom_object.side_effect = _ApiError(status)
    if status == 409:
        harness.claim["metadata"]["uid"] = _OTHER_UID
    harness.launcher.terminate(_HANDLE.encode())
    harness.closed.assert_called_once()


def test_termination_does_not_swallow_authorization_errors(harness: _Harness) -> None:
    harness.custom.delete_namespaced_custom_object.side_effect = _ApiError(403)
    with pytest.raises(_ApiError):
        harness.launcher.terminate(_HANDLE.encode())


def test_keepalive_updates_only_sandbox_deadline(harness: _Harness) -> None:
    assert harness.launcher.keep_alive(_HANDLE.encode()) is True
    patch = harness.custom.patch_namespaced_custom_object.call_args
    assert patch.args[3] == warm.SANDBOX_PLURAL
    assert patch.args[5]["metadata"] == {"uid": _SANDBOX_UID}
    assert patch.args[5]["spec"]["shutdownPolicy"] == "Retain"
    assert "lifecycle" not in patch.args[5]["spec"]


def test_legacy_handles_delegate_even_when_pool_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _launcher()
    for method, args, kwargs in (
        ("start_host", ("legacy-sandbox",), _START_ARGS),
        ("resume", ("legacy-sandbox",), {}),
        ("terminate", ("legacy-sandbox",), {}),
        ("keep_alive", ("legacy-sandbox",), {}),
        ("is_running", ("legacy-sandbox",), {}),
    ):
        delegated = MagicMock(return_value="legacy-result")
        monkeypatch.setattr(AgentSandboxLauncher, method, delegated)
        assert getattr(launcher, method)(*args, **kwargs) == "legacy-result"
        delegated.assert_called_once()


def test_disabled_pool_uses_direct_provision(monkeypatch: pytest.MonkeyPatch) -> None:
    delegated = MagicMock(return_value="legacy-sandbox")
    monkeypatch.setattr(AgentSandboxLauncher, "provision", delegated)
    assert _launcher(warm_pool=None).provision("new-session") == "legacy-sandbox"
    delegated.assert_called_once_with("new-session")


def test_exec_sends_activation_only_over_stdin_and_closes_transport(
    harness: _Harness, sdk: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_api = _ApiClient(configuration=SimpleNamespace())
    monkeypatch.setattr(harness.launcher, "_api_client", original_api)
    monkeypatch.setattr(harness.launcher, "_load_clients", MagicMock())
    connection = sdk.stream.return_value
    connection.is_open.return_value = False
    connection.returncode = 0
    connection.read_stdout.return_value = ""
    payload = {"token": _TOKEN, "generation": _GENERATION, "pod_uid": _POD_UID}
    assert harness.launcher._exec(_HANDLE, "pooled-sandbox-pod", "activate", payload) == ""

    call = sdk.stream.call_args
    assert call.args[1:] == ("pooled-sandbox-pod", _NAMESPACE)
    assert call.kwargs["container"] == "bootstrap"
    assert call.kwargs["command"] == ["python3", "-m", "omnigent.host.warm_bootstrap", "activate"]
    assert _TOKEN not in repr(call)
    assert call.kwargs["stdin"] is True
    sent = connection.write_stdin.call_args.args[0]
    assert sent.endswith("\n")
    assert json.loads(sent) == payload
    connection.close.assert_called_once()
    assert sdk.client.CoreV1Api.call_args.args[0] is not original_api
    assert harness.launcher._api_client is original_api


def test_exec_errors_never_surface_remote_stderr_credentials(
    harness: _Harness, sdk: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(harness.launcher, "_api_client", _ApiClient())
    monkeypatch.setattr(harness.launcher, "_load_clients", MagicMock())
    connection = sdk.stream.return_value
    connection.is_open.return_value = False
    connection.returncode = 1
    connection.read_stderr.return_value = _TOKEN
    with pytest.raises(click.ClickException) as error:
        harness.launcher._exec(_HANDLE, "pooled-sandbox-pod", "activate", {"token": _TOKEN})
    assert _TOKEN not in str(error.value)
    connection.read_stderr.assert_not_called()
    connection.close.assert_called_once()


@pytest.mark.parametrize("change", ["image", "privilege", "identity", "classifier"])
def test_runtime_pod_profile_is_checked_before_delivering_credentials(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    pod_spec = harness.pod.raw["spec"]
    if change == "image":
        pod_spec["containers"][0]["image"] = "different-image"
    elif change == "privilege":
        pod_spec["securityContext"]["runAsNonRoot"] = False
    elif change == "identity":
        pod_spec["containers"][1]["env"].append({"name": HOST_TOKEN_ENV_VAR, "value": "stale"})
    else:
        harness.pod.raw["metadata"]["labels"][k8s._AGENT_LABEL] = "another-agent"
    execute = MagicMock()
    monkeypatch.setattr(harness.launcher, "_exec", execute)
    with pytest.raises(click.ClickException, match="Pod does not match"):
        harness.launcher.start_host(_HANDLE.encode(), **_START_ARGS)
    execute.assert_not_called()


def test_runtime_profile_accepts_its_own_persistent_home(
    sdk: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(WORKSPACE_SIZE_ENV_VAR, "20Gi")
    launcher = _launcher()
    pod = _pod(launcher.template_spec())
    volumes = pod.raw["spec"]["volumes"]
    volumes[:] = [item for item in volumes if item["name"] != "home"]
    volumes.append({"name": "home", "persistentVolumeClaim": {"claimName": "home-pooled-sandbox"}})
    launcher._validate_pod(pod, _HANDLE, agent_name=None)
    volumes[-1]["persistentVolumeClaim"]["claimName"] = "another-workspace"
    with pytest.raises(click.ClickException, match="Pod does not match"):
        launcher._validate_pod(pod, _HANDLE, agent_name=None)


def _configure_tolerations(harness: _Harness, toleration: dict[str, Any]) -> None:
    harness.launcher._tolerations = [toleration]
    profile = harness.launcher.template_spec()
    harness.template["spec"] = copy.deepcopy(profile)
    harness.sandbox["spec"] = copy.deepcopy(profile)
    harness.pod.raw = _pod(profile).raw
    harness.pod.raw["spec"]["tolerations"].extend(
        {
            "key": f"node.kubernetes.io/{condition}",
            "operator": "Exists",
            "effect": "NoExecute",
            "tolerationSeconds": 300,
        }
        for condition in ("not-ready", "unreachable")
    )


def test_warm_launch_accepts_admission_added_tolerations(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure_tolerations(
        harness,
        {"key": "dedicated", "operator": "Equal", "value": "omnigent", "effect": "NoSchedule"},
    )
    execute = _exec_states(harness, monkeypatch, "waiting", "prepared")
    sandbox_id = harness.launcher.provision("managed-test")
    assert harness.launcher.start_host(sandbox_id, **_START_ARGS) == "/home/omnigent/workspace"
    assert len([call for call in execute.call_args_list if call.args[2] == "activate"]) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        (None, None),
        ("key", "different"),
        ("operator", "Exists"),
        ("value", "different"),
        ("effect", "NoSchedule"),
        ("tolerationSeconds", 300),
    ],
)
def test_warm_launch_rejects_missing_or_changed_configured_toleration(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, field: str | None, value: Any
) -> None:
    _configure_tolerations(
        harness,
        {
            "key": "dedicated",
            "operator": "Equal",
            "value": "omnigent",
            "effect": "NoExecute",
            "tolerationSeconds": 60,
        },
    )
    tolerations = harness.pod.raw["spec"]["tolerations"]
    if field is None:
        tolerations.pop(0)
    else:
        tolerations[0][field] = value
    execute = MagicMock()
    monkeypatch.setattr(harness.launcher, "_exec", execute)
    with pytest.raises(click.ClickException, match="Pod does not match"):
        harness.launcher.start_host(_HANDLE.encode(), **_START_ARGS)
    execute.assert_not_called()


def test_toleration_matching_preserves_defaults_and_unbounded_eviction() -> None:
    configured = {"key": "dedicated", "operator": "Exists", "effect": "NoExecute"}
    assert warm._contains(
        {"tolerations": [configured]}, {"tolerations": [{**configured, "value": ""}]}
    )
    assert not warm._contains(
        {"tolerations": [configured]},
        {"tolerations": [{**configured, "tolerationSeconds": 300}]},
    )
    assert not warm._contains({"command": ["host"]}, {"command": ["host", "extra"]})


def test_profile_comparison_accepts_api_defaults_but_not_weaker_permissions() -> None:
    assert warm._contains({"readOnly": False, "value": ""}, {})
    assert not warm._contains({"readOnly": True}, {})
    assert not warm._contains({"runAsNonRoot": True}, {"runAsNonRoot": False})
    assert warm._contains(
        [{"name": "host", "image": "a"}], [{"name": "host", "image": "a", "ports": []}]
    )
    assert not warm._contains([{"name": "host"}], [{"name": "host"}, {"name": "host"}])


def test_profile_comparison_delegates_equivalent_quantity_spellings(
    sdk: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    quantity = types.ModuleType("kubernetes.utils.quantity")
    parse = MagicMock(return_value=1024**3)
    quantity.parse_quantity = parse  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "kubernetes.utils.quantity", quantity)
    assert warm._contains({"memory": "1024Mi"}, {"memory": "1Gi"})
    assert [call.args[0] for call in parse.call_args_list] == ["1024Mi", "1Gi"]


def test_pod_selector_fallback_still_requires_sandbox_ownership(harness: _Harness) -> None:
    harness.sandbox["status"] = {"selector": "agents.x-k8s.io/sandbox=pooled-sandbox"}
    unrelated = copy.deepcopy(harness.pod)
    unrelated.metadata.owner_references[0].uid = _OTHER_UID
    harness.core.read_namespaced_pod.side_effect = _ApiError(404)
    harness.core.list_namespaced_pod.return_value = SimpleNamespace(items=[unrelated, harness.pod])
    harness.launcher.resume(_HANDLE.encode())
    listing = harness.core.list_namespaced_pod.call_args
    assert listing.args == (_NAMESPACE,)
    assert listing.kwargs["label_selector"] == harness.sandbox["status"]["selector"]
    assert (
        harness.core.delete_namespaced_pod.call_args.kwargs["body"].preconditions.uid == _POD_UID
    )


def test_multiple_owned_pods_cannot_be_selected_arbitrarily(harness: _Harness) -> None:
    harness.sandbox["status"] = {"selector": "sandbox=pooled-sandbox"}
    harness.core.read_namespaced_pod.side_effect = _ApiError(404)
    harness.core.list_namespaced_pod.return_value = SimpleNamespace(
        items=[harness.pod, harness.pod]
    )
    with pytest.raises(click.ClickException, match="Multiple Pods"):
        harness.launcher.resume(_HANDLE.encode())
    harness.core.delete_namespaced_pod.assert_not_called()


def test_failed_deadline_patch_leaves_abandoned_claim_expiry_armed(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    execute = MagicMock()
    monkeypatch.setattr(harness.launcher, "_exec", execute)
    harness.custom.patch_namespaced_custom_object.side_effect = _ApiError(403)
    with pytest.raises(_ApiError):
        harness.launcher.start_host(_HANDLE.encode(), **_START_ARGS)
    patches = harness.custom.patch_namespaced_custom_object.call_args_list
    assert len(patches) == 1 and patches[0].args[3] == warm.SANDBOX_PLURAL
    execute.assert_not_called()


def test_lost_activation_ack_recovers_only_the_same_generation(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    execute = MagicMock(
        side_effect=[
            json.dumps({"stage": "waiting", "generation": None}),
            ConnectionError("Connection closed after delivery"),
            json.dumps({"stage": "preparing", "generation": _GENERATION}),
            json.dumps({"stage": "prepared", "generation": _GENERATION}),
        ]
    )
    monkeypatch.setattr(harness.launcher, "_exec", execute)
    assert (
        harness.launcher.start_host(_HANDLE.encode(), **_START_ARGS) == "/home/omnigent/workspace"
    )
    assert len([call for call in execute.call_args_list if call.args[2] == "activate"]) == 1


@pytest.mark.parametrize(
    "response", ["not-json", "[]", '{"stage":"unknown"}', '{"stage":"prepared"}']
)
def test_malformed_status_fails_without_echoing_its_contents(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, response: str
) -> None:
    monkeypatch.setattr(harness.launcher, "_exec", MagicMock(return_value=response))
    with pytest.raises(click.ClickException, match="Invalid warm bootstrap status"):
        harness.launcher.start_host(_HANDLE.encode(), **_START_ARGS)


def test_conflicting_delete_for_same_claim_is_not_reported_as_success(harness: _Harness) -> None:
    harness.custom.delete_namespaced_custom_object.side_effect = _ApiError(409)
    with pytest.raises(_ApiError):
        harness.launcher.terminate(_HANDLE.encode())


@pytest.mark.parametrize("change", ["agent_classifier", "image"])
def test_incompatible_claimed_profile_preserves_allocation_before_activation(
    harness: _Harness, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    if change == "agent_classifier":
        harness.sandbox["spec"] = harness.launcher.template_spec(agent_name="built-in")
    else:
        harness.sandbox["spec"]["podTemplate"]["spec"]["containers"][0]["image"] = "old-image"
    execute = MagicMock()
    monkeypatch.setattr(harness.launcher, "_exec", execute)

    with pytest.raises(click.ClickException, match="template does not match"):
        harness.launcher.start_host(_HANDLE.encode(), **_START_ARGS, agent_name=None)

    execute.assert_not_called()
    harness.custom.patch_namespaced_custom_object.assert_not_called()
    harness.custom.delete_namespaced_custom_object.assert_not_called()
    harness.core.delete_namespaced_persistent_volume_claim.assert_not_called()
