"""
agent-sandbox launcher.

Runs the same sandbox Pod as the :mod:`kubernetes` provider, but as an
`agent-sandbox <https://github.com/kubernetes-sigs/agent-sandbox>`_
``Sandbox`` custom resource (``agents.x-k8s.io/v1beta1``) instead of a
``batch/v1`` Job. Everything about the Pod is inherited unchanged from
:class:`KubernetesSandboxLauncher` (the entrypoint-as-host command, the PID-1
reaper, the launch-token ``secretKeyRef``, the restricted security context, the
workspace init container, operator PVC / Secret mounts); only the enclosing
workload kind and its lifecycle differ.

The reason to prefer it is reclamation. A Job caps its Pod with
``activeDeadlineSeconds`` (7 days), which is a *fixed* lifetime: an abandoned
sandbox holds a node slot for a week whether or not anything ever ran in it.
A ``Sandbox`` carries ``spec.shutdownTime``, an *absolute deadline the owner is
expected to keep pushing forward*, which turns the same field into an
inactivity timeout:

- :meth:`~KubernetesSandboxLauncher.start_host` stamps
  ``shutdownTime = now + window`` (:data:`DEFAULT_SHUTDOWN_WINDOW_S`).
- :meth:`AgentSandboxLauncher.keep_alive` pushes it forward, and the server
  calls that for as long as the sandbox has a live runner tunnel
  (:mod:`omnigent.server.managed_host_keepalive`).
- Once nothing is running, nothing refreshes the deadline, and the
  agent-sandbox controller tears the Pod down.

So an idle sandbox reclaims itself within one window, a busy one lives as long
as work keeps arriving, and the controller (not the Omnigent server) does the
reaping. Nothing here has to enumerate or babysit live Pods.

**Expiry is a suspend, not a delete.** ``shutdownPolicy: Retain`` keeps the
``Sandbox`` object and its claims when the deadline lapses; only the Pod, the
part that costs CPU and memory, goes away. And because the controller recomputes
expiry from ``spec.shutdownTime`` on every reconcile rather than latching it,
pushing that field forward brings the Pod back. That is the whole wake path:
:meth:`AgentSandboxLauncher.resume` clears the two things the server re-mints
and :meth:`_create_workload` patches the deadline forward, so the sandbox comes
back under the same id. Deleting a sandbox for good stays an explicit
:meth:`~AgentSandboxLauncher.terminate` (session delete, or the deployment's
inactive-sandbox reaper).

Set :data:`WORKSPACE_SIZE_ENV_VAR` and a suspend also preserves the *workspace*:
``$HOME`` moves from the Pod's ``emptyDir`` onto a per-sandbox claim, so files,
``~/.omnigent`` and harness caches survive. Without it a woken sandbox keeps its
conversation and host identity but starts from an empty workspace, matching the
``kubernetes`` provider.

Requires the agent-sandbox controller installed in the cluster and the server's
ServiceAccount granted ``sandboxes`` create/get/patch/delete (see
``deploy/kubernetes/overlays/sandbox-runners/role.yaml``). Configuration is read
from the same ``sandbox.kubernetes`` block as the Job provider, so switching
``sandbox.provider`` between the two needs no other config change.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, ClassVar

import click

from omnigent.onboarding.sandboxes.kubernetes import (
    _POD_READY_REQUEST_TIMEOUT_S,
    KubernetesSandboxLauncher,
    _api_reason,
    _ensure_sdk,
    _format_api_error,
    _token_secret_name,
)

if TYPE_CHECKING:
    from kubernetes import client as k8s_client


_logger = logging.getLogger(__name__)


# ── Constants ──────────────────────────────────────────

API_GROUP: str = "agents.x-k8s.io"
"""API group of the agent-sandbox CRDs."""

API_VERSION: str = "v1beta1"
"""Served version of the ``Sandbox`` CRD."""

SANDBOX_PLURAL: str = "sandboxes"
"""Plural resource name, as required by ``CustomObjectsApi``."""

SHUTDOWN_WINDOW_ENV_VAR: str = "OMNIGENT_AGENT_SANDBOX_SHUTDOWN_WINDOW_S"
"""Environment variable overriding :data:`DEFAULT_SHUTDOWN_WINDOW_S`."""

DEFAULT_SHUTDOWN_WINDOW_S: int = 3600
"""How far ahead of now ``spec.shutdownTime`` is set, in seconds.

