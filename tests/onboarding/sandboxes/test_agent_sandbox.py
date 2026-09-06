"""
Tests for the agent-sandbox launcher.

The launcher inherits its Pod, credentials and readiness polling from the
Kubernetes provider (covered in ``test_kubernetes.py``), so these tests target
only what it changes: the Job -> ``Sandbox`` manifest conversion, the
refreshable ``shutdownTime``, and delete. The official ``kubernetes`` client is
an optional dependency, so a fake package with a recording
``CustomObjectsApi`` is injected into ``sys.modules``.
"""

from __future__ import annotations

import logging
import sys
import types
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import omnigent.onboarding.sandboxes.kubernetes as k8s
from omnigent.onboarding.sandboxes.agent_sandbox import (
    API_GROUP,
    API_VERSION,
    DEFAULT_SHUTDOWN_WINDOW_S,
    MIN_SHUTDOWN_WINDOW_S,
    SANDBOX_PLURAL,
    SHUTDOWN_WINDOW_ENV_VAR,
    STORAGE_CLASS_ENV_VAR,
    WORKSPACE_SIZE_ENV_VAR,
    WORKSPACE_VOLUME_NAME,
    AgentSandboxLauncher,
    build_sandbox_manifest,
    resolve_shutdown_window_s,
    resolve_workspace_volume,
)

_SANDBOX_ID = "omnigent-managed-abc-1a2b3c"
_MANIFEST_KW = {
    "job_name": _SANDBOX_ID,
    "namespace": "omnigent-sandboxes",
    "image": "ghcr.io/omnigent-ai/omnigent-host:latest",
    "service_account": "omnigent-runner",
    "host_id": "host_abcdef",
    "host_name": "managed-abcdef",
    "server_url": "http://srv.example.com",
    "token_secret_name": f"{_SANDBOX_ID}-token",
    "harness_secret": "omnigent-creds",
    "env_literals": {},
    "node_selector": None,
    "workspace": "/home/omnigent/workspace",
}


# ── fakes ──────────────────────────────────────────────


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
    """Recording stand-in for ``CoreV1Api`` (only what this launcher calls)."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.deleted_secrets: list[str] = []
        self.deleted_pods: list[str] = []
        self.read_pod_error: Exception | None = None
        self.pod_deletion_timestamp: str | None = None

    def read_namespaced_pod(self, name, namespace, _request_timeout=None):
        self.calls.append("read_pod")
        if self.read_pod_error is not None:
            raise self.read_pod_error
        return SimpleNamespace(
            metadata=SimpleNamespace(name=name, deletion_timestamp=self.pod_deletion_timestamp)
        )

    def delete_namespaced_secret(self, name, namespace, _request_timeout=None):
        self.calls.append("delete_secret")
        self.deleted_secrets.append(name)

    def delete_namespaced_pod(self, name, namespace, _request_timeout=None):
        self.calls.append("delete_pod")
        self.deleted_pods.append(name)


class _FakeCustom:
    """Recording stand-in for ``CustomObjectsApi``."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.created: list[dict[str, object]] = []
        self.patches: list[tuple[str, dict[str, object]]] = []
        self.deleted: list[str] = []
        self.create_error: Exception | None = None
        self.patch_error: Exception | None = None
        self.delete_error: Exception | None = None

    def create_namespaced_custom_object(
        self, group, version, namespace, plural, body, _request_timeout=None
    ):
        self.calls.append("create")
        assert (group, version, plural) == (API_GROUP, API_VERSION, SANDBOX_PLURAL)
        if self.create_error is not None:
            raise self.create_error
        self.created.append(body)

    def patch_namespaced_custom_object(
        self, group, version, namespace, plural, name, body, _request_timeout=None
    ):
        self.calls.append("patch")
        assert (group, version, plural) == (API_GROUP, API_VERSION, SANDBOX_PLURAL)
        if self.patch_error is not None:
            raise self.patch_error
        self.patches.append((name, body))

    def delete_namespaced_custom_object(
        self, group, version, namespace, plural, name, _request_timeout=None
    ):
        self.calls.append("delete")
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted.append(name)


