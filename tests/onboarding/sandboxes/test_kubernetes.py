"""
Tests for the Kubernetes (entrypoint-as-host) sandbox launcher.

The official ``kubernetes`` client is an optional dependency, so the SDK-driven
tests inject a small fake package into ``sys.modules`` (no real cluster, no real
client). The entrypoint model needs only ``CoreV1Api`` + ``BatchV1Api``
create/read/delete/log fakes — there is no exec transport to fake.
"""

from __future__ import annotations

import json
import logging
import sys
import types
from types import SimpleNamespace

import click
import pytest

import omnigent.onboarding.sandboxes.kubernetes as k8s
from omnigent.host.identity import (
    HOST_ID_ENV_VAR,
    HOST_NAME_ENV_VAR,
    HOST_TOKEN_ENV_VAR,
)
from omnigent.onboarding.sandboxes.base import (
    render_host_config_write_command,
)
from omnigent.onboarding.sandboxes.kubernetes import (
    KubernetesSandboxLauncher,
    build_job_manifest,
    build_token_secret_manifest,
)

_TOKEN = "launch-token-xyz"
_MANIFEST_KW = {
    "job_name": "omnigent-managed-abc-1a2b3c",
    "namespace": "omnigent-sandboxes",
    "image": "ghcr.io/omnigent-ai/omnigent-host:latest",
    "service_account": "omnigent-runner",
    "host_id": "host_abcdef",
    "host_name": "managed-abcdef",
    "server_url": "http://srv.example.com",
    "token_secret_name": "omnigent-managed-abc-1a2b3c-token",
    "harness_secret": "omnigent-creds",
    "env_literals": {},
    "node_selector": None,
    "workspace": "/home/omnigent/workspace",
}

# Minimal valid host_config exercised by the injection tests below.
_HOST_CONFIG: dict[str, object] = {"providers": {"litellm": {"kind": "gateway"}}}


def _pod_spec(manifest: dict) -> dict:
    """Extract the Pod spec from a Job manifest."""
    return manifest["spec"]["template"]["spec"]


# ── pure manifest / rendering tests (no SDK) ────────────────


def test_build_job_manifest_is_a_batch_v1_job() -> None:
    """The manifest is a batch/v1 Job with backoffLimit and activeDeadlineSeconds."""
    manifest = build_job_manifest(**_MANIFEST_KW)
    assert manifest["apiVersion"] == "batch/v1"
    assert manifest["kind"] == "Job"
    assert manifest["spec"]["backoffLimit"] == k8s._JOB_BACKOFF_LIMIT
    assert manifest["spec"]["activeDeadlineSeconds"] == k8s._JOB_ACTIVE_DEADLINE_S


def test_build_job_manifest_restart_policy_is_on_failure() -> None:
    """The Pod template uses OnFailure for automatic container restart."""
    manifest = build_job_manifest(**_MANIFEST_KW)
    assert _pod_spec(manifest)["restartPolicy"] == "OnFailure"


def test_build_job_manifest_runs_host_under_reaper_as_container_command() -> None:
    """The main container's command execs the PID-1 reaper, which runs the host."""
    manifest = build_job_manifest(**_MANIFEST_KW)
    containers = _pod_spec(manifest)["containers"]
    assert len(containers) == 1
    host = containers[0]
    assert host["name"] == "host"
    command = host["command"]
    assert command[:2] == ["bash", "-lc"]
    script = command[2]
    assert "exec python3 -c" in script
    assert "omnigent host --server http://srv.example.com" in script
    assert "os.wait()" in script


def test_build_job_manifest_has_no_liveness_probe() -> None:
    """No liveness probe: the reaper propagates child exit, OnFailure handles restarts."""
    manifest = build_job_manifest(**_MANIFEST_KW)
    host = _pod_spec(manifest)["containers"][0]
    assert "livenessProbe" not in host


def test_build_job_manifest_init_container_prepares_and_clones_workspace() -> None:
    """The init container makes the workspace and clones the repo before the host."""
    manifest = build_job_manifest(
        **{**_MANIFEST_KW, "clone_dir": "/home/omnigent/workspace/repo"},
        repo_url="https://github.com/org/repo.git",
        repo_branch="main",
    )
    init = _pod_spec(manifest)["initContainers"]
    assert len(init) == 1
    assert init[0]["name"] == "workspace-prep"
    script = init[0]["command"][2]
    assert "mkdir -p /home/omnigent/workspace" in script
    assert "git clone --branch main --single-branch -- " in script
    assert "https://github.com/org/repo.git /home/omnigent/workspace/repo" in script


def test_build_job_manifest_without_repo_has_no_clone() -> None:
    """No repo → the init container only makes the workspace, no git clone."""
    manifest = build_job_manifest(**_MANIFEST_KW)
    script = _pod_spec(manifest)["initContainers"][0]["command"][2]
    assert "mkdir -p /home/omnigent/workspace" in script
    assert "git clone" not in script


def test_build_job_manifest_host_config_is_written_by_init_container() -> None:
    """host_config rides the init container script, after mkdir/clone, before the host."""
    manifest = build_job_manifest(
        **{**_MANIFEST_KW, "clone_dir": "/home/omnigent/workspace/repo"},
        repo_url="https://github.com/org/repo.git",
        host_config=_HOST_CONFIG,
    )
    spec = _pod_spec(manifest)
    script = spec["initContainers"][0]["command"][2]
    write_command = render_host_config_write_command(_HOST_CONFIG)
    assert write_command in script
    assert script.index("mkdir -p") < script.index("git clone") < script.index(write_command)
    assert write_command not in spec["containers"][0]["command"][2]


