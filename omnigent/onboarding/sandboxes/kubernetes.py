"""
Kubernetes sandbox launcher.

Implements the managed-launch subset of
:class:`~omnigent.onboarding.sandboxes.base.SandboxLauncher` for an
agent-runner Pod spawned on demand in a Kubernetes cluster. This module ships
in the OSS build; the official ``kubernetes`` Python client is an optional
dependency (``pip install 'omnigent[kubernetes]'``) imported lazily, so the
provider can be listed and the module probed without it.

The model is **entrypoint-as-host**: the Pod's container command IS
``omnigent host``. :meth:`~KubernetesSandboxLauncher.provision` only RESERVES
the Pod name (no Pod yet); :meth:`~KubernetesSandboxLauncher.start_host` then
creates the Pod — an init container prepares the workspace (``mkdir`` + optional
``git clone``) and the main container runs the host under a tiny PID-1 reaper,
which dials back over the existing managed launch-token tunnel. Because the host
is never started by ``exec``-ing into an already-running container, this launcher
needs no ``pods/exec`` rights and no exec transport — it implements only
``prepare`` / ``provision`` / ``start_host`` / ``terminate``.

Platform notes that shape this launcher:

- **Token via Secret.** The launch token rides a per-Pod Kubernetes Secret
  referenced by ``secretKeyRef`` — never the Pod spec, an exec request URI, or
  any audit-logged surface. Harness LLM credentials ride a pre-created Secret
  projected via ``envFrom`` (``sandbox.kubernetes.secret_name``).
- **Writable HOME.** The host image's WORKDIR is ``/root`` (root-owned), but
  the Pod runs as uid 1000 for least privilege, so ``$HOME`` would be
  unwritable. The Pod sets ``HOME`` to :data:`_HOME_DIR`, mounts an ``emptyDir``
  there shared by both containers, and ``fsGroup`` 1000 makes it group-writable.
- **PID-1 reaper.** The in-sandbox host re-parents orphaned runner processes to
  PID 1, so the container command is a tiny supervisor that spawns
  ``omnigent host``, reaps any children, and forwards SIGTERM for prompt,
  graceful termination.
- **Least privilege.** ``automountServiceAccountToken: false`` keeps the runner
  SA's (absent) rights out of the sandbox, the Pod runs as a non-root user,
  drops all capabilities, and disables privilege escalation. The root
  filesystem stays writable (the host writes ``/tmp`` and ``~/.omnigent``).
- **No CLI bootstrap / port forward.** Like Modal/Daytona/Islo, the launcher
  exists for server-managed hosts only.
"""

from __future__ import annotations

import contextlib
import importlib
import logging
import os
import re
import shlex
import time
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING, ClassVar, Literal

import click