@pytest.fixture
def fake_clients(monkeypatch: pytest.MonkeyPatch) -> tuple[_FakeCore, _FakeCustom]:
    """Inject a fake ``kubernetes`` package; return the recording Core + CustomObjects."""
    core = _FakeCore()
    custom = _FakeCustom()

    client_mod = types.ModuleType("kubernetes.client")
    client_mod.ApiException = _FakeApiException  # type: ignore[attr-defined]
    client_mod.Configuration = SimpleNamespace  # type: ignore[attr-defined]
    client_mod.ApiClient = lambda cfg=None: SimpleNamespace(  # type: ignore[attr-defined]
        close=lambda: None
    )
    client_mod.CoreV1Api = lambda api_client=None: core  # type: ignore[attr-defined]
    client_mod.BatchV1Api = lambda api_client=None: SimpleNamespace()  # type: ignore[attr-defined]
    client_mod.CustomObjectsApi = lambda api_client=None: custom  # type: ignore[attr-defined]
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
    monkeypatch.setattr(k8s.time, "sleep", lambda _s: None)
    return core, custom


def _launcher() -> AgentSandboxLauncher:
    """A launcher pinned to in-cluster config with explicit, env-free settings."""
    return AgentSandboxLauncher(
        in_cluster=True, namespace="omnigent-sandboxes", secret_name="omnigent-creds", env=()
    )


# ── manifest conversion ────────────────────────────────


def test_sandbox_manifest_carries_the_pod_template_verbatim() -> None:
    """The Job's PodTemplateSpec becomes spec.podTemplate unchanged."""
    job = k8s.build_job_manifest(**_MANIFEST_KW)  # type: ignore[arg-type]
    sandbox = build_sandbox_manifest(job, shutdown_time="2026-09-02T12:00:00Z")

    assert sandbox["apiVersion"] == f"{API_GROUP}/{API_VERSION}"
    assert sandbox["kind"] == "Sandbox"
    assert sandbox["metadata"] == job["metadata"]
    assert sandbox["spec"]["podTemplate"] is job["spec"]["template"]  # type: ignore[index]
    # The credential + security decisions ride along rather than being restated.
    pod_spec = sandbox["spec"]["podTemplate"]["spec"]  # type: ignore[index]
    assert pod_spec["automountServiceAccountToken"] is False
    assert pod_spec["securityContext"]["runAsNonRoot"] is True
    assert pod_spec["restartPolicy"] == "OnFailure"


def test_sandbox_manifest_expiry_suspends_rather_than_destroys() -> None:
    """A pushable deadline plus Retain, so expiry is a resumable suspend."""
    job = k8s.build_job_manifest(**_MANIFEST_KW)  # type: ignore[arg-type]
    spec = build_sandbox_manifest(job, shutdown_time="2026-09-02T12:00:00Z")["spec"]

    assert spec["shutdownTime"] == "2026-09-02T12:00:00Z"  # type: ignore[index]
    # Retain keeps the object + claims when the deadline lapses; only the Pod
    # goes, so pushing shutdownTime forward can bring it back.
    assert spec["shutdownPolicy"] == "Retain"  # type: ignore[index]
    assert spec["operatingMode"] == "Running"  # type: ignore[index]
    # The Job's fixed 7-day cap is replaced by shutdownTime, not carried over.
    assert "activeDeadlineSeconds" not in spec
    assert "backoffLimit" not in spec


def test_workspace_claim_is_absent_unless_configured() -> None:
    """Default stays the Job provider's ephemeral emptyDir workspace."""
    job = k8s.build_job_manifest(**_MANIFEST_KW)  # type: ignore[arg-type]
    spec = build_sandbox_manifest(job, shutdown_time="2026-09-02T12:00:00Z")["spec"]
    assert "volumeClaimTemplates" not in spec