def test_build_job_manifest_forwards_config_home_to_init_container() -> None:
    """Init and host resolve injected config under the same configured directory."""
    manifest = build_job_manifest(
        **{
            **_MANIFEST_KW,
            "env_literals": {
                "OMNIGENT_CONFIG_HOME": "/home/omnigent/custom-config",
                "PLAIN_CONFIG": "host-only",
            },
        },
        host_config=_HOST_CONFIG,
    )

    spec = _pod_spec(manifest)
    init_env = spec["initContainers"][0]["env"]
    host_env = spec["containers"][0]["env"]
    assert init_env == [
        {"name": "HOME", "value": "/home/omnigent"},
        {
            "name": "OMNIGENT_CONFIG_HOME",
            "value": "/home/omnigent/custom-config",
        },
    ]
    assert {entry["name"] for entry in host_env} >= {
        "OMNIGENT_CONFIG_HOME",
        "PLAIN_CONFIG",
    }


@pytest.mark.parametrize(
    "config_home",
    ["/tmp/elsewhere", "/home/omnigent-other", "/home/omnigent/../tmp", "../etc"],
)
def test_build_job_manifest_rejects_config_home_outside_home_dir(config_home: str) -> None:
    """
    Init and host share only the HOME emptyDir, so a config dir that resolves
    outside it would make the injected config invisible to the host.
    """
    with pytest.raises(ValueError, match=r"OMNIGENT_CONFIG_HOME.*must resolve under"):
        build_job_manifest(
            **{**_MANIFEST_KW, "env_literals": {"OMNIGENT_CONFIG_HOME": config_home}},
            host_config=_HOST_CONFIG,
        )


@pytest.mark.parametrize(
    "config_home",
    ["/home/omnigent", "/home/omnigent/", "/home/omnigent/cfg", "cfg", "relative/dir", ".", ""],
)
def test_build_job_manifest_accepts_config_home_at_or_under_home_dir(config_home: str) -> None:
    """A dir at or under HOME is on the shared volume — allowed."""
    manifest = build_job_manifest(
        **{**_MANIFEST_KW, "env_literals": {"OMNIGENT_CONFIG_HOME": config_home}},
        host_config=_HOST_CONFIG,
    )
    assert {"name": "OMNIGENT_CONFIG_HOME", "value": config_home} in _pod_spec(manifest)[
        "initContainers"
    ][0]["env"]


def test_build_job_manifest_config_home_outside_home_dir_ok_without_host_config() -> None:
    """Without host_config the init container writes nothing, so the path is moot."""
    manifest = build_job_manifest(
        **{**_MANIFEST_KW, "env_literals": {"OMNIGENT_CONFIG_HOME": "/tmp/elsewhere"}},
    )
    init_env = _pod_spec(manifest)["initContainers"][0]["env"]
    assert {"name": "OMNIGENT_CONFIG_HOME", "value": "/tmp/elsewhere"} in init_env


def test_build_job_manifest_without_host_config_has_no_config_write() -> None:
    """No host_config → the init container only preps the workspace."""
    manifest = build_job_manifest(**_MANIFEST_KW)
    script = _pod_spec(manifest)["initContainers"][0]["command"][2]
    assert "config.yaml" not in script
    assert "python3 -c" not in script


def test_build_job_manifest_token_rides_secret_ref_not_the_spec() -> None:
    """The launch token is referenced via secretKeyRef, never written into the spec."""
    manifest = build_job_manifest(**_MANIFEST_KW)
    host_env = _pod_spec(manifest)["containers"][0]["env"]
    token_entry = next(e for e in host_env if e["name"] == HOST_TOKEN_ENV_VAR)
    assert token_entry["valueFrom"]["secretKeyRef"] == {
        "name": "omnigent-managed-abc-1a2b3c-token",
        "key": HOST_TOKEN_ENV_VAR,
    }
    assert "value" not in token_entry
    assert {e["name"]: e.get("value") for e in host_env}[HOST_ID_ENV_VAR] == "host_abcdef"
    assert {e["name"]: e.get("value") for e in host_env}[HOST_NAME_ENV_VAR] == "managed-abcdef"
    assert _TOKEN not in json.dumps(manifest)


def test_build_token_secret_manifest_carries_token_in_stringdata() -> None:
    """The token Secret holds the raw token under the host-token key, labeled for GC."""
    secret = build_token_secret_manifest(
        secret_name="omnigent-pod-token", namespace="omnigent-sandboxes", token=_TOKEN
    )
    assert secret["stringData"] == {HOST_TOKEN_ENV_VAR: _TOKEN}
    assert secret["metadata"]["labels"]["app.kubernetes.io/managed-by"] == "omnigent"
    assert secret["type"] == "Opaque"


def test_build_job_manifest_harness_secret_projects_into_both_containers() -> None:
    """The harness creds Secret is projected via envFrom on init (for clone) + host."""
    manifest = build_job_manifest(**_MANIFEST_KW)
    spec = _pod_spec(manifest)
    init = spec["initContainers"][0]
    host = spec["containers"][0]
    assert init["envFrom"] == [{"secretRef": {"name": "omnigent-creds"}}]
    assert host["envFrom"] == [{"secretRef": {"name": "omnigent-creds"}}]


def test_build_job_manifest_omits_envfrom_without_harness_secret() -> None:
    """No harness Secret → no envFrom key on either container."""
    manifest = build_job_manifest(**{**_MANIFEST_KW, "harness_secret": None})
    spec = _pod_spec(manifest)
    assert "envFrom" not in spec["initContainers"][0]
    assert "envFrom" not in spec["containers"][0]


def test_build_job_manifest_defaults_to_amd64_node_selector() -> None:
    """No node_selector → Pods keep the amd64 default placement."""
    manifest = build_job_manifest(**{**_MANIFEST_KW, "node_selector": None})
    assert _pod_spec(manifest)["nodeSelector"] == {"kubernetes.io/arch": "amd64"}


def test_build_job_manifest_node_selector_can_override_arch() -> None:
    """An operator kubernetes.io/arch entry overrides the amd64 default."""
    manifest = build_job_manifest(
        **{**_MANIFEST_KW, "node_selector": {"disktype": "ssd", "kubernetes.io/arch": "arm64"}}
    )
    selector = _pod_spec(manifest)["nodeSelector"]
    assert selector["kubernetes.io/arch"] == "arm64"
    assert selector["disktype"] == "ssd"