from omnigent.host.identity import (
    HOST_ID_ENV_VAR,
    HOST_NAME_ENV_VAR,
    HOST_TOKEN_ENV_VAR,
)
from omnigent.onboarding.sandboxes.base import (
    DEFAULT_HOST_IMAGE,
    RemoteCommandResult,
    SandboxLauncher,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from kubernetes import client as k8s_client


_logger = logging.getLogger(__name__)


# ── Constants ──────────────────────────────────────────

HOST_IMAGE_ENV_VAR: str = "OMNIGENT_KUBERNETES_HOST_IMAGE"
"""Environment variable overriding
:data:`~omnigent.onboarding.sandboxes.base.DEFAULT_HOST_IMAGE` for Kubernetes
sandbox Pods (amd64-only)."""

NAMESPACE_ENV_VAR: str = "OMNIGENT_KUBERNETES_NAMESPACE"
"""Environment variable naming the namespace sandbox Pods are created in.
Defaults to :data:`_DEFAULT_NAMESPACE`. The ``sandbox.kubernetes.namespace``
config takes precedence."""

SANDBOX_SECRET_ENV_VAR: str = "OMNIGENT_KUBERNETES_SECRET"
"""Environment variable naming a pre-created Kubernetes ``Secret`` whose keys
are projected into every sandbox Pod via ``envFrom`` — the harness LLM
credentials and ``GIT_TOKEN``. The ``sandbox.kubernetes.secret_name`` config
takes precedence."""

SANDBOX_ENV_PASSTHROUGH_ENV_VAR: str = "OMNIGENT_KUBERNETES_SANDBOX_ENV"
"""Environment variable naming (comma-separated) the SERVER-process environment
variables whose values are injected as literal ``env`` into every sandbox Pod.
Prefer :data:`SANDBOX_SECRET_ENV_VAR` for credentials. The
``sandbox.kubernetes.env`` config takes precedence."""

SERVICE_ACCOUNT_ENV_VAR: str = "OMNIGENT_KUBERNETES_SERVICE_ACCOUNT"
"""Environment variable naming the (deliberately powerless) ServiceAccount
sandbox Pods run as. Defaults to :data:`_DEFAULT_SERVICE_ACCOUNT`. The
``sandbox.kubernetes.service_account`` config takes precedence."""

KUBECONFIG_ENV_VAR: str = "OMNIGENT_KUBERNETES_KUBECONFIG"
"""Environment variable naming an explicit kubeconfig path for the
out-of-cluster fallback. Ignored when in-cluster config loads."""

# Default namespace / ServiceAccount, matching the deploy overlay
# (deploy/kubernetes/overlays/sandbox-runners/). The default namespace is the
# DEDICATED runner namespace, where the overlay grants the server SA its scoped
# pods + secrets rights — creating runner Pods in the server namespace would 403
# and defeat the two-namespace blast-radius split.
_DEFAULT_NAMESPACE: str = "omnigent-sandboxes"
_DEFAULT_SERVICE_ACCOUNT: str = "omnigent-runner"

# Pod resource sizing. Matches the other launchers' 2 vCPU / 4 GiB ceiling; a
# low request keeps the Pod schedulable on modest nodes while the limit caps a
# runaway runner.
_SANDBOX_CPU_REQUEST: str = "500m"
_SANDBOX_CPU_LIMIT: str = "2"
_SANDBOX_MEMORY_REQUEST: str = "1Gi"
_SANDBOX_MEMORY_LIMIT: str = "4Gi"

# Labels stamped on every managed runner Pod + its token Secret, so an operator
# (or a future reconciler) can select omnigent-managed objects for GC.
_MANAGED_BY_LABEL: str = "app.kubernetes.io/managed-by"
_MANAGED_BY_VALUE: str = "omnigent"
_ROLE_LABEL: str = "omnigent.ai/role"
_ROLE_VALUE: str = "sandbox-host"

# Non-root identity the Pod runs as. uid/gid 1000 is the conventional first
# non-system user; fsGroup makes the HOME emptyDir group-writable.
_RUN_AS_UID: int = 1000
_RUN_AS_GID: int = 1000

# Writable HOME for the uid-1000 Pod (the image's /root is unwritable to it).
# A constant the launcher controls, so the workspace path is known without
# asking the sandbox. Mounted as an emptyDir shared by both containers.
_HOME_DIR: str = "/home/omnigent"

# Container names. The init container prepares the workspace; the main container
# runs the host. Single-sourced so the manifest and the log/diagnostic lookups
# name the same containers.
_CONTAINER_NAME: str = "host"
_INIT_CONTAINER_NAME: str = "workspace-prep"

# Pod-start wait budget, consumed inside start_host BEFORE the
# shared _wait_for_host_online poll, so a Pod that can't schedule / pull its
# image / clone its repo fails fast with a clear reason instead of as a generic
# online timeout. Kept tight; a cold image pull is the usual slow case.
_POD_READY_TIMEOUT_S: int = 90
_POD_READY_POLL_S: float = 2.0

# Per-request client timeout for the blocking calls. Without it a stalled
# apiserver socket blocks indefinitely and the wait deadline never fires.
_POD_READY_REQUEST_TIMEOUT_S: float = 10.0

# terminate() retries a transient (timeout/connection) delete a few times before
# giving up best-effort: Kubernetes Pods have no platform lifetime cap, so a
# delete that never lands orphans a running, credential-bearing Pod.
_POD_DELETE_MAX_ATTEMPTS: int = 3
_POD_DELETE_BACKOFF_S: float = 1.0

# Lines of container log tail surfaced in a start-failure message (e.g. the git
# clone error from the init container).
_LOG_TAIL_LINES: int = 20

# Container ``waiting.reason`` values that are genuinely terminal — the kubelet
# will NOT self-heal them, so the start wait fast-fails rather than burning the
# budget. Deliberately EXCLUDES ImagePull* (kubelet retries cold pulls / flaps)
# and Unschedulable (autoscalers trigger scale-up by leaving Pods Pending).
_FATAL_WAITING_REASONS: frozenset[str] = frozenset(
    {"InvalidImageName", "CreateContainerConfigError", "RunContainerError"}
)

# Credential key SEGMENTS (uppercase) that mark an env-passthrough name as
# sensitive: an operator must put credentials in the envFrom Secret, not literal
# Pod env (which lands in the Pod spec / etcd). A name matches iff one of its
# ``_``-delimited segments is in this set — so ``MONKEY`` / ``KEYBOARD`` do not.
_SENSITIVE_KEY_SEGMENTS: frozenset[str] = frozenset(
    {"TOKEN", "KEY", "SECRET", "PASSWORD", "CREDENTIAL", "CREDENTIALS"}
)

# Reserved env names the Pod sets itself — an env passthrough naming one is an
# operator error (a duplicate entry could shadow the writable-HOME emptyDir).
_RESERVED_ENV_NAMES: frozenset[str] = frozenset(
    {"HOME", "IS_SANDBOX", HOST_ID_ENV_VAR, HOST_NAME_ENV_VAR, HOST_TOKEN_ENV_VAR}
)

# PID-1 reaper, run as the host container's entrypoint. It spawns its argv
# (``omnigent host …``) as a child, forwards SIGTERM/SIGINT for prompt graceful
# shutdown, and loops os.wait() to reap every child — including runner processes
# the in-sandbox host re-parents to PID 1 — until the host child exits. Stdlib
# only, so it runs under the image's bare python3.
_REAPER_SRC: str = """\
import os, signal, subprocess, sys

child = subprocess.Popen(sys.argv[1:])


def _forward(signum, _frame):
    try:
        child.send_signal(signum)
    except ProcessLookupError:
        pass


signal.signal(signal.SIGTERM, _forward)
signal.signal(signal.SIGINT, _forward)

while True:
    try:
        pid, status = os.wait()
    except ChildProcessError:
        break
    if pid == child.pid:
        if os.WIFSIGNALED(status):
            sys.exit(128 + os.WTERMSIG(status))
        sys.exit(os.WEXITSTATUS(status))
"""

# RFC 1123 forms for Kubernetes object names. Mirror the parse-time validators
# in :mod:`omnigent.server.managed_hosts` (which validate config-sourced names);
# duplicated rather than imported so this launcher stays self-contained (no
# onboarding→server dependency) while validating its own ENV-VAR overrides,
# which bypass the config parser. RFC 1123 is fixed, so the copies cannot drift.
_DNS1123_LABEL_RE: re.Pattern[str] = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
_DNS1123_SUBDOMAIN_RE: re.Pattern[str] = re.compile(
    r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$"
)


# ── module helpers ─────────────────────────────────────


def _ensure_sdk() -> None:
    """
    Verify the Kubernetes client is importable, with an install hint when not.

    Called at the top of every launcher entry point because the client is an
    optional dependency — the base ``omnigent`` install does not pull it in.

    :raises click.ClickException: When the ``kubernetes`` package is absent.
    """
    try:
        importlib.import_module("kubernetes")
    except ImportError as exc:
        raise click.ClickException(
            "The Kubernetes client is required for the 'kubernetes' sandbox "
            "provider. Install it with `pip install 'omnigent[kubernetes]'`."
        ) from exc


def _env_name_is_sensitive(name: str) -> bool:
    """
    Whether an env var NAME looks like a credential — i.e. a ``_``-delimited
    segment is in :data:`_SENSITIVE_KEY_SEGMENTS` (case-insensitive).
    """
    segments = {seg.upper() for seg in name.split("_") if seg}
    return bool(segments & _SENSITIVE_KEY_SEGMENTS)


def _validate_k8s_name_env(
    value: str, *, env_var: str, kind: Literal["label", "subdomain"]
) -> None:
    """
    Validate an environment-variable-sourced Kubernetes object name.

    The config path validates names at parse time, but env-var overrides bypass
    that parser and flow straight into the Pod spec; validating here fails fast
    with a message naming the variable instead of an opaque apiserver 422.

    :param value: The resolved env-var value (already known non-empty).
    :param env_var: The variable the value came from, named in the error.
    :param kind: ``"label"`` (namespace, ≤63 chars, no dots) or ``"subdomain"``
        (Secret / ServiceAccount, dot-separated labels, ≤253 chars).
    :raises click.ClickException: When *value* is not a valid RFC 1123 name.
    """
    max_len, pattern, form = (
        (63, _DNS1123_LABEL_RE, "RFC 1123 DNS label")
        if kind == "label"
        else (253, _DNS1123_SUBDOMAIN_RE, "RFC 1123 DNS subdomain")
    )
    # fullmatch, not match: ``$`` also matches just before a trailing newline,
    # so match() would accept e.g. "ns\n" — exactly the apiserver-422 class this
    # guards against.
    if len(value) > max_len or not pattern.fullmatch(value):
        raise click.ClickException(
            f"environment variable {env_var} is not a valid Kubernetes name "
            f"({form}, max {max_len} chars): {value!r}"
        )


def _resolve_pod_resources(resources: dict[str, object] | None) -> dict[str, dict[str, str]]:
    """
    Merge a configured ``sandbox.kubernetes.resources`` block over the built-in
    defaults, producing the container ``resources`` mapping.

    Each tier and field is optional; an omitted field keeps the default. The
    config shape is validated at parse time, so this merge reads only the
    recognized string fields.

    :param resources: The configured block, or ``None`` for the defaults.
    :returns: A ``{"requests": {...}, "limits": {...}}`` mapping.
    """
    resolved: dict[str, dict[str, str]] = {
        "requests": {"cpu": _SANDBOX_CPU_REQUEST, "memory": _SANDBOX_MEMORY_REQUEST},
        "limits": {"cpu": _SANDBOX_CPU_LIMIT, "memory": _SANDBOX_MEMORY_LIMIT},
    }
    if not resources:
        return resolved
    for tier in ("requests", "limits"):
        tier_cfg = resources.get(tier)
        if isinstance(tier_cfg, dict):
            for field in ("cpu", "memory"):
                value = tier_cfg.get(field)
                if value is not None:
                    resolved[tier][field] = str(value)
    return resolved


def _new_pod_name(label: str) -> str:
    """
    Derive a DNS-label-safe Pod name from a human label.

    Lowercase, non-``[a-z0-9-]`` runs collapse to ``-``, leading/trailing ``-``
    stripped, empty falls back to ``host``, truncated to keep the full name
    within the 63-char DNS label limit, and a 6-hex random suffix guarantees
    uniqueness across relaunches of the same session.

    :param label: Human-readable label, e.g. ``"managed-a1b2c3d4"``.
    :returns: A Pod name like ``"omnigent-managed-a1b2c3d4-1a2b3c"``.
    """
    base = re.sub(r"[^a-z0-9-]+", "-", label.lower()).strip("-")
    base = re.sub(r"-+", "-", base) or "host"
    return f"omnigent-{base[:40]}-{uuid.uuid4().hex[:6]}"


def _token_secret_name(pod_name: str) -> str:
    """
    Name of the per-Pod launch-token Secret for *pod_name*.

    :param pod_name: The Pod name (≤63 chars), so the ``-token`` suffix keeps
        the Secret within the 253-char DNS subdomain limit.
    :returns: The Secret name, e.g. ``"omnigent-managed-a1b2c3d4-1a2b3c-token"``.
    """
    return f"{pod_name}-token"


def _render_workspace_prep_command(
    workspace: str,
    clone_dir: str | None,
    repo_url: str | None,
    repo_branch: str | None,
) -> list[str]:
    """
    Render the init container command that prepares the workspace.

    Creates ``<workspace>`` and, when a repository is requested, clones it into
    ``<clone_dir>`` BEFORE the host starts. Running in an init container means a
    clone failure terminates the init container non-zero — surfaced fast by the
    start wait with the git error as the container log tail — rather than
    silently leaving the host without its workspace.

    :param workspace: The workspace root to create, e.g. ``"/home/omnigent/workspace"``.
    :param clone_dir: Directory the clone lands in, or ``None`` for no clone.
    :param repo_url: Repository clone URL, or ``None`` for an empty workspace.
    :param repo_branch: Branch to clone (``--branch … --single-branch``), or
        ``None`` for the default branch.
    :returns: The ``["bash", "-lc", script]`` command.
    """
    script = f"set -e\nmkdir -p {shlex.quote(workspace)}\n"
    if repo_url is not None and clone_dir is not None:
        # ``--`` separates options from the (already-validated) URL so it can
        # never be parsed as a flag; --single-branch keeps branch-pinned clones
        # fast. Private repos authenticate via the image's GIT_TOKEN credential
        # helper (projected from the harness Secret).
        branch = (
            f"--branch {shlex.quote(repo_branch)} --single-branch "
            if repo_branch is not None
            else ""
        )
        script += f"git clone {branch}-- {shlex.quote(repo_url)} {shlex.quote(clone_dir)}\n"
    return ["bash", "-lc", script]


def _render_host_command(server_url: str) -> list[str]:
    """
    Render the main container command that runs ``omnigent host`` under the
    PID-1 reaper.

    ``exec`` replaces the login shell with the reaper (so it becomes PID 1, with
    the venv on PATH from the image's profile), and the reaper spawns its argv —
    ``omnigent host --server <url>`` — as its child. Identity + token reach the
    host through the Pod environment (literal env + the token ``secretKeyRef``),
    not this command.

    :param server_url: URL of this server the host dials back to.
    :returns: The ``["bash", "-lc", script]`` command.
    """
    script = (
        f"exec python3 -c {shlex.quote(_REAPER_SRC)} "
        f"omnigent host --server {shlex.quote(server_url)}"
    )
    return ["bash", "-lc", script]


def build_token_secret_manifest(
    *, secret_name: str, namespace: str, token: str
) -> dict[str, object]:
    """
    Build the per-Pod launch-token Secret manifest as a plain dict.

    The token rides this Secret (referenced by the Pod's ``secretKeyRef``)
    instead of the Pod spec, so it never lands in an audit-logged surface. The
    Secret is labeled like its Pod for GC and deleted alongside it by
    :meth:`KubernetesSandboxLauncher.terminate`.

    :param secret_name: The Secret name (see :func:`_token_secret_name`).
    :param namespace: Namespace the Secret is created in.
    :param token: The raw launch token (the apiserver base64-encodes
        ``stringData``).
    :returns: The Secret manifest dict.
    """
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": secret_name,
            "namespace": namespace,
            "labels": {_MANAGED_BY_LABEL: _MANAGED_BY_VALUE, _ROLE_LABEL: _ROLE_VALUE},
        },
        "type": "Opaque",
        "stringData": {HOST_TOKEN_ENV_VAR: token},
    }


