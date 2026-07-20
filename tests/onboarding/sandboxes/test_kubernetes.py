"""
Tests for the Kubernetes (entrypoint-as-host) sandbox launcher.

The official ``kubernetes`` client is an optional dependency, so the SDK-driven
tests inject a small fake package into ``sys.modules`` (no real cluster, no real
client). The entrypoint model needs only ``CoreV1Api`` create/read/delete/log
fakes — there is no exec transport to fake.
"""

from __future__ import annotations

import json
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
    SandboxCapabilityError,
    render_host_config_write_command,
)
from omnigent.onboarding.sandboxes.kubernetes import (
    KubernetesSandboxLauncher,
    build_pod_manifest,
    build_token_secret_manifest,
)

_TOKEN = "launch-token-xyz"
_MANIFEST_KW = {
    "pod_name": "omnigent-managed-abc-1a2b3c",
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


# ── pure manifest / rendering tests (no SDK) ────────────────


def test_build_pod_manifest_runs_host_under_reaper_as_container_command() -> None:
    """The main container's command execs the PID-1 reaper, which runs the host."""
    manifest = build_pod_manifest(**_MANIFEST_KW)
    containers = manifest["spec"]["containers"]
    assert len(containers) == 1
    host = containers[0]
    assert host["name"] == "host"
    command = host["command"]
    assert command[:2] == ["bash", "-lc"]
    script = command[2]
    # exec the reaper (so it is PID 1) which then runs `omnigent host`.
    assert "exec python3 -c" in script
    assert "omnigent host --server http://srv.example.com" in script
    # The reaper source rides the command (spawns sys.argv[1:] + reaps children).
    assert "os.wait()" in script


def test_build_pod_manifest_init_container_prepares_and_clones_workspace() -> None:
    """The init container makes the workspace and clones the repo before the host."""
    manifest = build_pod_manifest(
        **{**_MANIFEST_KW, "clone_dir": "/home/omnigent/workspace/repo"},
        repo_url="https://github.com/org/repo.git",
        repo_branch="main",
    )
    init = manifest["spec"]["initContainers"]
    assert len(init) == 1
    assert init[0]["name"] == "workspace-prep"
    script = init[0]["command"][2]
    assert "mkdir -p /home/omnigent/workspace" in script
    assert "git clone --branch main --single-branch -- " in script
    assert "https://github.com/org/repo.git /home/omnigent/workspace/repo" in script


def test_build_pod_manifest_without_repo_has_no_clone() -> None:
    """No repo → the init container only makes the workspace, no git clone."""
    manifest = build_pod_manifest(**_MANIFEST_KW)
    script = manifest["spec"]["initContainers"][0]["command"][2]
    assert "mkdir -p /home/omnigent/workspace" in script
    assert "git clone" not in script


def test_build_pod_manifest_host_config_is_written_by_init_container() -> None:
    """host_config rides the init container script, after mkdir/clone, before the host."""
    manifest = build_pod_manifest(
        **{**_MANIFEST_KW, "clone_dir": "/home/omnigent/workspace/repo"},
        repo_url="https://github.com/org/repo.git",
        host_config=_HOST_CONFIG,
    )
    script = manifest["spec"]["initContainers"][0]["command"][2]
    write_command = render_host_config_write_command(_HOST_CONFIG)
    assert write_command in script
    # Ordering within the script: workspace prep and clone come first.
    assert script.index("mkdir -p") < script.index("git clone") < script.index(write_command)
    # The main container is untouched — the write happens before the host boots.
    assert write_command not in manifest["spec"]["containers"][0]["command"][2]


def test_build_pod_manifest_forwards_config_home_to_init_container() -> None:
    """Init and host resolve injected config under the same configured directory."""
    manifest = build_pod_manifest(
        **{
            **_MANIFEST_KW,
            "env_literals": {
                "OMNIGENT_CONFIG_HOME": "/home/omnigent/custom-config",
                "PLAIN_CONFIG": "host-only",
            },
        },
        host_config=_HOST_CONFIG,
    )

    init_env = manifest["spec"]["initContainers"][0]["env"]
    host_env = manifest["spec"]["containers"][0]["env"]
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
def test_build_pod_manifest_rejects_config_home_outside_home_dir(config_home: str) -> None:
    """
    Init and host share only the HOME emptyDir, so a config dir that resolves
    outside it would make the injected config invisible to the host. Fail the
    launch loudly — including paths that only escape after normalizing ``..``.
    """
    with pytest.raises(ValueError, match=r"OMNIGENT_CONFIG_HOME.*must resolve under"):
        build_pod_manifest(
            **{**_MANIFEST_KW, "env_literals": {"OMNIGENT_CONFIG_HOME": config_home}},
            host_config=_HOST_CONFIG,
        )


@pytest.mark.parametrize(
    "config_home",
    ["/home/omnigent", "/home/omnigent/", "/home/omnigent/cfg", "cfg", "relative/dir", ".", ""],
)
def test_build_pod_manifest_accepts_config_home_at_or_under_home_dir(config_home: str) -> None:
    """
    A dir at or under HOME (absolute or relative to the shared workingDir) is on
    the shared volume — allowed. An empty value is treated as unset by the
    writer, so it is forwarded without validation. All are forwarded to init.
    """
    manifest = build_pod_manifest(
        **{**_MANIFEST_KW, "env_literals": {"OMNIGENT_CONFIG_HOME": config_home}},
        host_config=_HOST_CONFIG,
    )
    assert {"name": "OMNIGENT_CONFIG_HOME", "value": config_home} in manifest["spec"][
        "initContainers"
    ][0]["env"]


def test_build_pod_manifest_config_home_outside_home_dir_ok_without_host_config() -> None:
    """Without host_config the init container writes nothing, so the path is moot."""
    manifest = build_pod_manifest(
        **{**_MANIFEST_KW, "env_literals": {"OMNIGENT_CONFIG_HOME": "/tmp/elsewhere"}},
    )
    init_env = manifest["spec"]["initContainers"][0]["env"]
    assert {"name": "OMNIGENT_CONFIG_HOME", "value": "/tmp/elsewhere"} in init_env


def test_build_pod_manifest_without_host_config_has_no_config_write() -> None:
    """No host_config → the init container only preps the workspace."""
    manifest = build_pod_manifest(**_MANIFEST_KW)
    script = manifest["spec"]["initContainers"][0]["command"][2]
    assert "config.yaml" not in script
    assert "python3 -c" not in script


def test_build_pod_manifest_token_rides_secret_ref_not_the_spec() -> None:
    """The launch token is referenced via secretKeyRef, never written into the spec."""
    manifest = build_pod_manifest(**_MANIFEST_KW)
    host_env = manifest["spec"]["containers"][0]["env"]
    token_entry = next(e for e in host_env if e["name"] == HOST_TOKEN_ENV_VAR)
    assert token_entry["valueFrom"]["secretKeyRef"] == {
        "name": "omnigent-managed-abc-1a2b3c-token",
        "key": HOST_TOKEN_ENV_VAR,
    }
    assert "value" not in token_entry
    # Identity is plain env; the raw token appears nowhere in the manifest.
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


def test_build_pod_manifest_harness_secret_projects_into_both_containers() -> None:
    """The harness creds Secret is projected via envFrom on init (for clone) + host."""
    manifest = build_pod_manifest(**_MANIFEST_KW)
    init = manifest["spec"]["initContainers"][0]
    host = manifest["spec"]["containers"][0]
    assert init["envFrom"] == [{"secretRef": {"name": "omnigent-creds"}}]
    assert host["envFrom"] == [{"secretRef": {"name": "omnigent-creds"}}]


def test_build_pod_manifest_omits_envfrom_without_harness_secret() -> None:
    """No harness Secret → no envFrom key on either container."""
    manifest = build_pod_manifest(**{**_MANIFEST_KW, "harness_secret": None})
    assert "envFrom" not in manifest["spec"]["initContainers"][0]
    assert "envFrom" not in manifest["spec"]["containers"][0]


def test_build_pod_manifest_defaults_to_amd64_node_selector() -> None:
    """No node_selector → Pods keep the amd64 default placement."""
    manifest = build_pod_manifest(**{**_MANIFEST_KW, "node_selector": None})
    assert manifest["spec"]["nodeSelector"] == {"kubernetes.io/arch": "amd64"}


def test_build_pod_manifest_node_selector_can_override_arch() -> None:
    """An operator kubernetes.io/arch entry overrides the amd64 default."""
    manifest = build_pod_manifest(
        **{**_MANIFEST_KW, "node_selector": {"disktype": "ssd", "kubernetes.io/arch": "arm64"}}
    )
    selector = manifest["spec"]["nodeSelector"]
    assert selector["kubernetes.io/arch"] == "arm64"
    assert selector["disktype"] == "ssd"


def test_build_pod_manifest_is_restricted_and_least_privilege() -> None:
    """The Pod satisfies Pod Security 'restricted' and mounts no SA token."""
    manifest = build_pod_manifest(**_MANIFEST_KW)
    spec = manifest["spec"]
    assert spec["restartPolicy"] == "Never"
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
    """Pod names are DNS-label-safe and the token Secret is the pod name + suffix."""
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


class _FakeCore:
    """Recording stand-in for ``CoreV1Api`` (entrypoint model: no exec)."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.created_secrets: list[dict[str, object]] = []
        self.created_pods: list[dict[str, object]] = []
        self.deleted_pods: list[str] = []
        self.deleted_secrets: list[str] = []
        self.events: list[object] = []
        self.logs: dict[str, str] = {}
        self.read_queue: list[object] = []
        self.read_default: object = _pod(phase="Pending")
        self.create_secret_error: Exception | None = None
        self.create_pod_error: Exception | None = None
        self.delete_pod_errors: list[Exception | None] = []

    def create_namespaced_secret(self, namespace, body, _request_timeout=None):
        self.calls.append("create_secret")
        if self.create_secret_error is not None:
            raise self.create_secret_error
        self.created_secrets.append(body)

    def create_namespaced_pod(self, namespace, body, _request_timeout=None):
        self.calls.append("create_pod")
        if self.create_pod_error is not None:
            raise self.create_pod_error
        self.created_pods.append(body)

    def read_namespaced_pod(self, name, namespace, _request_timeout=None):
        self.calls.append("read_pod")
        resp = self.read_queue.pop(0) if self.read_queue else self.read_default
        if isinstance(resp, Exception):
            raise resp
        return resp

    def delete_namespaced_pod(
        self, name, namespace, grace_period_seconds=None, _request_timeout=None
    ):
        self.calls.append("delete_pod")
        if self.delete_pod_errors:
            err = self.delete_pod_errors.pop(0)
            if err is not None:
                raise err
        self.deleted_pods.append(name)

    def delete_namespaced_secret(self, name, namespace, _request_timeout=None):
        self.calls.append("delete_secret")
        self.deleted_secrets.append(name)

    def list_namespaced_event(self, namespace, field_selector=None, _request_timeout=None):
        return SimpleNamespace(items=self.events)

    def read_namespaced_pod_log(
        self, name, namespace, container=None, tail_lines=None, _request_timeout=None
    ):
        return self.logs.get(container, "")


def _pod(phase=None, init_statuses=None, container_statuses=None, conditions=None):
    """Build a ``V1Pod`` stand-in (the launcher reads only ``status`` via getattr)."""
    return SimpleNamespace(
        status=SimpleNamespace(
            phase=phase,
            init_container_statuses=init_statuses,
            container_statuses=container_statuses,
            conditions=conditions,
        )
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
def fake_core(monkeypatch: pytest.MonkeyPatch) -> _FakeCore:
    """Inject a fake ``kubernetes`` package and return the recording CoreV1Api."""
    core = _FakeCore()

    client_mod = types.ModuleType("kubernetes.client")
    client_mod.ApiException = _FakeApiException  # type: ignore[attr-defined]
    client_mod.Configuration = lambda: SimpleNamespace()  # type: ignore[attr-defined]
    client_mod.ApiClient = lambda cfg=None: SimpleNamespace(  # type: ignore[attr-defined]
        close=lambda: None
    )
    client_mod.CoreV1Api = lambda api_client=None: core  # type: ignore[attr-defined]
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
    return core


def _launcher() -> KubernetesSandboxLauncher:
    """A launcher pinned to in-cluster config with explicit, env-free settings."""
    return KubernetesSandboxLauncher(
        in_cluster=True, namespace="omnigent-sandboxes", secret_name="omnigent-creds", env=()
    )


def test_launch_host_creates_secret_then_pod_and_returns_workspace(
    fake_core: _FakeCore,
) -> None:
    """The happy path creates the token Secret BEFORE the Pod and returns the workspace."""
    fake_core.read_queue = [_pod(phase="Running")]
    workspace = _launcher().start_host(
        "omnigent-pod-1",
        token=_TOKEN,
        host_id="host_1",
        host_name="managed-1",
        server_url="http://srv.example.com",
    )
    assert workspace == "/home/omnigent/workspace"
    # Secret is created before the Pod (so the secretKeyRef resolves immediately).
    assert fake_core.calls.index("create_secret") < fake_core.calls.index("create_pod")
    assert fake_core.created_secrets[0]["stringData"] == {HOST_TOKEN_ENV_VAR: _TOKEN}
    assert fake_core.created_pods[0]["metadata"]["name"] == "omnigent-pod-1"
    # Nothing torn down on success.
    assert fake_core.deleted_pods == []


def test_launch_host_with_repo_returns_clone_dir(fake_core: _FakeCore) -> None:
    """With a repo, the returned workspace is the cloned directory under the workspace."""
    fake_core.read_queue = [_pod(phase="Running")]
    workspace = _launcher().start_host(
        "omnigent-pod-2",
        token=_TOKEN,
        host_id="host_2",
        host_name="managed-2",
        server_url="http://srv.example.com",
        repo_url="https://github.com/org/repo.git",
        repo_name="repo",
    )
    assert workspace == "/home/omnigent/workspace/repo"


def test_launch_host_cleans_up_on_create_failure(fake_core: _FakeCore) -> None:
    """A failed Pod create reaps the already-created token Secret and raises."""
    fake_core.create_pod_error = _FakeApiException(status=500, reason="Internal Server Error")
    with pytest.raises(click.ClickException, match="create sandbox pod"):
        _launcher().start_host(
            "omnigent-pod-3",
            token=_TOKEN,
            host_id="host_3",
            host_name="managed-3",
            server_url="http://srv.example.com",
        )
    assert "omnigent-pod-3-token" in fake_core.deleted_secrets
    assert "omnigent-pod-3" in fake_core.deleted_pods


def test_launch_host_invalid_config_home_fails_before_creating_secret(
    fake_core: _FakeCore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    An out-of-HOME config dir must fail while the manifest is built — before the
    token Secret is created — so no credential-bearing Secret is orphaned.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", "/tmp/outside")
    launcher = KubernetesSandboxLauncher(
        in_cluster=True,
        namespace="omnigent-sandboxes",
        secret_name="omnigent-creds",
        env=["OMNIGENT_CONFIG_HOME"],
    )
    with pytest.raises(ValueError, match=r"OMNIGENT_CONFIG_HOME.*must resolve under"):
        launcher.start_host(
            "omnigent-pod-x",
            token=_TOKEN,
            host_id="host_x",
            host_name="managed-x",
            server_url="http://srv.example.com",
            host_config=_HOST_CONFIG,
        )
    assert "create_secret" not in fake_core.calls
    assert fake_core.created_secrets == []


def test_launch_host_fast_fails_on_clone_failure_with_log_tail(
    fake_core: _FakeCore,
) -> None:
    """A non-zero init container (clone failed) fails fast with the git error log tail."""
    fake_core.read_queue = [
        _pod(
            phase="Pending",
            init_statuses=[_terminated(128, name="workspace-prep")],
        )
    ]
    fake_core.logs["workspace-prep"] = "fatal: repository 'https://x/y.git' not found"
    with pytest.raises(click.ClickException) as exc:
        _launcher().start_host(
            "omnigent-pod-4",
            token=_TOKEN,
            host_id="host_4",
            host_name="managed-4",
            server_url="http://srv.example.com",
            repo_url="https://x/y.git",
            repo_name="y",
        )
    assert "workspace prep failed (exit 128" in exc.value.message
    assert "repository 'https://x/y.git' not found" in exc.value.message
    # The orphaned Pod + Secret are reaped.
    assert "omnigent-pod-4" in fake_core.deleted_pods
    assert "omnigent-pod-4-token" in fake_core.deleted_secrets


def test_launch_host_times_out_with_reason(
    fake_core: _FakeCore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Pod that never runs times out fast, surfacing the last waiting reason."""
    monkeypatch.setattr(k8s, "_POD_READY_TIMEOUT_S", 0.01)
    fake_core.read_default = _pod(
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
    with pytest.raises(click.ClickException, match="did not start within"):
        _launcher().start_host(
            "omnigent-pod-5",
            token=_TOKEN,
            host_id="host_5",
            host_name="managed-5",
            server_url="http://srv.example.com",
        )


def test_terminate_deletes_pod_and_secret(fake_core: _FakeCore) -> None:
    """Terminate deletes both the Pod and its token Secret."""
    _launcher().terminate("omnigent-pod-6")
    assert fake_core.deleted_pods == ["omnigent-pod-6"]
    assert fake_core.deleted_secrets == ["omnigent-pod-6-token"]


def test_terminate_is_idempotent_on_404(fake_core: _FakeCore) -> None:
    """A Pod that no longer exists (404) is treated as success, and the Secret too."""
    fake_core.delete_pod_errors = [_FakeApiException(status=404, reason="Not Found")]
    _launcher().terminate("omnigent-pod-7")  # must not raise
    assert fake_core.deleted_secrets == ["omnigent-pod-7-token"]


def test_terminate_retries_transient_then_gives_up_best_effort(
    fake_core: _FakeCore, capsys: pytest.CaptureFixture[str]
) -> None:
    """A persistent transient delete error is retried, then warned (not raised)."""
    from urllib3.exceptions import HTTPError

    fake_core.delete_pod_errors = [HTTPError("timeout")] * k8s._POD_DELETE_MAX_ATTEMPTS
    _launcher().terminate("omnigent-pod-8")  # best-effort: must not raise
    assert "could not delete Kubernetes pod 'omnigent-pod-8'" in capsys.readouterr().err
    # The Secret delete still runs after the Pod gives up.
    assert fake_core.deleted_secrets == ["omnigent-pod-8-token"]


def test_provision_reserves_pod_name_and_run_is_unsupported() -> None:
    """provision reserves a Pod name (no Pod created); run has no exec transport."""
    launcher = _launcher()
    # provision reserves the id — it does NOT create a Pod and does NOT raise.
    name = launcher.provision("managed-abc")
    assert name.startswith("omnigent-managed-abc-")
    # run is unsupported: the host is the Pod entrypoint, there is no exec-in.
    with pytest.raises(SandboxCapabilityError):
        launcher.run("sb", "echo hi")