def test_build_job_manifest_pvc_mounts_land_on_host_container_only() -> None:
    """Each pvc_mounts entry becomes a persistentVolumeClaim volume mounted on host only."""
    manifest = build_job_manifest(
        **_MANIFEST_KW,
        pvc_mounts=[
            {"claim_name": "omnigent-datasets", "mount_path": "/mnt/datasets", "read_only": True},
            {"claim_name": "scratch", "mount_path": "/mnt/scratch", "read_only": False},
        ],
    )
    spec = _pod_spec(manifest)
    volumes = {v["name"]: v for v in spec["volumes"]}
    assert volumes["home"] == {"name": "home", "emptyDir": {}}
    assert volumes["pvc-0"]["persistentVolumeClaim"] == {
        "claimName": "omnigent-datasets",
        "readOnly": True,
    }
    assert volumes["pvc-1"]["persistentVolumeClaim"] == {"claimName": "scratch"}
    host_mounts = {m["name"]: m for m in spec["containers"][0]["volumeMounts"]}
    assert host_mounts["pvc-0"] == {
        "name": "pvc-0",
        "mountPath": "/mnt/datasets",
        "readOnly": True,
    }
    assert host_mounts["pvc-1"] == {"name": "pvc-1", "mountPath": "/mnt/scratch"}
    assert spec["initContainers"][0]["volumeMounts"] == [
        {"name": "home", "mountPath": "/home/omnigent"}
    ]


def test_build_job_manifest_without_pvc_mounts_is_unchanged() -> None:
    """No pvc_mounts → the single home emptyDir, exactly as before."""
    manifest = build_job_manifest(**_MANIFEST_KW)
    spec = _pod_spec(manifest)
    assert spec["volumes"] == [{"name": "home", "emptyDir": {}}]
    assert spec["containers"][0]["volumeMounts"] == [
        {"name": "home", "mountPath": "/home/omnigent"}
    ]


def test_build_job_manifest_secret_mounts_land_on_host_container_only() -> None:
    """Each secret_mounts entry becomes a read-only secret volume mounted on host only."""
    manifest = build_job_manifest(
        **_MANIFEST_KW,
        secret_mounts=[
            {"secret_name": "git-token", "mount_path": "/mnt/secrets/git"},
            {"secret_name": "npm-token", "mount_path": "/mnt/secrets/npm"},
        ],
    )
    spec = _pod_spec(manifest)
    volumes = {v["name"]: v for v in spec["volumes"]}
    assert volumes["home"] == {"name": "home", "emptyDir": {}}
    assert volumes["secret-0"]["secret"] == {
        "secretName": "git-token",
        "optional": False,
        "defaultMode": 0o440,
    }
    assert volumes["secret-1"]["secret"] == {
        "secretName": "npm-token",
        "optional": False,
        "defaultMode": 0o440,
    }
    host_mounts = {m["name"]: m for m in spec["containers"][0]["volumeMounts"]}
    assert host_mounts["secret-0"] == {
        "name": "secret-0",
        "mountPath": "/mnt/secrets/git",
        "readOnly": True,
    }
    assert host_mounts["secret-1"] == {
        "name": "secret-1",
        "mountPath": "/mnt/secrets/npm",
        "readOnly": True,
    }
    assert spec["initContainers"][0]["volumeMounts"] == [
        {"name": "home", "mountPath": "/home/omnigent"}
    ]


def test_build_job_manifest_pvc_and_secret_mounts_coexist() -> None:
    """PVC and Secret mounts get independent name spaces (pvc-N / secret-N) on the host."""
    manifest = build_job_manifest(
        **_MANIFEST_KW,
        pvc_mounts=[{"claim_name": "datasets", "mount_path": "/mnt/datasets", "read_only": True}],
        secret_mounts=[{"secret_name": "git-token", "mount_path": "/mnt/secrets/git"}],
    )
    spec = _pod_spec(manifest)
    volume_names = {v["name"] for v in spec["volumes"]}
    assert volume_names == {"home", "pvc-0", "secret-0"}
    host_mount_names = {m["name"] for m in spec["containers"][0]["volumeMounts"]}
    assert host_mount_names == {"home", "pvc-0", "secret-0"}


def test_build_job_manifest_without_secret_mounts_is_unchanged() -> None:
    """No secret_mounts → the single home emptyDir, exactly as before."""
    manifest = build_job_manifest(**_MANIFEST_KW)
    spec = _pod_spec(manifest)
    assert spec["volumes"] == [{"name": "home", "emptyDir": {}}]
    assert spec["containers"][0]["volumeMounts"] == [
        {"name": "home", "mountPath": "/home/omnigent"}
    ]


def test_build_job_manifest_stamps_agent_label_alongside_reserved_pair() -> None:
    """A valid agent name adds the omnigent.ai/agent classifier; reserved pair stays."""
    manifest = build_job_manifest(**_MANIFEST_KW, agent_name="research-agent")
    assert manifest["metadata"]["labels"] == {
        "app.kubernetes.io/managed-by": "omnigent",
        "omnigent.ai/role": "sandbox-host",
        "omnigent.ai/agent": "research-agent",
    }


def test_build_job_manifest_echoes_valid_agent_name_verbatim() -> None:
    """The label value equals the agent name exactly — case, dots, and underscores
    are all valid label characters, so a valid name is never rewritten."""
    manifest = build_job_manifest(**_MANIFEST_KW, agent_name="Research.Agent_v2")
    assert manifest["metadata"]["labels"]["omnigent.ai/agent"] == "Research.Agent_v2"


def test_build_job_manifest_without_agent_label_keeps_only_reserved_pair() -> None:
    """No agent → labels are exactly the reserved managed-by/role pair."""
    manifest = build_job_manifest(**_MANIFEST_KW)
    assert manifest["metadata"]["labels"] == {
        "app.kubernetes.io/managed-by": "omnigent",
        "omnigent.ai/role": "sandbox-host",
    }