def build_pod_manifest(
    *,
    pod_name: str,
    namespace: str,
    image: str,
    service_account: str,
    host_id: str,
    host_name: str,
    server_url: str,
    token_secret_name: str,
    harness_secret: str | None,
    env_literals: dict[str, str],
    node_selector: dict[str, str] | None,
    workspace: str,
    clone_dir: str | None = None,
    repo_url: str | None = None,
    repo_branch: str | None = None,
    resources: dict[str, object] | None = None,
) -> dict[str, object]:
    """
    Build the sandbox Pod manifest as a plain dict.

    Pure: no SDK import, no I/O — the manifest is a literal dict the caller
    hands to ``create_namespaced_pod``, which makes it the primary unit-test
    surface for every security / lifecycle decision baked into a sandbox Pod.

    The encoded design:

    - An **init container** (:data:`_INIT_CONTAINER_NAME`) creates the workspace
      and clones the repository; the **main container**
      (:data:`_CONTAINER_NAME`) runs ``omnigent host`` under the PID-1 reaper.
      Both share the writable-HOME ``emptyDir``.
    - ``restartPolicy: Never`` — a crashed host should not silently restart with
      a stale launch token; the managed machinery provisions a replacement.
    - ``automountServiceAccountToken: false`` — a compromised agent cannot reach
      the API with the runner SA.
    - The launch token is referenced via ``secretKeyRef`` (never in the spec);
      the host identity rides literal env; harness credentials are projected via
      ``envFrom`` when *harness_secret* is set.
    - Pod + container ``securityContext`` satisfy Pod Security "restricted"
      (runAsNonRoot uid/gid 1000, drop ALL caps, ``seccompProfile:
      RuntimeDefault``, no privilege escalation). The root filesystem stays
      writable (the host writes ``/tmp`` + ``~/.omnigent``).
    - ``kubernetes.io/arch: amd64`` is always enforced (the host image is
      amd64-only) and CANNOT be overridden by *node_selector*.

    :param pod_name: DNS-label-safe Pod name (see :func:`_new_pod_name`).
    :param namespace: Namespace the Pod is created in.
    :param image: Host image reference to run.
    :param service_account: ServiceAccount the Pod runs as.
    :param host_id: Server-chosen host identity, injected as literal env.
    :param host_name: Server-chosen host display name, injected as literal env.
    :param server_url: URL the host dials back to (baked into the host command).
    :param token_secret_name: Per-Pod Secret holding the launch token, projected
        via ``secretKeyRef``.
    :param harness_secret: Name of the harness-credentials Secret projected via
        ``envFrom``, or ``None`` for none.
    :param env_literals: Literal name → value env entries (the resolved
        server-env passthrough). Secrets ride *harness_secret*, not this map.
    :param node_selector: Extra node selector labels, or ``None``. Merged with
        the mandatory amd64 constraint, which always wins.
    :param workspace: Absolute workspace root created by the init container.
    :param clone_dir: Directory the clone lands in, or ``None`` for no clone.
    :param repo_url: Repository clone URL, or ``None`` for an empty workspace.
    :param repo_branch: Branch to clone, or ``None`` for the default branch.
    :param resources: Configured resources block, or ``None`` for the defaults.
    :returns: The Pod manifest dict.
    """
    pod_resources = _resolve_pod_resources(resources)
    container_security = {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
    }
    home_mount = [{"name": "home", "mountPath": _HOME_DIR}]

    init_container: dict[str, object] = {
        "name": _INIT_CONTAINER_NAME,
        "image": image,
        "workingDir": _HOME_DIR,
        "command": _render_workspace_prep_command(workspace, clone_dir, repo_url, repo_branch),
        "env": [{"name": "HOME", "value": _HOME_DIR}],
        "resources": pod_resources,
        "securityContext": container_security,
        "volumeMounts": home_mount,
    }
    if harness_secret:
        # The clone may need GIT_TOKEN (private repos) from the harness Secret.
        init_container["envFrom"] = [{"secretRef": {"name": harness_secret}}]

    host_env: list[dict[str, object]] = [
        {"name": "HOME", "value": _HOME_DIR},
        {"name": "IS_SANDBOX", "value": "1"},
        {"name": HOST_ID_ENV_VAR, "value": host_id},
        {"name": HOST_NAME_ENV_VAR, "value": host_name},
        {
            "name": HOST_TOKEN_ENV_VAR,
            "valueFrom": {"secretKeyRef": {"name": token_secret_name, "key": HOST_TOKEN_ENV_VAR}},
        },
    ]
    host_env.extend({"name": name, "value": value} for name, value in env_literals.items())

    host_container: dict[str, object] = {
        "name": _CONTAINER_NAME,
        "image": image,
        "workingDir": _HOME_DIR,
        "command": _render_host_command(server_url),
        "env": host_env,
        "resources": pod_resources,
        "securityContext": container_security,
        "volumeMounts": home_mount,
    }
    if harness_secret:
        host_container["envFrom"] = [{"secretRef": {"name": harness_secret}}]

    spec: dict[str, object] = {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        "serviceAccountName": service_account,
        # arch spread LAST so the amd64 invariant always wins — an operator
        # "kubernetes.io/arch" key cannot drop it (the host image is amd64-only).
        "nodeSelector": {**(node_selector or {}), "kubernetes.io/arch": "amd64"},
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": _RUN_AS_UID,
            "runAsGroup": _RUN_AS_GID,
            "fsGroup": _RUN_AS_GID,
            "fsGroupChangePolicy": "OnRootMismatch",
            "seccompProfile": {"type": "RuntimeDefault"},
        },
        "volumes": [{"name": "home", "emptyDir": {}}],
        "initContainers": [init_container],
        "containers": [host_container],
    }
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod_name,
            "namespace": namespace,
            "labels": {_MANAGED_BY_LABEL: _MANAGED_BY_VALUE, _ROLE_LABEL: _ROLE_VALUE},
        },
        "spec": spec,
    }