def test_workspace_claim_is_named_to_replace_the_home_emptydir() -> None:
    """
    The claim must be named after the HOME volume the Pod already mounts: the
    controller merges volumeClaimTemplates into the Pod's volumes BY NAME, so
    the name is what swaps the emptyDir for the claim in both containers.
    """
    job = k8s.build_job_manifest(**_MANIFEST_KW)  # type: ignore[arg-type]
    sandbox = build_sandbox_manifest(
        job, shutdown_time="2026-09-02T12:00:00Z", workspace_volume=("20Gi", "fast-ssd")
    )
    (claim,) = sandbox["spec"]["volumeClaimTemplates"]  # type: ignore[index]
    assert claim["metadata"]["name"] == WORKSPACE_VOLUME_NAME

    pod_spec = sandbox["spec"]["podTemplate"]["spec"]  # type: ignore[index]
    home = [v for v in pod_spec["volumes"] if v["name"] == WORKSPACE_VOLUME_NAME]
    assert home == [{"name": WORKSPACE_VOLUME_NAME, "emptyDir": {}}]
    mounts = pod_spec["containers"][0]["volumeMounts"]
    assert any(m["name"] == WORKSPACE_VOLUME_NAME for m in mounts)
    init_mounts = pod_spec["initContainers"][0]["volumeMounts"]
    assert any(m["name"] == WORKSPACE_VOLUME_NAME for m in init_mounts)

    assert claim["spec"]["accessModes"] == ["ReadWriteOnce"]
    assert claim["spec"]["resources"]["requests"]["storage"] == "20Gi"
    assert claim["spec"]["storageClassName"] == "fast-ssd"


def test_workspace_claim_omits_storage_class_when_unset() -> None:
    """No storageClassName means the cluster's default class, not a null."""
    job = k8s.build_job_manifest(**_MANIFEST_KW)  # type: ignore[arg-type]
    sandbox = build_sandbox_manifest(
        job, shutdown_time="2026-09-02T12:00:00Z", workspace_volume=("5Gi", None)
    )
    (claim,) = sandbox["spec"]["volumeClaimTemplates"]  # type: ignore[index]
    assert "storageClassName" not in claim["spec"]


def test_workspace_volume_reads_both_env_knobs(monkeypatch: pytest.MonkeyPatch) -> None:
    """Size enables the claim; storage class is optional and size-gated."""
    monkeypatch.delenv(WORKSPACE_SIZE_ENV_VAR, raising=False)
    monkeypatch.delenv(STORAGE_CLASS_ENV_VAR, raising=False)
    assert resolve_workspace_volume() is None

    monkeypatch.setenv(STORAGE_CLASS_ENV_VAR, "fast-ssd")
    assert resolve_workspace_volume() is None  # class alone does nothing

    monkeypatch.setenv(WORKSPACE_SIZE_ENV_VAR, "20Gi")
    assert resolve_workspace_volume() == ("20Gi", "fast-ssd")

    monkeypatch.setenv(STORAGE_CLASS_ENV_VAR, "   ")
    assert resolve_workspace_volume() == ("20Gi", None)


# ── window resolution ──────────────────────────────────


def test_window_defaults_and_reads_the_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit positive env value wins; otherwise the default applies."""
    monkeypatch.delenv(SHUTDOWN_WINDOW_ENV_VAR, raising=False)
    assert resolve_shutdown_window_s() == DEFAULT_SHUTDOWN_WINDOW_S
    monkeypatch.setenv(SHUTDOWN_WINDOW_ENV_VAR, "1800")
    assert resolve_shutdown_window_s() == 1800


@pytest.mark.parametrize("bad", ["0", "-60", "soon", ""])
def test_malformed_window_falls_back_instead_of_raising(
    bad: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bad knob must not make sandboxes unlaunchable or expire them at birth."""
    monkeypatch.setenv(SHUTDOWN_WINDOW_ENV_VAR, bad)
    assert resolve_shutdown_window_s() == DEFAULT_SHUTDOWN_WINDOW_S