def test_build_job_manifest_empty_agent_label_is_omitted() -> None:
    """An empty agent name is treated as no agent — no omnigent.ai/agent key."""
    manifest = build_job_manifest(**_MANIFEST_KW, agent_name="")
    assert "omnigent.ai/agent" not in manifest["metadata"]["labels"]


@pytest.mark.parametrize(
    "raw",
    [
        "Research Agent",  # space is not a valid label character
        "agent/v2:beta",  # '/' and ':' are not valid label characters
        "--weird__name..",  # does not start/end alphanumeric
        "a" * 64,  # exceeds the 63-char label-value limit
        "///...---",  # nothing valid survives
    ],
)
def test_build_job_manifest_omits_agent_label_needing_transformation(
    raw: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A name that is not ALREADY a valid label value is omitted, never coerced."""
    with caplog.at_level(logging.WARNING):
        manifest = build_job_manifest(**_MANIFEST_KW, agent_name=raw)
    assert "omnigent.ai/agent" not in manifest["metadata"]["labels"]
    assert any(
        "stays unclassified" in r.getMessage() and r.levelno == logging.WARNING
        for r in caplog.records
    ), f"the dropped label was not warned about: {[r.getMessage() for r in caplog.records]}"


def test_build_job_manifest_is_restricted_and_least_privilege() -> None:
    """The Pod template satisfies Pod Security 'restricted' and mounts no SA token."""
    manifest = build_job_manifest(**_MANIFEST_KW)
    spec = _pod_spec(manifest)
    assert spec["restartPolicy"] == "OnFailure"
    assert spec["automountServiceAccountToken"] is False
    assert spec["securityContext"]["runAsNonRoot"] is True
    assert spec["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    host = spec["containers"][0]
    assert host["securityContext"]["allowPrivilegeEscalation"] is False
    assert host["securityContext"]["capabilities"] == {"drop": ["ALL"]}


@pytest.mark.parametrize(
    ("clone_dir", "repo_url", "repo_branch", "expect_clone", "expect_branch"),
    [
        (None, None, None, False, False),
        ("/ws/repo", "https://x/y.git", None, True, False),
        ("/ws/repo", "https://x/y.git", "release-1.2", True, True),
    ],
)
def test_render_workspace_prep_command(
    clone_dir: str | None,
    repo_url: str | None,
    repo_branch: str | None,
    expect_clone: bool,
    expect_branch: bool,
) -> None:
    """The init command always mkdir's the workspace and clones only when asked."""
    command = k8s._render_workspace_prep_command("/ws", clone_dir, repo_url, repo_branch)
    script = command[2]
    assert "mkdir -p /ws" in script
    assert ("git clone" in script) is expect_clone
    assert ("--branch release-1.2 --single-branch" in script) is expect_branch


def test_new_pod_name_and_token_secret_name() -> None:
    """Pod names are DNS-label-safe and the token Secret is the name + suffix."""
    name = k8s._new_pod_name("Managed-ABC_123!")
    assert name.startswith("omnigent-managed-abc-123-")
    assert all(c.islower() or c.isdigit() or c == "-" for c in name)
    assert k8s._token_secret_name(name) == f"{name}-token"


def test_resolve_sandbox_env_rejects_reserved_and_credential_and_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Env passthrough rejects reserved names, credential-looking names, and unset vars."""
    monkeypatch.setenv("PLAIN_CONFIG", "value")
    assert KubernetesSandboxLauncher(env=["PLAIN_CONFIG"])._resolve_sandbox_env() == {
        "PLAIN_CONFIG": "value"
    }
    with pytest.raises(click.ClickException, match="reserved"):
        KubernetesSandboxLauncher(env=["HOME"])._resolve_sandbox_env()
    with pytest.raises(click.ClickException, match="credential"):
        KubernetesSandboxLauncher(env=["MY_API_KEY"])._resolve_sandbox_env()
    with pytest.raises(click.ClickException, match="not set"):
        KubernetesSandboxLauncher(env=["DEFINITELY_UNSET_VAR_XYZ"])._resolve_sandbox_env()


def test_env_var_name_override_is_validated(monkeypatch: pytest.MonkeyPatch) -> None:
    """An env-var namespace override that isn't a valid RFC 1123 name fails fast."""
    monkeypatch.setenv(k8s.NAMESPACE_ENV_VAR, "Not_A_Valid_NS")
    with pytest.raises(click.ClickException, match="not a valid Kubernetes name"):
        KubernetesSandboxLauncher()._resolve_namespace()


# ── SDK-driven tests (fake kubernetes client) ───────────────


class _FakeApiException(Exception):
    """Stands in for ``kubernetes.client.rest.ApiException``."""

    def __init__(self, *, status: int | None = None, reason: str = "", body: str = "") -> None:
        super().__init__(reason or body or str(status))
        self.status = status
        self.reason = reason
        self.body = body


class _FakeConfigException(Exception):
    """Stands in for ``kubernetes.config.ConfigException``."""


class _FakeDeleteOptions:
    """Stands in for ``kubernetes.client.V1DeleteOptions``."""

    def __init__(self, propagation_policy=None):
        self.propagation_policy = propagation_policy


class _FakeCore:
    """Recording stand-in for ``CoreV1Api``."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.created_secrets: list[dict[str, object]] = []
        self.deleted_secrets: list[str] = []
        self.deleted_pods: list[str] = []
        self.delete_pod_errors: list[Exception | None] = []
        self.events: list[object] = []
        self.logs: dict[str, str] = {}
        self.read_queue: list[object] = []
        self.read_default: object = _pod(phase="Pending")
        self.create_secret_error: Exception | None = None
        self.list_pod_error: Exception | None = None
        self.last_label_selector: str | None = None
        self.pod_list_items: list[object] = []

    def create_namespaced_secret(self, namespace, body, _request_timeout=None):
        self.calls.append("create_secret")
        if self.create_secret_error is not None:
            raise self.create_secret_error
        self.created_secrets.append(body)

    def list_namespaced_pod(self, namespace, label_selector=None, _request_timeout=None):
        self.calls.append("list_pod")
        self.last_label_selector = label_selector
        if self.list_pod_error is not None:
            raise self.list_pod_error
        return SimpleNamespace(items=self.pod_list_items)

    def read_namespaced_pod(self, name, namespace, _request_timeout=None):
        self.calls.append("read_pod")
        resp = self.read_queue.pop(0) if self.read_queue else self.read_default
        if isinstance(resp, Exception):
            raise resp
        return resp

    def delete_namespaced_secret(self, name, namespace, _request_timeout=None):
        self.calls.append("delete_secret")
        self.deleted_secrets.append(name)

    def delete_namespaced_pod(self, name, namespace, _request_timeout=None):
        self.calls.append("delete_pod")
        if self.delete_pod_errors:
            err = self.delete_pod_errors.pop(0)
            if err is not None:
                raise err
        self.deleted_pods.append(name)

    def list_namespaced_event(self, namespace, field_selector=None, _request_timeout=None):
        return SimpleNamespace(items=self.events)

    def read_namespaced_pod_log(
        self, name, namespace, container=None, tail_lines=None, _request_timeout=None
    ):
        return self.logs.get(container, "")


class _FakeBatch:
    """Recording stand-in for ``BatchV1Api``."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.created_jobs: list[dict[str, object]] = []
        self.deleted_jobs: list[str] = []
        self.last_delete_body: object = None
        self.create_job_error: Exception | None = None
        self.delete_job_errors: list[Exception | None] = []

    def create_namespaced_job(self, namespace, body, _request_timeout=None):
        self.calls.append("create_job")
        if self.create_job_error is not None:
            raise self.create_job_error
        self.created_jobs.append(body)

    def delete_namespaced_job(self, name, namespace, body=None, _request_timeout=None):
        self.calls.append("delete_job")
        self.last_delete_body = body
        if self.delete_job_errors:
            err = self.delete_job_errors.pop(0)
            if err is not None:
                raise err
        self.deleted_jobs.append(name)


def _pod(phase=None, init_statuses=None, container_statuses=None, conditions=None):
    """Build a ``V1Pod`` stand-in (the launcher reads only ``status`` via getattr)."""
    return SimpleNamespace(
        metadata=SimpleNamespace(name="omnigent-pod-child-xyz"),
        status=SimpleNamespace(
            phase=phase,
            init_container_statuses=init_statuses,
            container_statuses=container_statuses,
            conditions=conditions,
        ),
    )


def _terminated(exit_code, *, name, reason="Error"):
    """A container status in the terminated state."""
    return SimpleNamespace(
        name=name,
        state=SimpleNamespace(
            terminated=SimpleNamespace(exit_code=exit_code, reason=reason), waiting=None
        ),
    )


@pytest.fixture
def fake_clients(monkeypatch: pytest.MonkeyPatch) -> tuple[_FakeCore, _FakeBatch]:
    """Inject a fake ``kubernetes`` package and return the recording CoreV1Api + BatchV1Api."""
    core = _FakeCore()
    batch = _FakeBatch()

    client_mod = types.ModuleType("kubernetes.client")
    client_mod.ApiException = _FakeApiException  # type: ignore[attr-defined]
    client_mod.Configuration = lambda: SimpleNamespace()  # type: ignore[attr-defined]
    client_mod.ApiClient = lambda cfg=None: SimpleNamespace(  # type: ignore[attr-defined]
        close=lambda: None
    )
    client_mod.CoreV1Api = lambda api_client=None: core  # type: ignore[attr-defined]
    client_mod.BatchV1Api = lambda api_client=None: batch  # type: ignore[attr-defined]
    client_mod.V1DeleteOptions = _FakeDeleteOptions  # type: ignore[attr-defined]
    rest_mod = types.ModuleType("kubernetes.client.rest")
    rest_mod.ApiException = _FakeApiException  # type: ignore[attr-defined]
    config_mod = types.ModuleType("kubernetes.config")
    config_mod.load_incluster_config = lambda client_configuration=None: None  # type: ignore[attr-defined]
    config_mod.load_kube_config = (  # type: ignore[attr-defined]
        lambda config_file=None, client_configuration=None: None
    )
    config_mod.ConfigException = _FakeConfigException  # type: ignore[attr-defined]
    pkg = types.ModuleType("kubernetes")
    pkg.client = client_mod  # type: ignore[attr-defined]
    pkg.config = config_mod  # type: ignore[attr-defined]

    for name, mod in (
        ("kubernetes", pkg),
        ("kubernetes.client", client_mod),
        ("kubernetes.client.rest", rest_mod),
        ("kubernetes.config", config_mod),
    ):
        monkeypatch.setitem(sys.modules, name, mod)
    # No-op the poll/backoff sleeps so the readiness/retry loops run instantly.
    monkeypatch.setattr(k8s.time, "sleep", lambda _s: None)
    return core, batch


def _launcher() -> KubernetesSandboxLauncher:
    """A launcher pinned to in-cluster config with explicit, env-free settings."""
    return KubernetesSandboxLauncher(
        in_cluster=True, namespace="omnigent-sandboxes", secret_name="omnigent-creds", env=()
    )


def _setup_pod_discovery(core: _FakeCore, pod_phase: str = "Running") -> None:
    """Set up the fake core to return a child Pod when listed by job-name label."""
    child_pod = _pod(phase=pod_phase)
    core.pod_list_items = [child_pod]
    core.read_queue = [child_pod]


def test_launch_host_creates_secret_then_job_and_returns_workspace(
    fake_clients: tuple[_FakeCore, _FakeBatch],
) -> None:
    """The happy path creates the token Secret BEFORE the Job and returns the workspace."""
    core, batch = fake_clients
    _setup_pod_discovery(core)
    workspace = _launcher().start_host(
        "omnigent-job-1",
        token=_TOKEN,
        host_id="host_1",
        host_name="managed-1",
        server_url="http://srv.example.com",
    )
    assert workspace == "/home/omnigent/workspace"
    # Secret is created before the Job.
    all_calls = core.calls + batch.calls
    assert all_calls.index("create_secret") < all_calls.index("create_job")
    assert core.created_secrets[0]["stringData"] == {HOST_TOKEN_ENV_VAR: _TOKEN}
    assert batch.created_jobs[0]["metadata"]["name"] == "omnigent-job-1"
    # Nothing torn down on success.
    assert batch.deleted_jobs == []


def test_launch_host_threads_pvc_mounts_into_the_job(
    fake_clients: tuple[_FakeCore, _FakeBatch],
) -> None:
    """A launcher built with pvc_mounts creates Jobs carrying the PVC volume."""
    core, batch = fake_clients
    _setup_pod_discovery(core)
    launcher = KubernetesSandboxLauncher(
        in_cluster=True,
        namespace="omnigent-sandboxes",
        secret_name="omnigent-creds",
        env=(),
        pvc_mounts=[
            {"claim_name": "omnigent-datasets", "mount_path": "/mnt/datasets", "read_only": True}
        ],
    )
    launcher.start_host(
        "omnigent-job-1",
        token=_TOKEN,
        host_id="host_1",
        host_name="managed-1",
        server_url="http://srv.example.com",
    )
    pod_spec = batch.created_jobs[0]["spec"]["template"]["spec"]
    assert {
        "name": "pvc-0",
        "persistentVolumeClaim": {"claimName": "omnigent-datasets", "readOnly": True},
    } in pod_spec["volumes"]


def test_launch_host_threads_secret_mounts_into_the_job(
    fake_clients: tuple[_FakeCore, _FakeBatch],
) -> None:
    """A launcher built with secret_mounts creates Jobs carrying the secret volume."""
    core, batch = fake_clients
    _setup_pod_discovery(core)
    launcher = KubernetesSandboxLauncher(
        in_cluster=True,
        namespace="omnigent-sandboxes",
        secret_name="omnigent-creds",
        env=(),
        secret_mounts=[{"secret_name": "git-token", "mount_path": "/mnt/secrets/git"}],
    )
    launcher.start_host(
        "omnigent-job-1",
        token=_TOKEN,
        host_id="host_1",
        host_name="managed-1",
        server_url="http://srv.example.com",
    )
    pod_spec = batch.created_jobs[0]["spec"]["template"]["spec"]
    assert {
        "name": "secret-0",
        "secret": {"secretName": "git-token", "optional": False, "defaultMode": 0o440},
    } in pod_spec["volumes"]


def test_launch_host_threads_agent_label_into_the_job(
    fake_clients: tuple[_FakeCore, _FakeBatch],
) -> None:
    """start_host stamps the resolved agent on the created Job's labels."""
    core, batch = fake_clients
    _setup_pod_discovery(core)
    _launcher().start_host(
        "omnigent-job-1",
        token=_TOKEN,
        host_id="host_1",
        host_name="managed-1",
        server_url="http://srv.example.com",
        agent_name="research-agent",
    )
    labels = batch.created_jobs[0]["metadata"]["labels"]
    assert labels["omnigent.ai/agent"] == "research-agent"
    assert labels["app.kubernetes.io/managed-by"] == "omnigent"


def test_launch_host_without_agent_label_keeps_reserved_labels(
    fake_clients: tuple[_FakeCore, _FakeBatch],
) -> None:
    """No agent_name → the created Job carries only the reserved managed-by/role pair."""
    core, batch = fake_clients
    _setup_pod_discovery(core)
    _launcher().start_host(
        "omnigent-job-1",
        token=_TOKEN,
        host_id="host_1",
        host_name="managed-1",
        server_url="http://srv.example.com",
    )
    assert "omnigent.ai/agent" not in batch.created_jobs[0]["metadata"]["labels"]


def test_launch_host_with_repo_returns_clone_dir(
    fake_clients: tuple[_FakeCore, _FakeBatch],
) -> None:
    """With a repo, the returned workspace is the cloned directory under the workspace."""
    core, _batch = fake_clients
    _setup_pod_discovery(core)
    workspace = _launcher().start_host(
        "omnigent-job-2",
        token=_TOKEN,
        host_id="host_2",
        host_name="managed-2",
        server_url="http://srv.example.com",
        repo_url="https://github.com/org/repo.git",
        repo_name="repo",
    )
    assert workspace == "/home/omnigent/workspace/repo"


def test_launch_host_cleans_up_on_create_failure(
    fake_clients: tuple[_FakeCore, _FakeBatch],
) -> None:
    """A failed Job create reaps the already-created token Secret and raises."""
    core, batch = fake_clients
    batch.create_job_error = _FakeApiException(status=500, reason="Internal Server Error")
    with pytest.raises(click.ClickException, match="create sandbox job"):
        _launcher().start_host(
            "omnigent-job-3",
            token=_TOKEN,
            host_id="host_3",
            host_name="managed-3",
            server_url="http://srv.example.com",
        )
    assert "omnigent-job-3-token" in core.deleted_secrets
    assert "omnigent-job-3" in batch.deleted_jobs


def test_launch_host_invalid_config_home_fails_before_creating_secret(
    fake_clients: tuple[_FakeCore, _FakeBatch], monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    An out-of-HOME config dir must fail while the manifest is built — before the
    token Secret is created.
    """
    core, _batch = fake_clients
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", "/tmp/outside")
    launcher = KubernetesSandboxLauncher(
        in_cluster=True,
        namespace="omnigent-sandboxes",
        secret_name="omnigent-creds",
        env=["OMNIGENT_CONFIG_HOME"],
    )
    with pytest.raises(ValueError, match=r"OMNIGENT_CONFIG_HOME.*must resolve under"):
        launcher.start_host(
            "omnigent-job-x",
            token=_TOKEN,
            host_id="host_x",
            host_name="managed-x",
            server_url="http://srv.example.com",
            host_config=_HOST_CONFIG,
        )
    assert "create_secret" not in core.calls
    assert core.created_secrets == []


def test_launch_host_fast_fails_on_clone_failure_with_log_tail(
    fake_clients: tuple[_FakeCore, _FakeBatch],
) -> None:
    """Once backoffLimit is exhausted (phase Failed), fast-fail with the log tail."""
    core, batch = fake_clients
    # Phase "Failed" means the Job exhausted its backoffLimit — terminal.
    failed_pod = _pod(
        phase="Failed",
        init_statuses=[_terminated(128, name="workspace-prep")],
    )
    core.pod_list_items = [failed_pod]
    core.read_queue = [failed_pod]
    core.logs["workspace-prep"] = "fatal: repository 'https://x/y.git' not found"
    with pytest.raises(click.ClickException) as exc:
        _launcher().start_host(
            "omnigent-job-4",
            token=_TOKEN,
            host_id="host_4",
            host_name="managed-4",
            server_url="http://srv.example.com",
            repo_url="https://x/y.git",
            repo_name="y",
        )
    assert "workspace prep failed (exit 128" in exc.value.message
    assert "repository 'https://x/y.git' not found" in exc.value.message
    # The orphaned Job and Secret are cleaned up on failure.
    assert "delete_job" in batch.calls
    assert core.deleted_secrets == ["omnigent-job-4-token"]


def test_launch_host_times_out_with_reason(
    fake_clients: tuple[_FakeCore, _FakeBatch], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Pod that never runs times out fast, surfacing the last waiting reason."""
    core, _batch = fake_clients
    monkeypatch.setattr(k8s, "_POD_READY_TIMEOUT_S", 0.01)
    pending_pod = _pod(
        phase="Pending",
        container_statuses=[
            SimpleNamespace(
                name="host",
                state=SimpleNamespace(
                    waiting=SimpleNamespace(reason="ImagePullBackOff", message="back-off"),
                    terminated=None,
                ),
            )
        ],
    )
    core.pod_list_items = [pending_pod]
    core.read_default = pending_pod
    with pytest.raises(click.ClickException, match="did not start within"):
        _launcher().start_host(
            "omnigent-job-5",
            token=_TOKEN,
            host_id="host_5",
            host_name="managed-5",
            server_url="http://srv.example.com",
        )


@pytest.mark.parametrize(
    ("wait_state", "expected"),
    [
        ("undiscovered", "did not create a child pod within 1s"),
        ("replaced", "could not be rediscovered before the 1s deadline"),
        ("read-error", "could not be read before the 1s deadline"),
        ("pending", "did not start within 1s"),
    ],
)
def test_configured_pod_ready_timeout_bounds_entire_job_wait(
    fake_clients: tuple[_FakeCore, _FakeBatch],
    monkeypatch: pytest.MonkeyPatch,
    wait_state: str,
    expected: str,
) -> None:
    """The configured budget bounds discovery, replacement, reads, and Pending."""
    core, _batch = fake_clients
    pod = _pod(phase="Pending")
    if wait_state != "undiscovered":
        core.pod_list_items = [pod]
    if wait_state == "replaced":
        core.read_default = _FakeApiException(status=404, reason="Not Found")
    elif wait_state == "read-error":
        core.read_default = _FakeApiException(status=500, reason="Internal Server Error")
    else:
        core.read_default = pod

    ticks = iter((0.0, 1.0))
    monkeypatch.setattr(k8s.time, "monotonic", lambda: next(ticks))
    launcher = KubernetesSandboxLauncher(
        in_cluster=True,
        namespace="omnigent-sandboxes",
        secret_name="omnigent-creds",
        env=(),
        pod_ready_timeout_s=1,
    )

    with pytest.raises(click.ClickException, match=expected):
        launcher._wait_for_pod_running("omnigent-sandboxes", "omnigent-job-timeout")


def test_terminate_deletes_job_and_secret(
    fake_clients: tuple[_FakeCore, _FakeBatch],
) -> None:
    """Terminate deletes the Job, attempts bare-Pod fallback, and deletes the Secret."""
    core, batch = fake_clients
    _launcher().terminate("omnigent-job-6")
    assert batch.deleted_jobs == ["omnigent-job-6"]
    assert batch.last_delete_body.propagation_policy == "Foreground"
    assert core.deleted_pods == ["omnigent-job-6"]
    assert core.deleted_secrets == ["omnigent-job-6-token"]


def test_resume_recycles_job_and_token_secret(
    fake_clients: tuple[_FakeCore, _FakeBatch],
) -> None:
    """Resume clears stale launch resources so start_host can recreate the same id."""
    core, batch = fake_clients
    launcher = _launcher()

    assert launcher.can_resume is True
    assert launcher.capabilities.resume_stopped is True

    launcher.resume("omnigent-job-resume")

    assert batch.deleted_jobs == ["omnigent-job-resume"]
    assert batch.last_delete_body.propagation_policy == "Foreground"
    assert core.deleted_pods == ["omnigent-job-resume"]
    assert core.deleted_secrets == ["omnigent-job-resume-token"]


def test_terminate_is_idempotent_on_404(
    fake_clients: tuple[_FakeCore, _FakeBatch],
) -> None:
    """A Job 404 still deletes the bare Pod fallback and the Secret."""
    core, batch = fake_clients
    batch.delete_job_errors = [_FakeApiException(status=404, reason="Not Found")]
    _launcher().terminate("omnigent-job-7")  # must not raise
    assert core.deleted_pods == ["omnigent-job-7"]
    assert core.deleted_secrets == ["omnigent-job-7-token"]


def test_terminate_retries_transient_then_gives_up_best_effort(
    fake_clients: tuple[_FakeCore, _FakeBatch], capsys: pytest.CaptureFixture[str]
) -> None:
    """A persistent transient delete error is retried, then warned (not raised)."""
    from urllib3.exceptions import HTTPError

    core, batch = fake_clients
    batch.delete_job_errors = [HTTPError("timeout")] * k8s._DELETE_MAX_ATTEMPTS
    _launcher().terminate("omnigent-job-8")  # best-effort: must not raise
    assert "could not delete Kubernetes job 'omnigent-job-8'" in capsys.readouterr().err
    # The Secret delete still runs after the Job gives up.
    assert core.deleted_secrets == ["omnigent-job-8-token"]


def test_terminate_still_deletes_secret_on_job_403(
    fake_clients: tuple[_FakeCore, _FakeBatch],
) -> None:
    """A 403 on Job delete still cleans up the Secret (no leak)."""
    core, batch = fake_clients
    batch.delete_job_errors = [_FakeApiException(status=403, reason="Forbidden")]
    with pytest.raises(click.ClickException, match="Forbidden"):
        _launcher().terminate("omnigent-job-9")
    # Secret must still be deleted even though Job delete raised.
    assert core.deleted_secrets == ["omnigent-job-9-token"]


def test_find_job_pod_raises_on_403(
    fake_clients: tuple[_FakeCore, _FakeBatch],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 403 on pods:list surfaces immediately, not as a misleading 90s timeout."""
    core, _batch = fake_clients
    monkeypatch.setattr(k8s, "_POD_READY_TIMEOUT_S", 0.01)
    core.list_pod_error = _FakeApiException(status=403, reason="Forbidden")
    core.read_default = _pod(phase="Running")
    with pytest.raises(click.ClickException, match="list sandbox pods"):
        _launcher().start_host(
            "omnigent-job-rbac",
            token=_TOKEN,
            host_id="host_rbac",
            host_name="managed-rbac",
            server_url="http://srv.example.com",
        )


def test_find_job_pod_sends_correct_label_selector(
    fake_clients: tuple[_FakeCore, _FakeBatch],
) -> None:
    """The launcher queries for the child Pod using the correct job-name label."""
    core, _batch = fake_clients
    _setup_pod_discovery(core)
    _launcher().start_host(
        "omnigent-job-sel",
        token=_TOKEN,
        host_id="host_sel",
        host_name="managed-sel",
        server_url="http://srv.example.com",
    )
    assert core.last_label_selector == "job-name=omnigent-job-sel"


def test_wait_rediscovers_pod_on_404(
    fake_clients: tuple[_FakeCore, _FakeBatch],
) -> None:
    """A 404 on read (Pod replaced) triggers re-discovery, not a terminal failure."""
    core, _batch = fake_clients
    replacement_pod = _pod(phase="Running")
    replacement_pod.metadata = SimpleNamespace(
        name="omnigent-job-repl-abc", deletion_timestamp=None
    )
    # First discovery returns original, which 404s on read.
    # Second discovery returns the replacement, which is Running.
    original_pod = _pod(phase="Pending")
    original_pod.metadata = SimpleNamespace(name="omnigent-job-repl-xyz", deletion_timestamp=None)
    core.pod_list_items = [original_pod]
    core.read_queue = [
        _FakeApiException(status=404, reason="Not Found"),
    ]

    # After the 404, re-discovery should find the replacement.
    def list_pod_side_effect(namespace, label_selector=None, _request_timeout=None):
        core.calls.append("list_pod")
        core.last_label_selector = label_selector
        # Second call returns the replacement.
        core.pod_list_items = [replacement_pod]
        return SimpleNamespace(items=core.pod_list_items)

    core.list_namespaced_pod = list_pod_side_effect
    core.read_default = replacement_pod
    workspace = _launcher().start_host(
        "omnigent-job-repl",
        token=_TOKEN,
        host_id="host_repl",
        host_name="managed-repl",
        server_url="http://srv.example.com",
    )
    assert workspace is not None


def test_crashloopbackoff_is_terminal(
    fake_clients: tuple[_FakeCore, _FakeBatch],
) -> None:
    """A host in CrashLoopBackOff is detected even though the Pod is Running."""
    core, _batch = fake_clients
    crashloop_pod = _pod(
        phase="Running",
        container_statuses=[
            SimpleNamespace(
                name="host",
                restart_count=3,
                state=SimpleNamespace(
                    waiting=SimpleNamespace(reason="CrashLoopBackOff", message="back-off"),
                    terminated=None,
                ),
            )
        ],
    )
    core.pod_list_items = [crashloop_pod]
    core.read_queue = [crashloop_pod]
    with pytest.raises(click.ClickException, match="crash-looping"):
        _launcher().start_host(
            "omnigent-job-crash",
            token=_TOKEN,
            host_id="host_crash",
            host_name="managed-crash",
            server_url="http://srv.example.com",
        )


def test_init_failure_pending_is_not_terminal(
    fake_clients: tuple[_FakeCore, _FakeBatch],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under OnFailure, a Pending init failure is NOT terminal (kubelet retries)."""
    core, _batch = fake_clients
    monkeypatch.setattr(k8s, "_POD_READY_TIMEOUT_S", 0.01)
    # Init container failed but Pod is still Pending — kubelet will retry.
    pending_pod = _pod(
        phase="Pending",
        init_statuses=[_terminated(128, name="workspace-prep")],
    )
    core.pod_list_items = [pending_pod]
    core.read_default = pending_pod
    # Should time out, NOT fast-fail with "workspace prep failed".
    with pytest.raises(click.ClickException, match="did not start within"):
        _launcher().start_host(
            "omnigent-job-retry",
            token=_TOKEN,
            host_id="host_retry",
            host_name="managed-retry",
            server_url="http://srv.example.com",
        )


def test_provision_reserves_pod_name_and_no_exec_transport() -> None:
    """provision reserves a Pod name (no Pod created); no exec transport."""
    launcher = _launcher()
    name = launcher.provision("managed-abc")
    assert name.startswith("omnigent-managed-abc-")
    assert not hasattr(launcher, "run")
    assert launcher.capabilities.cli_bootstrap is False
    assert launcher.capabilities.classifies_runner_by_agent is True