def _api_reason(exc: Exception) -> str:
    """
    Short human reason for an ``ApiException`` / urllib3 ``HTTPError``.

    :param exc: The raised exception.
    :returns: Its ``reason``, or its class name + message, or the class name.
    """
    reason = getattr(exc, "reason", None)
    if reason:
        return str(reason)
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _format_api_error(action: str, name: str, exc: k8s_client.ApiException) -> str:
    """
    Build a launcher-contract message for a Kubernetes ``ApiException``.

    Includes the HTTP reason and any response body, and adds an RBAC pointer on
    403 (the usual cause: the server ServiceAccount lacks the sandbox-manager
    Role) — the single most common misconfiguration of this provider.

    :param action: What was attempted, e.g. ``"create sandbox pod"``.
    :param name: The object the action targeted.
    :param exc: The raised ``ApiException``.
    :returns: The error message.
    """
    reason = getattr(exc, "reason", None) or "unknown error"
    message = f"Failed to {action} '{name}': {reason}"
    body = getattr(exc, "body", None)
    if body:
        message += f" ({body})"
    if getattr(exc, "status", None) == 403:
        message += (
            " — the server ServiceAccount likely lacks the sandbox-manager Role "
            "(pods, secrets); apply "
            "`kubectl apply -k deploy/kubernetes/overlays/sandbox-runners/`."
        )
    return message


def _pod_phase(pod: object) -> str | None:
    """
    Return the Pod's ``status.phase`` (e.g. ``"Running"``), or ``None``.

    :param pod: A ``V1Pod`` read from the API.
    :returns: The phase string, or ``None`` when status is absent.
    """
    status = getattr(pod, "status", None)
    return getattr(status, "phase", None) if status is not None else None


def _all_container_statuses(pod: object) -> list[object]:
    """
    Return the Pod's init + main container statuses as one list.

    :param pod: A ``V1Pod`` read from the API.
    :returns: Every container status, init first; empty when none are present.
    """
    status = getattr(pod, "status", None)
    if status is None:
        return []
    init = getattr(status, "init_container_statuses", None) or []
    main = getattr(status, "container_statuses", None) or []
    return [*init, *main]


def _terminal_failure(pod: object) -> tuple[str, str] | None:
    """
    Return ``(container_name, summary)`` for a container that has terminally
    failed, or ``None`` — so the start wait fast-fails instead of polling to the
    deadline.

    An init container that exited non-zero (e.g. the clone failed) wedges a
    ``restartPolicy: Never`` Pod forever; the host container terminating at all
    means the host died before coming online. Both are terminal.

    :param pod: A ``V1Pod`` read from the API.
    :returns: The failed container name + summary, or ``None``.
    """
    status = getattr(pod, "status", None)
    if status is None:
        return None
    for cs in getattr(status, "init_container_statuses", None) or []:
        terminated = getattr(getattr(cs, "state", None), "terminated", None)
        if terminated is not None and getattr(terminated, "exit_code", 0) != 0:
            reason = getattr(terminated, "reason", None) or "Error"
            return getattr(cs, "name", _INIT_CONTAINER_NAME), (
                f"workspace prep failed (exit {terminated.exit_code}, {reason})"
            )
    for cs in getattr(status, "container_statuses", None) or []:
        terminated = getattr(getattr(cs, "state", None), "terminated", None)
        if terminated is not None:
            code = getattr(terminated, "exit_code", "?")
            reason = getattr(terminated, "reason", None) or "Terminated"
            return getattr(cs, "name", _CONTAINER_NAME), (
                f"host container exited before coming online (exit {code}, {reason})"
            )
    return None