def test_window_below_the_floor_is_clamped_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A window shorter than the server's refresh interval would lapse before
    anything pushed it forward, suspending every sandbox mid-run. Clamp, don't
    honour it.
    """
    monkeypatch.setenv(SHUTDOWN_WINDOW_ENV_VAR, "60")
    assert resolve_shutdown_window_s() == MIN_SHUTDOWN_WINDOW_S
    monkeypatch.setenv(SHUTDOWN_WINDOW_ENV_VAR, str(MIN_SHUTDOWN_WINDOW_S - 1))
    assert resolve_shutdown_window_s() == MIN_SHUTDOWN_WINDOW_S
    # At or above the floor is honoured verbatim.
    monkeypatch.setenv(SHUTDOWN_WINDOW_ENV_VAR, str(MIN_SHUTDOWN_WINDOW_S))
    assert resolve_shutdown_window_s() == MIN_SHUTDOWN_WINDOW_S
    monkeypatch.setenv(SHUTDOWN_WINDOW_ENV_VAR, "1800")
    assert resolve_shutdown_window_s() == 1800


def test_floor_outlives_two_server_refresh_intervals() -> None:
    """
    The floor is declared locally to keep the onboarding layer free of a server
    import; this is what stops the two constants drifting apart.
    """
    from omnigent.server.managed_host_keepalive import _MIN_INTERVAL_S

    assert MIN_SHUTDOWN_WINDOW_S >= 2 * _MIN_INTERVAL_S
    assert DEFAULT_SHUTDOWN_WINDOW_S >= MIN_SHUTDOWN_WINDOW_S


# ── keep_alive ─────────────────────────────────────────


def test_keep_alive_pushes_shutdown_time_forward(
    fake_clients: tuple[_FakeCore, _FakeCustom], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single-field merge patch moves the deadline one window out."""
    _, custom = fake_clients
    monkeypatch.setenv(SHUTDOWN_WINDOW_ENV_VAR, "1800")
    _launcher().keep_alive(_SANDBOX_ID)

    assert custom.calls == ["patch"]
    name, body = custom.patches[0]
    assert name == _SANDBOX_ID
    assert list(body) == ["spec"] and list(body["spec"]) == ["shutdownTime"]
    when = datetime.strptime(body["spec"]["shutdownTime"], "%Y-%m-%dT%H:%M:%SZ").replace(
        tzinfo=UTC
    )
    ahead = (when - datetime.now(UTC)).total_seconds()
    assert 1740 <= ahead <= 1800


def test_keep_alive_is_idempotent_across_calls(
    fake_clients: tuple[_FakeCore, _FakeCustom],
) -> None:
    """Repeating it is safe: the server calls it on every runner refresh."""
    _, custom = fake_clients
    launcher = _launcher()
    launcher.keep_alive(_SANDBOX_ID)
    launcher.keep_alive(_SANDBOX_ID)

    assert custom.calls == ["patch", "patch"]


def test_keep_alive_soft_fails_on_api_error(
    fake_clients: tuple[_FakeCore, _FakeCustom], caplog: pytest.LogCaptureFixture
) -> None:
    """A failed patch only shortens the remaining life; it must not raise."""
    _, custom = fake_clients
    custom.patch_error = _FakeApiException(status=500, reason="ServerTimeout")
    with caplog.at_level(logging.WARNING):
        _launcher().keep_alive(_SANDBOX_ID)
    assert "could not extend agent-sandbox" in caplog.text


