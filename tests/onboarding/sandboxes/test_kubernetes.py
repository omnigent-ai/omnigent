"""Tests for :mod:`omnigent.onboarding.sandboxes.kubernetes`."""

from __future__ import annotations

import sys
import types
from collections.abc import Callable
from dataclasses import dataclass, field

import click
import pytest

from omnigent.onboarding.sandboxes import available_providers
from omnigent.onboarding.sandboxes.base import DEFAULT_HOST_IMAGE
from omnigent.onboarding.sandboxes.kubernetes import (
    HOST_IMAGE_ENV_VAR,
    NAMESPACE_ENV_VAR,
    SANDBOX_ENV_PASSTHROUGH_ENV_VAR,
    SANDBOX_SECRET_ENV_VAR,
    SERVICE_ACCOUNT_ENV_VAR,
    KubernetesSandboxLauncher,
    _new_pod_name,
    _parse_exec_status,
    _redact_command,
    build_pod_manifest,
)

# ── PURE: build_pod_manifest ────────────────────────────────


def _manifest(
    *,
    pod_name: str = "omnigent-host-abc123",
    namespace: str = "omnigent",
    image: str = "ghcr.io/omnigent-ai/omnigent-host:latest",
    service_account: str = "omnigent-runner",
    harness_secret: str | None = None,
    env_literals: dict[str, str] | None = None,
    node_selector: dict[str, str] | None = None,
) -> dict[str, object]:
    """
    Build a manifest with sensible defaults, overridable per test.

    Mirrors ``build_pod_manifest``'s keyword signature so each override
    stays type-checked.

    :returns: The Pod manifest dict.
    """
    return build_pod_manifest(
        pod_name=pod_name,
        namespace=namespace,
        image=image,
        service_account=service_account,
        harness_secret=harness_secret,
        env_literals=env_literals or {},
        node_selector=node_selector,
    )


def _spec(manifest: dict[str, object]) -> dict[str, object]:
    """Return the manifest's ``spec`` block (typed for the asserts)."""
    spec = manifest["spec"]
    assert isinstance(spec, dict)
    return spec


def _container(manifest: dict[str, object]) -> dict[str, object]:
    """Return the manifest's sole container block."""
    containers = _spec(manifest)["containers"]
    assert isinstance(containers, list)
    container = containers[0]
    assert isinstance(container, dict)
    return container


def _node_selector(manifest: dict[str, object]) -> dict[str, object]:
    """Return the manifest's node selector mapping (typed for asserts)."""
    selector = _spec(manifest)["nodeSelector"]
    assert isinstance(selector, dict)
    return selector


def test_manifest_never_restarts_and_disables_token_automount() -> None:
    """
    A crashed host must not restart with a stale token (the managed
    machinery relaunches), and the sandbox must never carry the server
    SA token (codex M4) — a compromised agent could otherwise wield its
    pods/exec rights.
    """
    spec = _spec(_manifest())
    assert spec["restartPolicy"] == "Never"
    assert spec["automountServiceAccountToken"] is False


def test_manifest_sets_service_account() -> None:
    """The Pod runs as the resolved sandbox ServiceAccount."""
    spec = _spec(_manifest(service_account="custom-runner"))
    assert spec["serviceAccountName"] == "custom-runner"


def test_manifest_node_selector_pins_amd64_and_merges_operator_labels() -> None:
    """
    The host image is amd64-only, so arch is always pinned; operator
    node selector labels merge on top (a mixed-arch homelab needs both).
    """
    spec = _spec(_manifest(node_selector={"disktype": "ssd", "pool": "agents"}))
    assert spec["nodeSelector"] == {
        "kubernetes.io/arch": "amd64",
        "disktype": "ssd",
        "pool": "agents",
    }


def test_manifest_node_selector_arch_invariant_cannot_be_overridden() -> None:
    """
    ``kubernetes.io/arch: amd64`` is ALWAYS enforced and cannot be dropped
    by an operator ``kubernetes.io/arch`` key (the host image is amd64-only,
    so an override would only schedule a Pod that segfaults at exec). The
    operator's other labels still merge.

    Mutation guard: with the original ``{arch, **operator}`` merge order an
    operator arm64 key would win and this assertion fails.
    """
    # No arch override: the default is present.
    assert _node_selector(_manifest())["kubernetes.io/arch"] == "amd64"
    # An operator arch override is IGNORED — amd64 wins — and the operator's
    # other labels survive.
    override = _manifest(node_selector={"kubernetes.io/arch": "arm64", "disktype": "ssd"})
    selector = _node_selector(override)
    assert selector["kubernetes.io/arch"] == "amd64"
    assert selector["disktype"] == "ssd"


def test_manifest_pod_security_context_runs_as_uid_gid_1000() -> None:
    """
    Least privilege: non-root uid/gid 1000 with fsGroup 1000 (so the
    HOME emptyDir is group-writable) and OnRootMismatch (skip a costly
    recursive chown when ownership already matches).
    """
    sec = _spec(_manifest())["securityContext"]
    assert sec == {
        "runAsUser": 1000,
        "runAsGroup": 1000,
        "fsGroup": 1000,
        "fsGroupChangePolicy": "OnRootMismatch",
    }


def test_manifest_writable_home_volume_mount_env_and_workingdir() -> None:
    """
    The image WORKDIR /root is unwritable to uid 1000, so the Pod must
    provide a writable HOME: an emptyDir at /home/omnigent, mounted into
    the container, exported as $HOME, and set as workingDir (codex M2 —
    _start_host_in_sandbox does `mkdir -p $HOME/workspace`).
    """
    manifest = _manifest()
    spec = _spec(manifest)
    container = _container(manifest)
    assert spec["volumes"] == [{"name": "home", "emptyDir": {}}]
    assert container["volumeMounts"] == [{"name": "home", "mountPath": "/home/omnigent"}]
    assert container["workingDir"] == "/home/omnigent"
    env = container["env"]
    assert isinstance(env, list)
    assert {"name": "HOME", "value": "/home/omnigent"} in env


def test_manifest_marks_is_sandbox() -> None:
    """IS_SANDBOX=1 lets in-sandbox code detect the managed sandbox."""
    env = _container(_manifest())["env"]
    assert isinstance(env, list)
    assert {"name": "IS_SANDBOX", "value": "1"} in env


def test_manifest_includes_env_literals() -> None:
    """
    Resolved server-env passthrough lands as literal container env
    entries (in addition to HOME / IS_SANDBOX).
    """
    env = _container(_manifest(env_literals={"OMNIGENT_GATEWAY_URL": "https://gw"}))["env"]
    assert isinstance(env, list)
    assert {"name": "OMNIGENT_GATEWAY_URL", "value": "https://gw"} in env


def test_manifest_envfrom_secret_ref_when_secret_set() -> None:
    """
    A configured Secret is projected via envFrom secretRef — this is how
    harness LLM credentials reach the Pod without living in the Pod spec.
    """
    container = _container(_manifest(harness_secret="omnigent-creds"))
    assert container["envFrom"] == [{"secretRef": {"name": "omnigent-creds"}}]


def test_manifest_no_envfrom_key_when_secret_absent() -> None:
    """
    No configured Secret → the envFrom key is omitted entirely (an empty
    list would be harmless but the absent key is cleaner).
    """
    assert "envFrom" not in _container(_manifest(harness_secret=None))


def test_manifest_resources_from_sizing_constants() -> None:
    """Requests/limits come from the module sizing constants."""
    resources = _container(_manifest())["resources"]
    assert resources == {
        "requests": {"cpu": "500m", "memory": "1Gi"},
        "limits": {"cpu": "2", "memory": "4Gi"},
    }


def test_manifest_container_disallows_privilege_escalation_but_keeps_rw_root() -> None:
    """
    The container blocks privilege escalation but must NOT set
    readOnlyRootFilesystem — the host writes /tmp and ~/.omnigent.
    """
    sec = _container(_manifest())["securityContext"]
    assert sec == {"allowPrivilegeEscalation": False}
    assert isinstance(sec, dict)
    assert "readOnlyRootFilesystem" not in sec


def test_manifest_command_is_pid1_reaper_supervising_sleep_infinity() -> None:
    """
    PID 1 must reap orphaned runner procs the host re-parents to it, so
    the command is a python reaper that supervises `sleep infinity`
    (codex M3) — a bare `sleep infinity` would leak zombies.
    """
    command = _container(_manifest())["command"]
    assert isinstance(command, list)
    assert command[:2] == ["bash", "-lc"]
    reaper = command[2]
    assert "exec python3 -c " in reaper
    assert "sleep" in reaper and "infinity" in reaper
    # The reaper must actually reap (os.wait loop) and forward signals,
    # not just spawn-and-block.
    assert "os.wait" in reaper
    assert "SIGTERM" in reaper