def _fatal_waiting_reason(pod: object) -> str | None:
    """
    Return a ``reason: message`` for a container in a genuinely terminal waiting
    state (see :data:`_FATAL_WAITING_REASONS`), or ``None``.

    :param pod: A ``V1Pod`` read from the API.
    :returns: The fatal detail, or ``None``.
    """
    for cs in _all_container_statuses(pod):
        waiting = getattr(getattr(cs, "state", None), "waiting", None)
        reason = getattr(waiting, "reason", None) if waiting is not None else None
        if reason in _FATAL_WAITING_REASONS:
            message = getattr(waiting, "message", None) or ""
            return f"{reason}: {message}".strip()
    return None


def _current_wait_reason(pod: object) -> str | None:
    """
    Return the reason the Pod is not yet running — a container ``waiting.reason``
    (e.g. ``ImagePullBackOff``) or an ``Unschedulable`` condition — for the
    timeout diagnosis.

    :param pod: A ``V1Pod`` read from the API.
    :returns: A short reason string, or ``None``.
    """
    for cs in _all_container_statuses(pod):
        waiting = getattr(getattr(cs, "state", None), "waiting", None)
        reason = getattr(waiting, "reason", None) if waiting is not None else None
        if reason:
            message = getattr(waiting, "message", None)
            return f"{reason}: {message}" if message else str(reason)
    status = getattr(pod, "status", None)
    for cond in getattr(status, "conditions", None) or []:
        if (
            getattr(cond, "type", None) == "PodScheduled"
            and getattr(cond, "status", None) == "False"
            and getattr(cond, "reason", None) == "Unschedulable"
        ):
            return f"Unschedulable: {getattr(cond, 'message', None) or ''}".strip()
    return None