def test_keep_alive_ignores_a_vanished_sandbox(
    fake_clients: tuple[_FakeCore, _FakeCustom], caplog: pytest.LogCaptureFixture
) -> None:
    """404 is not a problem worth warning about: there is nothing to extend."""
    _, custom = fake_clients
    custom.patch_error = _FakeApiException(status=404, reason="NotFound")
    with caplog.at_level(logging.WARNING):
        _launcher().keep_alive(_SANDBOX_ID)
    assert caplog.text == ""


# ── pod lookup / terminate ─────────────────────────────


def test_backing_pod_is_read_by_sandbox_name(
    fake_clients: tuple[_FakeCore, _FakeCustom],
) -> None:
    """The controller names the Pod after the Sandbox, so no label lookup."""
    core, _ = fake_clients
    launcher = _launcher()

    assert launcher._find_job_pod("omnigent-sandboxes", _SANDBOX_ID) == _SANDBOX_ID
    assert core.calls == ["read_pod"]

    core.read_pod_error = _FakeApiException(status=404, reason="NotFound")
    assert launcher._find_job_pod("omnigent-sandboxes", _SANDBOX_ID) is None


def test_rbac_gap_on_pod_read_surfaces_as_itself(
    fake_clients: tuple[_FakeCore, _FakeCustom],
) -> None:
    """A 403 must not masquerade as a readiness timeout."""
    import click

    core, _ = fake_clients
    core.read_pod_error = _FakeApiException(status=403, reason="Forbidden")
    with pytest.raises(click.ClickException, match="read sandbox pod"):
        _launcher()._find_job_pod("omnigent-sandboxes", _SANDBOX_ID)


def test_terminate_deletes_the_sandbox_and_its_token_secret(
    fake_clients: tuple[_FakeCore, _FakeCustom],
) -> None:
    """Both go, so a leaked Secret cannot keep a valid launch token alive."""
    core, custom = fake_clients
    _launcher().terminate(_SANDBOX_ID)

    assert custom.deleted == [_SANDBOX_ID]
    assert core.deleted_secrets == [f"{_SANDBOX_ID}-token"]


def test_terminate_still_deletes_the_secret_when_the_sandbox_delete_fails(
    fake_clients: tuple[_FakeCore, _FakeCustom],
) -> None:
    """A failure on one delete must not skip the other."""
    import click

    core, custom = fake_clients
    custom.delete_error = _FakeApiException(status=403, reason="Forbidden")
    with pytest.raises(click.ClickException):
        _launcher().terminate(_SANDBOX_ID)

    assert core.deleted_secrets == [f"{_SANDBOX_ID}-token"]


def test_terminate_treats_a_missing_sandbox_as_success(
    fake_clients: tuple[_FakeCore, _FakeCustom],
) -> None:
    """Idempotent: an already-expired (self-deleted) Sandbox is not an error."""
    core, custom = fake_clients
    custom.delete_error = _FakeApiException(status=404, reason="NotFound")
    _launcher().terminate(_SANDBOX_ID)

    assert core.deleted_secrets == [f"{_SANDBOX_ID}-token"]


# ── suspend / wake in place ─────────────────────────────