This is effectively the sandbox's inactivity timeout: a sandbox with no live
runner is reclaimed within one window of its last refresh. It MUST stay
comfortably above :data:`omnigent.server.managed_host_keepalive._MIN_INTERVAL_S`
(the server's per-runner refresh rate) so a couple of missed or slow refreshes
cannot reclaim a busy sandbox. One hour against a 10-minute refresh leaves five
misses of headroom, and also covers the gap between a host starting and its
first session spawning a runner (no runner yet means no refresh yet).
"""


WORKSPACE_SIZE_ENV_VAR: str = "OMNIGENT_AGENT_SANDBOX_WORKSPACE_SIZE"
"""Environment variable enabling a durable workspace, as a Kubernetes quantity
(e.g. ``"20Gi"``). Unset means the workspace stays an ``emptyDir`` that dies with
the Pod, matching the ``kubernetes`` provider."""

STORAGE_CLASS_ENV_VAR: str = "OMNIGENT_AGENT_SANDBOX_STORAGE_CLASS"
"""Environment variable naming the ``StorageClass`` for the durable workspace.
Unset uses the cluster's default class. Ignored without
:data:`WORKSPACE_SIZE_ENV_VAR`."""

WORKSPACE_VOLUME_NAME: str = "home"
"""Volume name the workspace claim must use.

Not arbitrary: the agent-sandbox controller merges ``volumeClaimTemplates`` into
the Pod's volumes by NAME, replacing a match (StatefulSet semantics), and
:func:`~omnigent.onboarding.sandboxes.kubernetes.build_job_manifest` already
names the HOME ``emptyDir`` ``home`` and mounts it in both the init and host
containers. Matching that name swaps the ``emptyDir`` for the claim in both
containers with no manifest surgery, which is what makes ``$HOME`` (and with it
the workspace, ``~/.omnigent``, and harness caches) survive a suspend.
"""


MIN_SHUTDOWN_WINDOW_S: int = 1200
"""Floor on the resolved window, in seconds.

A window shorter than the server's keepalive refresh interval is a footgun that
looks like it works: the deadline lapses before anything ever pushes it forward,
so every sandbox suspends mid-run. A configured value below this is clamped up
rather than honoured.

Declared here rather than imported from
``omnigent.server.managed_host_keepalive._MIN_INTERVAL_S`` on purpose: this
module is in the onboarding layer and the server imports IT, so reading the
server's constant here would invert that dependency. The test suite pins this
floor at >= 2x that interval instead, so the two cannot drift apart silently.

To watch a suspend happen quickly in a lab, patch ``spec.shutdownTime`` into the
past directly (``kubectl patch sandbox … shutdownTime``) rather than shortening
the window below this floor.
"""


def resolve_workspace_volume() -> tuple[str, str | None] | None:
    """
    Resolve the durable-workspace claim from the environment.

    :returns: ``(size, storage_class)`` when a workspace size is configured,
        else ``None`` for the ephemeral ``emptyDir`` workspace.
    """
    size = os.environ.get(WORKSPACE_SIZE_ENV_VAR, "").strip()
    if not size:
        return None
    storage_class = os.environ.get(STORAGE_CLASS_ENV_VAR, "").strip() or None
    return size, storage_class


def resolve_shutdown_window_s() -> int:
    """
    Resolve the shutdown window from the environment, else the default.

    A non-positive or unparseable value falls through to the default rather
    than raising: a malformed knob must not make sandboxes unlaunchable, and a
    zero window would expire every sandbox at birth. A positive value below
    :data:`MIN_SHUTDOWN_WINDOW_S` is clamped up to it.

    :returns: The window in seconds, always >= :data:`MIN_SHUTDOWN_WINDOW_S`.
    """
    raw = os.environ.get(SHUTDOWN_WINDOW_ENV_VAR, "").strip()
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            _logger.warning(
                "ignoring %s=%r (not an integer); using %ss",
                SHUTDOWN_WINDOW_ENV_VAR,
                raw,
                DEFAULT_SHUTDOWN_WINDOW_S,
            )
        else:
            if parsed >= MIN_SHUTDOWN_WINDOW_S:
                return parsed
            if parsed > 0:
                _logger.warning(
                    "%s=%r is below the %ss floor (the server would not refresh a "
                    "deadline that short in time); using %ss",
                    SHUTDOWN_WINDOW_ENV_VAR,
                    raw,
                    MIN_SHUTDOWN_WINDOW_S,
                    MIN_SHUTDOWN_WINDOW_S,
                )
                return MIN_SHUTDOWN_WINDOW_S
            _logger.warning(
                "ignoring %s=%r (must be positive); using %ss",
                SHUTDOWN_WINDOW_ENV_VAR,
                raw,
                DEFAULT_SHUTDOWN_WINDOW_S,
            )
    return DEFAULT_SHUTDOWN_WINDOW_S


def _shutdown_time(window_s: int, *, now: datetime | None = None) -> str:
    """
    Render an absolute ``shutdownTime`` *window_s* ahead of now.

    :param window_s: Seconds ahead of *now*.
    :param now: Reference time, or ``None`` for the current UTC time.
    :returns: An RFC 3339 UTC timestamp, the format ``metav1.Time`` expects.
    """
    base = now or datetime.now(UTC)
    return (base + timedelta(seconds=window_s)).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_sandbox_manifest(
    job_manifest: dict[str, object],
    *,
    shutdown_time: str,
    workspace_volume: tuple[str, str | None] | None = None,
) -> dict[str, object]:
    """
    Convert a sandbox Job manifest into an agent-sandbox ``Sandbox`` manifest.

    Pure: a dict in, a dict out, which makes it the unit-test surface for the
    conversion. The Job's ``spec.template`` is already a full
    ``PodTemplateSpec`` (``{"metadata": {"labels": …}, "spec": …}``), which is
    exactly the shape of the CRD's ``spec.podTemplate``, so the Pod is carried
    over verbatim: there is no second copy of the Pod's security or
    credential decisions to keep in sync.

    The Job's own ``backoffLimit`` and ``activeDeadlineSeconds`` are dropped:
    the Pod's ``restartPolicy: OnFailure`` still restarts a crashed host in
    place, and the fixed deadline is replaced by the refreshable
    *shutdown_time*.

    ``shutdownPolicy: Retain`` (the CRD default) is what makes expiry a
    *suspend* rather than a delete: on expiry the controller tears down the Pod
    but keeps the ``Sandbox`` object and its claims, and because expiry is
    recomputed from ``spec.shutdownTime`` on every reconcile, pushing that
    field forward brings the Pod back. See :meth:`AgentSandboxLauncher.resume`.

    :param job_manifest: The manifest from
        :func:`~omnigent.onboarding.sandboxes.kubernetes.build_job_manifest`.
    :param shutdown_time: Absolute RFC 3339 expiry for ``spec.shutdownTime``.
    :param workspace_volume: ``(size, storage_class)`` to back ``$HOME`` with a
        per-sandbox claim that survives suspend, or ``None`` to leave the
        workspace on the Pod's ``emptyDir``. Note the CRD makes
        ``volumeClaimTemplates`` immutable after creation, so this is fixed for
        a sandbox's lifetime: turning it on later applies only to new sandboxes.
    :returns: The ``Sandbox`` manifest to hand to ``CustomObjectsApi``.
    """
    metadata = dict(job_manifest["metadata"])  # type: ignore[arg-type]
    template = job_manifest["spec"]["template"]  # type: ignore[index]
    spec: dict[str, object] = {
        "podTemplate": template,
        "operatingMode": "Running",
        "shutdownTime": shutdown_time,
        # Retain keeps the object and its claims when the deadline lapses, so
        # expiry suspends the sandbox instead of destroying it. The expensive
        # part (the Pod) still goes; the resumable part stays.
        "shutdownPolicy": "Retain",
    }
    if workspace_volume is not None:
        size, storage_class = workspace_volume
        claim: dict[str, object] = {
            # ReadWriteOnce: exactly one Pod ever mounts a sandbox's workspace.
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": size}},
        }
        if storage_class is not None:
            claim["storageClassName"] = storage_class
        spec["volumeClaimTemplates"] = [
            {"metadata": {"name": WORKSPACE_VOLUME_NAME}, "spec": claim}
        ]
    return {
        "apiVersion": f"{API_GROUP}/{API_VERSION}",
        "kind": "Sandbox",
        "metadata": metadata,
        "spec": spec,
    }


class AgentSandboxLauncher(KubernetesSandboxLauncher):
    """
    :class:`SandboxLauncher` backing managed hosts with agent-sandbox
    ``Sandbox`` custom resources.

    A lifecycle specialization of :class:`KubernetesSandboxLauncher`: the Pod,
    its credentials and its start-readiness polling are all inherited, and only
    the verbs that differ are overridden:

    - :meth:`_create_workload` creates a ``Sandbox`` rather than a Job, or
      patches an existing one back to life on a wake.
    - :meth:`_find_job_pod` reads the backing Pod by name (it is named after the
      ``Sandbox``), and treats one being torn down as absent.
    - :meth:`keep_alive` pushes ``shutdownTime`` forward.
    - :meth:`resume` clears the stale token Secret and Pod but keeps the
      ``Sandbox`` and its workspace claim.
    - :meth:`terminate` deletes the ``Sandbox``, cascading to its claims.
    """

    provider: ClassVar[str] = "agent_sandbox"
    workload_kind: ClassVar[str] = "sandbox"

    # ── clients ─────────────────────────────────────────────

    def _load_custom(self) -> k8s_client.CustomObjectsApi:
        """
        Return a ``CustomObjectsApi`` on the launcher's isolated config.

        Built fresh per call rather than cached: it is a stateless wrapper over
        the shared ``ApiClient`` (which owns the connection pool), so there is
        nothing extra for :meth:`_close_clients` to release.

        :returns: A ``CustomObjectsApi`` bound to the shared ``ApiClient``.
        :raises click.ClickException: When cluster config cannot be loaded.
        """
        from kubernetes import client

        self._load_clients()
        return client.CustomObjectsApi(self._api_client)

    # ── lifecycle ───────────────────────────────────────────

    def _create_workload(self, namespace: str, manifest: dict[str, object]) -> None:
        """
        Create the ``Sandbox`` custom resource, or wake the one already there.

        Both halves of the lifecycle land here, because the managed wake path
        calls :meth:`~KubernetesSandboxLauncher.start_host` again under the same
        sandbox id. A fresh launch creates; a wake finds the suspended object
        (409) and patches it back to life instead, which is what preserves the
        workspace claim. The patch deliberately omits ``volumeClaimTemplates``
        (immutable after creation) and carries the re-rendered ``podTemplate``,
        so a token-Secret or ``host_config`` change since the last run is picked
        up when the controller rebuilds the Pod.

        :param namespace: Namespace to create the ``Sandbox`` in.
        :param manifest: The manifest from ``build_job_manifest``.
        """
        from kubernetes.client.rest import ApiException

        body = build_sandbox_manifest(
            manifest,
            shutdown_time=_shutdown_time(resolve_shutdown_window_s()),
            workspace_volume=resolve_workspace_volume(),
        )
        custom = self._load_custom()
        try:
            custom.create_namespaced_custom_object(
                API_GROUP,
                API_VERSION,
                namespace,
                SANDBOX_PLURAL,
                body,
                _request_timeout=_POD_READY_REQUEST_TIMEOUT_S,
            )
            return
        except ApiException as exc:
            if getattr(exc, "status", None) != 409:
                raise
        spec = dict(body["spec"])  # type: ignore[arg-type]
        spec.pop("volumeClaimTemplates", None)
        click.echo(f"  → waking suspended agent-sandbox '{body['metadata']['name']}'")  # type: ignore[index]
        custom.patch_namespaced_custom_object(
            API_GROUP,
            API_VERSION,
            namespace,
            SANDBOX_PLURAL,
            body["metadata"]["name"],  # type: ignore[index]
            {"spec": spec},
            _request_timeout=_POD_READY_REQUEST_TIMEOUT_S,
        )

    def _find_job_pod(self, namespace: str, job_name: str) -> str | None:
        """
        Return the ``Sandbox``'s backing Pod name once it exists.

        The controller names the backing Pod after the ``Sandbox`` itself
        (``resolvePodName`` in its sandbox controller), so this is a direct read
        rather than the base class's ``job-name`` label lookup. Re-raises 401/403
        so an RBAC gap surfaces as itself instead of a readiness timeout.

        A Pod carrying a ``deletionTimestamp`` counts as absent. On a wake the
        previous Pod is being torn down under the same name, and reporting it
        ready would hand the caller a Pod that is about to vanish; the poll has
        to wait for the controller's replacement.

        :param namespace: Namespace the ``Sandbox`` lives in.
        :param job_name: The ``Sandbox`` name (also the Pod name).
        :returns: The Pod name, or ``None`` while no live Pod exists yet.
        :raises click.ClickException: On a 401/403 from the apiserver.
        """
        from kubernetes.client.rest import ApiException
        from urllib3.exceptions import HTTPError

        try:
            pod = self._load_core().read_namespaced_pod(
                job_name, namespace, _request_timeout=_POD_READY_REQUEST_TIMEOUT_S
            )
        except ApiException as exc:
            if getattr(exc, "status", None) in (401, 403):
                raise click.ClickException(
                    _format_api_error("read sandbox pod", job_name, exc)
                ) from exc
            # Every other status (404 "not created yet", but also a transient
            # 500/503) reads as "no pod yet" so the readiness poll retries until
            # its deadline. Deliberately not narrowed to 404: an apiserver blip
            # mid-wake should cost a retry, not fail the launch.
            return None
        except HTTPError:
            return None
        if getattr(getattr(pod, "metadata", None), "deletion_timestamp", None):
            return None
        return job_name

    def keep_alive(self, sandbox_id: str) -> None:
        """
        Push ``spec.shutdownTime`` one window into the future.

        Idempotent and cheap by design, a single-field JSON merge patch, so
        the server can call it on every runner-tunnel refresh. Soft-fails: a
        patch that does not land only shortens the sandbox's remaining life, so
        it is logged rather than raised, and a genuinely gone sandbox (404) is
        not an error at all.

        :param sandbox_id: The ``Sandbox`` to extend.
        """
        _ensure_sdk()
        from kubernetes.client.rest import ApiException
        from urllib3.exceptions import HTTPError

        namespace = self._resolve_namespace()
        window_s = resolve_shutdown_window_s()
        shutdown_time = _shutdown_time(window_s)
        try:
            self._load_custom().patch_namespaced_custom_object(
                API_GROUP,
                API_VERSION,
                namespace,
                SANDBOX_PLURAL,
                sandbox_id,
                {"spec": {"shutdownTime": shutdown_time}},
                _request_timeout=_POD_READY_REQUEST_TIMEOUT_S,
            )
        except ApiException as exc:
            if getattr(exc, "status", None) != 404:
                _logger.warning(
                    "could not extend agent-sandbox '%s' to %s: %s",
                    sandbox_id,
                    shutdown_time,
                    _api_reason(exc),
                )
        except HTTPError as exc:
            _logger.warning(
                "could not extend agent-sandbox '%s' to %s: %s",
                sandbox_id,
                shutdown_time,
                _api_reason(exc),
            )
        else:
            _logger.debug("extended agent-sandbox '%s' to %s", sandbox_id, shutdown_time)
        finally:
            self._close_clients()

    def terminate(self, sandbox_id: str) -> None:
        """
        Delete the ``Sandbox`` (cascading to its Pod) and its token Secret.

        Idempotent: a 404 on either object is success. Both deletes are always
        attempted, so a failure on one cannot leak the other: in particular a
        leaked token Secret would keep a valid launch token alive.

        :param sandbox_id: The ``Sandbox`` to delete.
        :raises click.ClickException: On an API delete failure other than
            not-found.
        """
        _ensure_sdk()
        namespace = self._resolve_namespace()
        secret_name = _token_secret_name(sandbox_id)
        first_error: click.ClickException | None = None
        try:
            for kind, name, delete in (
                (
                    "sandbox",
                    sandbox_id,
                    lambda: self._load_custom().delete_namespaced_custom_object(
                        API_GROUP,
                        API_VERSION,
                        namespace,
                        SANDBOX_PLURAL,
                        sandbox_id,
                        _request_timeout=_POD_READY_REQUEST_TIMEOUT_S,
                    ),
                ),
                (
                    "secret",
                    secret_name,
                    lambda: self._load_core().delete_namespaced_secret(
                        secret_name,
                        namespace,
                        _request_timeout=_POD_READY_REQUEST_TIMEOUT_S,
                    ),
                ),
            ):
                try:
                    self._delete_with_retry(kind, name, delete)
                except click.ClickException as exc:
                    if first_error is None:
                        first_error = exc
        finally:
            self._close_clients()
        if first_error is not None:
            raise first_error

    def resume(self, sandbox_id: str) -> None:
        """
        Prepare a suspended sandbox to be woken in place, keeping its workspace.

        Unlike the Job provider, this does NOT delete the sandbox. The
        ``Sandbox`` object and its workspace claim are exactly what a resume has
        to preserve, so only the two things
        :meth:`~KubernetesSandboxLauncher.start_host` is about to re-mint are
        cleared: the stale launch-token Secret (the wake path mints a fresh
        token, and the create would 409 on the old Secret) and the previous
        backing Pod, so the controller rebuilds it from the re-rendered
        template. Deleting a Pod that has finished or been left behind is also
        what makes the wake work at all: the controller does not replace a Pod
        that reached a terminal phase.

        The wake itself happens in :meth:`_create_workload`, which runs after
        the new Secret exists. Doing it here instead would let the controller
        recreate the Pod against a deleted Secret and stall it in
        ``CreateContainerConfigError`` until the new one appeared.

        Best-effort on both deletes: a sandbox that expired before its Pod was
        ever created has neither, and that is a normal wake, not a failure.

        :param sandbox_id: The sandbox id to wake.
        """
        _ensure_sdk()
        from kubernetes.client.rest import ApiException
        from urllib3.exceptions import HTTPError

        click.echo(f"▸ Resuming agent-sandbox '{sandbox_id}'")
        namespace = self._resolve_namespace()
        core = self._load_core()
        try:
            for kind, delete in (
                (
                    "token secret",
                    lambda: core.delete_namespaced_secret(
                        _token_secret_name(sandbox_id),
                        namespace,
                        _request_timeout=_POD_READY_REQUEST_TIMEOUT_S,
                    ),
                ),
                (
                    "pod",
                    lambda: core.delete_namespaced_pod(
                        sandbox_id, namespace, _request_timeout=_POD_READY_REQUEST_TIMEOUT_S
                    ),
                ),
            ):
                try:
                    delete()
                except ApiException as exc:
                    if getattr(exc, "status", None) != 404:
                        click.echo(
                            f"  → warning: could not clear stale {kind} for "
                            f"'{sandbox_id}': {_api_reason(exc)}",
                            err=True,
                        )
                except HTTPError as exc:
                    click.echo(
                        f"  → warning: could not clear stale {kind} for "
                        f"'{sandbox_id}': {_api_reason(exc)}",
                        err=True,
                    )
        finally:
            self._close_clients()
