"""
Tests for the Kubernetes launcher's per-user namespace strategy (dual-layer data
plane): the owner→namespace mapping, the tenant manifests, and both provisioning
modes, driven against the same in-memory ``kubernetes`` package fake the base
launcher tests use (no cluster, no real client).
"""

from __future__ import annotations

import hashlib
import sys
import types
from types import SimpleNamespace

import click
import pytest

import omnigent.onboarding.sandboxes.kubernetes as k8s
from omnigent.onboarding.sandboxes.kubernetes import (
    DEFAULT_NAMESPACE_TEMPLATE,
    KubernetesSandboxLauncher,
    build_default_deny_ingress_policy_manifest,
    build_sandbox_manager_rolebinding_manifest,
    build_tenant_namespace_manifest,
    build_tenant_resource_quota_manifest,
    tenant_namespace,
)

_OWNER = "alice@example.com"
_OWNER_HASH = hashlib.sha256(_OWNER.encode()).hexdigest()[:8]
_ALICE_NS = f"omnigent-u-alice-example-com-{_OWNER_HASH}"


# ── owner → namespace mapping ───────────────────────────────


def test_tenant_namespace_keeps_a_label_safe_owner_verbatim() -> None:
    assert tenant_namespace("alice") == "omnigent-u-alice"


def test_tenant_namespace_normalizes_and_hash_suffixes_a_lossy_owner() -> None:
    # '@' and '.' are not label characters; the suffix keeps "a.b" and "a-b" apart.
    assert tenant_namespace(_OWNER) == _ALICE_NS
    assert tenant_namespace("alice.example-com") != tenant_namespace("alice-example.com")


def test_tenant_namespace_is_case_insensitive_but_collision_safe() -> None:
    # Kubernetes names are lowercase; distinct mixed-case owners must not merge.
    assert tenant_namespace("Alice") != tenant_namespace("alice")
    assert tenant_namespace("Alice").startswith("omnigent-u-alice-")


def test_tenant_namespace_truncates_long_owners_within_63_chars() -> None:
    name = tenant_namespace("a" * 200)
    assert len(name) == 63
    assert k8s._DNS1123_LABEL_RE.fullmatch(name)
    assert name != tenant_namespace("a" * 201)


def test_tenant_namespace_honours_a_custom_template_with_fixed_parts() -> None:
    assert tenant_namespace("bob", "team-{owner}-runners") == "team-bob-runners"


@pytest.mark.parametrize(
    "template",
    ["omnigent-sandboxes", "{owner}-{owner}", "Omnigent-{owner}", "ns_{owner}"],
)
def test_tenant_namespace_rejects_bad_templates(template: str) -> None:
    with pytest.raises(ValueError):
        tenant_namespace("alice", template)


def test_tenant_namespace_rejects_an_empty_owner() -> None:
    with pytest.raises(ValueError, match="empty"):
        tenant_namespace("   ")


# ── manifests ───────────────────────────────────────────────


def test_namespace_manifest_stamps_tenant_pod_security_and_extra_labels() -> None:
    manifest = build_tenant_namespace_manifest(
        name=_ALICE_NS,
        owner=_OWNER,
        labels={"team": "{owner}", "cost-centre": "cc-42"},
        pod_security="restricted",
    )
    labels = manifest["metadata"]["labels"]  # type: ignore[index]
    tenant = f"alice-example-com-{_OWNER_HASH}"
    assert manifest["kind"] == "Namespace"
    assert labels["omnigent.ai/tenant"] == tenant
    assert labels["team"] == tenant  # {owner} substituted
    assert labels["cost-centre"] == "cc-42"
    assert labels["app.kubernetes.io/managed-by"] == "omnigent"
    assert labels["pod-security.kubernetes.io/enforce"] == "restricted"
    assert labels["pod-security.kubernetes.io/warn"] == "restricted"


def test_namespace_manifest_without_pod_security_leaves_admission_alone() -> None:
    labels = build_tenant_namespace_manifest(name="ns", owner="bob")["metadata"]["labels"]  # type: ignore[index]
    assert not any(key.startswith("pod-security.kubernetes.io/") for key in labels)


def test_rolebinding_binds_the_cluster_role_to_the_server_sa_in_one_namespace() -> None:
    manifest = build_sandbox_manager_rolebinding_manifest(
        namespace=_ALICE_NS, owner=_OWNER, server_service_account="omnigent/omnigent-server"
    )
    assert manifest["kind"] == "RoleBinding"
    assert manifest["metadata"]["namespace"] == _ALICE_NS  # type: ignore[index]
    assert manifest["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "ClusterRole",
        "name": "omnigent-sandbox-manager",
    }
    assert manifest["subjects"] == [
        {"kind": "ServiceAccount", "name": "omnigent-server", "namespace": "omnigent"}
    ]


