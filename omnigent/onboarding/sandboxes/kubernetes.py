"""
Kubernetes sandbox launcher.

Implements the managed-launch subset of
:class:`~omnigent.onboarding.sandboxes.base.SandboxLauncher` for an
agent-runner Pod spawned on demand in a Kubernetes cluster. This module
ships in the OSS build; the official ``kubernetes`` Python client is an
optional dependency (``pip install 'omnigent[kubernetes]'``) imported
lazily, so the provider can be listed and the module probed without it.

The model is **Option A** (sleep-infinity + ``pods/exec``): ``provision``
creates a Pod that boots ``sleep infinity`` under a tiny PID-1 reaper and
waits for its container to become *ready*; the server then execs into it
(:meth:`KubernetesSandboxLauncher.run`) to start ``omnigent host``, which
dials back over the existing managed launch-token tunnel. The shared
``_start_host_in_sandbox`` orchestration is reused unchanged — the reason
Option A was chosen — so this launcher implements only
``prepare`` / ``provision`` / ``run`` / ``terminate``.

Platform notes that shape this launcher:

- **Writable HOME.** The host image's WORKDIR is ``/root`` (root-owned),
  but the Pod runs as uid 1000 for least privilege, so ``$HOME`` would
  be unwritable. The Pod therefore exposes a writable HOME: ``HOME`` is
  set to :data:`_HOME_DIR`, an ``emptyDir`` is mounted there,
  ``fsGroup`` 1000 makes it group-writable, and ``workingDir`` points at
  it. ``_start_host_in_sandbox`` (unchanged) reads ``$HOME`` and creates
  ``$HOME/workspace`` inside it.
- **PID-1 reaper.** A bare ``sleep infinity`` as PID 1 has no zombie
  reaper, but the in-sandbox host re-parents orphaned runner processes
  to PID 1. The Pod ``command`` is therefore a tiny supervisor that
  spawns ``sleep infinity``, reaps any children, and forwards SIGTERM
  for prompt, graceful termination.
- **Least privilege.** ``automountServiceAccountToken: false`` keeps the
  server ServiceAccount's ``pods/exec`` rights out of the sandbox, the
  Pod runs as a non-root user, and the container disables privilege
  escalation. The root filesystem stays writable (the host writes
  ``/tmp`` and ``~/.omnigent``).
- **No local port forwarding.** Like Modal/Daytona/Islo, the launcher
  exists for server-managed hosts only, so ``supports_cli_bootstrap`` /
  ``supports_local_port_forward`` stay ``False``.
- **Credentials via Secret.** Harness LLM credentials are attached as an
  ``envFrom`` reference to a pre-created Kubernetes Secret (named by
  ``sandbox.kubernetes.secret_name`` / :data:`SANDBOX_SECRET_ENV_VAR`),
  so secret values never transit the server config file. A small set of
  non-secret server-env values may additionally be injected by name via
  :data:`SANDBOX_ENV_PASSTHROUGH_ENV_VAR`.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import importlib
import os
import re
import shlex
import time
import uuid
from collections.abc import Sequence
from typing import TYPE_CHECKING, ClassVar

import click
import yaml

from omnigent.onboarding.sandboxes.base import (
    DEFAULT_HOST_IMAGE,
    RemoteCommandResult,
    SandboxLauncher,
)

if TYPE_CHECKING:
    from typing import Protocol

    from kubernetes import client as k8s_client

    class _ExecStream(Protocol):
        """
        The subset of ``kubernetes.stream.ws_client.WSClient`` that
        :meth:`KubernetesSandboxLauncher.run` drives.

        Declared as a Protocol because the client is a lazy optional
        import (so the real ``WSClient`` type is unavailable at module
        load and would only be ``Any`` under the ignore-missing-imports
        override). The real-client smoke test asserts the actual
        ``WSClient`` satisfies this exact surface, so the Protocol can't
        silently drift from the upstream API.
        """

        def is_open(self) -> bool: ...

        def update(self, timeout: float = ...) -> None: ...

        def read_channel(self, channel: int, timeout: float = ...) -> str: ...

        def close(self, **kwargs: object) -> None: ...


# ── Constants ──────────────────────────────────────────

HOST_IMAGE_ENV_VAR: str = "OMNIGENT_KUBERNETES_HOST_IMAGE"
"""Environment variable overriding
:data:`~omnigent.onboarding.sandboxes.base.DEFAULT_HOST_IMAGE` for
Kubernetes sandbox Pods, e.g. an org-internal copy of the host image
(``ghcr.io/<your-org>/omnigent-host:latest``). amd64-only."""

NAMESPACE_ENV_VAR: str = "OMNIGENT_KUBERNETES_NAMESPACE"
"""Environment variable naming the namespace sandbox Pods are created in.
Defaults to :data:`_DEFAULT_NAMESPACE` (``"omnigent-sandboxes"``, the
DEDICATED runner namespace the deploy overlay grants the server SA rights
in — creating Pods in the server's own namespace would 403 and defeat the
blast-radius split). The server's managed-host
``sandbox.kubernetes.namespace`` config takes precedence when set."""

SANDBOX_SECRET_ENV_VAR: str = "OMNIGENT_KUBERNETES_SECRET"
"""Environment variable naming a pre-created Kubernetes ``Secret`` whose
keys are projected into every sandbox Pod's environment via ``envFrom``
— typically the harness LLM credentials (``ANTHROPIC_API_KEY``,
``OPENAI_API_KEY``, …) and ``GIT_TOKEN`` the in-sandbox host forwards to
runners. Unset means no Secret is attached. The server's managed-host
``sandbox.kubernetes.secret_name`` config takes precedence when set."""

SANDBOX_ENV_PASSTHROUGH_ENV_VAR: str = "OMNIGENT_KUBERNETES_SANDBOX_ENV"
"""Environment variable naming (comma-separated) the SERVER-process
environment variables whose values are injected as literal ``env`` into
every sandbox Pod this launcher creates. Names, not values: the values
are read from the server's own environment at provision time, so they
never live in config files. Prefer :data:`SANDBOX_SECRET_ENV_VAR` for
actual credentials (Secret keys are not stored in the Pod spec); this is
for non-secret config a deployment wants threaded through. The server's
managed-host ``sandbox.kubernetes.env`` config takes precedence when
set."""

SERVICE_ACCOUNT_ENV_VAR: str = "OMNIGENT_KUBERNETES_SERVICE_ACCOUNT"
"""Environment variable naming the ServiceAccount sandbox Pods run as.
Defaults to :data:`_DEFAULT_SERVICE_ACCOUNT`. The sandbox SA needs no
API access (token automounting is disabled); it exists so cluster RBAC
can target sandbox Pods distinctly from the server. The server's
managed-host ``sandbox.kubernetes.service_account`` config takes
precedence when set."""

KUBECONFIG_ENV_VAR: str = "OMNIGENT_KUBERNETES_KUBECONFIG"
"""Environment variable naming an explicit kubeconfig file path for the
out-of-cluster fallback. Unset falls back to the ambient
``KUBECONFIG`` / ``~/.kube/config`` resolution. Ignored when the
launcher loads in-cluster ServiceAccount config (the primary path)."""

# Default namespace / ServiceAccount, matching the deploy overlay
# (deploy/kubernetes/overlays/sandbox-runners/). The default namespace is the
# DEDICATED runner namespace `omnigent-sandboxes` — NOT the server's namespace
# — because that is where the overlay grants the server SA its scoped pods +
# pods/exec rights (round-3 FIX-D); creating runner Pods in the server
# namespace would 403 and defeat the two-namespace blast-radius isolation.
_DEFAULT_NAMESPACE: str = "omnigent-sandboxes"
_DEFAULT_SERVICE_ACCOUNT: str = "omnigent-runner"

# Pod resource sizing. Matches the other launchers' 2 vCPU / 4 GiB
# ceiling (enough for a host running one interactive session); a low
# request keeps the Pod schedulable on modest homelab nodes while the
# limit caps a runaway runner.
_SANDBOX_CPU_REQUEST: str = "500m"
_SANDBOX_CPU_LIMIT: str = "2"
_SANDBOX_MEMORY_REQUEST: str = "1Gi"
_SANDBOX_MEMORY_LIMIT: str = "4Gi"

# Non-root identity the Pod runs as. uid/gid 1000 is the conventional
# first non-system user; fsGroup makes the HOME emptyDir group-writable.
_RUN_AS_UID: int = 1000
_RUN_AS_GID: int = 1000

# Writable HOME for the uid-1000 Pod (the image's /root is unwritable to
# it). Mounted as an emptyDir and exported as $HOME / workingDir.
_HOME_DIR: str = "/home/omnigent"

# The Pod's single container name. Single-sourced so build_pod_manifest and
# the pods/exec call name the same container (exec must target it explicitly
# on sidecar-injected clusters).
_CONTAINER_NAME: str = "host"

# Pod-ready wait budget. Consumed inside provision() BEFORE the shared
# _wait_for_host_online 120s poll, so it is kept tight; transient image
# pulls on a cold node are the usual reason a Pod takes the full window.
_POD_READY_TIMEOUT_S: int = 90
_POD_READY_POLL_S: float = 2.0

# Per-request client timeout for the blocking calls inside the ready wait
# (round-3 FIX-C). Without it, a stalled apiserver socket blocks the read
# indefinitely and the patient-wait deadline never fires. Kept short
# (a few poll intervals) so a hung request becomes a transient read error
# that the loop logs + retries until the readiness deadline, never a hang.
_POD_READY_REQUEST_TIMEOUT_S: float = 10.0

# Container ``waiting.reason`` values that are genuinely terminal — the
# kubelet will NOT self-heal them, so the ready wait fast-fails rather
# than burning the budget. Deliberately EXCLUDES ImagePull* / ErrImagePull
# (kubelet retries cold pulls / registry+network flaps / delayed pull
# creds — a transient ImagePullBackOff resolved itself in homelab testing)
# and Unschedulable (autoscaler/Karpenter trigger scale-up by leaving Pods
# Pending). Those are treated as recoverable and polled until the deadline.
_FATAL_WAITING_REASONS: frozenset[str] = frozenset(
    {
        "InvalidImageName",
        "CreateContainerConfigError",
        "RunContainerError",
    }
)

# HTTP status codes from ``read_namespaced_pod`` that are transient (the
# apiserver is briefly unavailable / overloaded). Logged and retried until
# the readiness deadline rather than aborting the launch. A 4xx other than
# these (e.g. 403 Forbidden, 404 deleted) is surfaced immediately.
_TRANSIENT_READ_STATUSES: frozenset[int] = frozenset({429, 500, 502, 503, 504})

# A ``create_namespaced_pod`` outcome is AMBIGUOUS — the apiserver may have
# accepted the Pod even though the call surfaced an error, so the known
# pod_name could be an orphan — ONLY for (round-3 final FIX-2):
#   * a urllib3 transport error/timeout (handled separately), or
#   * an ApiException with NO status, or a 5xx (500–599).
# Every DEFINITE client rejection (any 4xx — incl. 409 conflict, 415, 429,
# 400/403/404/422) means the Pod was NOT created, so it must NOT trigger a
# cleanup delete (that would delete another launch's Pod of the same name).
# See :func:`_create_outcome_ambiguous`.

# Credential key SEGMENTS (uppercase) that mark an env assignment as
# sensitive. A key is redacted iff one of its ``_``-delimited segments is in
# this set — so `TOKEN`, `API_KEY`, `FOO_SECRET`, `MY__SECRET` (empty segment
# ignored), `_SECRET`/`SECRET_` match, but `MONKEY`/`HOTKEY`/`KEYBOARD_LAYOUT`/
# `TOKENIZER` (keyword only a substring of a segment) do NOT.
_SENSITIVE_KEY_SEGMENTS: frozenset[str] = frozenset(
    {"TOKEN", "KEY", "SECRET", "PASSWORD", "CREDENTIAL"}
)

# Redaction for command strings before they enter error messages / logs
# (FIX-3; reworked round-3 to be genuinely LINEAR-time). _start_host_in_sandbox
# runs the host with `OMNIGENT_HOST_TOKEN=<launch token>` (and harness creds may
# ride other env assignments), so an exec timeout / non-zero must not leak the
# value into the error detail (which becomes an HTTP 502 body + server log).
#
# This matches an env assignment as THREE unambiguous, NON-overlapping runs —
#   group 1: the leading shell-token boundary (start / whitespace / `;`/`&`/`|`/`(`),
#   group 2: the whole key, a single `[A-Za-z0-9_]+` run (no nested/overlapping
#            quantifiers, so matching is O(n) — the previous segment-star
#            pattern was O(n²) and backtracked catastrophically when the `=`
#            was absent),
#   group 3: the value — a single-quoted run, a double-quoted run, OR a bare
#            run that STOPS at a shell separator (round-3 final FIX-3): the
#            value class `[^\s;&|()<>]+` excludes `;&|()<>` so the bare value
#            does NOT swallow an adjacent command. Without that, `TOKEN=abc;echo`
#            over-redacted `;echo`, and worse `FOO=bar;TOKEN=leak` let the
#            NON-sensitive `FOO=` match eat `;TOKEN=leak` so the secret was
#            never matched/redacted (a LEAK). A quoted value WITH SPACES is
#            still masked whole.
# Whether to redact is decided in Python (:func:`_redact_command`) by splitting
# the key on `_` and checking for a credential segment — keeping the regex
# linear and the keyword match boundary-aware.
_SENSITIVE_ENV_RE: re.Pattern[str] = re.compile(
    r"(^|[\s;&|(])([A-Za-z0-9_]+)=('[^']*'|\"[^\"]*\"|[^\s;&|()<>]+)"
)

# Reserved env names the Pod sets itself (writable-HOME contract + sandbox
# marker). A sandbox env passthrough that names one is an operator error:
# letting it through would emit a duplicate ``env`` entry and the kubelet's
# precedence is order-dependent, so a user-supplied HOME could shadow the
# emptyDir mount and break the writable-HOME guarantee. Rejected loud in
# :meth:`KubernetesSandboxLauncher._resolve_sandbox_env`.
_RESERVED_ENV_NAMES: frozenset[str] = frozenset({"HOME", "IS_SANDBOX"})

# Transient first-exec race (codex S2): a Pod can report container-ready a
# beat before the kubelet's exec endpoint can attach, so the first
# ``pods/exec`` may briefly 404 / "container not found". Retry on a short
# bounded loop before giving up; a genuinely deleted Pod still surfaces
# after the window.
_EXEC_NOT_READY_RETRIES: int = 5
_EXEC_NOT_READY_BACKOFF_S: float = 1.0

# Substrings (matched case-insensitively against an ApiException's reason +
# body) that mark the transient "container not ready for exec yet" race —
# as opposed to a permanent failure (forbidden, deleted-for-good, …).
_EXEC_TRANSIENT_MARKERS: tuple[str, ...] = (
    "container not found",
    "container not running",
    "is waiting to start",
    "podinitializing",
    "containercreating",
    "unable to upgrade connection",
)

# Exec read-loop safeguards against a websocket wedged open forever (it
# never delivers a STATUS frame). Both windows are deliberately GENEROUS —
# they are escape hatches, not deadlines, and MUST NOT fire for normal
# work. A legitimate ``git clone`` / ``pip``/``npm`` install of a large
# repo can take many minutes AND have multi-minute silent stretches (a
# single large object download, dependency resolution), so:
#   * the overall cap bounds total runtime at a generous ceiling, and
#   * the idle cap (no stdout/stderr/STATUS at all for this long) is set
#     well above any plausible quiet stretch of a real command, so only a
#     genuinely stuck stream — silent indefinitely — trips it.
_EXEC_OVERALL_TIMEOUT_S: float = 30 * 60.0
_EXEC_IDLE_TIMEOUT_S: float = 10 * 60.0
# Per-frame websocket poll. One second keeps the loop responsive to the
# idle/overall guards without busy-spinning.
_EXEC_POLL_S: float = 1.0

# PID-1 reaper run as the Pod's entrypoint (codex M3). It spawns
# ``sleep infinity`` as a child, forwards SIGTERM/SIGINT to it for prompt
# graceful shutdown, and loops os.wait() to reap every child (including
# runner processes the in-sandbox host re-parents to PID 1) until the
# sleep child exits. Kept dependency-free (stdlib only) so it runs under
# the image's bare python3.
_REAPER_SRC: str = """\
import os, signal, subprocess, sys