def test_create_workload_creates_a_fresh_sandbox(
    fake_clients: tuple[_FakeCore, _FakeCustom], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The normal launch path creates, and carries the workspace claim."""
    _, custom = fake_clients
    monkeypatch.setenv(WORKSPACE_SIZE_ENV_VAR, "20Gi")
    job = k8s.build_job_manifest(**_MANIFEST_KW)  # type: ignore[arg-type]
    _launcher()._create_workload("omnigent-sandboxes", job)

    assert custom.calls == ["create"]
    assert "volumeClaimTemplates" in custom.created[0]["spec"]


def test_create_workload_wakes_an_existing_sandbox_instead_of_failing(
    fake_clients: tuple[_FakeCore, _FakeCustom], monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    On a wake, start_host runs again under the same id. A 409 must become a
    patch back to Running, not an error, and must NOT resend the immutable
    volumeClaimTemplates.
    """
    _, custom = fake_clients
    custom.create_error = _FakeApiException(status=409, reason="AlreadyExists")
    monkeypatch.setenv(WORKSPACE_SIZE_ENV_VAR, "20Gi")
    job = k8s.build_job_manifest(**_MANIFEST_KW)  # type: ignore[arg-type]
    _launcher()._create_workload("omnigent-sandboxes", job)

    assert custom.calls == ["create", "patch"]
    name, body = custom.patches[0]
    assert name == _SANDBOX_ID
    spec = body["spec"]
    assert spec["operatingMode"] == "Running"
    assert "shutdownTime" in spec
    # Immutable after creation: resending it is rejected by the CRD's own rule.
    assert "volumeClaimTemplates" not in spec
    # The re-rendered Pod template rides along so a changed token Secret or
    # host_config is picked up when the controller rebuilds the Pod.
    assert spec["podTemplate"] is job["spec"]["template"]


def test_create_workload_still_raises_on_a_real_error(
    fake_clients: tuple[_FakeCore, _FakeCustom],
) -> None:
    """Only 409 means "already there"; anything else is a launch failure."""
    _, custom = fake_clients
    custom.create_error = _FakeApiException(status=403, reason="Forbidden")
    job = k8s.build_job_manifest(**_MANIFEST_KW)  # type: ignore[arg-type]
    with pytest.raises(_FakeApiException):
        _launcher()._create_workload("omnigent-sandboxes", job)


def test_resume_keeps_the_sandbox_and_clears_only_what_is_re_minted(
    fake_clients: tuple[_FakeCore, _FakeCustom],
) -> None:
    """
    The whole point of resume here: the Sandbox object and its workspace claim
    survive. Only the stale token Secret and the previous Pod go.
    """
    core, custom = fake_clients
    _launcher().resume(_SANDBOX_ID)

    assert custom.deleted == []  # the Sandbox (and its PVC) must survive
    assert core.deleted_secrets == [f"{_SANDBOX_ID}-token"]
    assert core.deleted_pods == [_SANDBOX_ID]


def test_resume_tolerates_a_sandbox_that_never_had_a_pod(
    fake_clients: tuple[_FakeCore, _FakeCustom], capsys: pytest.CaptureFixture[str]
) -> None:
    """A sandbox that expired before starting has neither; that is a normal wake."""
    core, _ = fake_clients
    missing = _FakeApiException(status=404, reason="NotFound")

    def _gone(name, namespace, _request_timeout=None):
        raise missing

    core.delete_namespaced_secret = _gone  # type: ignore[method-assign]
    core.delete_namespaced_pod = _gone  # type: ignore[method-assign]
    _launcher().resume(_SANDBOX_ID)  # must not raise
    assert "warning" not in capsys.readouterr().err


def test_terminating_pod_counts_as_absent(
    fake_clients: tuple[_FakeCore, _FakeCustom],
) -> None:
    """
    On a wake the old Pod is torn down under the same name. Reporting it ready
    would hand the caller a Pod about to vanish, so the poll must keep waiting.
    """
    core, _ = fake_clients
    launcher = _launcher()
    assert launcher._find_job_pod("omnigent-sandboxes", _SANDBOX_ID) == _SANDBOX_ID

    core.pod_deletion_timestamp = "2026-09-02T12:00:00Z"
    assert launcher._find_job_pod("omnigent-sandboxes", _SANDBOX_ID) is None


def test_terminate_still_hard_deletes(
    fake_clients: tuple[_FakeCore, _FakeCustom],
) -> None:
    """resume preserves; terminate must still destroy (and cascade the PVC)."""
    core, custom = fake_clients
    _launcher().terminate(_SANDBOX_ID)

    assert custom.deleted == [_SANDBOX_ID]
    assert core.deleted_secrets == [f"{_SANDBOX_ID}-token"]