def test_manifest_metadata_labels_and_namespace() -> None:
    """Pod metadata carries the managed-by / role labels and namespace."""
    manifest = _manifest(pod_name="omnigent-x-1", namespace="agents")
    metadata = manifest["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["name"] == "omnigent-x-1"
    assert metadata["namespace"] == "agents"
    assert metadata["labels"] == {
        "app.kubernetes.io/managed-by": "omnigent",
        "omnigent.ai/role": "sandbox-host",
    }


# ── PURE: _new_pod_name ─────────────────────────────────────


def test_new_pod_name_is_dns_label_safe() -> None:
    """
    Pod names must be DNS labels: lowercased, illegal chars collapsed to
    '-', and within the 63-char limit.
    """
    name = _new_pod_name("Managed_Session #42!!")
    assert name.startswith("omnigent-managed-session-42-")
    assert name == name.lower()
    assert len(name) <= 63
    # Only [a-z0-9-] survive.
    assert all(ch.isalnum() or ch == "-" for ch in name)


def test_new_pod_name_truncates_long_labels() -> None:
    """A very long label is truncated so the full name stays ≤ 63 chars."""
    name = _new_pod_name("x" * 200)
    assert len(name) <= 63


def test_new_pod_name_empty_label_falls_back_to_host() -> None:
    """A label with no usable characters falls back to 'host'."""
    name = _new_pod_name("!!!")
    assert name.startswith("omnigent-host-")


def test_new_pod_name_unique_suffix() -> None:
    """Two calls for the same label differ (the random suffix prevents
    collisions across relaunches)."""
    assert _new_pod_name("managed-a") != _new_pod_name("managed-a")


# ── PURE: _parse_exec_status ────────────────────────────────


def test_parse_exec_status_success_is_zero() -> None:
    """A Success status frame means exit 0."""
    frame = '{"metadata":{},"status":"Success"}'
    assert _parse_exec_status([frame], "pod-1") == 0


def test_parse_exec_status_reads_exit_code_cause() -> None:
    """
    A non-zero exit carries the code in a details.causes ExitCode entry —
    WSClient.returncode is unreliable, so this frame is the truth
    (codex M1).
    """
    frame = (
        '{"metadata":{},"status":"Failure",'
        '"reason":"NonZeroExitCode",'
        '"details":{"causes":[{"reason":"ExitCode","message":"7"}]}}'
    )
    assert _parse_exec_status([frame], "pod-1") == 7


def test_parse_exec_status_joins_split_frames() -> None:
    """The status frame can arrive in chunks; they must be joined first."""
    chunks = ['{"metadata":{},"status":', '"Success"}']
    assert _parse_exec_status(chunks, "pod-1") == 0


def test_parse_exec_status_raises_when_no_frame() -> None:
    """No status frame is a transport fault — must raise, not pass as 0."""
    with pytest.raises(RuntimeError, match="no status frame"):
        _parse_exec_status([], "pod-1")


def test_parse_exec_status_raises_when_no_exit_code() -> None:
    """
    A failure frame without an ExitCode cause carries no usable code —
    raise rather than invent a status.
    """
    frame = '{"metadata":{},"status":"Failure","reason":"InternalError"}'
    with pytest.raises(RuntimeError, match="no exit code"):
        _parse_exec_status([frame], "pod-1")


# ── Fake Kubernetes client ──────────────────────────────────
#
# The kubernetes client is an optional dependency the base test env does
# not install, and real cluster objects only exist server-side anyway —
# so these are hand-rolled stub classes (never MagicMock: the launcher's
# attribute access must hit explicitly defined recorders, not silently
# succeed). The fake package + submodules are injected via sys.modules so
# the launcher's function-local `from kubernetes ... import` resolves to
# them.


class _FakeApiException(Exception):
    """Stands in for ``kubernetes.client.rest.ApiException``."""

    def __init__(
        self, *, status: int = 500, reason: str = "error", body: str | None = None
    ) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.body = body


class _FakeConfigException(Exception):
    """Stands in for ``kubernetes.config.config_exception.ConfigException``."""


class _FakeWebSocketException(Exception):
    """
    Stands in for ``websocket.WebSocketException``.

    ``websocket-client`` is only a TRANSITIVE dependency of the optional
    ``kubernetes`` package, so it is ABSENT in CI jobs that run without the
    kubernetes extra. The launcher's ``run()`` does
    ``from websocket import WebSocketException`` (to guard the post-loop
    socket flush), so the fake SDK injection must provide a fake
    ``websocket`` module too — otherwise that import fails with
    ``ModuleNotFoundError`` in CI even though every other k8s symbol is
    faked.
    """


# ── status object stand-ins (mirror V1Pod's attribute shape) ──


@dataclass
class _Waiting:
    """Stands in for ``V1ContainerStateWaiting``."""

    reason: str | None = None
    message: str | None = None


@dataclass
class _Terminated:
    """Stands in for ``V1ContainerStateTerminated``."""

    exit_code: int | None = None
    reason: str | None = None


@dataclass
class _ContainerState:
    """Stands in for ``V1ContainerState`` (``waiting`` / ``terminated``)."""

    waiting: _Waiting | None = None
    terminated: _Terminated | None = None


@dataclass
class _ContainerStatus:
    """
    Stands in for ``V1ContainerStatus``.

    ``name`` defaults to ``"host"`` (the launcher's ``_CONTAINER_NAME``)
    so the common "host container ready" case is the default; sidecar
    statuses pass an explicit name.
    """

    ready: bool = False
    state: _ContainerState = field(default_factory=_ContainerState)
    name: str = "host"


@dataclass
class _Condition:
    """Stands in for ``V1PodCondition``."""

    type: str
    status: str
    reason: str | None = None
    message: str | None = None


@dataclass
class _PodStatus:
    """Stands in for ``V1PodStatus``."""

    phase: str | None = None
    container_statuses: list[_ContainerStatus] | None = None
    conditions: list[_Condition] | None = None


@dataclass
class _Pod:
    """Stands in for a ``V1Pod`` (only `status` is read)."""

    status: _PodStatus


def _ready_pod(_name: str) -> _Pod:
    """A Pod that is Running with a ready container (the happy path)."""
    return _Pod(
        status=_PodStatus(
            phase="Running",
            container_statuses=[_ContainerStatus(ready=True)],
        )
    )


@dataclass
class _Event:
    """Stands in for ``CoreV1Event``."""

    reason: str
    message: str


@dataclass
class _EventList:
    """Stands in for ``CoreV1EventList``."""

    items: list[_Event] = field(default_factory=list)


@dataclass
class _CreateCall:
    """One recorded ``create_namespaced_pod`` invocation."""

    namespace: str
    manifest: dict[str, object]


@dataclass
class _ExecCall:
    """
    One recorded exec invocation.

    Records the full call so a test can assert the launcher keeps using
    streaming mode with the right channels (a tautology-proof check: the
    test must fail if production drops ``_preload_content=False`` or the
    ``bash -lc`` wrapper).

    :param api_method: The bound API method passed to ``stream`` (must be
        ``connect_get_namespaced_pod_exec``).
    :param pod: Positional pod name.
    :param namespace: Positional namespace.
    :param command: The ``command`` kwarg.
    :param kwargs: Every keyword argument ``stream`` received.
    """

    api_method: object
    pod: str
    namespace: str
    command: list[str]
    kwargs: dict[str, object]


class _FakeWSClient:
    """
    Canned stand-in for the exec ``WSClient``.

    Serves one frame of channel data per ``is_open()``/``update`` cycle,
    then closes — mirroring the real read loop the launcher drives. In
    ``stuck`` mode it stays open forever and delivers nothing, modelling a
    wedged stream the read-loop safeguard must abandon.

    ``status_after_close`` models the fast-command STATUS-frame race: the
    socket reports closed (``is_open()`` False) with the STATUS frame still
    unbuffered, and only the post-loop ``update()`` flush surfaces it — so
    the test fails if the launcher drops that final flush.

    :param channels: Per-channel text the stream delivers (channel id →
        text), e.g. ``{1: "out", 3: status_frame}``.
    :param stuck: When True, never close and never deliver a frame.
    :param status_after_close: STATUS-channel text revealed only by an
        ``update()`` called after the socket has closed, or ``None``.
    """

    def __init__(
        self,
        channels: dict[int, str],
        *,
        stuck: bool = False,
        status_after_close: str | None = None,
    ) -> None:
        self._pending = dict(channels)
        self._open = True
        self._stuck = stuck
        self._status_after_close = status_after_close
        self.update_calls = 0
        self.closed = False

    def is_open(self) -> bool:
        """Open until every channel has been read out (or forever if stuck)."""
        return self._open

    def update(self, timeout: float = 0) -> None:
        """
        Pull the next frame. When modelling the STATUS-at-close race, an
        ``update()`` issued AFTER close moves the held-back STATUS frame
        into the readable buffer (the real client buffers a frame the
        close-time read missed).
        """
        del timeout
        self.update_calls += 1
        if not self._open and self._status_after_close is not None:
            self._pending[3] = self._status_after_close
            self._status_after_close = None

    def read_channel(self, channel: int, timeout: float = 0) -> str:
        """Pop a channel's buffered text, then close once all drained."""
        del timeout
        if self._stuck:
            return ""
        data = self._pending.pop(channel, "")
        # Close once the in-loop frames drain. With a STATUS frame held back
        # for the post-close flush, the loop still exits here — the launcher
        # must then recover the STATUS frame via its post-loop update().
        if not self._pending:
            self._open = False
        return data

    def close(self, **kwargs: object) -> None:
        """Record the teardown."""
        del kwargs
        self.closed = True


class _FakeCoreV1Api:
    """Recording stand-in for ``CoreV1Api`` bound to one fake config."""

    def __init__(self, state: _FakeK8sState) -> None:
        self._state = state

    def create_namespaced_pod(
        self, namespace: str, body: dict[str, object], *, _request_timeout: object = None
    ) -> object:
        """
        Record creation and register the resulting Pod.

        ``_request_timeout`` is captured (FIX-1) so a test can assert the
        launcher bounds the call. ``create_raises`` (popped front-first)
        raises BEFORE registering (a definite reject / pre-accept timeout).
        ``create_register_then_raises`` registers the Pod FIRST and then
        raises — modelling an apiserver-accepted-but-client-timed-out
        create whose Pod is now an orphan the launcher must clean up.
        """
        self._state.create_request_timeouts.append(_request_timeout)
        metadata = body["metadata"]
        assert isinstance(metadata, dict)
        pod_name = metadata["name"]
        assert isinstance(pod_name, str)
        if self._state.create_register_then_raises:
            self._state.create_calls.append(_CreateCall(namespace=namespace, manifest=body))
            self._state.pods[pod_name] = self._state.pod_factory(pod_name)
            raise self._state.create_register_then_raises.pop(0)
        if self._state.create_raises:
            raise self._state.create_raises.pop(0)
        self._state.create_calls.append(_CreateCall(namespace=namespace, manifest=body))
        self._state.pods[pod_name] = self._state.pod_factory(pod_name)
        return object()

    def read_namespaced_pod(
        self, name: str, namespace: str, *, _request_timeout: object = None
    ) -> _Pod:
        """
        Resolve a registered Pod or raise the fake 404.

        ``read_raises`` (popped front-first) lets a test inject transient
        apiserver errors on the first N reads before a Pod is returned.
        When ``read_sequence`` is set, successive reads walk it (staying
        on the last element once exhausted) — used to model a Pod that
        becomes ready only after a few polls. ``_request_timeout`` is
        captured (FIX-C) so a test can assert the launcher bounds the call.
        """
        del namespace
        self._state.read_count += 1
        self._state.read_request_timeouts.append(_request_timeout)
        if self._state.read_raises:
            raise self._state.read_raises.pop(0)
        if self._state.read_sequence:
            index = min(self._state.read_index, len(self._state.read_sequence) - 1)
            self._state.read_index += 1
            return self._state.read_sequence[index]
        pod = self._state.pods.get(name)
        if pod is None:
            raise _FakeApiException(status=404, reason="Not Found")
        return pod

    def list_namespaced_event(
        self, namespace: str, *, field_selector: str, _request_timeout: object = None
    ) -> _EventList:
        """Return canned events for the pod-ready failure surface."""
        del namespace, field_selector
        self._state.event_request_timeouts.append(_request_timeout)
        return _EventList(items=list(self._state.events))

    def delete_namespaced_pod(
        self,
        name: str,
        namespace: str,
        *,
        grace_period_seconds: int,
        _request_timeout: object = None,
    ) -> object:
        """Record the deletion (with grace period) or raise as configured.

        ``_request_timeout`` is captured (FIX-2/FIX-3) so a test can assert
        the delete is bounded.
        """
        del namespace
        self._state.delete_calls.append((name, grace_period_seconds))
        self._state.delete_request_timeouts.append(_request_timeout)
        if self._state.delete_raises:
            raise self._state.delete_raises.pop(0)
        self._state.pods.pop(name, None)
        return object()

    def connect_get_namespaced_pod_exec(self, *args: object, **kwargs: object) -> str:
        """Sentinel: ``stream`` intercepts this method, never calls it."""
        raise AssertionError("connect_get_namespaced_pod_exec must go through stream()")


@dataclass
class _FakeK8sState:
    """
    Recorder the fake client package writes into.

    :param create_calls: Every ``create_namespaced_pod`` invocation.
    :param delete_calls: ``(pod_name, grace_period_seconds)`` per delete.
    :param exec_calls: Every exec invocation (via the fake ``stream``).
    :param pods: Registered Pods by name (``read`` resolves here).
    :param events: Canned events the failure surface reports.
    :param create_raises: Exceptions successive creates raise (popped
        front-first) before succeeding.
    :param delete_raises: Exceptions successive deletes raise.
    :param exec_channels: Per-channel text the next exec stream serves.
    :param exec_raises: Exception ``stream`` raises on EVERY open attempt
        instead of returning a WSClient (models a permanent failure, e.g.
        a pod deleted for good or a forbidden exec).
    :param exec_open_raises: Exceptions ``stream`` raises on successive
        open attempts (popped front-first); once empty, the open
        succeeds. Models the transient first-exec race + retry.
    :param exec_open_blocks_s: When > 0, ``stream`` sleeps this long before
        returning — models a stalled apiserver websocket OPEN that the
        worker-thread timeout must abandon (round-3 final FIX-1).
    :param exec_stuck: When True, the WSClient stays open forever and
        never delivers a frame (models a wedged exec the read-loop
        safeguard must abandon).
    :param exec_status_after_close: STATUS-channel text the WSClient
        reveals only via an ``update()`` after the socket closes (models
        the fast-command STATUS-at-close race).
    :param ws_clients: Every WSClient ``stream`` handed back (for the
        websocket-close assertion).
    :param incluster_raises: Whether ``load_incluster_config`` raises the
        fake ConfigException (models running off-cluster).
    :param kubeconfig_raises: Whether ``load_kube_config`` raises the fake
        ConfigException (models no kubeconfig available either).
    :param incluster_calls / kubeconfig_calls: Recorded config loads (the
        Configuration each was handed, for the isolation assert).
    :param pod_factory: Builds the Pod registered on create — defaults to
        an immediately-ready Pod; tests override for fast-fail cases.
    :param read_sequence: When set, successive ``read_namespaced_pod``
        calls walk this list (models a Pod that becomes ready after a few
        polls); empty falls back to the registered-pods lookup.
    :param read_index: Cursor into ``read_sequence``.
    :param read_raises: Exceptions successive ``read_namespaced_pod`` calls
        raise (popped front-first) before falling through to the normal
        lookup — models transient apiserver errors during the ready wait.
    :param read_request_timeouts: ``_request_timeout`` captured per
        ``read_namespaced_pod`` call (FIX-C assertion).
    :param event_request_timeouts: ``_request_timeout`` captured per
        ``list_namespaced_event`` call (FIX-C assertion).
    :param create_request_timeouts: ``_request_timeout`` captured per
        ``create_namespaced_pod`` call (round-3 FIX-1 assertion).
    :param delete_request_timeouts: ``_request_timeout`` captured per
        ``delete_namespaced_pod`` call (round-3 FIX-2/FIX-3 assertion).
    :param create_register_then_raises: Exceptions ``create_namespaced_pod``
        raises AFTER registering the Pod (models an accepted-but-client-
        timed-out create whose Pod is now an orphan; round-3 FIX-1).
    :param configurations: Every ``Configuration()`` constructed.
    :param api_clients: Every ``ApiClient`` built (for the close assertion).
    """

    create_calls: list[_CreateCall] = field(default_factory=list)
    delete_calls: list[tuple[str, int]] = field(default_factory=list)
    exec_calls: list[_ExecCall] = field(default_factory=list)
    pods: dict[str, _Pod] = field(default_factory=dict)
    events: list[_Event] = field(default_factory=list)
    create_raises: list[Exception] = field(default_factory=list)
    create_register_then_raises: list[Exception] = field(default_factory=list)
    delete_raises: list[Exception] = field(default_factory=list)
    exec_channels: dict[int, str] = field(default_factory=dict)
    exec_raises: Exception | None = None
    exec_open_raises: list[Exception] = field(default_factory=list)
    exec_open_blocks_s: float = 0.0
    exec_stuck: bool = False
    exec_status_after_close: str | None = None
    ws_clients: list[_FakeWSClient] = field(default_factory=list)
    incluster_raises: bool = False
    kubeconfig_raises: bool = False
    incluster_calls: list[object] = field(default_factory=list)
    kubeconfig_calls: list[tuple[str | None, object]] = field(default_factory=list)
    configurations: list[object] = field(default_factory=list)
    api_clients: list[_FakeApiClient] = field(default_factory=list)
    pod_factory: Callable[[str], _Pod] = _ready_pod
    read_sequence: list[_Pod] = field(default_factory=list)
    read_index: int = 0
    read_count: int = 0
    read_raises: list[Exception] = field(default_factory=list)
    # _request_timeout captured per API call (round-3 FIX-C / FIX-1..3).
    read_request_timeouts: list[object] = field(default_factory=list)
    event_request_timeouts: list[object] = field(default_factory=list)
    create_request_timeouts: list[object] = field(default_factory=list)
    delete_request_timeouts: list[object] = field(default_factory=list)


class _FakeConfiguration:
    """Stands in for ``client.Configuration`` (an isolated config bag)."""


class _FakeApiClient:
    """
    Stands in for ``client.ApiClient`` — records the config it wraps and
    how many times it was closed (for the connection-pool-leak assertion).
    """

    def __init__(self, configuration: object) -> None:
        self.configuration = configuration
        self.close_count = 0

    def close(self) -> None:
        """Record a pool close (the real ApiClient closes its PoolManager)."""
        self.close_count += 1


def _install_fake_kubernetes(
    monkeypatch: pytest.MonkeyPatch, state: _FakeK8sState
) -> _FakeK8sState:
    """
    Inject a fake ``kubernetes`` package (with the submodules the
    launcher imports) into ``sys.modules``.

    :param monkeypatch: pytest monkeypatch (restores sys.modules after
        the test).
    :param state: The recorder the fakes write into.
    :returns: The same *state*, for convenience.
    """

    def _make_configuration() -> _FakeConfiguration:
        cfg = _FakeConfiguration()
        state.configurations.append(cfg)
        return cfg

    def _make_api_client(configuration: object) -> _FakeApiClient:
        api_client = _FakeApiClient(configuration)
        state.api_clients.append(api_client)
        return api_client

    def _make_core(api_client: object) -> _FakeCoreV1Api:
        del api_client
        return _FakeCoreV1Api(state)

    def _load_incluster(*, client_configuration: object, **_kw: object) -> None:
        state.incluster_calls.append(client_configuration)
        if state.incluster_raises:
            raise _FakeConfigException("no service account token")

    def _load_kubeconfig(
        *, config_file: str | None = None, client_configuration: object, **_kw: object
    ) -> None:
        state.kubeconfig_calls.append((config_file, client_configuration))
        if state.kubeconfig_raises:
            raise _FakeConfigException("no kubeconfig")

    def _stream(api_method: object, *args: object, **kwargs: object) -> _FakeWSClient:
        if state.exec_open_blocks_s > 0:
            # Models a stalled apiserver websocket OPEN. Uses the stdlib
            # sleep directly (not the module's possibly-monkeypatched
            # `time`) so the block is real on the worker thread.
            import time as _stdlib_time

            _stdlib_time.sleep(state.exec_open_blocks_s)
        if state.exec_open_raises:
            raise state.exec_open_raises.pop(0)
        if state.exec_raises is not None:
            raise state.exec_raises
        pod = str(args[0])
        namespace = str(args[1])
        command = kwargs["command"]
        assert isinstance(command, list)
        state.exec_calls.append(
            _ExecCall(
                api_method=api_method,
                pod=pod,
                namespace=namespace,
                command=list(command),
                kwargs=dict(kwargs),
            )
        )
        ws = _FakeWSClient(
            state.exec_channels,
            stuck=state.exec_stuck,
            status_after_close=state.exec_status_after_close,
        )
        state.ws_clients.append(ws)
        return ws

    # kubernetes.client (+ rest submodule)
    client_mod = types.ModuleType("kubernetes.client")
    client_mod.Configuration = _make_configuration  # type: ignore[attr-defined]
    client_mod.ApiClient = _make_api_client  # type: ignore[attr-defined]
    client_mod.CoreV1Api = _make_core  # type: ignore[attr-defined]
    client_mod.ApiException = _FakeApiException  # type: ignore[attr-defined]
    rest_mod = types.ModuleType("kubernetes.client.rest")
    rest_mod.ApiException = _FakeApiException  # type: ignore[attr-defined]
    client_mod.rest = rest_mod  # type: ignore[attr-defined]

    # kubernetes.config (+ config_exception submodule)
    config_mod = types.ModuleType("kubernetes.config")
    config_mod.ConfigException = _FakeConfigException  # type: ignore[attr-defined]
    config_mod.load_incluster_config = _load_incluster  # type: ignore[attr-defined]
    config_mod.load_kube_config = _load_kubeconfig  # type: ignore[attr-defined]
    config_exc_mod = types.ModuleType("kubernetes.config.config_exception")
    config_exc_mod.ConfigException = _FakeConfigException  # type: ignore[attr-defined]
    config_mod.config_exception = config_exc_mod  # type: ignore[attr-defined]

    # kubernetes.stream (+ ws_client submodule with channel constants)
    stream_mod = types.ModuleType("kubernetes.stream")
    stream_mod.stream = _stream  # type: ignore[attr-defined]
    ws_client_mod = types.ModuleType("kubernetes.stream.ws_client")
    ws_client_mod.STDOUT_CHANNEL = 1  # type: ignore[attr-defined]
    ws_client_mod.STDERR_CHANNEL = 2  # type: ignore[attr-defined]
    ws_client_mod.ERROR_CHANNEL = 3  # type: ignore[attr-defined]
    stream_mod.ws_client = ws_client_mod  # type: ignore[attr-defined]

    # kubernetes (top-level package tying the submodules together)
    pkg = types.ModuleType("kubernetes")
    pkg.client = client_mod  # type: ignore[attr-defined]
    pkg.config = config_mod  # type: ignore[attr-defined]
    pkg.stream = stream_mod  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "kubernetes", pkg)
    monkeypatch.setitem(sys.modules, "kubernetes.client", client_mod)
    monkeypatch.setitem(sys.modules, "kubernetes.client.rest", rest_mod)
    monkeypatch.setitem(sys.modules, "kubernetes.config", config_mod)
    monkeypatch.setitem(sys.modules, "kubernetes.config.config_exception", config_exc_mod)
    monkeypatch.setitem(sys.modules, "kubernetes.stream", stream_mod)
    monkeypatch.setitem(sys.modules, "kubernetes.stream.ws_client", ws_client_mod)

    # Fake top-level `websocket` module: websocket-client is only a
    # transitive dep of the optional kubernetes package, so it is absent in
    # CI jobs without the kubernetes extra. The launcher's run() does
    # `from websocket import WebSocketException`, so without this fake those
    # run/exec tests fail with ModuleNotFoundError in CI even though every
    # k8s symbol is faked. setitem overrides a real-package-absent
    # `sys.modules["websocket"] = None` too, keeping the tests hermetic.
    websocket_mod = types.ModuleType("websocket")
    websocket_mod.WebSocketException = _FakeWebSocketException  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "websocket", websocket_mod)
    return state