child = subprocess.Popen(["sleep", "infinity"])


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


def _ensure_sdk() -> None:
    """
    Verify the Kubernetes client is importable, with an install hint
    when not.

    Called at the top of every launcher entry point because the client
    is an optional dependency — the base ``omnigent`` install does not
    pull it in.

    :raises click.ClickException: When the ``kubernetes`` package is not
        installed.
    """
    # import_module (not a bare ``import``) is the presence probe: it
    # raises ImportError exactly like ``import`` when the package is
    # absent, but returns the module unbound so there is no unused name
    # to suppress.
    try:
        importlib.import_module("kubernetes")
    except ImportError as exc:
        raise click.ClickException(
            "The Kubernetes client is required for the 'kubernetes' "
            "sandbox provider. Install it with "
            "`pip install 'omnigent[kubernetes]'`."
        ) from exc


def build_pod_manifest(
    *,
    pod_name: str,
    namespace: str,
    image: str,
    service_account: str,
    harness_secret: str | None,
    env_literals: dict[str, str],
    node_selector: dict[str, str] | None,
) -> dict[str, object]:
    """
    Build the sandbox Pod manifest as a plain dict.

    Pure: no SDK import, no I/O — the manifest is a literal dict the
    caller hands to ``create_namespaced_pod`` (the client accepts a dict
    body), which makes it the primary unit-test surface for every
    security / lifecycle decision baked into a sandbox Pod.

    The encoded hardening:

    - ``restartPolicy: Never`` — a crashed host should not silently
      restart with a stale launch token; the managed machinery
      provisions a replacement.
    - ``automountServiceAccountToken: false`` — a compromised agent must
      not be able to reach the API with the server SA's ``pods/exec``
      rights (codex M4).
    - Pod ``securityContext`` runs as uid/gid 1000 with ``fsGroup`` 1000
      and ``fsGroupChangePolicy: OnRootMismatch`` (only chown the volume
      when needed — cheap on the small HOME emptyDir).
    - A writable HOME: an ``emptyDir`` mounted at :data:`_HOME_DIR`, with
      ``HOME`` exported and ``workingDir`` pointed at it (codex M2).
    - ``IS_SANDBOX=1`` so in-sandbox code can detect it runs in a
      managed sandbox.
    - ``envFrom`` projects the harness Secret's keys when one is
      configured (and is omitted entirely otherwise — an empty list is
      harmless but the absent key is cleaner).
    - The container disables ``allowPrivilegeEscalation`` but keeps the
      root filesystem writable (the host writes ``/tmp`` + ``~/.omnigent``).
    - The container ``command`` is the PID-1 reaper (codex M3).

    :param pod_name: DNS-label-safe Pod name (see :func:`_new_pod_name`).
    :param namespace: Namespace the Pod is created in.
    :param image: Host image reference to run.
    :param service_account: ServiceAccount the Pod runs as.
    :param harness_secret: Name of the Secret to project via ``envFrom``,
        or ``None`` for no attached Secret.
    :param env_literals: Literal name → value env entries to add (the
        resolved server-env passthrough). Secret values should ride
        *harness_secret* instead, not this map.
    :param node_selector: Extra node selector labels, or ``None`` for
        none. The mandatory ``kubernetes.io/arch: amd64`` constraint is
        always enforced and CANNOT be overridden here — the official host
        image is amd64-only, so an arch override would only schedule a Pod
        that fails at exec. Any ``kubernetes.io/arch`` key supplied here
        is ignored.
    :returns: The Pod manifest dict.
    """
    env: list[dict[str, str]] = [
        {"name": "HOME", "value": _HOME_DIR},
        {"name": "IS_SANDBOX", "value": "1"},
    ]
    env.extend({"name": name, "value": value} for name, value in env_literals.items())

    container: dict[str, object] = {
        "name": _CONTAINER_NAME,
        "image": image,
        "workingDir": _HOME_DIR,
        # PID-1 reaper (codex M3): a login shell so the image's
        # /etc/profile.d venv activation runs, then exec python3 so the
        # reaper becomes PID 1 (no intermediate bash to leak).
        "command": ["bash", "-lc", "exec python3 -c " + shlex.quote(_REAPER_SRC)],
        "env": env,
        "resources": {
            "requests": {
                "cpu": _SANDBOX_CPU_REQUEST,
                "memory": _SANDBOX_MEMORY_REQUEST,
            },
            "limits": {
                "cpu": _SANDBOX_CPU_LIMIT,
                "memory": _SANDBOX_MEMORY_LIMIT,
            },
        },
        # Not readOnlyRootFilesystem: the host writes /tmp and ~/.omnigent.
        "securityContext": {"allowPrivilegeEscalation": False},
        "volumeMounts": [{"name": "home", "mountPath": _HOME_DIR}],
    }
    if harness_secret:
        container["envFrom"] = [{"secretRef": {"name": harness_secret}}]

    spec: dict[str, object] = {
        "restartPolicy": "Never",
        "automountServiceAccountToken": False,
        "serviceAccountName": service_account,
        # arch is spread LAST so the amd64 invariant always wins — an
        # operator key "kubernetes.io/arch" cannot drop it (the host image
        # is amd64-only; an override would only schedule a Pod that segfaults
        # on the first exec).
        "nodeSelector": {**(node_selector or {}), "kubernetes.io/arch": "amd64"},
        "securityContext": {
            "runAsUser": _RUN_AS_UID,
            "runAsGroup": _RUN_AS_GID,
            "fsGroup": _RUN_AS_GID,
            "fsGroupChangePolicy": "OnRootMismatch",
        },
        "volumes": [{"name": "home", "emptyDir": {}}],
        "containers": [container],
    }

    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": pod_name,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/managed-by": "omnigent",
                "omnigent.ai/role": "sandbox-host",
            },
        },
        "spec": spec,
    }