def test_quota_and_network_policy_manifests() -> None:
    quota = build_tenant_resource_quota_manifest(
        namespace=_ALICE_NS, owner=_OWNER, hard={"pods": "10", "requests.cpu": "8"}
    )
    assert quota["spec"] == {"hard": {"pods": "10", "requests.cpu": "8"}}
    policy = build_default_deny_ingress_policy_manifest(namespace=_ALICE_NS, owner=_OWNER)
    assert policy["spec"] == {"podSelector": {}, "policyTypes": ["Ingress"]}
    assert policy["metadata"]["namespace"] == _ALICE_NS  # type: ignore[index]


# ── launcher behaviour against a fake cluster ───────────────


class _ApiException(Exception):
    def __init__(self, *, status: int | None = None, reason: str = "") -> None:
        super().__init__(reason or str(status))
        self.status = status
        self.reason = reason
        self.body = ""


def _pod(phase: str = "Running") -> SimpleNamespace:
    return SimpleNamespace(
        metadata=SimpleNamespace(name="child"),
        status=SimpleNamespace(
            phase=phase, init_container_statuses=None, container_statuses=None, conditions=None
        ),
    )


class _Core:
    """Recording ``CoreV1Api`` fake with namespace/SA/quota support."""

    def __init__(self) -> None:
        self.namespaces: dict[str, dict[str, str]] = {}  # name -> labels
        self.created: list[tuple[str, str, object]] = []  # (kind, namespace, body)
        self.read_namespace_error: Exception | None = None
        self.create_namespace_error: Exception | None = None
        self.calls: list[str] = []

    def read_namespace(self, name, _request_timeout=None):
        self.calls.append("read_namespace")
        if self.read_namespace_error is not None:
            raise self.read_namespace_error
        if name not in self.namespaces:
            raise _ApiException(status=404, reason="Not Found")
        return SimpleNamespace(metadata=SimpleNamespace(name=name, labels=self.namespaces[name]))

    def create_namespace(self, body, _request_timeout=None):
        self.calls.append("create_namespace")
        if self.create_namespace_error is not None:
            raise self.create_namespace_error
        self.namespaces[body["metadata"]["name"]] = body["metadata"]["labels"]
        self.created.append(("namespace", body["metadata"]["name"], body))

    def create_namespaced_service_account(self, namespace, body, _request_timeout=None):
        self.created.append(("serviceaccount", namespace, body))

    def create_namespaced_resource_quota(self, namespace, body, _request_timeout=None):
        self.created.append(("resourcequota", namespace, body))

    def create_namespaced_secret(self, namespace, body, _request_timeout=None):
        self.created.append(("secret", namespace, body))

    def list_namespaced_pod(self, namespace, label_selector=None, _request_timeout=None):
        return SimpleNamespace(items=[_pod()])

    def read_namespaced_pod(self, name, namespace, _request_timeout=None):
        return _pod()

    def delete_namespaced_secret(self, name, namespace, _request_timeout=None):
        self.created.append(("delete_secret", namespace, name))

    def delete_namespaced_pod(self, name, namespace, _request_timeout=None):
        raise _ApiException(status=404)

    def list_namespaced_event(self, namespace, field_selector=None, _request_timeout=None):
        return SimpleNamespace(items=[])

    def read_namespaced_pod_log(self, *args, **kwargs):
        return ""


class _Batch:
    def __init__(self) -> None:
        self.created: list[tuple[str, object]] = []
        self.deleted: list[tuple[str, str]] = []

    def create_namespaced_job(self, namespace, body, _request_timeout=None):
        self.created.append((namespace, body))

    def delete_namespaced_job(self, name, namespace, body=None, _request_timeout=None):
        self.deleted.append((namespace, name))


class _Rbac:
    def __init__(self) -> None:
        self.created: list[tuple[str, object]] = []
        self.error: Exception | None = None

    def create_namespaced_role_binding(self, namespace, body, _request_timeout=None):
        if self.error is not None:
            raise self.error
        self.created.append((namespace, body))


class _Networking:
    def __init__(self) -> None:
        self.created: list[tuple[str, object]] = []

    def create_namespaced_network_policy(self, namespace, body, _request_timeout=None):
        self.created.append((namespace, body))