class KubernetesSandboxLauncher(SandboxLauncher):
    """
    :class:`SandboxLauncher` for on-demand Kubernetes Pods.

    Server-managed only and entrypoint-as-host: :meth:`provision` reserves a Pod
    name, :meth:`start_host` creates a per-Pod token Secret and a Pod whose init
    container prepares the workspace and whose main container runs
    ``omnigent host``, and :meth:`terminate` deletes both. All transport rides the
    official ``kubernetes`` client's ``CoreV1Api`` built into an isolated
    :class:`~kubernetes.client.Configuration` (no global client-state mutation),
    preferring in-cluster ServiceAccount config and falling back to a kubeconfig.
    """

    provider: ClassVar[str] = "kubernetes"
    # Managed-only: no CLI bootstrap, no local→sandbox port forward.
    supports_cli_bootstrap: ClassVar[bool] = False
    supports_local_port_forward: ClassVar[bool] = False

    def __init__(
        self,
        *,
        image: str | None = None,
        namespace: str | None = None,
        env: Sequence[str] | None = None,
        secret_name: str | None = None,
        node_selector: dict[str, str] | None = None,
        service_account: str | None = None,
        kubeconfig: str | None = None,
        in_cluster: bool | None = None,
        resources: dict[str, object] | None = None,
    ) -> None:
        """
        Initialize the launcher.

        :param image: Host image reference — the ``sandbox.kubernetes.image``
            config. ``None`` resolves :data:`HOST_IMAGE_ENV_VAR` then
            :data:`~omnigent.onboarding.sandboxes.base.DEFAULT_HOST_IMAGE`.
        :param namespace: Namespace to create Pods in. ``None`` resolves
            :data:`NAMESPACE_ENV_VAR` then :data:`_DEFAULT_NAMESPACE`.
        :param env: Names of server-process environment variables to inject as
            literal env. ``None`` resolves :data:`SANDBOX_ENV_PASSTHROUGH_ENV_VAR`.
        :param secret_name: Kubernetes Secret to project via ``envFrom``.
            ``None`` resolves :data:`SANDBOX_SECRET_ENV_VAR` then no Secret.
        :param node_selector: Extra node selector labels merged with the
            mandatory ``kubernetes.io/arch: amd64`` constraint.
        :param service_account: ServiceAccount Pods run as. ``None`` resolves
            :data:`SERVICE_ACCOUNT_ENV_VAR` then :data:`_DEFAULT_SERVICE_ACCOUNT`.
        :param kubeconfig: Kubeconfig path for the out-of-cluster fallback.
            ``None`` resolves :data:`KUBECONFIG_ENV_VAR` then the ambient config.
        :param in_cluster: Force the config source: ``True`` in-cluster only,
            ``False`` kubeconfig only, ``None`` to try in-cluster then fall back.
        :param resources: ``sandbox.kubernetes.resources`` block, or ``None``
            for the built-in defaults.
        """
        self._image_ref = image
        self._namespace = namespace
        self._env_names = tuple(env) if env is not None else None
        self._secret_name = secret_name
        self._node_selector = dict(node_selector) if node_selector is not None else None
        self._service_account = service_account
        self._kubeconfig = kubeconfig
        self._in_cluster = in_cluster
        self._resources = resources
        self._core: k8s_client.CoreV1Api | None = None
        self._api_client: k8s_client.ApiClient | None = None

    # ── config / clients ────────────────────────────────────

    def _load_core(self) -> k8s_client.CoreV1Api:
        """
        Return the (lazily built) ``CoreV1Api``, loading cluster config into an
        isolated :class:`~kubernetes.client.Configuration`.

        The config never mutates the client library's global default: a fresh
        ``Configuration`` is created, in-cluster ServiceAccount config (primary)
        or a kubeconfig (fallback) is loaded into it, and an ``ApiClient`` is
        built around that instance. With ``in_cluster`` unset the in-cluster
        path is tried first and a ``ConfigException`` falls through to kubeconfig.

        :returns: The cached ``CoreV1Api`` bound to the isolated config.
        :raises click.ClickException: When neither config source is available.
        """
        if self._core is not None:
            return self._core
        from kubernetes import client, config

        cfg = client.Configuration()
        kubeconfig_path = self._kubeconfig or os.environ.get(KUBECONFIG_ENV_VAR) or None
        try:
            if self._in_cluster is True:
                config.load_incluster_config(client_configuration=cfg)
            elif self._in_cluster is False:
                config.load_kube_config(config_file=kubeconfig_path, client_configuration=cfg)
            else:
                try:
                    config.load_incluster_config(client_configuration=cfg)
                except config.ConfigException:
                    config.load_kube_config(config_file=kubeconfig_path, client_configuration=cfg)
        except config.ConfigException as exc:
            raise click.ClickException(
                "Could not load Kubernetes configuration for the 'kubernetes' "
                "sandbox provider. In-cluster, mount the server pod's "
                "ServiceAccount token; out of cluster, set a kubeconfig "
                f"(KUBECONFIG or {KUBECONFIG_ENV_VAR}). Underlying error: {exc}"
            ) from exc
        self._api_client = client.ApiClient(cfg)
        self._core = client.CoreV1Api(self._api_client)
        return self._core

    def _close_clients(self) -> None:
        """
        Close the cached ``ApiClient`` (its urllib3 ``PoolManager``) and drop
        the cached handles.

        A fresh launcher is built per managed op, so an unclosed pool leaks
        sockets. Idempotent and best-effort: a close error is swallowed so it
        can never mask the operation's result. The next ``_load_core`` rebuilds.
        """
        api_client = self._api_client
        self._api_client = None
        self._core = None
        if api_client is not None:
            with contextlib.suppress(Exception):
                api_client.close()

    # ── resolution helpers ──────────────────────────────────

    def _name_env_override(
        self, env_var: str, *, kind: Literal["label", "subdomain"]
    ) -> str | None:
        """
        Read and validate a Kubernetes-name environment-variable override.

        Config-supplied names are validated at parse time, but env-var overrides
        bypass that parser, so they are validated here before reaching the spec.

        :param env_var: The environment variable to read.
        :param kind: RFC 1123 form to enforce — ``"label"`` or ``"subdomain"``.
        :returns: The validated value, or ``None`` when unset/empty.
        :raises click.ClickException: When the value is not a valid RFC 1123 name.
        """
        value = os.environ.get(env_var)
        if not value:
            return None
        _validate_k8s_name_env(value, env_var=env_var, kind=kind)
        return value

    def _resolve_image(self) -> str:
        """
        Resolve the host image: constructor → env override → default.

        :returns: The image reference to run.
        """
        return self._image_ref or os.environ.get(HOST_IMAGE_ENV_VAR) or DEFAULT_HOST_IMAGE

    def _resolve_namespace(self) -> str:
        """
        Resolve the namespace: constructor → env override → default.

        :returns: The namespace to create Pods in.
        :raises click.ClickException: When the env-var override is invalid.
        """
        return (
            self._namespace
            or self._name_env_override(NAMESPACE_ENV_VAR, kind="label")
            or _DEFAULT_NAMESPACE
        )

    def _resolve_secret(self) -> str | None:
        """
        Resolve the harness Secret name: constructor → env override → ``None``.

        :returns: The Secret name to project, or ``None`` for none.
        :raises click.ClickException: When the env-var override is invalid.
        """
        return (
            self._secret_name
            or self._name_env_override(SANDBOX_SECRET_ENV_VAR, kind="subdomain")
            or None
        )

    def _resolve_service_account(self) -> str:
        """
        Resolve the ServiceAccount: constructor → env override → default.

        :returns: The ServiceAccount the Pod runs as.
        :raises click.ClickException: When the env-var override is invalid.
        """
        return (
            self._service_account
            or self._name_env_override(SERVICE_ACCOUNT_ENV_VAR, kind="subdomain")
            or _DEFAULT_SERVICE_ACCOUNT
        )

    def _resolve_sandbox_env(self) -> dict[str, str]:
        """
        Resolve the literal env vars to inject into created Pods.

        Explicit constructor names win; otherwise
        :data:`SANDBOX_ENV_PASSTHROUGH_ENV_VAR` (comma-separated) applies. Values
        come from the server's own environment — a configured name that is unset
        there fails loud (silently launching without it would surface much later
        as an opaque failure inside the sandbox).

        :returns: Name → value mapping for literal Pod ``env``.
        :raises click.ClickException: When a configured name is unset in the
            server environment, names a reserved variable, or looks like a
            credential (use ``sandbox.kubernetes.secret_name`` for those).
        """
        if self._env_names is not None:
            names: Sequence[str] = self._env_names
        else:
            names = [
                name.strip()
                for name in os.environ.get(SANDBOX_ENV_PASSTHROUGH_ENV_VAR, "").split(",")
                if name.strip()
            ]
        resolved: dict[str, str] = {}
        for name in names:
            if name in _RESERVED_ENV_NAMES:
                raise click.ClickException(
                    f"sandbox env passthrough names '{name}', which is reserved "
                    "by the kubernetes sandbox (the launcher sets it on every "
                    f"pod) — remove it from sandbox.kubernetes.env / "
                    f"{SANDBOX_ENV_PASSTHROUGH_ENV_VAR}."
                )
            if _env_name_is_sensitive(name):
                raise click.ClickException(
                    f"sandbox env passthrough names '{name}', which looks like a "
                    "credential — its value would be stored in the Pod spec (and "
                    "etcd). Put it in the Secret named by "
                    "sandbox.kubernetes.secret_name (projected via envFrom) "
                    "instead, or rename it if it is not actually sensitive."
                )
            value = os.environ.get(name)
            if value is None:
                raise click.ClickException(
                    f"sandbox env passthrough names '{name}' but it is not set in "
                    "the server's environment — set it (or remove it from "
                    f"sandbox.kubernetes.env / {SANDBOX_ENV_PASSTHROUGH_ENV_VAR})."
                )
            resolved[name] = value
        return resolved

    # ── lifecycle ───────────────────────────────────────────

    def prepare(self) -> None:
        """
        Local preflight: verify the Kubernetes client is installed.

        Cluster reachability is not pre-checked — the first
        :meth:`start_host` call surfaces a config/connection error with the same
        clear message and cleans up after itself, so a separate probe would only
        open a client pool with no later op to close it.

        :raises click.ClickException: When the client is not installed.
        """
        _ensure_sdk()

    def provision(self, name: str) -> str:
        """
        Reserve a Pod name for a managed launch — no Pod is created here.

        Entrypoint-as-host: the Pod (which boots running ``omnigent host``) is
        materialized by :meth:`start_host`, not here. ``provision`` only mints
        the DNS-label-safe Pod name, so the server can register the launch token
        against it BEFORE the Pod exists — closing the host dial-back race by
        construction.

        :param name: Human-readable label, e.g. ``"managed-a1b2c3d4"``.
        :returns: The reserved Pod name (see :func:`_new_pod_name`).
        """
        return _new_pod_name(name)

    def start_host(
        self,
        sandbox_id: str,
        *,
        token: str,
        host_id: str,
        host_name: str,
        server_url: str,
        repo_url: str | None = None,
        repo_branch: str | None = None,
        repo_name: str | None = None,
        on_stage: Callable[[str], None] | None = None,
    ) -> str:
        """
        Create the token Secret + runner Pod and wait for the host to start.

        The entrypoint-as-host override of
        :meth:`~omnigent.onboarding.sandboxes.base.SandboxLauncher.start_host`
        (there is no exec bootstrap): the Pod's init container creates
        ``<HOME>/workspace`` and clones the repository (when requested), and its
        main container runs ``omnigent host``, which dials back over the
        launch-token tunnel. Because the launcher controls ``HOME``
        (:data:`_HOME_DIR`), the workspace path is known without asking the
        sandbox. The pod-start wait fast-fails (with the container log tail) on a
        Pod that can't schedule, pull, or clone, BEFORE the shared online poll —
        so the failure reason survives the cleanup that deletes the Pod.

        :param sandbox_id: The Pod name from :meth:`provision`.
        :param token: The raw launch token, delivered via the per-Pod Secret.
        :param host_id: Server-chosen host identity.
        :param host_name: Server-chosen host display name.
        :param server_url: URL the host dials back to.
        :param repo_url: Repository clone URL, or ``None`` for an empty workspace.
        :param repo_branch: Branch to clone, or ``None`` for the default branch.
        :param repo_name: Directory the clone lands in, or ``None``.
        :param on_stage: Progress observer; invoked with ``"starting"``.
        :returns: The absolute in-sandbox workspace path (the cloned repository
            directory when *repo_url* is set).
        :raises click.ClickException: When creation fails or the host does not
            start in time.
        """
        _ensure_sdk()
        from kubernetes.client.rest import ApiException
        from urllib3.exceptions import HTTPError

        namespace = self._resolve_namespace()
        image = self._resolve_image()
        env_literals = self._resolve_sandbox_env()
        secret_name = _token_secret_name(sandbox_id)
        workspace = f"{_HOME_DIR}/workspace"
        clone_dir = f"{workspace}/{repo_name}" if repo_name else None
        if on_stage is not None:
            on_stage("starting")
        core = self._load_core()
        click.echo(
            f"▸ Creating Kubernetes pod '{sandbox_id}' in namespace '{namespace}' from {image}"
        )
        try:
            try:
                # Secret first so the Pod's secretKeyRef resolves immediately —
                # a Pod referencing a missing Secret would sit in
                # CreateContainerConfigError (which the start wait treats as
                # terminal).
                core.create_namespaced_secret(
                    namespace,
                    build_token_secret_manifest(
                        secret_name=secret_name, namespace=namespace, token=token
                    ),
                    _request_timeout=_POD_READY_REQUEST_TIMEOUT_S,
                )
                manifest = build_pod_manifest(
                    pod_name=sandbox_id,
                    namespace=namespace,
                    image=image,
                    service_account=self._resolve_service_account(),
                    host_id=host_id,
                    host_name=host_name,
                    server_url=server_url,
                    token_secret_name=secret_name,
                    harness_secret=self._resolve_secret(),
                    env_literals=env_literals,
                    node_selector=self._node_selector,
                    workspace=workspace,
                    clone_dir=clone_dir,
                    repo_url=repo_url,
                    repo_branch=repo_branch,
                    resources=self._resources,
                )
                core.create_namespaced_pod(
                    namespace, manifest, _request_timeout=_POD_READY_REQUEST_TIMEOUT_S
                )
            except (ApiException, HTTPError) as exc:
                # Tear down whatever landed (a created Secret, or a Pod the
                # apiserver accepted before the response failed) so a failed
                # create never leaks the token Secret or a running Pod.
                self._best_effort_delete(namespace, sandbox_id, secret_name)
                if isinstance(exc, ApiException):
                    raise click.ClickException(
                        _format_api_error("create sandbox pod", sandbox_id, exc)
                    ) from exc
                raise click.ClickException(
                    f"timed out creating Kubernetes pod '{sandbox_id}' ({_api_reason(exc)})"
                ) from exc

            try:
                self._wait_for_pod_running(namespace, sandbox_id)
            except BaseException:
                # Readiness failed (Unschedulable, ImagePull, clone error, …):
                # the host will never come online, so reap the Pod + Secret and
                # re-raise the diagnosed reason.
                self._best_effort_delete(namespace, sandbox_id, secret_name)
                raise
        finally:
            # start_host is the launcher's only API work on the launch path
            # (the online wait that follows polls the host store), so release
            # the connection pool here on both paths.
            self._close_clients()
        click.echo(f"  → pod '{sandbox_id}' is starting the host")
        return clone_dir or workspace

    def _wait_for_pod_running(self, namespace: str, pod_name: str) -> None:
        """
        Block until the Pod's main container is running, fast-failing on
        genuinely terminal states.

        ``phase == Running`` means every init container succeeded and the host
        container started — the handoff point to the shared online poll. The
        wait is patient on recoverable conditions (Pending / Unschedulable /
        ImagePull*, transient read errors) and fast-fails on terminal ones (Pod
        ``Failed``, a container terminated, non-self-healing config/image
        errors), surfacing recent events + the failed container's log tail.

        :param namespace: Namespace the Pod lives in.
        :param pod_name: The Pod to wait on.
        :raises click.ClickException: On a terminal state or timeout.
        """
        from kubernetes.client.rest import ApiException
        from urllib3.exceptions import HTTPError

        core = self._load_core()
        deadline = time.monotonic() + _POD_READY_TIMEOUT_S
        last_reason: str | None = None
        while True:
            try:
                pod = core.read_namespaced_pod(
                    pod_name, namespace, _request_timeout=_POD_READY_REQUEST_TIMEOUT_S
                )
            except ApiException as exc:
                # A definite client rejection (RBAC / Pod gone) fails fast; a
                # transient apiserver hiccup (5xx / 429) is polled until the
                # deadline.
                if exc.status in (401, 403, 404):
                    raise click.ClickException(
                        _format_api_error("read sandbox pod", pod_name, exc)
                    ) from exc
                last_reason = _api_reason(exc)
                if time.monotonic() >= deadline:
                    raise click.ClickException(
                        self._pod_failure_message(
                            namespace,
                            pod_name,
                            "could not be read before the "
                            f"{_POD_READY_TIMEOUT_S}s deadline ({last_reason})",
                        )
                    ) from exc
                time.sleep(_POD_READY_POLL_S)
                continue
            except HTTPError as exc:
                last_reason = _api_reason(exc)
                if time.monotonic() >= deadline:
                    raise click.ClickException(
                        self._pod_failure_message(
                            namespace,
                            pod_name,
                            "could not be read before the "
                            f"{_POD_READY_TIMEOUT_S}s deadline ({last_reason})",
                        )
                    ) from exc
                time.sleep(_POD_READY_POLL_S)
                continue

            phase = _pod_phase(pod)
            if phase == "Running":
                return
            failure = _terminal_failure(pod)
            if failure is not None:
                container, summary = failure
                raise click.ClickException(
                    self._pod_failure_message(
                        namespace, pod_name, summary, log_container=container
                    )
                )
            if phase == "Failed":
                raise click.ClickException(
                    self._pod_failure_message(
                        namespace,
                        pod_name,
                        "entered terminal phase 'Failed' before the host started",
                        log_container=_CONTAINER_NAME,
                    )
                )
            fatal = _fatal_waiting_reason(pod)
            if fatal is not None:
                raise click.ClickException(
                    self._pod_failure_message(
                        namespace, pod_name, f"container cannot start ({fatal})"
                    )
                )
            last_reason = _current_wait_reason(pod) or last_reason
            if time.monotonic() >= deadline:
                detail = f"; last reason: {last_reason}" if last_reason else ""
                raise click.ClickException(
                    self._pod_failure_message(
                        namespace,
                        pod_name,
                        f"did not start within {_POD_READY_TIMEOUT_S}s "
                        f"(last phase '{phase or 'unknown'}'{detail})",
                    )
                )
            time.sleep(_POD_READY_POLL_S)

    def _pod_failure_message(
        self,
        namespace: str,
        pod_name: str,
        summary: str,
        *,
        log_container: str | None = None,
    ) -> str:
        """
        Build a pod-start failure message with recent events, an optional
        container log tail, and a ``kubectl describe`` pointer.

        Events carry the scheduler/kubelet's own reason; the log tail carries
        the failed container's own output (e.g. the git clone error). Both are
        best-effort — a lookup that itself errors is omitted, never masking the
        real failure.

        :param namespace: Namespace the Pod lives in.
        :param pod_name: The failed Pod.
        :param summary: What went wrong.
        :param log_container: Container whose log tail to append, or ``None``.
        :returns: The full error message.
        """
        message = f"Kubernetes sandbox pod '{pod_name}' {summary}."
        events = self._recent_events(namespace, pod_name)
        if events:
            message += f" Recent events: {events}"
        if log_container is not None:
            tail = self._pod_log_tail(namespace, pod_name, log_container).strip()
            if tail:
                message += f" Container '{log_container}' log tail: {tail[-1500:]}"
        message += f" Inspect with `kubectl describe pod {pod_name} -n {namespace}`."
        return message

    def _recent_events(self, namespace: str, pod_name: str) -> str:
        """
        Return a compact ``reason: message`` summary of the Pod's recent events,
        or empty when none are available.

        :param namespace: Namespace the Pod lives in.
        :param pod_name: The Pod to fetch events for.
        :returns: A ``"; "``-joined summary, or ``""``.
        """
        from kubernetes.client.rest import ApiException
        from urllib3.exceptions import HTTPError

        try:
            event_list = self._load_core().list_namespaced_event(
                namespace,
                field_selector=f"involvedObject.name={pod_name}",
                _request_timeout=_POD_READY_REQUEST_TIMEOUT_S,
            )
        except (ApiException, HTTPError):
            return ""
        parts: list[str] = []
        for event in getattr(event_list, "items", None) or []:
            reason = getattr(event, "reason", None)
            message = getattr(event, "message", None)
            if reason or message:
                parts.append(f"{reason or '?'}: {message or ''}".strip())
        return "; ".join(parts)

    def _pod_log_tail(self, namespace: str, pod_name: str, container: str) -> str:
        """
        Return the last :data:`_LOG_TAIL_LINES` lines of a container's log, or
        empty when unavailable.

        :param namespace: Namespace the Pod lives in.
        :param pod_name: The Pod to read logs from.
        :param container: The container whose log to tail.
        :returns: The log tail, or ``""``.
        """
        from kubernetes.client.rest import ApiException
        from urllib3.exceptions import HTTPError

        try:
            log: str = self._load_core().read_namespaced_pod_log(
                pod_name,
                namespace,
                container=container,
                tail_lines=_LOG_TAIL_LINES,
                _request_timeout=_POD_READY_REQUEST_TIMEOUT_S,
            )
        except (ApiException, HTTPError):
            return ""
        return log

    def _best_effort_delete(self, namespace: str, pod_name: str, secret_name: str) -> None:
        """
        Delete a Pod and its token Secret, swallowing (and logging) any failure.

        Used to reap a partially-created or failed-to-start launch: the cleanup
        must not mask the original error, so a delete that itself errors only
        warns. A 404 means the object is already gone.

        :param namespace: Namespace the objects live in.
        :param pod_name: The Pod to delete.
        :param secret_name: The token Secret to delete.
        """
        from kubernetes.client.rest import ApiException
        from urllib3.exceptions import HTTPError

        core = self._load_core()

        def _warn(kind: str, detail: str) -> None:
            click.echo(
                f"  → warning: could not clean up {kind} for '{pod_name}': {detail}",
                err=True,
            )

        deletes: tuple[tuple[str, Callable[[], object]], ...] = (
            (
                "pod",
                lambda: core.delete_namespaced_pod(
                    pod_name,
                    namespace,
                    grace_period_seconds=0,
                    _request_timeout=_POD_READY_REQUEST_TIMEOUT_S,
                ),
            ),
            (
                "secret",
                lambda: core.delete_namespaced_secret(
                    secret_name,
                    namespace,
                    _request_timeout=_POD_READY_REQUEST_TIMEOUT_S,
                ),
            ),
        )
        for kind, delete in deletes:
            try:
                delete()
            except ApiException as exc:
                if getattr(exc, "status", None) != 404:
                    _warn(kind, _api_reason(exc))
            except HTTPError as exc:
                _warn(kind, _api_reason(exc))

    def terminate(self, sandbox_id: str) -> None:
        """
        Delete a sandbox Pod and its token Secret, releasing compute.

        Idempotent: an object that no longer exists (404) is success. Kubernetes
        Pods have no platform lifetime cap, so a transient timeout/connection
        error is retried a few bounded times before giving up best-effort — a
        straggler keeps its managed-by/role labels for a cluster GC sweep.

        :param sandbox_id: The Pod to delete.
        :raises click.ClickException: On an API delete failure other than
            not-found (a urllib3 timeout/connection error is logged best-effort,
            not raised — managed teardown must not hang on a stalled apiserver).
        """
        _ensure_sdk()

        namespace = self._resolve_namespace()
        secret_name = _token_secret_name(sandbox_id)
        try:
            for kind, name, delete in (
                (
                    "pod",
                    sandbox_id,
                    lambda: self._load_core().delete_namespaced_pod(
                        sandbox_id,
                        namespace,
                        grace_period_seconds=0,
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
                self._delete_with_retry(kind, name, delete)
        finally:
            # terminate() is the launcher's last op for a sandbox (a fresh
            # launcher is built per managed op) — release the connection pool.
            self._close_clients()

    def _delete_with_retry(self, kind: str, name: str, delete: Callable[[], object]) -> None:
        """
        Run *delete* with bounded retries on a transient timeout/connection
        error, treating 404 as success and never raising on a transient failure.

        :param kind: The object kind, for the warning, e.g. ``"pod"``.
        :param name: The object name, for the warning/error.
        :param delete: The zero-arg delete call.
        :raises click.ClickException: On an ``ApiException`` other than 404.
        """
        from kubernetes.client.rest import ApiException
        from urllib3.exceptions import HTTPError

        reason = ""
        for attempt in range(_POD_DELETE_MAX_ATTEMPTS):
            try:
                delete()
                return
            except ApiException as exc:
                if getattr(exc, "status", None) == 404:
                    return
                raise click.ClickException(_format_api_error(f"delete {kind}", name, exc)) from exc
            except HTTPError as exc:
                reason = _api_reason(exc)
            if attempt + 1 < _POD_DELETE_MAX_ATTEMPTS:
                time.sleep(_POD_DELETE_BACKOFF_S)
        click.echo(
            f"  → warning: could not delete Kubernetes {kind} '{name}' after "
            f"{_POD_DELETE_MAX_ATTEMPTS} attempts ({reason}); it may still exist "
            "and carries the omnigent managed-by/role labels for GC.",
            err=True,
        )

    # ── unsupported: no exec transport (the host is the Pod entrypoint) ──

    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        """
        Unsupported: the host runs as the Pod's entrypoint, so there is no
        exec-in transport.

        :param sandbox_id: Unused.
        :param command: Unused.
        :param check: Unused.
        :raises SandboxCapabilityError: Always.
        """
        raise self._capability_error(
            "run a command via exec — the host runs as the Pod entrypoint"
        )