def _new_pod_name(label: str) -> str:
    """
    Derive a DNS-label-safe Pod name from a human label.

    Mirrors :func:`omnigent.onboarding.sandboxes.islo._new_sandbox_name`:
    lowercase, non-``[a-z0-9-]`` runs collapse to ``-``, leading/trailing
    ``-`` stripped, empty falls back to ``host``, truncated to keep the
    full name within the 63-char DNS label limit, and a 6-hex random
    suffix guarantees uniqueness across relaunches of the same session.

    :param label: Human-readable label, e.g. ``"managed-a1b2c3d4"``.
    :returns: A Pod name like ``"omnigent-managed-a1b2c3d4-1a2b3c"``.
    """
    base = re.sub(r"[^a-z0-9-]+", "-", label.lower()).strip("-")
    base = re.sub(r"-+", "-", base) or "host"
    return f"omnigent-{base[:40]}-{uuid.uuid4().hex[:6]}"


def _parse_exec_status(status_frames: list[str], pod: str) -> int:
    """
    Parse the exit code from an exec STATUS frame (codex M1).

    The Kubernetes exec websocket reports the real exit status on the
    error channel (channel 3) as a serialized ``v1.Status`` object —
    ``WSClient.returncode`` is unreliable, so the STATUS frame is the
    source of truth. ``status: Success`` means exit 0; a failure carries
    the code in a ``details.causes[*]`` entry whose ``reason`` is
    ``ExitCode`` (its ``message`` is the integer code).

    :param status_frames: Raw error-channel text chunks collected from
        the exec stream.
    :param pod: Pod name, for the error message.
    :returns: The remote command's exit code.
    :raises RuntimeError: When the STATUS frame is missing, unparseable,
        or carries no exit code (a transport fault rather than a clean
        command exit — must not be silently treated as success).
    """
    raw = "".join(status_frames).strip()
    if not raw:
        raise RuntimeError(
            f"exec on pod '{pod}' returned no status frame — cannot "
            "determine the command's exit code"
        )
    try:
        status = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise RuntimeError(
            f"exec on pod '{pod}' returned an unparseable status frame: {raw!r}"
        ) from exc
    if not isinstance(status, dict):
        raise RuntimeError(f"exec on pod '{pod}' returned a non-object status frame: {raw!r}")
    if status.get("status") == "Success":
        return 0
    details = status.get("details")
    causes = details.get("causes") if isinstance(details, dict) else None
    if isinstance(causes, list):
        for cause in causes:
            if isinstance(cause, dict) and cause.get("reason") == "ExitCode":
                message = cause.get("message")
                try:
                    return int(str(message))
                except ValueError as exc:
                    raise RuntimeError(
                        f"exec on pod '{pod}' returned a non-integer exit "
                        f"code in its status frame: {message!r}"
                    ) from exc
    raise RuntimeError(f"exec on pod '{pod}' returned a status frame with no exit code: {raw!r}")