@pytest.fixture
def cluster(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Install a fake ``kubernetes`` package exposing core/batch/rbac/networking."""
    fake = SimpleNamespace(core=_Core(), batch=_Batch(), rbac=_Rbac(), networking=_Networking())
    client_mod = types.ModuleType("kubernetes.client")
    client_mod.ApiException = _ApiException  # type: ignore[attr-defined]
    client_mod.Configuration = lambda: SimpleNamespace()  # type: ignore[attr-defined]
    client_mod.ApiClient = lambda cfg=None: SimpleNamespace(close=lambda: None)  # type: ignore[attr-defined]
    client_mod.CoreV1Api = lambda api_client=None: fake.core  # type: ignore[attr-defined]
    client_mod.BatchV1Api = lambda api_client=None: fake.batch  # type: ignore[attr-defined]
    client_mod.RbacAuthorizationV1Api = lambda api_client=None: fake.rbac  # type: ignore[attr-defined]
    client_mod.NetworkingV1Api = lambda api_client=None: fake.networking  # type: ignore[attr-defined]
    client_mod.V1DeleteOptions = lambda propagation_policy=None: None  # type: ignore[attr-defined]
    rest_mod = types.ModuleType("kubernetes.client.rest")
    rest_mod.ApiException = _ApiException  # type: ignore[attr-defined]
    config_mod = types.ModuleType("kubernetes.config")
    config_mod.load_incluster_config = lambda client_configuration=None: None  # type: ignore[attr-defined]
    config_mod.load_kube_config = lambda config_file=None, client_configuration=None: None  # type: ignore[attr-defined]
    config_mod.ConfigException = type("ConfigException", (Exception,), {})  # type: ignore[attr-defined]
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
    return fake


def _launcher(**overrides: object) -> KubernetesSandboxLauncher:
    kwargs: dict[str, object] = {
        "in_cluster": True,
        "namespace": "omnigent-sandboxes",
        "env": (),
        "namespace_strategy": "per-user",
    }
    kwargs.update(overrides)
    return KubernetesSandboxLauncher(**kwargs)  # type: ignore[arg-type]


def _start(launcher: KubernetesSandboxLauncher) -> str:
    return launcher.start_host(
        "omnigent-managed-abc-1a2b3c",
        token="tok",
        host_id="host_1",
        host_name="managed-1",
        server_url="http://srv",
    )


def test_shared_strategy_is_unchanged_and_needs_no_owner(cluster: SimpleNamespace) -> None:
    launcher = _launcher(namespace_strategy="shared")
    _start(launcher)
    assert cluster.core.calls == []  # never touched the Namespace API
    assert cluster.batch.created[0][0] == "omnigent-sandboxes"


def test_per_user_without_a_bound_owner_fails_loud_instead_of_defaulting(
    cluster: SimpleNamespace,
) -> None:
    launcher = _launcher()
    with pytest.raises(click.ClickException, match="no session owner"):
        _start(launcher)
    assert cluster.batch.created == []


def test_external_mode_selects_the_pre_created_tenant_namespace(cluster: SimpleNamespace) -> None:
    cluster.core.namespaces[_ALICE_NS] = {"omnigent.ai/tenant": f"alice-example-com-{_OWNER_HASH}"}
    launcher = _launcher()
    launcher.bind_owner(_OWNER)
    _start(launcher)
    assert cluster.core.calls == ["read_namespace"]
    # Secret and Job both land in Alice's namespace, never the shared default.
    assert [c[:2] for c in cluster.core.created if c[0] == "secret"] == [("secret", _ALICE_NS)]
    assert cluster.batch.created[0][0] == _ALICE_NS
    assert cluster.batch.created[0][1]["metadata"]["namespace"] == _ALICE_NS


def test_external_mode_fails_fast_when_the_tenant_namespace_is_missing(
    cluster: SimpleNamespace,
) -> None:
    launcher = _launcher()
    launcher.bind_owner(_OWNER)
    with pytest.raises(click.ClickException) as exc_info:
        _start(launcher)
    assert _ALICE_NS in exc_info.value.message
    assert "namespace_provisioning: external" in exc_info.value.message
    assert "create_namespace" not in cluster.core.calls
    assert cluster.batch.created == []


def test_a_namespace_labelled_for_another_tenant_is_refused(cluster: SimpleNamespace) -> None:
    cluster.core.namespaces[_ALICE_NS] = {"omnigent.ai/tenant": "mallory"}
    launcher = _launcher(namespace_provisioning="server")
    launcher.bind_owner(_OWNER)
    with pytest.raises(click.ClickException, match="refusing to share"):
        _start(launcher)
    assert cluster.batch.created == []


def test_two_owners_land_in_two_namespaces(cluster: SimpleNamespace) -> None:
    for owner in ("alice", "bob"):
        launcher = _launcher(namespace_provisioning="server")
        launcher.bind_owner(owner)
        _start(launcher)
    assert [ns for ns, _ in cluster.batch.created] == ["omnigent-u-alice", "omnigent-u-bob"]
    assert set(cluster.core.namespaces) == {"omnigent-u-alice", "omnigent-u-bob"}


def test_server_mode_provisions_namespace_sa_binding_quota_and_policy_once(
    cluster: SimpleNamespace,
) -> None:
    launcher = _launcher(
        namespace_provisioning="server",
        namespace_labels={"omnigent.ai/team": "{owner}"},
        namespace_defaults={
            "resource_quota": {"pods": "10", "requests.cpu": "8"},
            "network_policy": "default-deny-ingress",
            "pod_security": "restricted",
        },
        server_service_account="control/omnigent-server",
    )
    launcher.bind_owner(_OWNER)
    _start(launcher)

    kinds = [c[0] for c in cluster.core.created]
    assert kinds[:3] == ["namespace", "serviceaccount", "resourcequota"]
    ns_labels = cluster.core.namespaces[_ALICE_NS]
    assert ns_labels["omnigent.ai/tenant"] == f"alice-example-com-{_OWNER_HASH}"
    assert ns_labels["omnigent.ai/team"] == f"alice-example-com-{_OWNER_HASH}"
    assert ns_labels["pod-security.kubernetes.io/enforce"] == "restricted"
    sa = next(c for c in cluster.core.created if c[0] == "serviceaccount")
    assert sa[1] == _ALICE_NS
    assert sa[2]["metadata"]["name"] == "omnigent-runner"
    assert sa[2]["automountServiceAccountToken"] is False
    [(rb_ns, rb)] = cluster.rbac.created
    assert rb_ns == _ALICE_NS
    assert rb["subjects"] == [
        {"kind": "ServiceAccount", "name": "omnigent-server", "namespace": "control"}
    ]
    quota = next(c for c in cluster.core.created if c[0] == "resourcequota")
    assert quota[2]["spec"]["hard"] == {"pods": "10", "requests.cpu": "8"}
    [(np_ns, np)] = cluster.networking.created
    assert np_ns == _ALICE_NS and np["metadata"]["name"] == "default-deny-ingress"
    # The runner Job itself went into the freshly provisioned namespace.
    assert cluster.batch.created[0][0] == _ALICE_NS

    # Second launch for the same owner: the namespace exists, so defaults are
    # NOT re-applied (an operator's later edits must survive).
    again = _launcher(
        namespace_provisioning="server", namespace_defaults={"pod_security": "baseline"}
    )
    again.bind_owner(_OWNER)
    _start(again)
    assert cluster.core.calls.count("create_namespace") == 1
    assert len(cluster.rbac.created) == 1


def test_server_mode_treats_already_exists_races_as_success(cluster: SimpleNamespace) -> None:
    # A concurrent launch for the same owner created the binding first.
    cluster.rbac.error = _ApiException(status=409, reason="AlreadyExists")
    launcher = _launcher(namespace_provisioning="server")
    launcher.bind_owner(_OWNER)
    _start(launcher)
    assert cluster.batch.created[0][0] == _ALICE_NS


def test_server_mode_surfaces_a_forbidden_namespace_create(cluster: SimpleNamespace) -> None:
    cluster.core.create_namespace_error = _ApiException(status=403, reason="Forbidden")
    launcher = _launcher(namespace_provisioning="server")
    launcher.bind_owner(_OWNER)
    with pytest.raises(click.ClickException, match="provision tenant namespace"):
        _start(launcher)
    assert cluster.batch.created == []


def test_terminate_and_resume_delete_in_the_owners_namespace(cluster: SimpleNamespace) -> None:
    launcher = _launcher()
    launcher.bind_owner(_OWNER)
    launcher.terminate("omnigent-managed-abc-1a2b3c")
    assert cluster.batch.deleted == [(_ALICE_NS, "omnigent-managed-abc-1a2b3c")]
    assert (
        "delete_secret",
        _ALICE_NS,
        "omnigent-managed-abc-1a2b3c-token",
    ) in cluster.core.created


def test_default_template_constant_matches_the_documented_shape() -> None:
    assert DEFAULT_NAMESPACE_TEMPLATE == "omnigent-u-{owner}"