@pytest.fixture()
def fake_k8s(monkeypatch: pytest.MonkeyPatch) -> _FakeK8sState:
    """
    Install the fake client with a clean environment.

    A developer's ambient overrides must not leak into the default
    assertions.

    :param monkeypatch: pytest monkeypatch fixture.
    :returns: The fake's recorder state.
    """
    for var in (
        HOST_IMAGE_ENV_VAR,
        NAMESPACE_ENV_VAR,
        SANDBOX_SECRET_ENV_VAR,
        SANDBOX_ENV_PASSTHROUGH_ENV_VAR,
        SERVICE_ACCOUNT_ENV_VAR,
    ):
        monkeypatch.delenv(var, raising=False)
    return _install_fake_kubernetes(monkeypatch, _FakeK8sState())


# ── prepare ─────────────────────────────────────────────────


def test_prepare_raises_with_install_hint_when_sdk_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Without the optional client, prepare must fail with the exact extras
    install hint, not a raw ImportError. ``sys.modules[name] = None``
    makes ``import kubernetes`` raise ImportError.
    """
    monkeypatch.setitem(sys.modules, "kubernetes", None)
    with pytest.raises(click.ClickException) as exc:
        KubernetesSandboxLauncher().prepare()
    assert "omnigent[kubernetes]" in str(exc.value)


def test_prepare_loads_cluster_config(fake_k8s: _FakeK8sState) -> None:
    """A reachable cluster (in-cluster config loads) passes preflight."""
    KubernetesSandboxLauncher().prepare()
    # In-cluster was tried (and succeeded), so kubeconfig was untouched.
    assert len(fake_k8s.incluster_calls) == 1
    assert fake_k8s.kubeconfig_calls == []


def test_prepare_falls_back_to_kubeconfig_off_cluster(fake_k8s: _FakeK8sState) -> None:
    """
    Off-cluster (no SA token → ConfigException), prepare falls back to
    kubeconfig instead of failing (codex S3 fallback).
    """
    fake_k8s.incluster_raises = True
    KubernetesSandboxLauncher().prepare()
    assert len(fake_k8s.incluster_calls) == 1
    assert len(fake_k8s.kubeconfig_calls) == 1


def test_config_is_loaded_into_isolated_configuration(fake_k8s: _FakeK8sState) -> None:
    """
    Config must load into a fresh Configuration wired through ApiClient,
    never the library's global default (codex S3): the same Configuration
    instance the loader received is the one ApiClient wraps.
    """
    KubernetesSandboxLauncher().prepare()
    assert len(fake_k8s.configurations) == 1
    loaded_into = fake_k8s.incluster_calls[0]
    assert loaded_into is fake_k8s.configurations[0]


def test_prepare_honors_explicit_kubeconfig_path(fake_k8s: _FakeK8sState) -> None:
    """in_cluster=False routes straight to kubeconfig with the given path."""
    KubernetesSandboxLauncher(in_cluster=False, kubeconfig="/tmp/kc").prepare()
    assert fake_k8s.incluster_calls == []
    assert fake_k8s.kubeconfig_calls[0][0] == "/tmp/kc"


def test_prepare_wraps_config_failure_with_remediation(fake_k8s: _FakeK8sState) -> None:
    """
    No usable config (in-cluster fails AND no kubeconfig) surfaces a
    remediation naming both paths, not a raw ConfigException.
    """
    fake_k8s.incluster_raises = True
    fake_k8s.kubeconfig_raises = True

    with pytest.raises(click.ClickException) as exc:
        KubernetesSandboxLauncher().prepare()
    assert "KUBECONFIG" in str(exc.value)


# ── provision ───────────────────────────────────────────────


def test_provision_creates_pod_in_default_runner_namespace(fake_k8s: _FakeK8sState) -> None:
    """
    Provision creates one Pod from the default image in the DEFAULT runner
    namespace and returns the generated (DNS-safe) Pod name.

    FIX-D: the default namespace is the dedicated runner namespace
    ``omnigent-sandboxes`` (NOT the server's ``omnigent``) — the overlay
    grants the server SA rights there, and using the server namespace would
    403 + defeat the blast-radius split. Mutation guard: reverting
    _DEFAULT_NAMESPACE to "omnigent" fails this.
    """
    pod_name = KubernetesSandboxLauncher().provision("managed-abc")

    assert pod_name.startswith("omnigent-managed-abc-")
    [create] = fake_k8s.create_calls
    assert create.namespace == "omnigent-sandboxes"
    # The manifest's own metadata.namespace must match too.
    metadata = create.manifest["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["namespace"] == "omnigent-sandboxes"
    assert _container(create.manifest)["image"] == DEFAULT_HOST_IMAGE
    # The created Pod is the one returned.
    assert pod_name in fake_k8s.pods


def test_provision_waits_for_readiness_before_returning(
    fake_k8s: _FakeK8sState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Provision must not return until the container is ready (codex S2):
    a Pod that is Running but not-yet-ready, then flips ready, resolves
    the wait without error (rather than execing into a not-yet-up host).
    """
    # No real poll sleeps in tests.
    monkeypatch.setattr("omnigent.onboarding.sandboxes.kubernetes.time.sleep", lambda _s: None)
    pending = _Pod(
        status=_PodStatus(phase="Running", container_statuses=[_ContainerStatus(ready=False)])
    )
    # Two not-ready reads, then ready — the wait must keep polling.
    fake_k8s.read_sequence = [pending, pending, _ready_pod("x")]

    pod_name = KubernetesSandboxLauncher().provision("managed-abc")

    assert len(fake_k8s.create_calls) == 1
    # The wait polled past the not-ready reads (≥3 reads consumed).
    assert fake_k8s.read_index >= 3
    assert pod_name.startswith("omnigent-managed-abc-")


def test_provision_readiness_checks_host_container_not_sidecar(
    fake_k8s: _FakeK8sState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    MEDIUM-1: on sidecar-injected clusters a ready sidecar must NOT count
    as the Pod being ready — exec targets the ``host`` container, so the
    wait must gate on the host container's readiness specifically. A Pod
    whose sidecar is ready but whose host container is not stays not-ready
    until the host flips ready.

    Mutation guard: an ``any(cs.ready ...)`` readiness check would return
    on the first (sidecar-ready) read and never poll for the host — this
    asserts the wait kept polling until the host container was ready.
    """
    monkeypatch.setattr("omnigent.onboarding.sandboxes.kubernetes.time.sleep", lambda _s: None)
    # Sidecar ready, host NOT ready — must be treated as not-ready.
    sidecar_only = _Pod(
        status=_PodStatus(
            phase="Running",
            container_statuses=[
                _ContainerStatus(ready=True, name="istio-proxy"),
                _ContainerStatus(ready=False, name="host"),
            ],
        )
    )
    # Then the host container flips ready (sidecar still ready).
    both_ready = _Pod(
        status=_PodStatus(
            phase="Running",
            container_statuses=[
                _ContainerStatus(ready=True, name="istio-proxy"),
                _ContainerStatus(ready=True, name="host"),
            ],
        )
    )
    fake_k8s.read_sequence = [sidecar_only, sidecar_only, both_ready]

    pod_name = KubernetesSandboxLauncher().provision("managed-abc")

    # The wait did NOT return on the sidecar-ready reads — it polled until
    # the host container was ready (≥3 reads consumed).
    assert fake_k8s.read_index >= 3
    assert pod_name.startswith("omnigent-managed-abc-")
    # Readiness succeeded → no cleanup delete.
    assert fake_k8s.delete_calls == []


def _trip_deadline_after_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Patch the launcher clock so the readiness wait polls a few times and
    then crosses the deadline (FIX-2: recoverable conditions poll until the
    deadline rather than fast-failing). ``sleep`` is a no-op; ``monotonic``
    starts at 0 then jumps past the 90s budget on the 3rd call.
    """
    monkeypatch.setattr("omnigent.onboarding.sandboxes.kubernetes.time.sleep", lambda _s: None)
    ticks = iter([0.0, 1.0, 2.0, 10_000.0, 20_000.0, 30_000.0])
    monkeypatch.setattr(
        "omnigent.onboarding.sandboxes.kubernetes.time.monotonic",
        lambda: next(ticks),
    )


def test_provision_polls_through_image_pull_backoff_then_times_out(
    fake_k8s: _FakeK8sState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    FIX-2: ImagePullBackOff is RECOVERABLE (the kubelet retries cold pulls
    / registry+cred flaps), so the wait must NOT fast-fail on it — it polls
    until the deadline, then times out surfacing the LATEST reason +
    events, and the orphan Pod is still cleaned up.

    Mutation guard: with ImagePullBackOff back in the fast-fail set this
    would raise on the first read with no events / no timeout wording.
    """
    _trip_deadline_after_polls(monkeypatch)

    def _bad_image(name: str) -> _Pod:
        return _Pod(
            status=_PodStatus(
                phase="Pending",
                container_statuses=[
                    _ContainerStatus(
                        ready=False,
                        state=_ContainerState(
                            waiting=_Waiting(
                                reason="ImagePullBackOff", message="back-off pulling image"
                            )
                        ),
                    )
                ],
            )
        )

    fake_k8s.pod_factory = _bad_image
    fake_k8s.events = [_Event(reason="Failed", message="Failed to pull image")]
    with pytest.raises(click.ClickException) as exc:
        KubernetesSandboxLauncher().provision("managed-abc")
    message = str(exc.value)
    # Timed out (not immediate fast-fail), surfacing the current reason…
    assert "did not become ready" in message
    assert "ImagePullBackOff" in message
    # …and the latest kubelet events, plus a describe hint.
    assert "Failed to pull image" in message
    assert "kubectl describe pod" in message
    # It actually polled (more than one read) before giving up.
    assert fake_k8s.read_count > 1
    # HIGH-2: the orphan Pod is still cleaned up on the timeout failure.
    [(deleted_name, grace)] = fake_k8s.delete_calls
    assert grace == 0
    [create] = fake_k8s.create_calls
    metadata = create.manifest["metadata"]
    assert isinstance(metadata, dict)
    assert deleted_name == metadata["name"]


def test_provision_polls_through_unschedulable_then_times_out(
    fake_k8s: _FakeK8sState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    FIX-2: Unschedulable is RECOVERABLE (autoscaler/Karpenter trigger
    scale-up by leaving Pods Pending), so the wait polls until the deadline
    rather than aborting; on timeout it surfaces the Unschedulable reason +
    scheduler events, and the orphan Pod is cleaned up.

    Mutation guard: with Unschedulable back in the fast-fail set this would
    raise immediately with "cannot be scheduled" and no timeout wording.
    """
    _trip_deadline_after_polls(monkeypatch)

    def _unschedulable(name: str) -> _Pod:
        return _Pod(
            status=_PodStatus(
                phase="Pending",
                container_statuses=[_ContainerStatus(ready=False)],
                conditions=[
                    _Condition(
                        type="PodScheduled",
                        status="False",
                        reason="Unschedulable",
                        message="0/3 nodes are available: insufficient cpu",
                    )
                ],
            )
        )

    fake_k8s.pod_factory = _unschedulable
    fake_k8s.events = [_Event(reason="FailedScheduling", message="insufficient cpu")]
    with pytest.raises(click.ClickException) as exc:
        KubernetesSandboxLauncher().provision("managed-abc")
    message = str(exc.value)
    assert "did not become ready" in message
    assert "Unschedulable" in message
    assert "FailedScheduling" in message
    assert fake_k8s.read_count > 1
    # HIGH-2: the orphan is cleaned up on the timeout failure.
    assert len(fake_k8s.delete_calls) == 1


def test_provision_bounds_event_lookup_with_request_timeout(
    fake_k8s: _FakeK8sState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    round-3 FIX-4: the events-enrichment lookup (``list_namespaced_event``,
    run while building the readiness-failure message) is also bounded by a
    non-None ``_request_timeout`` — like create/read/delete — so a stalled
    apiserver can't hang the failure path.

    Drives a readiness timeout (so _recent_events runs) and asserts every
    captured event request-timeout is non-None.

    Mutation guard: dropping the kwarg from list_namespaced_event leaves the
    captured timeout None.
    """
    _trip_deadline_after_polls(monkeypatch)

    def _pending(name: str) -> _Pod:
        return _Pod(
            status=_PodStatus(phase="Pending", container_statuses=[_ContainerStatus(ready=False)])
        )

    fake_k8s.pod_factory = _pending
    fake_k8s.events = [_Event(reason="FailedScheduling", message="no nodes")]

    with pytest.raises(click.ClickException, match="did not become ready"):
        KubernetesSandboxLauncher().provision("managed-abc")

    # The events lookup ran (failure message enrichment) AND was bounded.
    assert fake_k8s.event_request_timeouts
    assert all(t is not None for t in fake_k8s.event_request_timeouts)


def test_provision_polls_through_transient_read_error_then_succeeds(
    fake_k8s: _FakeK8sState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    FIX-2: a transient apiserver error (5xx / connection) from
    read_namespaced_pod must NOT abort the launch — it is logged and
    retried until the Pod reads ready.

    Mutation guard: re-raising on every ApiException makes this fail with
    the 503.
    """
    monkeypatch.setattr("omnigent.onboarding.sandboxes.kubernetes.time.sleep", lambda _s: None)
    # First two reads 503 (apiserver hiccup), then the Pod reads ready.
    fake_k8s.read_raises = [
        _FakeApiException(status=503, reason="Service Unavailable"),
        _FakeApiException(status=0, reason="Connection refused"),
    ]

    pod_name = KubernetesSandboxLauncher().provision("managed-abc")

    assert pod_name.startswith("omnigent-managed-abc-")
    # No cleanup — the launch succeeded after the transient errors cleared.
    assert fake_k8s.delete_calls == []


def test_provision_passes_request_timeout_to_read(fake_k8s: _FakeK8sState) -> None:
    """
    FIX-C: every readiness read is bounded by a non-None ``_request_timeout``
    so a stalled apiserver socket can't hang past the deadline.

    Mutation guard: dropping the kwarg leaves the captured timeout None.
    """
    KubernetesSandboxLauncher().provision("managed-abc")

    assert fake_k8s.read_request_timeouts  # at least one read happened
    assert all(t is not None for t in fake_k8s.read_request_timeouts)


def test_provision_passes_request_timeout_to_create(fake_k8s: _FakeK8sState) -> None:
    """
    round-3 FIX-1: create_namespaced_pod is bounded by a non-None
    ``_request_timeout`` so a stalled apiserver can't hang provision()
    before the readiness deadline even starts.

    Mutation guard: dropping the kwarg leaves the captured timeout None.
    """
    KubernetesSandboxLauncher().provision("managed-abc")

    assert fake_k8s.create_request_timeouts  # the create happened
    assert all(t is not None for t in fake_k8s.create_request_timeouts)


def test_provision_create_timeout_cleans_up_and_raises(fake_k8s: _FakeK8sState) -> None:
    """
    round-3 FIX-1: a urllib3 timeout from create is AMBIGUOUS (the
    apiserver may have accepted the Pod). provision() best-effort deletes
    the known pod_name and raises a clear error — never a silent orphan.

    Mutation guard: dropping the create HTTPError handling lets the timeout
    propagate uncaught with no cleanup delete.
    """
    from urllib3.exceptions import ReadTimeoutError

    fake_k8s.create_raises = [
        ReadTimeoutError(pool=None, url="/api/v1/pods", message="create timed out"),
    ]

    with pytest.raises(click.ClickException, match="timed out creating"):
        KubernetesSandboxLauncher().provision("managed-abc")

    # The known pod_name was best-effort deleted (orphan cleanup), with the
    # bounded grace period.
    assert len(fake_k8s.delete_calls) == 1
    deleted_name, grace = fake_k8s.delete_calls[0]
    assert grace == 0
    assert deleted_name.startswith("omnigent-managed-abc-")


def test_provision_accepted_but_timed_out_create_orphan_is_deleted(
    fake_k8s: _FakeK8sState,
) -> None:
    """
    round-3 FIX-1: when the apiserver ACCEPTED the create (Pod exists) but
    the client timed out, the now-orphan Pod must actually be removed by
    the best-effort cleanup — not left running.
    """
    from urllib3.exceptions import ReadTimeoutError

    # create registers the Pod, THEN raises a client timeout.
    fake_k8s.create_register_then_raises = [
        ReadTimeoutError(pool=None, url="/api/v1/pods", message="create timed out"),
    ]

    with pytest.raises(click.ClickException, match="timed out creating"):
        KubernetesSandboxLauncher().provision("managed-abc")

    # The Pod the apiserver created is gone (cleanup deleted it), so no
    # orphan survives.
    assert fake_k8s.pods == {}
    [(deleted_name, _grace)] = fake_k8s.delete_calls
    assert deleted_name.startswith("omnigent-managed-abc-")


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_provision_ambiguous_create_apiexception_cleans_up_and_raises(
    fake_k8s: _FakeK8sState, status: int
) -> None:
    """
    round-3 FIX-2: a non-409 create ApiException whose status is NOT a
    definite client reject (5xx — e.g. 504 Gateway Timeout) is AMBIGUOUS
    (the apiserver may have accepted the Pod then failed the response), so
    provision() best-effort deletes the known pod_name before re-raising —
    no silent orphan.

    Mutation guard: without the ambiguous-ApiException cleanup branch the
    error raises with delete_calls empty.
    """
    fake_k8s.create_raises = [_FakeApiException(status=status, reason="ServerError")]

    with pytest.raises(click.ClickException, match="create pod"):
        KubernetesSandboxLauncher().provision("managed-abc")

    [(deleted_name, grace)] = fake_k8s.delete_calls
    assert grace == 0
    assert deleted_name.startswith("omnigent-managed-abc-")


def test_provision_ambiguous_create_apiexception_deletes_real_orphan(
    fake_k8s: _FakeK8sState,
) -> None:
    """
    round-3 FIX-2: when the apiserver actually created the Pod but then
    returned a 500, the orphan is removed (not just the delete attempted).
    """
    # create registers the Pod, THEN raises a 500 (accepted-but-failed).
    fake_k8s.create_register_then_raises = [
        _FakeApiException(status=500, reason="InternalError"),
    ]

    with pytest.raises(click.ClickException, match="create pod"):
        KubernetesSandboxLauncher().provision("managed-abc")

    assert fake_k8s.pods == {}


@pytest.mark.parametrize("status", [400, 403, 404, 415, 422, 429])
def test_provision_definite_create_reject_does_not_clean_up(
    fake_k8s: _FakeK8sState, status: int
) -> None:
    """
    round-3 final FIX-2: any DEFINITE client reject (4xx) means the Pod was
    NOT created — provision() raises WITHOUT a cleanup delete. Includes 415
    and 429, which the previous denylist wrongly treated as ambiguous and
    would have deleted (another launch's same-named Pod).

    Mutation guard: a denylist / "delete on any non-4xx-subset" check adds a
    spurious delete for 415/429 here.
    """
    fake_k8s.create_raises = [_FakeApiException(status=status, reason="Rejected")]

    with pytest.raises(click.ClickException, match="create pod"):
        KubernetesSandboxLauncher().provision("managed-abc")

    assert fake_k8s.delete_calls == []


def test_provision_retry_exhausted_409_does_not_clean_up(fake_k8s: _FakeK8sState) -> None:
    """
    round-3 final FIX-2: a 409 conflict that survives the one name-regen
    retry (still 409 on the second attempt) is a DEFINITE "not created"
    (the name is taken by another Pod) — provision() must NOT delete it,
    or it would delete that OTHER launch's Pod.

    Mutation guard: any cleanup path that fires for 409 deletes here.
    """
    # First create 409s → name regenerated; second create 409s again →
    # falls through to the post-409 handling, which must NOT clean up.
    fake_k8s.create_raises = [
        _FakeApiException(status=409, reason="AlreadyExists"),
        _FakeApiException(status=409, reason="AlreadyExists"),
    ]

    with pytest.raises(click.ClickException, match="create pod"):
        KubernetesSandboxLauncher().provision("managed-abc")

    assert fake_k8s.delete_calls == []


def test_provision_treats_request_timeout_as_transient(
    fake_k8s: _FakeK8sState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    FIX-C: a urllib3 request TIMEOUT (raised by the ``_request_timeout``
    bound) is a transient read error — logged and retried until the Pod
    reads ready, NOT a fatal that aborts the launch.

    Mutation guard: if the timeout weren't classified transient it would
    propagate and fail the launch (and the orphan would be cleaned up).
    """
    from urllib3.exceptions import ReadTimeoutError

    monkeypatch.setattr("omnigent.onboarding.sandboxes.kubernetes.time.sleep", lambda _s: None)
    # First two reads time out at the socket; then the Pod reads ready.
    fake_k8s.read_raises = [
        ReadTimeoutError(pool=None, url="/readyz", message="read timed out"),
        ReadTimeoutError(pool=None, url="/readyz", message="read timed out"),
    ]

    pod_name = KubernetesSandboxLauncher().provision("managed-abc")

    assert pod_name.startswith("omnigent-managed-abc-")
    # Retried through the timeouts, no orphan cleanup (launch succeeded).
    assert fake_k8s.read_count >= 3
    assert fake_k8s.delete_calls == []


def test_provision_fast_fails_on_definite_read_error(fake_k8s: _FakeK8sState) -> None:
    """
    FIX-2: a DEFINITE read error (e.g. 403 Forbidden — RBAC) is surfaced
    immediately, not retried, and the orphan Pod is cleaned up.
    """
    fake_k8s.read_raises = [_FakeApiException(status=403, reason="Forbidden")]

    with pytest.raises(click.ClickException) as exc:
        KubernetesSandboxLauncher().provision("managed-abc")
    assert "Forbidden" in str(exc.value)
    # HIGH-2: orphan cleaned up.
    assert len(fake_k8s.delete_calls) == 1


def test_provision_fast_fails_on_terminal_config_error(fake_k8s: _FakeK8sState) -> None:
    """
    FIX-2: a non-self-healing config error (CreateContainerConfigError /
    InvalidImageName / RunContainerError) IS terminal — fail fast rather
    than poll, and clean up the orphan.
    """

    def _bad_config(name: str) -> _Pod:
        return _Pod(
            status=_PodStatus(
                phase="Pending",
                container_statuses=[
                    _ContainerStatus(
                        ready=False,
                        state=_ContainerState(
                            waiting=_Waiting(
                                reason="CreateContainerConfigError",
                                message="secret 'omnigent-creds' not found",
                            )
                        ),
                    )
                ],
            )
        )

    fake_k8s.pod_factory = _bad_config
    with pytest.raises(click.ClickException) as exc:
        KubernetesSandboxLauncher().provision("managed-abc")
    message = str(exc.value)
    assert "CreateContainerConfigError" in message
    assert "container cannot start" in message
    assert len(fake_k8s.delete_calls) == 1


def test_provision_fast_fails_on_early_host_container_exit(fake_k8s: _FakeK8sState) -> None:
    """
    FIX-2: a ``restartPolicy: Never`` Pod whose host container ran and
    exited (state.terminated) will never become ready — fail fast on the
    early exit (before the phase even flips to Failed), and clean up.

    Mutation guard: dropping the terminated-state check makes this poll to
    the deadline instead of fast-failing.
    """

    def _exited(name: str) -> _Pod:
        return _Pod(
            status=_PodStatus(
                phase="Running",
                container_statuses=[
                    _ContainerStatus(
                        ready=False,
                        state=_ContainerState(terminated=_Terminated(exit_code=1, reason="Error")),
                    )
                ],
            )
        )

    fake_k8s.pod_factory = _exited
    with pytest.raises(click.ClickException) as exc:
        KubernetesSandboxLauncher().provision("managed-abc")
    message = str(exc.value)
    assert "host container exited" in message
    assert "exit 1" in message
    assert len(fake_k8s.delete_calls) == 1


def test_provision_fast_fails_on_terminal_phase(fake_k8s: _FakeK8sState) -> None:
    """A Pod that lands in a terminal Failed phase fails fast."""

    def _failed(name: str) -> _Pod:
        return _Pod(status=_PodStatus(phase="Failed"))

    fake_k8s.pod_factory = _failed
    with pytest.raises(click.ClickException) as exc:
        KubernetesSandboxLauncher().provision("managed-abc")
    assert "terminal phase 'Failed'" in str(exc.value)
    # HIGH-2: the terminal Pod is cleaned up rather than orphaned.
    assert len(fake_k8s.delete_calls) == 1


def test_provision_cleans_up_pod_on_readiness_timeout(
    fake_k8s: _FakeK8sState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    HIGH-2 path (c): when the readiness wait times out (Pod never ready,
    no terminal/fast-fail signal), provision deletes the just-created Pod
    before re-raising — otherwise it orphans (the caller gets no id).

    Mutation guard: removing the provision() cleanup leaves delete_calls
    empty and this fails.
    """
    # Make the deadline trip immediately: monotonic jumps past the budget,
    # and sleep is a no-op so the loop spins fast.
    monkeypatch.setattr("omnigent.onboarding.sandboxes.kubernetes.time.sleep", lambda _s: None)
    ticks = iter([0.0, 10_000.0, 20_000.0, 30_000.0])
    monkeypatch.setattr(
        "omnigent.onboarding.sandboxes.kubernetes.time.monotonic",
        lambda: next(ticks),
    )

    def _never_ready(name: str) -> _Pod:
        # Running but the host container never reports ready, and no
        # terminal / unschedulable / image-pull signal — only the timeout
        # can end the wait.
        return _Pod(
            status=_PodStatus(phase="Running", container_statuses=[_ContainerStatus(ready=False)])
        )

    fake_k8s.pod_factory = _never_ready
    with pytest.raises(click.ClickException) as exc:
        KubernetesSandboxLauncher().provision("managed-abc")
    assert "did not become ready" in str(exc.value)
    # The orphan was reaped.
    [(deleted_name, grace)] = fake_k8s.delete_calls
    assert grace == 0
    [create] = fake_k8s.create_calls
    metadata = create.manifest["metadata"]
    assert isinstance(metadata, dict)
    assert deleted_name == metadata["name"]


def test_provision_readiness_cleanup_swallows_delete_failure(
    fake_k8s: _FakeK8sState,
) -> None:
    """
    The best-effort cleanup must not mask the original readiness failure:
    if the cleanup delete itself errors, provision still raises the
    ORIGINAL error (not the delete error).
    """

    def _failed(name: str) -> _Pod:
        return _Pod(status=_PodStatus(phase="Failed"))

    fake_k8s.pod_factory = _failed
    # The cleanup delete blows up — must be swallowed.
    fake_k8s.delete_raises = [_FakeApiException(status=500, reason="ServerError")]

    with pytest.raises(click.ClickException) as exc:
        KubernetesSandboxLauncher().provision("managed-abc")
    # Original readiness error surfaces, NOT the delete's ServerError.
    assert "terminal phase 'Failed'" in str(exc.value)
    assert "ServerError" not in str(exc.value)
    # The cleanup was attempted.
    assert len(fake_k8s.delete_calls) == 1


def test_best_effort_delete_swallows_urllib3_timeout(fake_k8s: _FakeK8sState) -> None:
    """
    round-3 FIX-2: the cleanup delete is bounded AND a urllib3
    timeout/connection error from it is swallowed (best-effort) so the
    original readiness failure is preserved — a stalled apiserver on the
    cleanup path must not hang or change the surfaced error.

    Mutation guard: if _best_effort_delete only caught ApiException, the
    urllib3 timeout would propagate and replace the original error.
    """
    from urllib3.exceptions import ReadTimeoutError

    def _failed(name: str) -> _Pod:
        return _Pod(status=_PodStatus(phase="Failed"))

    fake_k8s.pod_factory = _failed
    fake_k8s.delete_raises = [
        ReadTimeoutError(pool=None, url="/api/v1/pods", message="delete timed out"),
    ]

    with pytest.raises(click.ClickException) as exc:
        KubernetesSandboxLauncher().provision("managed-abc")
    # Original readiness error preserved (not the delete timeout).
    assert "terminal phase 'Failed'" in str(exc.value)
    assert "timed out" not in str(exc.value)
    # The cleanup delete was attempted AND bounded.
    assert len(fake_k8s.delete_calls) == 1
    assert fake_k8s.delete_request_timeouts
    assert all(t is not None for t in fake_k8s.delete_request_timeouts)


def test_provision_image_resolution_order(
    fake_k8s: _FakeK8sState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Explicit constructor image wins over the env override, which wins
    over the default — the precedence the server's
    ``sandbox.kubernetes.image`` config relies on.
    """
    monkeypatch.setenv(HOST_IMAGE_ENV_VAR, "ghcr.io/env/host:1")

    KubernetesSandboxLauncher(image="ghcr.io/explicit/host:2").provision("a")
    KubernetesSandboxLauncher().provision("b")

    first, second = fake_k8s.create_calls
    assert _container(first.manifest)["image"] == "ghcr.io/explicit/host:2"
    assert _container(second.manifest)["image"] == "ghcr.io/env/host:1"


def test_provision_resolves_namespace_secret_and_service_account(
    fake_k8s: _FakeK8sState,
) -> None:
    """
    Constructor namespace / secret_name / service_account thread into the
    created Pod (the managed-host config's path to a custom deployment).
    """
    KubernetesSandboxLauncher(
        namespace="agents",
        secret_name="omnigent-creds",
        service_account="custom-runner",
    ).provision("a")

    [create] = fake_k8s.create_calls
    assert create.namespace == "agents"
    assert _spec(create.manifest)["serviceAccountName"] == "custom-runner"
    assert _container(create.manifest)["envFrom"] == [{"secretRef": {"name": "omnigent-creds"}}]


def test_provision_env_passthrough_resolves_from_server_env(
    fake_k8s: _FakeK8sState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Constructor env NAMES resolve to values from the server process
    environment at provision time (config carries names only).
    """
    monkeypatch.setenv("OMNIGENT_GATEWAY_URL", "https://gw")

    KubernetesSandboxLauncher(env=["OMNIGENT_GATEWAY_URL"]).provision("a")

    [create] = fake_k8s.create_calls
    env = _container(create.manifest)["env"]
    assert isinstance(env, list)
    assert {"name": "OMNIGENT_GATEWAY_URL", "value": "https://gw"} in env


def test_provision_env_passthrough_missing_var_fails_loud(
    fake_k8s: _FakeK8sState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A configured name unset in the server environment is an operator
    error — fail loud and create nothing (it would otherwise surface
    later as an opaque in-sandbox failure).
    """
    monkeypatch.delenv("OMNIGENT_GATEWAY_URL", raising=False)

    with pytest.raises(click.ClickException, match="OMNIGENT_GATEWAY_URL"):
        KubernetesSandboxLauncher(env=["OMNIGENT_GATEWAY_URL"]).provision("a")
    assert fake_k8s.create_calls == []


@pytest.mark.parametrize("reserved", ["HOME", "IS_SANDBOX"])
def test_provision_env_passthrough_rejects_reserved_names(
    fake_k8s: _FakeK8sState, monkeypatch: pytest.MonkeyPatch, reserved: str
) -> None:
    """
    A passthrough that names HOME / IS_SANDBOX is rejected loud: letting
    it through would emit a duplicate env entry that could shadow the
    writable-HOME emptyDir and break the host's `mkdir -p $HOME/workspace`.
    Nothing is created.
    """
    # Even if the operator has the var set in the server env, it's still
    # rejected — the reason is the collision, not a missing value.
    monkeypatch.setenv(reserved, "/somewhere")

    with pytest.raises(click.ClickException, match=f"'{reserved}'.*reserved"):
        KubernetesSandboxLauncher(env=[reserved]).provision("a")
    assert fake_k8s.create_calls == []


def test_manifest_fixed_env_is_not_duplicated_by_passthrough() -> None:
    """
    The writable-HOME guarantee: even constructed directly, HOME appears
    exactly once (the reserved-name guard lives in the launcher, but the
    manifest builder must not itself double up the fixed entries).
    """
    manifest = build_pod_manifest(
        pod_name="omnigent-x-1",
        namespace="omnigent",
        image="img",
        service_account="sa",
        harness_secret=None,
        env_literals={"OMNIGENT_GATEWAY_URL": "https://gw"},
        node_selector=None,
    )
    env = _container(manifest)["env"]
    assert isinstance(env, list)
    home_entries = [e for e in env if isinstance(e, dict) and e.get("name") == "HOME"]
    assert home_entries == [{"name": "HOME", "value": "/home/omnigent"}]


def test_provision_regenerates_name_on_conflict(
    fake_k8s: _FakeK8sState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A 409 name collision (two launches raced the same slug) is recovered
    by retrying with a FRESH name — not the same one (which would just
    409 again).

    ``_new_pod_name`` is pinned to a deterministic two-name sequence so
    the test can assert the retry's create used the SECOND name; this
    fails if production were to retry with the original name.
    """
    names = iter(["omnigent-a-firstx", "omnigent-a-second"])
    monkeypatch.setattr(
        "omnigent.onboarding.sandboxes.kubernetes._new_pod_name",
        lambda _label: next(names),
    )
    fake_k8s.create_raises = [_FakeApiException(status=409, reason="AlreadyExists")]

    pod_name = KubernetesSandboxLauncher().provision("a")

    # The first create (with the first name) 409'd and recorded nothing;
    # the retry created the pod under the SECOND, regenerated name.
    assert pod_name == "omnigent-a-second"
    [create] = fake_k8s.create_calls
    metadata = create.manifest["metadata"]
    assert isinstance(metadata, dict)
    assert metadata["name"] == "omnigent-a-second"
    assert "omnigent-a-second" in fake_k8s.pods
    assert "omnigent-a-firstx" not in fake_k8s.pods


def test_provision_wraps_api_errors_with_reason_and_rbac_hint(
    fake_k8s: _FakeK8sState,
) -> None:
    """
    A 403 (the server SA lacks the sandbox-manager Role) surfaces the
    API reason AND an RBAC remediation — the most common misconfig.
    """
    fake_k8s.create_raises = [
        _FakeApiException(status=403, reason="Forbidden", body="pods is forbidden")
    ]

    with pytest.raises(click.ClickException) as exc:
        KubernetesSandboxLauncher().provision("a")
    message = str(exc.value)
    assert "Forbidden" in message
    assert "pods is forbidden" in message
    # LOW-1: the remediation points at the real RBAC location.
    assert "deploy/kubernetes/overlays/sandbox-runners/" in message
    # FIX-2: a definite client reject (403) means the Pod was NOT created,
    # so there is NO orphan to clean up — no delete attempted.
    assert fake_k8s.delete_calls == []


# ── run ─────────────────────────────────────────────────────


def _provisioned(fake_k8s: _FakeK8sState) -> tuple[KubernetesSandboxLauncher, str]:
    """Provision a launcher and return it with the created Pod name."""
    launcher = KubernetesSandboxLauncher()
    pod_name = launcher.provision("a")
    return launcher, pod_name


def test_run_execs_bash_lc_and_parses_returncode(fake_k8s: _FakeK8sState) -> None:
    """
    ``run`` execs via ``bash -lc`` (codex S5, for the login-shell venv
    PATH) and returns the exit code parsed from the STATUS frame plus the
    captured streams.

    The exec call's full shape is asserted so the test fails if production
    drops streaming mode (``_preload_content=False``) or the bash -lc
    wrapper — i.e. it is not satisfiable by a degenerate implementation.
    """
    launcher, pod_name = _provisioned(fake_k8s)
    fake_k8s.exec_channels = {
        1: "remote-out\n",
        2: "remote-err\n",
        3: '{"metadata":{},"status":"Success"}',
    }

    result = launcher.run(pod_name, "echo hi")

    [call] = fake_k8s.exec_calls
    assert call.command == ["bash", "-lc", "echo hi"]
    assert call.pod == pod_name
    # Default runner namespace (FIX-D).
    assert call.namespace == "omnigent-sandboxes"
    # The bound API method must be the pod-exec connector (not, say, a
    # non-streaming read) — addressed by name on the fake CoreV1Api.
    assert getattr(call.api_method, "__name__", None) == "connect_get_namespaced_pod_exec"
    # Streaming mode + channel flags: dropping any of these would break
    # the channel-by-channel STATUS-frame exit-code parsing.
    assert call.kwargs["_preload_content"] is False
    assert call.kwargs["tty"] is False
    assert call.kwargs["stdin"] is False
    assert call.kwargs["stdout"] is True
    assert call.kwargs["stderr"] is True
    assert call.kwargs["command"] == ["bash", "-lc", "echo hi"]
    # Exec must name the Pod's single container ("host") explicitly, or a
    # sidecar-injected cluster (Istio/Linkerd) rejects the ambiguous exec.
    assert call.kwargs["container"] == "host"
    # round-3 FIX-1: the exec OPEN is bounded too (no unbounded blocking
    # k8s call remains). It must not break streaming reads (asserted by the
    # streaming/real-client tests passing with this kwarg present).
    assert call.kwargs["_request_timeout"] is not None
    assert result.returncode == 0
    assert result.stdout == "remote-out\n"
    assert result.stderr == "remote-err\n"


def test_run_passes_request_timeout_kwarg_to_exec_open(fake_k8s: _FakeK8sState) -> None:
    """
    round-3 final FIX-1: ``stream()`` is still passed ``_request_timeout``
    as FUTURE-PROOFING (in case a later kubernetes client honors it for the
    streaming connect). NOTE: in 36.0.2 this kwarg is INERT for
    ``_preload_content=False`` — the REAL open bound is the worker-thread
    timeout exercised by
    :func:`test_run_exec_open_timeout_is_bounded_by_worker_thread`.

    Mutation guard: dropping the kwarg from the stream() call leaves the
    captured value absent/None.
    """
    launcher, pod_name = _provisioned(fake_k8s)
    fake_k8s.exec_channels = {3: '{"metadata":{},"status":"Success"}'}

    launcher.run(pod_name, "true")

    [call] = fake_k8s.exec_calls
    assert call.kwargs.get("_request_timeout") is not None


def test_run_exec_open_timeout_is_bounded_by_worker_thread(
    fake_k8s: _FakeK8sState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    round-3 final FIX-1 (BLOCKER): a stalled exec websocket OPEN must be
    abandoned by the WORKER-THREAD timeout (the ``_request_timeout`` kwarg
    is inert for the streaming connect in kubernetes 36.0.2). The stream()
    open blocks longer than the bound; run() must raise a clear timeout
    PROMPTLY — not hang.

    Mutation guard: removing the ThreadPoolExecutor/.result(timeout=...)
    wrapper makes run() block on the sleeping fake open until it returns,
    so this test would exceed its wall-clock guard (or hang).
    """
    import time as _t

    # Tiny bound; the fake open blocks well past it.
    monkeypatch.setattr(
        "omnigent.onboarding.sandboxes.kubernetes._POD_READY_REQUEST_TIMEOUT_S", 0.2
    )
    launcher, pod_name = _provisioned(fake_k8s)
    fake_k8s.exec_open_blocks_s = 10.0  # would hang the test without the bound

    started = _t.monotonic()
    with pytest.raises(click.ClickException, match="timed out opening exec stream"):
        launcher.run(pod_name, "true")
    elapsed = _t.monotonic() - started

    # Returned promptly (≈ the 0.2s bound), NOT after the 10s fake block.
    assert elapsed < 5.0, f"exec-open timeout did not return promptly: {elapsed:.1f}s"


def test_run_exec_open_timeout_is_not_retried_as_transient(
    fake_k8s: _FakeK8sState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    round-3 final FIX-1: an open TIMEOUT is an apiserver stall, NOT the
    container-not-ready race, so it is raised immediately — it must NOT be
    consumed as a retryable transient (which would multiply the wait by the
    retry count).

    Mutation guard: classifying the worker-timeout as transient would retry
    it and the total elapsed would exceed the single-attempt bound × ~1.
    """
    import time as _t

    monkeypatch.setattr(
        "omnigent.onboarding.sandboxes.kubernetes._POD_READY_REQUEST_TIMEOUT_S", 0.2
    )
    # If the timeout were retried, the open would block _EXEC_NOT_READY_RETRIES
    # times; keep the fake block modest so a buggy retry is still observable
    # but the test stays fast.
    monkeypatch.setattr("omnigent.onboarding.sandboxes.kubernetes._EXEC_NOT_READY_BACKOFF_S", 0.0)
    launcher, pod_name = _provisioned(fake_k8s)
    fake_k8s.exec_open_blocks_s = 1.0

    started = _t.monotonic()
    with pytest.raises(click.ClickException, match="timed out opening exec stream"):
        launcher.run(pod_name, "true")
    elapsed = _t.monotonic() - started

    # One attempt only (~0.2s bound). Retrying 5× would be ≈1s+; assert well
    # under a single retry-multiplied window.
    assert elapsed < 0.6, f"open timeout looks retried (elapsed {elapsed:.2f}s)"


def test_run_parses_nonzero_exit_and_raises_when_checked(fake_k8s: _FakeK8sState) -> None:
    """
    A non-zero STATUS frame yields the real code; check=True (the managed
    default) raises with the command named.
    """
    launcher, pod_name = _provisioned(fake_k8s)
    fake_k8s.exec_channels = {
        3: (
            '{"metadata":{},"status":"Failure",'
            '"details":{"causes":[{"reason":"ExitCode","message":"3"}]}}'
        ),
    }

    with pytest.raises(click.ClickException, match="exit 3"):
        launcher.run(pod_name, "false")


def test_run_nonzero_error_redacts_secret_env_in_command(fake_k8s: _FakeK8sState) -> None:
    """
    FIX-3: a non-zero exec whose command carries OMNIGENT_HOST_TOKEN=...
    (how _start_host_in_sandbox launches the host) must NOT leak the token
    value into the raised error (it becomes an HTTP 502 body + server log).
    The key is kept (masked value) for diagnosability.

    Mutation guard: dropping the redaction puts the raw secret in the error.
    """
    launcher, pod_name = _provisioned(fake_k8s)
    fake_k8s.exec_channels = {
        3: (
            '{"metadata":{},"status":"Failure",'
            '"details":{"causes":[{"reason":"ExitCode","message":"1"}]}}'
        ),
    }
    secret = "oa_live_super_secret_token_value"
    command = (
        f"OMNIGENT_HOST_TOKEN={secret} ANTHROPIC_API_KEY=sk-ant-leakme "
        "setsid nohup omnigent host --server https://srv > /tmp/log 2>&1"
    )

    with pytest.raises(click.ClickException) as exc:
        launcher.run(pod_name, command)
    message = str(exc.value)
    # The secret VALUES are gone…
    assert secret not in message
    assert "sk-ant-leakme" not in message
    # …replaced by the masked form, keys preserved for diagnosis.
    assert "OMNIGENT_HOST_TOKEN=***" in message
    assert "ANTHROPIC_API_KEY=***" in message
    # Non-sensitive parts of the command survive.
    assert "omnigent host --server https://srv" in message


def test_run_stuck_error_redacts_secret_env_in_command(
    fake_k8s: _FakeK8sState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    FIX-3: the wedged-exec (idle/overall) error path also redacts secrets
    in the abandoned command.
    """
    # Trip the idle guard immediately via a fake clock + a stuck ws.
    monkeypatch.setattr("omnigent.onboarding.sandboxes.kubernetes.time.sleep", lambda _s: None)
    ticks = iter([0.0, 100_000.0, 200_000.0, 300_000.0])
    monkeypatch.setattr(
        "omnigent.onboarding.sandboxes.kubernetes.time.monotonic",
        lambda: next(ticks),
    )
    launcher, pod_name = _provisioned(fake_k8s)
    fake_k8s.exec_stuck = True
    secret = "oa_live_wedged_token"

    with pytest.raises(click.ClickException) as exc:
        launcher.run(pod_name, f"OMNIGENT_HOST_TOKEN={secret} omnigent host")
    message = str(exc.value)
    assert secret not in message
    assert "OMNIGENT_HOST_TOKEN=***" in message


@pytest.mark.parametrize(
    ("command", "secret", "expected_key"),
    [
        # FIX-B: single-quoted value WITH SPACES — the whole value must go,
        # not just its first word (the old non-space matcher leaked the rest).
        ("FOO_SECRET='abc def ghi' ls", "abc def ghi", "FOO_SECRET=***"),
        # FIX-B: double-quoted value with spaces.
        ('FOO_SECRET="abc def ghi" ls', "abc def ghi", "FOO_SECRET=***"),
        # FIX-B: a BARE credential key (no prefix) must match.
        ("TOKEN=bare_secret_value omnigent host", "bare_secret_value", "TOKEN=***"),
        ("KEY=another_bare_secret ls", "another_bare_secret", "KEY=***"),
        # The real launch shape — bare unquoted token.
        (
            "OMNIGENT_HOST_TOKEN=oa_live_realshape setsid nohup omnigent host",
            "oa_live_realshape",
            "OMNIGENT_HOST_TOKEN=***",
        ),
        # Case-insensitive key keyword, quoted value with spaces (value
        # fragments are distinctive so they can't collide with the key/args).
        ("my_password='zzz qqq vvv' run", "zzz qqq vvv", "my_password=***"),
        # FIX-4: a token at a shell separator boundary (`;`) must match.
        (";TOKEN=semicolon_secret run", "semicolon_secret", ";TOKEN=***"),
        ("a&&API_KEY=amp_secret", "amp_secret", "API_KEY=***"),
        # FIX-4: API_KEY (keyword as a full trailing segment) matches.
        ("API_KEY=apikey_secret run", "apikey_secret", "API_KEY=***"),
        # round-3 (final): underscore-edge keys — a LEADING `_SECRET`, a
        # TRAILING `SECRET_`, and an empty middle segment `MY__SECRET` (the
        # empty split piece is ignored) — all redact.
        ("_SECRET=lead_secret run", "lead_secret", "_SECRET=***"),
        ("SECRET_=trail_secret run", "trail_secret", "SECRET_=***"),
        ("MY__SECRET=dbl_secret run", "dbl_secret", "MY__SECRET=***"),
    ],
)
def test_redact_command_masks_whole_value(command: str, secret: str, expected_key: str) -> None:
    """
    FIX-B/FIX-4: ``_redact_command`` must mask the ENTIRE sensitive value —
    single-quoted, double-quoted (incl. embedded spaces), or bare — match
    bare credential key names, AND match a key at a shell separator
    boundary (`;`/`&`/`|`), leaving no part of the secret.

    Mutation guard: the old non-space value class leaks the post-space
    suffix; a prefix-only key class misses bare ``TOKEN=``; a `(?:^|\\s)`
    boundary misses ``;TOKEN=``.
    """
    redacted = _redact_command(command)
    # No fragment of the secret survives (split on whitespace so a partial
    # leak of any word is caught).
    for fragment in secret.split():
        assert fragment not in redacted, f"leaked {fragment!r} in {redacted!r}"
    assert expected_key in redacted


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # round-3 final FIX-3 — the LEAK case: a NON-sensitive `FOO=` value
        # must NOT swallow the adjacent `;TOKEN=leak` (the old `[^\s]+` value
        # class ate it, so the secret was never matched and leaked verbatim).
        ("FOO=bar;TOKEN=leak", "FOO=bar;TOKEN=***"),
        # OVER-redaction: the bare value stops at `;`, so the trailing
        # command survives (old behavior dropped `;echo hi`).
        ("TOKEN=abc;echo hi", "TOKEN=***;echo hi"),
        # Other shell separators (`&`, `|`) bound the value the same way.
        ("FOO=bar&TOKEN=x", "FOO=bar&TOKEN=***"),
        ("FOO=bar|TOKEN=x", "FOO=bar|TOKEN=***"),
        ("FOO=bar&&TOKEN=x echo", "FOO=bar&&TOKEN=*** echo"),
        # Redirection / subshell metacharacters also bound the bare value.
        ("TOKEN=abc>out", "TOKEN=***>out"),
        ("TOKEN=abc(x", "TOKEN=***(x"),
    ],
)
def test_redact_command_stops_value_at_shell_separators(command: str, expected: str) -> None:
    """
    round-3 final FIX-3: the bare value class stops at shell separators
    (`;&|()<>`), fixing BOTH (a) over-redaction (a value swallowing an
    adjacent command) and (b) a LEAK (a non-sensitive ``FOO=`` value
    swallowing a following ``;TOKEN=secret`` so the secret was never
    redacted). Asserts the exact output: secret masked, adjacent commands
    preserved.

    Mutation guard: the old `[^\\s]+` value class fails ``FOO=bar;TOKEN=leak``
    (leaks the secret) and ``TOKEN=abc;echo hi`` (drops ``;echo hi``).
    """
    assert _redact_command(command) == expected
    # Belt-and-suspenders: the literal secret never survives the leak case.
    if "leak" in command:
        assert "leak" not in _redact_command(command)


@pytest.mark.parametrize(
    "command",
    [
        # FIX-4: the keyword must be a FULL underscore-delimited segment, so
        # these substring matches are NOT redacted (no noisy over-masking).
        "MONKEY=banana run",
        "HOTKEY=ctrl+c run",
        "KEYBOARD_LAYOUT=us run",
        "TOKENIZER=gpt2 run",
        # Ordinary non-credential assignments + args are untouched.
        "FOO=keepme BAR=alsokeep omnigent host --server https://srv",
    ],
)
def test_redact_command_leaves_non_credential_keys_untouched(command: str) -> None:
    """
    FIX-4: a credential keyword that is only a SUBSTRING of a key segment
    (MONKEY, HOTKEY, KEYBOARD_LAYOUT, TOKENIZER) must NOT be redacted —
    redaction is boundary-aware, so the command is returned verbatim.

    Mutation guard: the old substring key class redacts MONKEY=/HOTKEY=.
    """
    assert _redact_command(command) == command


def test_redact_command_is_linear_time_on_killer_input() -> None:
    """
    FIX-3 (round-3): the redaction must be genuinely O(n). The KILLER shape
    is a boundary-anchored underscore run that CONTAINS a credential keyword
    segment but has NO trailing `=` — exactly what made the old overlapping
    `(?:[A-Za-z0-9]+_)* keyword (?:_[A-Za-z0-9]+)*` pattern explore O(n)
    partitions (measured ~22s at this size). The linear rewrite handles it
    in milliseconds.

    Mutation guard: restoring the overlapping segment-star regex blows this
    sub-second ceiling apart.
    """
    import time

    # Leading space = boundary; a_*4000 + key_*4000 = a long key-like run
    # with a `key` keyword segment; trailing `x`, NO `=` (forces the
    # backtracking search in the old pattern).
    killer = " " + "a_" * 4000 + "key_" * 4000 + "x"
    start = time.perf_counter()
    _redact_command(killer)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.5, f"redaction took {elapsed:.2f}s — catastrophic backtracking"


def test_run_nonzero_returns_when_unchecked(fake_k8s: _FakeK8sState) -> None:
    """check=False returns the failing result for the caller to inspect."""
    launcher, pod_name = _provisioned(fake_k8s)
    fake_k8s.exec_channels = {
        2: "boom\n",
        3: (
            '{"metadata":{},"status":"Failure",'
            '"details":{"causes":[{"reason":"ExitCode","message":"1"}]}}'
        ),
    }

    result = launcher.run(pod_name, "false", check=False)
    assert result.returncode == 1
    assert result.stderr == "boom\n"


def test_run_closes_websocket(fake_k8s: _FakeK8sState) -> None:
    """The exec websocket is always closed (no leaked connections)."""
    launcher, pod_name = _provisioned(fake_k8s)
    fake_k8s.exec_channels = {3: '{"metadata":{},"status":"Success"}'}

    launcher.run(pod_name, "true")

    [ws] = fake_k8s.ws_clients
    assert ws.closed is True


def test_run_wraps_api_error(fake_k8s: _FakeK8sState) -> None:
    """
    An exec ApiException (e.g. the Pod was deleted mid-run) surfaces the
    API reason through the launcher contract, not a raw client error.
    """
    launcher, pod_name = _provisioned(fake_k8s)
    fake_k8s.exec_raises = _FakeApiException(status=404, reason="Not Found")

    with pytest.raises(click.ClickException, match="Not Found"):
        launcher.run(pod_name, "true")


def test_run_raises_when_status_frame_missing(fake_k8s: _FakeK8sState) -> None:
    """
    An exec that yields no STATUS frame is a transport fault — raise (do
    not silently report success).
    """
    launcher, pod_name = _provisioned(fake_k8s)
    fake_k8s.exec_channels = {1: "some output\n"}

    with pytest.raises(click.ClickException, match="no status frame"):
        launcher.run(pod_name, "true")


def test_run_recovers_status_frame_delivered_at_socket_close(fake_k8s: _FakeK8sState) -> None:
    """
    A fast command's STATUS frame can land right as the socket closes, so
    the in-loop reads miss it. The post-loop ``update()`` flush must
    recover it — otherwise the FIRST call every launch makes
    (``printf %s "$HOME"``) would spuriously fail with "no status frame".

    The fake withholds the STATUS frame until an ``update()`` issued after
    the socket has closed, so this passes ONLY because run() performs that
    final flush.
    """
    launcher, pod_name = _provisioned(fake_k8s)
    # No STATUS in the in-loop channels — only stdout. The success STATUS
    # is revealed by the post-close flush.
    fake_k8s.exec_channels = {1: "/home/omnigent\n"}
    fake_k8s.exec_status_after_close = '{"metadata":{},"status":"Success"}'

    result = launcher.run(pod_name, 'printf %s "$HOME"')

    assert result.returncode == 0
    assert result.stdout == "/home/omnigent\n"
    # The post-close flush ran (update called again after the loop exited).
    [ws] = fake_k8s.ws_clients
    assert ws.update_calls >= 2


def test_run_retries_transient_container_not_found(
    fake_k8s: _FakeK8sState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The first ``pods/exec`` can race container-readiness and 404 with
    "container not found" (codex S2). ``run`` must retry on a short loop
    and succeed once the container is exec-ready, rather than failing the
    whole launch.
    """
    monkeypatch.setattr("omnigent.onboarding.sandboxes.kubernetes.time.sleep", lambda _s: None)
    launcher, pod_name = _provisioned(fake_k8s)
    # First two opens hit the transient race; the third succeeds.
    fake_k8s.exec_open_raises = [
        _FakeApiException(status=404, reason="Not Found", body="container not found in pod"),
        _FakeApiException(status=404, reason="Not Found", body="container is waiting to start"),
    ]
    fake_k8s.exec_channels = {1: "ok\n", 3: '{"metadata":{},"status":"Success"}'}

    result = launcher.run(pod_name, "echo hi")

    assert result.returncode == 0
    assert result.stdout == "ok\n"
    # Exactly one real exec ran (the two transient opens raised before
    # recording a call); the open was retried, not the command.
    assert len(fake_k8s.exec_calls) == 1


def test_run_exhausts_transient_retries_then_fails(
    fake_k8s: _FakeK8sState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A transient exec error that never clears within the retry window
    surfaces a clear failure (naming the attempts), not an infinite hang.
    """
    monkeypatch.setattr("omnigent.onboarding.sandboxes.kubernetes.time.sleep", lambda _s: None)
    launcher, pod_name = _provisioned(fake_k8s)
    # Always raise the transient error — more than the retry budget.
    fake_k8s.exec_open_raises = [
        _FakeApiException(status=404, reason="Not Found", body="container not found")
        for _ in range(10)
    ]

    with pytest.raises(click.ClickException, match="not exec-ready"):
        launcher.run(pod_name, "echo hi")


def test_run_does_not_retry_permanent_exec_error(
    fake_k8s: _FakeK8sState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A permanent exec failure (e.g. 403 Forbidden) must surface
    immediately with the RBAC hint — retrying it would only delay a
    clear, actionable error.
    """
    sleeps: list[float] = []
    monkeypatch.setattr("omnigent.onboarding.sandboxes.kubernetes.time.sleep", sleeps.append)
    launcher, pod_name = _provisioned(fake_k8s)
    fake_k8s.exec_raises = _FakeApiException(
        status=403, reason="Forbidden", body="pods/exec is forbidden"
    )

    with pytest.raises(click.ClickException, match="sandbox-runners"):
        launcher.run(pod_name, "echo hi")
    # No backoff sleeps — a permanent error is not retried.
    assert sleeps == []


def test_run_abandons_wedged_stream_with_output(
    fake_k8s: _FakeK8sState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A websocket that stays open but never delivers a STATUS frame must be
    abandoned by the read-loop safeguard (rather than looping forever),
    and the failure must include any buffered output.

    The idle guard is driven by a fake monotonic clock that jumps past
    the idle window, so the test is instant and does not depend on the
    (generous, production) real timeout — proving the guard fires without
    risking a cut-off of legitimately long commands.
    """
    from omnigent.onboarding.sandboxes import kubernetes as k8s_mod

    launcher, pod_name = _provisioned(fake_k8s)
    fake_k8s.exec_stuck = True

    # A monotonic clock that advances by more than the production idle
    # window on every call — so the guard trips within a couple of loop
    # iterations regardless of the (generous) real constant's value. Read
    # from the module constant so this can't silently drift if the window
    # is retuned.
    step = k8s_mod._EXEC_IDLE_TIMEOUT_S + 1.0
    counter = {"n": 0.0}

    def _fake_monotonic() -> float:
        value = counter["n"]
        counter["n"] += step
        return value

    monkeypatch.setattr("omnigent.onboarding.sandboxes.kubernetes.time.monotonic", _fake_monotonic)

    with pytest.raises(click.ClickException, match="no output"):
        launcher.run(pod_name, "sleep 999999")
    # The wedged stream was still closed (no leaked connection).
    [ws] = fake_k8s.ws_clients
    assert ws.closed is True


# ── terminate ───────────────────────────────────────────────


def test_terminate_deletes_with_zero_grace_period(fake_k8s: _FakeK8sState) -> None:
    """Terminate deletes the Pod with grace_period_seconds=0 (prompt)."""
    launcher, pod_name = _provisioned(fake_k8s)

    launcher.terminate(pod_name)

    assert fake_k8s.delete_calls == [(pod_name, 0)]
    assert pod_name not in fake_k8s.pods


def test_terminate_is_idempotent_on_404(fake_k8s: _FakeK8sState) -> None:
    """
    Deleting an already-gone Pod (404) is a no-op success — cleanup
    paths race the provider's own deletion.
    """
    launcher = KubernetesSandboxLauncher()
    fake_k8s.delete_raises = [_FakeApiException(status=404, reason="Not Found")]

    launcher.terminate("omnigent-gone-1")  # must not raise


def test_terminate_wraps_non_404_errors(fake_k8s: _FakeK8sState) -> None:
    """A non-404 delete failure surfaces the API reason."""
    launcher = KubernetesSandboxLauncher()
    fake_k8s.delete_raises = [_FakeApiException(status=500, reason="ServerError")]

    with pytest.raises(click.ClickException, match="ServerError"):
        launcher.terminate("omnigent-x-1")


def test_terminate_passes_request_timeout_to_delete(fake_k8s: _FakeK8sState) -> None:
    """
    round-3 FIX-3: terminate()'s delete is bounded by a non-None
    ``_request_timeout`` so a stalled apiserver can't block the managed
    teardown forever.

    Mutation guard: dropping the kwarg leaves the captured timeout None.
    """
    launcher, pod_name = _provisioned(fake_k8s)

    launcher.terminate(pod_name)

    assert fake_k8s.delete_request_timeouts
    assert all(t is not None for t in fake_k8s.delete_request_timeouts)


def test_terminate_handles_delete_timeout_without_raising(fake_k8s: _FakeK8sState) -> None:
    """
    round-3 FIX-3: a urllib3 timeout/connection error from terminate()'s
    delete is best-effort (logged, NOT raised) so the managed teardown
    caller can't hang or abort on a provider hiccup.

    Mutation guard: if terminate only caught ApiException, the urllib3
    timeout would propagate and break teardown.
    """
    from urllib3.exceptions import ReadTimeoutError

    launcher = KubernetesSandboxLauncher()
    fake_k8s.delete_raises = [
        ReadTimeoutError(pool=None, url="/api/v1/pods", message="delete timed out"),
    ]

    launcher.terminate("omnigent-x-1")  # must not raise / hang
    assert len(fake_k8s.delete_calls) == 1


def test_terminate_closes_api_client(fake_k8s: _FakeK8sState) -> None:
    """
    FIX-4: terminate() (the launcher's last op for a sandbox) closes the
    ApiClient's connection pool so a fresh per-op launcher can't leak
    sockets, and drops the cached handles.

    Mutation guard: removing the close leaves close_count at 0.
    """
    launcher, pod_name = _provisioned(fake_k8s)
    # provision built one ApiClient.
    [api_client] = fake_k8s.api_clients

    launcher.terminate(pod_name)

    assert api_client.close_count == 1
    # Cached handles dropped (a later op rebuilds lazily).
    assert launcher._api_client is None
    assert launcher._core is None


def test_terminate_closes_api_client_even_when_delete_fails(fake_k8s: _FakeK8sState) -> None:
    """
    FIX-4: the pool is freed even when the delete raises (the close is in a
    finally), and the close error never masks the delete error.
    """
    launcher, pod_name = _provisioned(fake_k8s)
    [api_client] = fake_k8s.api_clients
    fake_k8s.delete_raises = [_FakeApiException(status=500, reason="ServerError")]

    with pytest.raises(click.ClickException, match="ServerError"):
        launcher.terminate(pod_name)
    assert api_client.close_count == 1


def test_terminate_close_is_idempotent(fake_k8s: _FakeK8sState) -> None:
    """
    FIX-4: a double terminate (cleanup paths can race) is safe — the second
    call rebuilds a client (lazy) and closes it again without error.
    """
    launcher, pod_name = _provisioned(fake_k8s)

    launcher.terminate(pod_name)
    launcher.terminate(pod_name)  # must not raise

    # Each terminate built+closed its own ApiClient (lazy rebuild after the
    # first close nulled the cache).
    assert len(fake_k8s.api_clients) == 2
    assert all(c.close_count == 1 for c in fake_k8s.api_clients)


def test_provision_cleanup_closes_api_client_on_readiness_failure(
    fake_k8s: _FakeK8sState,
) -> None:
    """
    FIX-4: a failed launch (readiness fast-fail) is also the launcher's
    last op for that sandbox, so _best_effort_delete closes the pool too.
    """

    def _failed(name: str) -> _Pod:
        return _Pod(status=_PodStatus(phase="Failed"))

    fake_k8s.pod_factory = _failed
    with pytest.raises(click.ClickException):
        KubernetesSandboxLauncher().provision("managed-abc")
    [api_client] = fake_k8s.api_clients
    assert api_client.close_count == 1


def test_close_releases_pool_on_the_success_path(fake_k8s: _FakeK8sState) -> None:
    """
    FIX-A: the public ``close()`` lifecycle hook releases the pool of a
    launcher used for a SUCCESSFUL op (provision+run) and discarded without
    a terminate — the real leak (the server calls this in a finally).

    Mutation guard: a close() that doesn't release the pool leaves
    close_count at 0.
    """
    launcher, pod_name = _provisioned(fake_k8s)
    fake_k8s.exec_channels = {3: '{"metadata":{},"status":"Success"}'}
    launcher.run(pod_name, "true")  # successful op, no terminate
    [api_client] = fake_k8s.api_clients

    launcher.close()

    assert api_client.close_count == 1
    assert launcher._api_client is None
    assert launcher._core is None


def test_close_is_noop_when_no_client_built(fake_k8s: _FakeK8sState) -> None:
    """
    FIX-A: ``close()`` on a launcher that never loaded a client (no API
    call made) is a harmless no-op — no client to close, no error.
    """
    del fake_k8s
    launcher = KubernetesSandboxLauncher()

    launcher.close()  # must not raise
    launcher.close()  # idempotent


# ── real-client contract ────────────────────────────────────


def test_real_kubernetes_client_exposes_expected_exec_api() -> None:
    """
    Lock the real ``kubernetes`` client's exec API against the exact
    symbols + channel constants the launcher depends on.

    Every fake-SDK test above stubs this surface, so a future
    kubernetes-client bump could silently move/rename it and the fakes
    would keep passing while production broke. This test imports the REAL
    package (skipped only if it isn't installed) and asserts the contract,
    so such a break fails CI loudly. The package is in the ``kubernetes``
    extra, installed in this venv — so this runs, it does not skip.
    """
    pytest.importorskip("kubernetes")

    # Exec channel constants the launcher reads stdout/stderr/STATUS from.
    from kubernetes.stream.ws_client import (
        ERROR_CHANNEL,
        STDERR_CHANNEL,
        STDOUT_CHANNEL,
        WSClient,
    )

    assert STDOUT_CHANNEL == 1
    assert STDERR_CHANNEL == 2
    assert ERROR_CHANNEL == 3

    # The WSClient surface the run() read loop drives.
    for method in ("read_channel", "update", "is_open", "close"):
        assert callable(getattr(WSClient, method)), method
    # returncode is the property the launcher deliberately does NOT trust
    # (it parses the STATUS frame instead) — but its presence is part of
    # the contract we reason about, so assert it exists.
    assert isinstance(WSClient.returncode, property)

    # The streaming entry point + the exec method it wraps.
    from kubernetes.stream import stream

    assert callable(stream)
    from kubernetes.client import CoreV1Api

    assert callable(CoreV1Api.connect_get_namespaced_pod_exec)

    # The exception + config-exception types the launcher catches.
    from kubernetes.client.rest import ApiException

    assert issubclass(ApiException, Exception)
    from kubernetes.config.config_exception import ConfigException

    assert issubclass(ConfigException, Exception)

    # The config-loading + isolated-Configuration surface (codex S3).
    from kubernetes import config

    assert callable(config.load_incluster_config)
    assert callable(config.load_kube_config)
    from kubernetes.client import ApiClient, Configuration

    assert callable(Configuration)
    assert callable(ApiClient)


def test_real_wsclient_satisfies_exec_stream_protocol() -> None:
    """
    The real ``WSClient`` must structurally satisfy the launcher's
    internal ``_ExecStream`` Protocol — the typed seam ``_open_exec_stream``
    returns. If upstream renames a method, this fails (the Protocol is
    runtime-checkable here only for the assertion).
    """
    pytest.importorskip("kubernetes")
    from typing import Protocol, runtime_checkable

    from kubernetes.stream.ws_client import WSClient

    @runtime_checkable
    class _ExecStreamRuntime(Protocol):
        def is_open(self) -> bool: ...
        def update(self, timeout: float = ...) -> None: ...
        def read_channel(self, channel: int, timeout: float = ...) -> str: ...
        def close(self, **kwargs: object) -> None: ...

    # issubclass against a runtime_checkable Protocol checks method
    # presence — exactly the structural contract _open_exec_stream relies
    # on.
    assert issubclass(WSClient, _ExecStreamRuntime)


# ── registration ────────────────────────────────────────────


def test_available_providers_includes_kubernetes() -> None:
    """
    The provider is registered (its module exists in the build), so
    ``available_providers`` lists it — gating the CLI/config on its
    presence.
    """
    assert "kubernetes" in available_providers()