class KubernetesSandboxLauncher(SandboxLauncher):
    """
    :class:`SandboxLauncher` for on-demand Kubernetes Pods.

    Server-managed only: ``provision`` creates a Pod and waits for its
    container to be ready, ``run`` execs commands through
    ``pods/exec``, and ``terminate`` deletes the Pod. All transport
    rides the official ``kubernetes`` client's ``CoreV1Api`` built into
    an isolated :class:`~kubernetes.client.Configuration` (no mutation of
    global client state), preferring in-cluster ServiceAccount config and
    falling back to a kubeconfig out of cluster.
    """

    provider: ClassVar[str] = "kubernetes"
    # Managed-only provider: it implements just provision/run/terminate,
    # so the CLI bootstrap flow is unsupported.
    supports_cli_bootstrap: ClassVar[bool] = False
    # No local→sandbox port forward path (the in-sandbox App OAuth flow
    # would need one); managed servers that need it use another provider.
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
    ) -> None:
        """
        Initialize the launcher.

        :param image: Optional host image reference to run, e.g.
            ``"ghcr.io/me/omnigent-host:latest"`` — the server's
            ``sandbox.kubernetes.image`` config. ``None`` resolves
            :data:`HOST_IMAGE_ENV_VAR` and falls back to
            :data:`~omnigent.onboarding.sandboxes.base.DEFAULT_HOST_IMAGE`.
        :param namespace: Namespace to create Pods in — the server's
            ``sandbox.kubernetes.namespace`` config. ``None`` resolves
            :data:`NAMESPACE_ENV_VAR` and falls back to
            :data:`_DEFAULT_NAMESPACE` (the dedicated runner namespace
            ``omnigent-sandboxes``, matching the deploy overlay).
        :param env: Optional names of server-process environment
            variables to inject as literal env — the server's
            ``sandbox.kubernetes.env`` config. ``None`` resolves
            :data:`SANDBOX_ENV_PASSTHROUGH_ENV_VAR`.
        :param secret_name: Optional Kubernetes Secret to project via
            ``envFrom`` — the server's ``sandbox.kubernetes.secret_name``
            config. ``None`` resolves :data:`SANDBOX_SECRET_ENV_VAR` and
            falls back to no attached Secret.
        :param node_selector: Optional extra node selector labels merged
            with the mandatory ``kubernetes.io/arch: amd64`` constraint —
            the server's ``sandbox.kubernetes.node_selector`` config.
        :param service_account: Optional ServiceAccount Pods run as —
            the server's ``sandbox.kubernetes.service_account`` config.
            ``None`` resolves :data:`SERVICE_ACCOUNT_ENV_VAR` and falls
            back to :data:`_DEFAULT_SERVICE_ACCOUNT`.
        :param kubeconfig: Optional kubeconfig path for the
            out-of-cluster fallback. ``None`` resolves
            :data:`KUBECONFIG_ENV_VAR` then the ambient kubeconfig.
        :param in_cluster: Force the config source: ``True`` for
            in-cluster ServiceAccount only, ``False`` for kubeconfig
            only, ``None`` (default) to try in-cluster then fall back to
            kubeconfig.
        """
        self._image_ref = image
        self._namespace = namespace
        self._env_names = tuple(env) if env is not None else None
        self._secret_name = secret_name
        self._node_selector = dict(node_selector) if node_selector is not None else None
        self._service_account = service_account
        self._kubeconfig = kubeconfig
        self._in_cluster = in_cluster
        self._core: k8s_client.CoreV1Api | None = None
        self._api_client: k8s_client.ApiClient | None = None

    # ── config / clients ────────────────────────────────────

    def _load_core(self) -> k8s_client.CoreV1Api:
        """
        Return the (lazily built) ``CoreV1Api``, loading cluster config
        into an isolated :class:`~kubernetes.client.Configuration`
        (codex S3).

        The config never mutates the client library's global default
        configuration: a fresh ``Configuration`` is created, the
        in-cluster ServiceAccount config (primary) or a kubeconfig
        (fallback) is loaded INTO it, and an ``ApiClient`` is built
        around that instance. With ``in_cluster`` unset the in-cluster
        path is tried first and a :class:`~kubernetes.config.ConfigException`
        (no ServiceAccount mounted, i.e. running off-cluster) falls
        through to the kubeconfig path.

        :returns: The cached ``CoreV1Api`` bound to the isolated config.
        :raises click.ClickException: When neither config source is
            available, with remediation naming both paths.
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
                "Could not load Kubernetes configuration for the "
                "'kubernetes' sandbox provider. In-cluster, mount the "
                "server pod's ServiceAccount token; out of cluster, set "
                f"a kubeconfig (KUBECONFIG or {KUBECONFIG_ENV_VAR}). "
                f"Underlying error: {exc}"
            ) from exc
        self._api_client = client.ApiClient(cfg)
        self._core = client.CoreV1Api(self._api_client)
        return self._core

    def close(self) -> None:
        """
        Release the cached ``ApiClient``'s connection pool — the
        :class:`SandboxLauncher` lifecycle hook (round-3 FIX-A).

        A fresh launcher is built per managed op (launch / relaunch /
        teardown), so the server's ``launch_managed_host`` /
        ``relaunch_managed_host`` call this in a ``finally`` to release the
        pool on BOTH the success and the failure path (terminate() /
        _best_effort_delete() also call it). Idempotent and never raises.
        """
        self._close_clients()

    def _close_clients(self) -> None:
        """
        Close the cached ``ApiClient`` (its urllib3 ``PoolManager``) and
        drop the cached handles.

        A fresh launcher is built per managed op (launch / relaunch /
        teardown), so an unclosed pool leaks sockets. Idempotent (safe to
        call twice) and best-effort: a close error is swallowed so it can
        never mask the real operation's result. The next ``_load_core``
        rebuilds the client lazily.
        """
        api_client = self._api_client
        self._api_client = None
        self._core = None
        if api_client is not None:
            with contextlib.suppress(Exception):
                api_client.close()

    # ── resolution helpers ──────────────────────────────────

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
        """
        return self._namespace or os.environ.get(NAMESPACE_ENV_VAR) or _DEFAULT_NAMESPACE

    def _resolve_secret(self) -> str | None:
        """
        Resolve the harness Secret name: constructor → env override →
        ``None``.

        :returns: The Secret name to project, or ``None`` for none.
        """
        return self._secret_name or os.environ.get(SANDBOX_SECRET_ENV_VAR) or None

    def _resolve_service_account(self) -> str:
        """
        Resolve the ServiceAccount: constructor → env override →
        default.

        :returns: The ServiceAccount the Pod runs as.
        """
        return (
            self._service_account
            or os.environ.get(SERVICE_ACCOUNT_ENV_VAR)
            or _DEFAULT_SERVICE_ACCOUNT
        )

    def _resolve_sandbox_env(self) -> dict[str, str]:
        """
        Resolve the literal env vars to inject into created Pods.

        Explicit constructor names win; otherwise
        :data:`SANDBOX_ENV_PASSTHROUGH_ENV_VAR` (comma-separated)
        applies; an empty resolution injects nothing. Values come from
        the server's own environment — a configured name that is unset
        there fails loud (an operator listed a value the deployment
        never provided; silently launching without it would surface much
        later as an opaque failure inside the sandbox).

        :returns: Name → value mapping for literal Pod ``env``.
        :raises click.ClickException: When a configured name is not set
            in the server process environment, or names a reserved
            variable (:data:`_RESERVED_ENV_NAMES`) the Pod sets itself.
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
                # Passing HOME/IS_SANDBOX would emit a duplicate env entry
                # and could shadow the writable-HOME emptyDir — reject it
                # as an operator error rather than silently undermining the
                # contract.
                raise click.ClickException(
                    f"sandbox env passthrough names '{name}', which is reserved "
                    "by the kubernetes sandbox (the launcher sets it on every "
                    f"pod) — remove it from sandbox.kubernetes.env / "
                    f"{SANDBOX_ENV_PASSTHROUGH_ENV_VAR}."
                )
            value = os.environ.get(name)
            if value is None:
                raise click.ClickException(
                    f"sandbox env passthrough names '{name}' but it is not set "
                    "in the server's environment — set it (or remove it from "
                    f"sandbox.kubernetes.env / {SANDBOX_ENV_PASSTHROUGH_ENV_VAR})."
                )
            resolved[name] = value
        return resolved

    # ── lifecycle ───────────────────────────────────────────

    def prepare(self) -> None:
        """
        Local preflight: the client must be installed and the cluster
        reachable via in-cluster or kubeconfig config.

        :raises click.ClickException: When the client is missing or no
            usable configuration can be loaded.
        """
        _ensure_sdk()
        self._load_core()

    def provision(self, name: str) -> str:
        """
        Create a sandbox Pod from the host image and wait for it ready.

        The Pod boots ``sleep infinity`` under the PID-1 reaper; the
        pod-ready wait (:meth:`_wait_for_pod_ready`) consumes its budget
        here, BEFORE the shared ``_wait_for_host_online`` poll, so a Pod
        that can't schedule or pull its image fails fast with a clear
        reason rather than as a generic online timeout.

        :param name: Human-readable label, e.g. ``"managed-a1b2c3d4"``.
            Slugged into a DNS-safe Pod name; the returned name is the
            canonical reference.
        :returns: The created Pod's name.
        :raises click.ClickException: If creation fails or the Pod does
            not become ready in time.
        """
        _ensure_sdk()
        from kubernetes.client.rest import ApiException
        from urllib3.exceptions import HTTPError

        namespace = self._resolve_namespace()
        image = self._resolve_image()
        env_literals = self._resolve_sandbox_env()
        core = self._load_core()

        pod_name = _new_pod_name(name)
        click.echo(
            f"▸ Creating Kubernetes pod '{pod_name}' in namespace '{namespace}' from {image}"
        )
        for attempt in range(2):
            manifest = build_pod_manifest(
                pod_name=pod_name,
                namespace=namespace,
                image=image,
                service_account=self._resolve_service_account(),
                harness_secret=self._resolve_secret(),
                env_literals=env_literals,
                node_selector=self._node_selector,
            )
            try:
                # _request_timeout bounds the create so a stalled apiserver
                # can't hang provision() before the readiness deadline even
                # starts (round-3 FIX-1).
                core.create_namespaced_pod(
                    namespace, manifest, _request_timeout=_POD_READY_REQUEST_TIMEOUT_S
                )
                break
            except HTTPError as exc:
                # A urllib3 timeout/connection error is AMBIGUOUS: the
                # apiserver may have accepted the create (the Pod exists)
                # even though the client gave up — so pod_name (computed
                # before the call) could now be an orphan. Best-effort
                # delete it before failing so a client timeout can't leak a
                # running Pod, then raise a clear error.
                self._best_effort_delete(namespace, pod_name)
                raise click.ClickException(
                    f"timed out creating Kubernetes pod '{pod_name}' "
                    f"({_read_error_reason(exc)}); cleaned up any orphan and aborting"
                ) from exc
            except ApiException as exc:
                # A name collision (another launch raced the same slug)
                # is recoverable once: regenerate the random suffix and
                # retry.
                if exc.status == 409 and attempt == 0:
                    pod_name = _new_pod_name(name)
                    continue
                # Best-effort delete the known pod_name ONLY when the create
                # outcome is genuinely AMBIGUOUS (no status, or 5xx): the
                # apiserver may have accepted the Pod then failed the
                # response, orphaning it (round-3 final FIX-2). A DEFINITE
                # client rejection — any 4xx, INCLUDING a retry-exhausted
                # 409, 415, or 429 — means the Pod was NOT created, so we do
                # NOT delete (the pod_name could belong to another launch's
                # Pod).
                if _create_outcome_ambiguous(exc):
                    self._best_effort_delete(namespace, pod_name)
                raise click.ClickException(_format_api_error("create pod", pod_name, exc)) from exc

        # The Pod now exists. If readiness fails (Unschedulable,
        # ImagePullBackOff, timeout, …) provision() returns no sandbox id,
        # so the caller can never terminate() it — best-effort delete the
        # orphan here and re-raise the original failure.
        try:
            self._wait_for_pod_ready(namespace, pod_name)
        except BaseException:
            self._best_effort_delete(namespace, pod_name)
            raise
        click.echo(f"  → pod '{pod_name}' is ready")
        return pod_name

    def _best_effort_delete(self, namespace: str, pod_name: str) -> None:
        """
        Delete a Pod, swallowing (and logging) any failure.

        Used to reap a just-created Pod whose readiness wait failed: the
        cleanup must not mask the original error, so a delete that itself
        errors only warns. Mirrors :meth:`terminate`'s delete
        (``grace_period_seconds=0``); a 404 means the Pod is already gone.

        :param namespace: Namespace the Pod lives in.
        :param pod_name: The Pod to delete.
        """
        from kubernetes.client.rest import ApiException
        from urllib3.exceptions import HTTPError

        try:
            # _request_timeout bounds the cleanup delete so it can't itself
            # hang (round-3 FIX-2) — this runs on the failure path, often
            # after a readiness timeout, so it must be bounded AND swallow
            # urllib3 timeouts/connection errors, not just ApiException.
            self._load_core().delete_namespaced_pod(
                pod_name,
                namespace,
                grace_period_seconds=0,
                _request_timeout=_POD_READY_REQUEST_TIMEOUT_S,
            )
        except HTTPError as exc:
            click.echo(
                f"  → warning: could not clean up pod '{pod_name}' after a "
                f"failed readiness wait ({_read_error_reason(exc)})",
                err=True,
            )
        except ApiException as exc:
            if getattr(exc, "status", None) != 404:
                click.echo(
                    f"  → warning: could not clean up pod '{pod_name}' after a "
                    f"failed readiness wait: {_format_api_error('delete pod', pod_name, exc)}",
                    err=True,
                )
        finally:
            # A failed launch is the launcher's last op for this sandbox —
            # release the connection pool too (in a finally so it happens
            # even when the cleanup delete itself raised).
            self._close_clients()

    def _wait_for_pod_ready(self, namespace: str, pod_name: str) -> None:
        """
        Block until the Pod's ``host`` container is ready, fast-failing
        ONLY on genuinely terminal states (codex S2; tri-model FIX-2).

        Readiness — not merely ``phase == Running`` — gates the first exec
        (a container can be ``Running`` a beat before its process is up).
        The wait is **patient on recoverable conditions** so it doesn't
        abort what the cluster is busy resolving:

        - ``Pending`` / ``Unschedulable`` — the autoscaler/Karpenter
          trigger scale-up by leaving Pods Pending; poll until the deadline.
        - ``ImagePullBackOff`` / ``ErrImagePull`` / registry backoff — the
          kubelet retries cold pulls and registry/network/cred flaps; poll.
        - a transient :class:`ApiException` from ``read_namespaced_pod``
          (5xx / 429 / connection error) — log and keep polling.

        It fast-fails immediately ONLY on unrecoverable states: Pod phase
        ``Failed``, the ``host`` container terminated (a ``restartPolicy:
        Never`` Pod whose entrypoint already exited never becomes ready),
        and non-self-healing config errors (see
        :data:`_FATAL_WAITING_REASONS`). On a deadline timeout it surfaces
        the LATEST scheduler/kubelet events plus the current
        waiting/unschedulable reason so a genuinely stuck Pod still gives a
        clear diagnosis.

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
                # _request_timeout bounds the blocking socket so a stalled
                # apiserver can't hang past the readiness deadline (FIX-C).
                pod = core.read_namespaced_pod(
                    pod_name, namespace, _request_timeout=_POD_READY_REQUEST_TIMEOUT_S
                )
            except (ApiException, HTTPError) as exc:
                # Transient apiserver hiccups (5xx / 429 / connection error,
                # and a urllib3 request TIMEOUT from _request_timeout) must
                # not abort the launch — log and retry until the deadline. A
                # definite failure (403/404/…) surfaces immediately.
                if not _is_transient_read_error(exc):
                    raise click.ClickException(
                        _format_api_error("read pod", pod_name, exc)
                    ) from exc
                reason = _read_error_reason(exc)
                if time.monotonic() >= deadline:
                    raise click.ClickException(
                        self._pod_failure_message(
                            namespace,
                            pod_name,
                            "pod readiness could not be read before the "
                            f"{_POD_READY_TIMEOUT_S}s deadline ({reason})",
                        )
                    ) from exc
                click.echo(
                    f"  → transient error reading pod '{pod_name}' ({reason}); retrying",
                    err=True,
                )
                time.sleep(_POD_READY_POLL_S)
                continue

            phase = _pod_phase(pod)
            # ── Terminal / unrecoverable: fast-fail ──
            if phase == "Failed":
                raise click.ClickException(
                    self._pod_failure_message(
                        namespace,
                        pod_name,
                        "pod entered terminal phase 'Failed' before becoming ready",
                    )
                )
            terminated = _terminal_container_exit(pod)
            if terminated is not None:
                exit_code, reason = terminated
                raise click.ClickException(
                    self._pod_failure_message(
                        namespace,
                        pod_name,
                        f"host container exited before becoming ready "
                        f"(exit {exit_code}, {reason})",
                    )
                )
            fatal = _fatal_container_reason(pod)
            if fatal is not None:
                reason, message = fatal
                detail = f"{reason}: {message}" if message else reason
                raise click.ClickException(
                    self._pod_failure_message(
                        namespace, pod_name, f"container cannot start ({detail})"
                    )
                )

            # ── Ready: done ──
            if phase == "Running" and _container_ready(pod):
                return

            # ── Recoverable (Pending/Unschedulable/ImagePull*/…): keep
            #    polling, remembering the latest reason for the timeout
            #    diagnosis. ──
            last_reason = _current_wait_reason(pod) or last_reason
            if time.monotonic() >= deadline:
                detail = f"; last reason: {last_reason}" if last_reason else ""
                raise click.ClickException(
                    self._pod_failure_message(
                        namespace,
                        pod_name,
                        f"pod did not become ready within {_POD_READY_TIMEOUT_S}s "
                        f"(last phase '{phase or 'unknown'}'{detail})",
                    )
                )
            time.sleep(_POD_READY_POLL_S)

    def _pod_failure_message(self, namespace: str, pod_name: str, summary: str) -> str:
        """
        Build a pod-ready failure message, appending recent Pod events
        and a ``kubectl describe`` pointer.

        Events carry the scheduler/kubelet's own reason (Failed
        Scheduling, Failed pull, …), which is what an operator needs to
        diagnose the failure; best-effort, so an events lookup that
        itself errors is omitted rather than masking the real failure.

        :param namespace: Namespace the Pod lives in.
        :param pod_name: The failed Pod.
        :param summary: The failure summary (what went wrong).
        :returns: The full error message.
        """
        message = f"Kubernetes sandbox pod '{pod_name}' {summary}."
        events = self._recent_events(namespace, pod_name)
        if events:
            message += f" Recent events: {events}"
        message += f" Inspect with `kubectl describe pod {pod_name} -n {namespace}`."
        return message

    def _recent_events(self, namespace: str, pod_name: str) -> str:
        """
        Return a compact ``reason: message`` summary of the Pod's recent
        events, or empty when none are available.

        :param namespace: Namespace the Pod lives in.
        :param pod_name: The Pod to fetch events for.
        :returns: A ``"; "``-joined summary, or ``""``.
        """
        from kubernetes.client.rest import ApiException
        from urllib3.exceptions import HTTPError

        try:
            core = self._load_core()
            # Bounded by _request_timeout (FIX-C): this enrichment runs on the
            # failure path, so a stalled apiserver must not hang it past the
            # already-tripped deadline.
            event_list = core.list_namespaced_event(
                namespace,
                field_selector=f"involvedObject.name={pod_name}",
                _request_timeout=_POD_READY_REQUEST_TIMEOUT_S,
            )
        except (ApiException, HTTPError):
            # Best-effort diagnostic: a failed (or timed-out) events lookup
            # must never raise and mask the real readiness failure.
            return ""
        parts: list[str] = []
        for event in getattr(event_list, "items", None) or []:
            reason = getattr(event, "reason", None)
            message = getattr(event, "message", None)
            if reason or message:
                parts.append(f"{reason or '?'}: {message or ''}".strip())
        return "; ".join(parts)

    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        """
        Run a shell command in the Pod via ``pods/exec`` and capture its
        output (codex M1, S2, S5).

        The command runs under ``["bash", "-lc", command]`` so the
        image's login-shell venv activation puts ``omnigent`` on PATH
        (codex S5). The exec websocket is read with ``_preload_content=
        False``: STDOUT/STDERR are drained channel by channel, and the
        real exit code comes from the error-channel STATUS frame via
        :func:`_parse_exec_status` (``WSClient.returncode`` is
        unreliable).

        Opening the exec stream is retried on the transient first-exec
        race (the container reports ready a beat before the kubelet's exec
        endpoint can attach — codex S2); a genuinely missing Pod surfaces
        after the retry window.

        :param sandbox_id: Target Pod name.
        :param command: Shell command to execute remotely.
        :param check: When ``True``, raise on non-zero exit.
        :returns: Exit code plus captured stdout/stderr.
        :raises click.ClickException: If the exec transport fails, the
            stream wedges open without resolving, the status frame is
            unusable, or *check* is ``True`` and the command exits
            non-zero.
        """
        _ensure_sdk()
        from kubernetes.stream.ws_client import (
            ERROR_CHANNEL,
            STDERR_CHANNEL,
            STDOUT_CHANNEL,
        )

        # websocket-client (a kubernetes-client dependency) is the transport
        # under WSClient; its base exception guards the best-effort final
        # flush below alongside OSError.
        from websocket import WebSocketException

        ws = self._open_exec_stream(self._resolve_namespace(), sandbox_id, command)
        stdout_chunks: list[str] = []
        stderr_chunks: list[str] = []
        error_chunks: list[str] = []

        def _drain() -> bool:
            """Drain buffered channels; return True if any data arrived."""
            progressed = False
            out = ws.read_channel(STDOUT_CHANNEL)
            if out:
                stdout_chunks.append(out)
                click.echo(out, nl=False)
                progressed = True
            err = ws.read_channel(STDERR_CHANNEL)
            if err:
                stderr_chunks.append(err)
                click.echo(err, nl=False, err=True)
                progressed = True
            status = ws.read_channel(ERROR_CHANNEL)
            if status:
                error_chunks.append(status)
                progressed = True
            return progressed

        start = time.monotonic()
        last_progress = start
        try:
            while ws.is_open():
                ws.update(timeout=_EXEC_POLL_S)
                now = time.monotonic()
                if _drain():
                    last_progress = now
                # Safeguards against a websocket wedged open forever (it
                # never delivers a STATUS frame). Both windows are
                # generous so a legitimately long clone/install — which
                # streams output, refreshing last_progress — is never cut
                # off; these only catch a truly stuck stream.
                if now - last_progress > _EXEC_IDLE_TIMEOUT_S:
                    raise click.ClickException(
                        self._exec_stuck_message(
                            sandbox_id,
                            command,
                            f"produced no output for {_EXEC_IDLE_TIMEOUT_S:.0f}s",
                            stdout_chunks,
                            stderr_chunks,
                        )
                    )
                if now - start > _EXEC_OVERALL_TIMEOUT_S:
                    raise click.ClickException(
                        self._exec_stuck_message(
                            sandbox_id,
                            command,
                            f"did not complete within {_EXEC_OVERALL_TIMEOUT_S:.0f}s",
                            stdout_chunks,
                            stderr_chunks,
                        )
                    )
            # A fast command (e.g. the `printf %s "$HOME"` every launch runs
            # first) can have its STATUS frame buffered right as the socket
            # closes, so the last in-loop update() may miss it. Pull one
            # more frame, then drain — otherwise _parse_exec_status sees no
            # STATUS frame and raises a spurious "no status frame" error.
            # Best-effort: a closed socket makes update() a no-op, and a
            # transport error here would only mask the parse below, so the
            # realistic socket/websocket failures are swallowed.
            with contextlib.suppress(OSError, WebSocketException):
                ws.update(timeout=0)
            _drain()
        finally:
            ws.close()

        try:
            returncode = _parse_exec_status(error_chunks, sandbox_id)
        except RuntimeError as exc:
            raise click.ClickException(str(exc)) from exc

        stdout = "".join(stdout_chunks)
        stderr = "".join(stderr_chunks)
        if check and returncode != 0:
            raise click.ClickException(
                f"Remote command failed on pod '{sandbox_id}' (exit {returncode}): "
                f"{_redact_command(command)}"
            )
        return RemoteCommandResult(returncode=returncode, stdout=stdout, stderr=stderr)

    def _open_exec_stream(self, namespace: str, sandbox_id: str, command: str) -> _ExecStream:
        """
        Open the ``pods/exec`` websocket, retrying the transient
        first-exec race (codex S2) and bounding the open (round-3 FIX-1).

        A Pod can report its container ready a beat before the kubelet's
        exec endpoint can attach, so the first ``connect_get_namespaced_
        pod_exec`` may briefly fail with a "container not found" /
        "ContainerCreating" style :class:`ApiException`. Those are
        retried on a short bounded loop
        (:data:`_EXEC_NOT_READY_RETRIES` × :data:`_EXEC_NOT_READY_BACKOFF_S`);
        a permanent failure (forbidden, Pod deleted for good) is raised
        immediately, and a transient one that outlives the window is
        raised after it.

        The websocket OPEN itself is bounded by a worker-thread timeout
        (:data:`_POD_READY_REQUEST_TIMEOUT_S`): the kubernetes 36.0.2 client
        does NOT honor ``_request_timeout`` for the ``_preload_content=
        False`` connect (the underlying ``websocket.connect`` gets no
        socket timeout and the default is ``None``), so a stalled apiserver
        would otherwise block ``stream()`` forever. We run each open in a
        :class:`~concurrent.futures.ThreadPoolExecutor` and ``.result(
        timeout=...)``. A connect timeout is an apiserver stall, NOT the
        container-not-ready race, so it is raised (not retried as
        transient). The long-lived streaming reads are unaffected — they
        stay on the explicit select-based ``ws.update(timeout=...)`` in
        :meth:`run`, not on this connect bound.

        :param namespace: Namespace the Pod lives in.
        :param sandbox_id: Target Pod name.
        :param command: Shell command to execute remotely.
        :returns: The opened ``WSClient`` (``_preload_content=False``).
        :raises click.ClickException: When the stream cannot be opened
            (forbidden / deleted / not-ready-after-retries / open timeout).
        """
        from kubernetes.client.rest import ApiException

        last_exc: ApiException | None = None
        for attempt in range(_EXEC_NOT_READY_RETRIES):
            try:
                return self._open_exec_stream_once(namespace, sandbox_id, command)
            except concurrent.futures.TimeoutError as exc:
                # The connect stalled (apiserver hung), NOT the
                # container-not-ready race — surface it immediately rather
                # than burning the retry budget on a hung socket.
                raise click.ClickException(
                    f"timed out opening exec stream on pod '{sandbox_id}' after "
                    f"{_POD_READY_REQUEST_TIMEOUT_S:.0f}s"
                ) from exc
            except ApiException as exc:
                # The Pod may have been deleted mid-run (a racing
                # terminate), pods/exec may be forbidden, or the container
                # may just not be exec-ready yet. Only the last case is
                # retryable; surface the rest immediately.
                if not _is_transient_exec_error(exc):
                    raise click.ClickException(
                        _format_api_error("exec in pod", sandbox_id, exc)
                    ) from exc
                last_exc = exc
                if attempt < _EXEC_NOT_READY_RETRIES - 1:
                    time.sleep(_EXEC_NOT_READY_BACKOFF_S)
        # Exhausted the retries on a transient error — the container never
        # became exec-ready in the window. (last_exc is set: the loop only
        # falls through here after a transient ApiException each pass.)
        assert last_exc is not None
        raise click.ClickException(
            _format_api_error("exec in pod", sandbox_id, last_exc)
            + f" (container not exec-ready after {_EXEC_NOT_READY_RETRIES} attempts)"
        )

    def _open_exec_stream_once(self, namespace: str, sandbox_id: str, command: str) -> _ExecStream:
        """
        Open the exec websocket ONCE, bounded by a worker-thread timeout
        (round-3 FIX-1).

        The kubernetes 36.0.2 client leaves the ``_preload_content=False``
        websocket connect unbounded, so ``stream()`` is run on a worker
        thread and awaited with ``.result(timeout=_POD_READY_REQUEST_TIMEOUT_S)``.

        On timeout the executor is shut down with ``wait=False,
        cancel_futures=True`` — NOT a ``with`` block, whose ``__exit__``
        does ``shutdown(wait=True)`` and would BLOCK on the orphaned
        still-connecting thread, defeating the timeout. The orphaned worker
        is left to unwind on its own (it cannot leak a returned WSClient
        because we never read its result).

        :param namespace: Namespace the Pod lives in.
        :param sandbox_id: Target Pod name.
        :param command: Shell command to execute remotely.
        :returns: The opened ``WSClient``.
        :raises concurrent.futures.TimeoutError: When the open does not
            complete within :data:`_POD_READY_REQUEST_TIMEOUT_S`.
        :raises kubernetes.client.rest.ApiException: When the open fails.
        """
        from kubernetes.stream import stream

        core = self._load_core()

        def _open() -> _ExecStream:
            # _request_timeout is kept as future-proofing (if a later client
            # honors it for the streaming connect), but it is INERT in
            # 36.0.2 for _preload_content=False — the REAL bound is the
            # .result(timeout=...) below.
            ws: _ExecStream = stream(
                core.connect_get_namespaced_pod_exec,
                sandbox_id,
                namespace,
                # The Pod has a single container named "host"
                # (build_pod_manifest). Naming it explicitly keeps exec
                # unambiguous on clusters that inject sidecars (Istio,
                # Linkerd), where an unspecified container is rejected.
                container=_CONTAINER_NAME,
                command=["bash", "-lc", command],
                stderr=True,
                stdin=False,
                stdout=True,
                tty=False,
                _preload_content=False,
                _request_timeout=_POD_READY_REQUEST_TIMEOUT_S,
            )
            return ws

        executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="omnigent-k8s-exec-open"
        )
        future = executor.submit(_open)
        try:
            ws = future.result(timeout=_POD_READY_REQUEST_TIMEOUT_S)
        except concurrent.futures.TimeoutError:
            # Do NOT wait for the orphaned connecting thread (that is the
            # whole point of the timeout) — cancel and return promptly.
            executor.shutdown(wait=False, cancel_futures=True)
            raise
        except BaseException:
            # The open itself failed (e.g. ApiException) — the worker is
            # done, so a non-blocking shutdown is fine; re-raise to the
            # caller's transient-retry handling.
            executor.shutdown(wait=False)
            raise
        # Success: the worker finished, so a normal shutdown won't block.
        executor.shutdown(wait=True)
        return ws

    @staticmethod
    def _exec_stuck_message(
        sandbox_id: str,
        command: str,
        summary: str,
        stdout_chunks: list[str],
        stderr_chunks: list[str],
    ) -> str:
        """
        Build the failure message for a wedged exec stream, including any
        output captured so far (the only diagnostic for a hung command).

        :param sandbox_id: The Pod the exec ran in.
        :param command: The command that wedged.
        :param summary: What tripped the guard (idle / overall window).
        :param stdout_chunks: Captured stdout so far.
        :param stderr_chunks: Captured stderr so far.
        :returns: The error message.
        """
        # Redact sensitive env-assignment values (the launch token rides
        # OMNIGENT_HOST_TOKEN=...) before they enter the error / 502 body /
        # log — both in the echoed command and in any output that echoed it.
        message = (
            f"Remote command on pod '{sandbox_id}' {summary} and was abandoned: "
            f"{_redact_command(command)}"
        )
        tail = ("".join(stdout_chunks) + "".join(stderr_chunks)).strip()
        if tail:
            message += f" — output so far: {_redact_command(tail[-2000:])}"
        return message

    def terminate(self, sandbox_id: str) -> None:
        """
        Delete a sandbox Pod, releasing its compute.

        Idempotent: a Pod that no longer exists (404) is treated as
        success — the desired end state holds, and managed teardown can
        race the provider's own deletion. ``grace_period_seconds=0``
        deletes promptly (the reaper forwards SIGTERM, but a torn-down
        session host needn't linger).

        :param sandbox_id: The Pod to delete.
        :raises click.ClickException: On any API delete failure other than
            not-found (a urllib3 timeout/connection error is logged as a
            best-effort failure, not raised — the managed teardown path
            must not hang or abort on a stalled apiserver).
        """
        _ensure_sdk()
        from kubernetes.client.rest import ApiException
        from urllib3.exceptions import HTTPError

        namespace = self._resolve_namespace()
        try:
            # _request_timeout bounds the delete so a stalled apiserver
            # can't block _terminate_sandbox_best_effort forever (round-3
            # FIX-3).
            self._load_core().delete_namespaced_pod(
                sandbox_id,
                namespace,
                grace_period_seconds=0,
                _request_timeout=_POD_READY_REQUEST_TIMEOUT_S,
            )
        except HTTPError as exc:
            # A timeout/connection error is best-effort here: the managed
            # teardown caller wraps terminate() and must not hang or abort
            # on a provider hiccup (the provider's lifetime cap reaps a
            # straggler). Log, don't raise.
            click.echo(
                f"  → warning: timed out deleting Kubernetes pod '{sandbox_id}' "
                f"({_read_error_reason(exc)}); leaving it for the cluster to reap",
                err=True,
            )
        except ApiException as exc:
            if exc.status == 404:
                return
            raise click.ClickException(_format_api_error("delete pod", sandbox_id, exc)) from exc
        finally:
            # terminate() is the launcher's last op for a sandbox — release
            # the connection pool (a fresh launcher is built per managed
            # op). In a finally so the pool is freed even when the delete
            # raised; _close_clients swallows its own errors.
            self._close_clients()


# ── module helpers ─────────────────────────────────────────


def _redact_command(command: str) -> str:
    """
    Redact sensitive env-assignment VALUES in a shell command before it is
    put into an error message or log (FIX-3).

    Replaces ``FOO_TOKEN=abc`` with ``FOO_TOKEN=***`` for any key with a
    ``_``-delimited segment in :data:`_SENSITIVE_KEY_SEGMENTS`
    (case-insensitive) — so an exec timeout / non-zero exit can't leak the
    launch token (the host is started with ``OMNIGENT_HOST_TOKEN=<token>``)
    or harness creds into the surfaced error / HTTP 502 body / server log.
    The boundary char and key are preserved (diagnosability); only the
    value is masked. ``MONKEY=`` / ``HOTKEY=`` / ``KEYBOARD_LAYOUT=`` etc.
    (keyword only a substring of a segment) are left untouched.

    The regex matches the whole key as one linear run and the redact
    decision is made here, so this is O(n) (no catastrophic backtracking).

    :param command: The shell command, possibly with env assignments.
    :returns: The command with sensitive values masked.
    """

    def _mask(match: re.Match[str]) -> str:
        boundary, key, _value = match.group(1), match.group(2), match.group(3)
        segments = {seg.upper() for seg in key.split("_") if seg}
        if segments & _SENSITIVE_KEY_SEGMENTS:
            return f"{boundary}{key}=***"
        return match.group(0)

    return _SENSITIVE_ENV_RE.sub(_mask, command)


def _is_transient_read_error(exc: Exception) -> bool:
    """
    Report whether a ``read_namespaced_pod`` error is transient (the
    apiserver is briefly unavailable / the request timed out) rather than a
    definite failure (FIX-2; FIX-C request timeouts).

    Transient (retry until the deadline):

    - a urllib3 ``HTTPError`` — a connection error, or the request TIMEOUT
      raised by the ``_request_timeout`` bound (FIX-C); never an HTTP status
      the apiserver chose.
    - an ``ApiException`` with a status in :data:`_TRANSIENT_READ_STATUSES`
      (5xx / 429), or a missing status (``0``/``None`` — a connection/SSL
      error the client wrapped).

    Definite (surface immediately): a 4xx like 403 (RBAC) or 404 (Pod gone).

    :param exc: The raised exception (``ApiException`` or urllib3
        ``HTTPError``).
    :returns: ``True`` when the read should be retried.
    """
    from urllib3.exceptions import HTTPError

    if isinstance(exc, HTTPError):
        return True
    status = getattr(exc, "status", None)
    if not status:  # 0 / None → connection/timeout, no HTTP response
        return True
    return status in _TRANSIENT_READ_STATUSES


def _read_error_reason(exc: Exception) -> str:
    """
    Short human reason for a transient read error, for the retry log /
    timeout message.

    :param exc: The raised ``ApiException`` or urllib3 ``HTTPError``.
    :returns: The exception's ``reason`` (ApiException) or its class name +
        message (urllib3), or a generic fallback.
    """
    reason = getattr(exc, "reason", None)
    if reason:
        return str(reason)
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _create_outcome_ambiguous(exc: k8s_client.ApiException) -> bool:
    """
    Report whether a non-409 ``create_namespaced_pod`` ``ApiException`` is
    AMBIGUOUS — the apiserver may have accepted the Pod despite the error,
    so the known pod_name could be an orphan to best-effort delete
    (round-3 final FIX-2).

    Ambiguous (cleanup) = NO status (a wrapped transport error the client
    surfaced as ``status == 0``/``None``) OR a 5xx (500–599). A DEFINITE
    client rejection — any 4xx, including 409 conflict, 415, 429,
    400/403/404/422 — means the Pod was NOT created, so it must NOT trigger
    cleanup (the pod_name may belong to another concurrent launch).

    :param exc: The raised create ``ApiException`` (already known not to be
        a 409 the caller handled by name regeneration).
    :returns: ``True`` only when the Pod may have been created.
    """
    status = getattr(exc, "status", None)
    if not status:  # 0 / None → no HTTP response; the create may have landed
        return True
    return bool(500 <= status <= 599)


def _is_transient_exec_error(exc: k8s_client.ApiException) -> bool:
    """
    Report whether an exec ``ApiException`` is the transient first-exec
    race rather than a permanent failure (codex S2).

    Matches the exception's reason + body (case-insensitively) against
    :data:`_EXEC_TRANSIENT_MARKERS` — kubelet phrasings for a container
    that isn't yet attachable. Permanent failures (403 Forbidden, a Pod
    deleted for good) don't match and are surfaced immediately.

    :param exc: The raised ``ApiException``.
    :returns: ``True`` when the error looks retryable.
    """
    # 403 is always permanent (RBAC), never a not-ready race — short
    # circuit so a forbidden exec can't be mistaken for transient.
    if getattr(exc, "status", None) == 403:
        return False
    haystack = f"{getattr(exc, 'reason', '') or ''} {getattr(exc, 'body', '') or ''}".lower()
    return any(marker in haystack for marker in _EXEC_TRANSIENT_MARKERS)


def _format_api_error(action: str, pod: str, exc: k8s_client.ApiException) -> str:
    """
    Build a launcher-contract message for a Kubernetes ``ApiException``.

    Includes the HTTP reason and any response body so the managed-launch
    error surface carries the cluster's own explanation, and adds an
    RBAC pointer on 403 (the usual cause: the server ServiceAccount
    lacks the sandbox-manager Role) — the single most common
    misconfiguration of this provider.

    :param action: What was attempted, e.g. ``"create pod"``.
    :param pod: The Pod the action targeted.
    :param exc: The raised ``ApiException``.
    :returns: The error message.
    """
    reason = getattr(exc, "reason", None) or "unknown error"
    message = f"Failed to {action} '{pod}': {reason}"
    body = getattr(exc, "body", None)
    if body:
        message += f" ({body})"
    if getattr(exc, "status", None) == 403:
        message += (
            " — the server ServiceAccount likely lacks the sandbox-manager "
            "Role (pods, pods/exec); apply "
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


def _container_ready(pod: object) -> bool:
    """
    Report whether the ``host`` container's status is ``ready`` (codex
    S2).

    Checks the status whose name is :data:`_CONTAINER_NAME` specifically,
    NOT ``any()`` container — on sidecar-injected clusters (Istio,
    Linkerd) a ready sidecar would otherwise be mistaken for a ready host,
    and exec (which targets the ``host`` container) would race a container
    that isn't up. A missing host status is treated as not-ready.

    :param pod: A ``V1Pod`` read from the API.
    :returns: ``True`` only when the ``host`` container reports ``ready``.
    """
    status = getattr(pod, "status", None)
    statuses = getattr(status, "container_statuses", None) if status is not None else None
    for cs in statuses or []:
        if getattr(cs, "name", None) == _CONTAINER_NAME:
            return bool(getattr(cs, "ready", False))
    return False


def _fatal_container_reason(pod: object) -> tuple[str, str] | None:
    """
    Return a ``(reason, message)`` for a container in a genuinely terminal
    waiting state, or ``None`` (fast-fail).

    Matches the ``host`` container's ``state.waiting.reason`` against
    :data:`_FATAL_WAITING_REASONS` — a non-self-healing config error
    (bad image name, bad config, runtime error) the ready wait surfaces
    immediately. Recoverable waits (ImagePull*, ContainerCreating,
    PodInitializing) are deliberately NOT here — the kubelet retries them,
    so the loop polls until the deadline instead.

    :param pod: A ``V1Pod`` read from the API.
    :returns: The fatal ``(reason, message)``, or ``None``.
    """
    waiting = _host_container_waiting(pod)
    reason = getattr(waiting, "reason", None) if waiting is not None else None
    if reason in _FATAL_WAITING_REASONS:
        return reason, getattr(waiting, "message", None) or ""
    return None


def _terminal_container_exit(pod: object) -> tuple[int, str] | None:
    """
    Return ``(exit_code, reason)`` when the ``host`` container has
    terminated, or ``None`` (fast-fail on early exit).

    A ``restartPolicy: Never`` Pod whose host container ran and exited
    (``state.terminated``) will never become ready — fail fast rather than
    poll to the deadline. Catches the early-container-exit case (the
    reaper/entrypoint died) before the Pod phase flips to ``Failed``.

    :param pod: A ``V1Pod`` read from the API.
    :returns: ``(exit_code, reason)``, or ``None`` when not terminated.
    """
    cs = _host_container_status(pod)
    state = getattr(cs, "state", None) if cs is not None else None
    terminated = getattr(state, "terminated", None) if state is not None else None
    if terminated is None:
        return None
    exit_code = getattr(terminated, "exit_code", None)
    reason = getattr(terminated, "reason", None) or "Terminated"
    return (exit_code if isinstance(exit_code, int) else -1), reason


def _current_wait_reason(pod: object) -> str | None:
    """
    Return the host container's current ``waiting.reason`` (e.g.
    ``ImagePullBackOff``) or the Pod's ``Unschedulable`` reason — whichever
    explains why the Pod is not yet ready — for the timeout diagnosis.

    :param pod: A ``V1Pod`` read from the API.
    :returns: A short reason string, or ``None`` when none applies.
    """
    waiting = _host_container_waiting(pod)
    reason = getattr(waiting, "reason", None) if waiting is not None else None
    if reason:
        message = getattr(waiting, "message", None)
        return f"{reason}: {message}" if message else str(reason)
    unschedulable = _unschedulable_message(pod)
    if unschedulable is not None:
        return f"Unschedulable: {unschedulable}"
    return None


def _host_container_status(pod: object) -> object | None:
    """
    Return the ``host`` container's ``V1ContainerStatus``, or ``None``.

    :param pod: A ``V1Pod`` read from the API.
    :returns: The host container status, or ``None`` when absent.
    """
    status = getattr(pod, "status", None)
    statuses = getattr(status, "container_statuses", None) if status is not None else None
    for cs in statuses or []:
        if getattr(cs, "name", None) == _CONTAINER_NAME:
            host_status: object = cs
            return host_status
    return None


def _host_container_waiting(pod: object) -> object | None:
    """
    Return the ``host`` container's ``state.waiting`` object, or ``None``.

    :param pod: A ``V1Pod`` read from the API.
    :returns: The waiting state, or ``None`` when the host container is not
        waiting.
    """
    cs = _host_container_status(pod)
    state = getattr(cs, "state", None) if cs is not None else None
    waiting: object | None = getattr(state, "waiting", None) if state is not None else None
    return waiting


def _unschedulable_message(pod: object) -> str | None:
    """
    Return the scheduler's message when the Pod is unschedulable, or
    ``None`` (fast-fail).

    Matches a ``PodScheduled`` condition with ``status == "False"`` and
    ``reason == "Unschedulable"`` — no node fits the Pod's resource
    requests / node selector, which won't resolve without operator
    action.

    :param pod: A ``V1Pod`` read from the API.
    :returns: The scheduler message (or the bare reason), or ``None``.
    """
    status = getattr(pod, "status", None)
    conditions = getattr(status, "conditions", None) if status is not None else None
    for cond in conditions or []:
        if (
            getattr(cond, "type", None) == "PodScheduled"
            and getattr(cond, "status", None) == "False"
            and getattr(cond, "reason", None) == "Unschedulable"
        ):
            return getattr(cond, "message", None) or "Unschedulable"
    return None
