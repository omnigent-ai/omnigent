"""AWS Lambda MicroVMs sandbox launcher.

Implements the managed-launch subset of
:class:`~omnigent.onboarding.sandboxes.base.SandboxLauncher` for `AWS Lambda
MicroVMs <https://aws.amazon.com/about-aws/whats-new/2026/06/aws-lambda-microvms/>`_
— a Firecracker-isolated, snapshot-resumable serverless compute primitive
(up to 8 hours). This module ships in the base package; ``boto3`` is an optional
dependency (``pip install 'omnigent[lambda-microvm]'``) imported lazily, so the
provider can be listed and the module probed without it.

The model is **entrypoint-as-host**, like the Kubernetes launcher:
:meth:`~LambdaMicroVMSandboxLauncher.provision` only RESERVES a MicroVM name (no
compute yet), and :meth:`~LambdaMicroVMSandboxLauncher.start_host` calls
``run-microvm`` on a prebuilt MicroVM image whose container command is
``omnigent host``. The host boots inside the MicroVM and dials back over the
existing managed launch-token tunnel. Because the host is never started by
exec-ing into a running box, this launcher needs no exec transport — it
implements ``prepare`` / ``provision`` / ``start_host`` / ``resume`` /
``terminate``.

Platform notes that shape this launcher:

- **First host-preserving resume.** Lambda MicroVMs snapshot an idle VM to disk
  and thaw it in place with the workspace *and the running host process* intact,
  which fits the base class's ``can_resume`` / :meth:`resume` contract. This
  launcher sets ``can_resume = True`` and implements :meth:`resume`; it is the
  first launcher to set ``resume_preserves_host = True`` (others, e.g. Islo,
  resume compute but restart the host), so the wake path
  (:func:`omnigent.server.managed_hosts.resume_managed_host`) reconnects the
  existing host rather than starting a fresh one.
- **8-hour lifetime cap.** A MicroVM lives at most 8 hours; the managed
  launch-token TTL is derived from (and kept above) that cap via
  :func:`managed_token_ttl_s`.
- **Prebuilt image.** Unlike the registry-image providers (Modal, Daytona),
  Lambda MicroVMs boot from a *MicroVM image* built ahead of time via
  ``create-microvm-image`` (a Dockerfile + zip in S3, closer to E2B's template
  model). The image identifier is operator-supplied config; building it is a
  deploy-time step (see ``deploy/aws-lambda-microvm/README.md``).
- **Idle policy drives suspend/resume.** ``run-microvm`` is given an idle policy
  so an idle VM auto-suspends and auto-resumes on the next request. A snapshot
  thaw restores the whole guest — the running ``omnigent host`` and its still-
  valid token — so the host reconnects on its own; the wake path does not
  restart it (``resume_preserves_host = True``).
- **No CLI bootstrap / port forward.** Like Modal/Daytona/Kubernetes, the
  launcher exists for server-managed hosts only.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, ClassVar

import click

from omnigent.host.identity import (
    HOST_ID_ENV_VAR,
    HOST_NAME_ENV_VAR,
    HOST_TOKEN_ENV_VAR,
)
from omnigent.onboarding.sandboxes.base import (
    RemoteCommandResult,
    SandboxLauncher,
)

if TYPE_CHECKING:
    from collections.abc import Callable


# ── Constants ──────────────────────────────────────────

REGION_ENV_VAR: str = "OMNIGENT_LAMBDA_MICROVM_REGION"
"""Environment variable naming the AWS region MicroVMs run in. The
``sandbox.lambda_microvm.region`` config takes precedence; falls back to the
ambient boto3 region resolution (``AWS_REGION`` / profile) when neither is set."""

IMAGE_IDENTIFIER_ENV_VAR: str = "OMNIGENT_LAMBDA_MICROVM_IMAGE_IDENTIFIER"
"""Environment variable naming the MicroVM image identifier (name or ARN) the
host was built into via ``create-microvm-image``. The
``sandbox.lambda_microvm.image_identifier`` config takes precedence. Required —
Lambda MicroVMs boot from a prebuilt image, not an arbitrary registry ref."""

IMAGE_VERSION_ENV_VAR: str = "OMNIGENT_LAMBDA_MICROVM_IMAGE_VERSION"
"""Environment variable naming the MicroVM image version (e.g. ``"1.0"``). The
``sandbox.lambda_microvm.image_version`` config takes precedence. Optional; when
unset the account's latest version is used."""

EXECUTION_ROLE_ENV_VAR: str = "OMNIGENT_LAMBDA_MICROVM_EXECUTION_ROLE_ARN"
"""Environment variable naming the IAM role the running MicroVM assumes
(``executionRoleArn``). The ``sandbox.lambda_microvm.execution_role_arn`` config
takes precedence. Required."""

SANDBOX_ENV_PASSTHROUGH_ENV_VAR: str = "OMNIGENT_LAMBDA_MICROVM_SANDBOX_ENV"
"""Environment variable naming (comma-separated) the SERVER-process environment
variables whose values are injected into every MicroVM — typically the harness
LLM credentials (``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``, gateway base URLs,
…) and ``GIT_TOKEN`` that the in-VM host forwards to runners. Names, not values:
read from the server's own environment at run time, so secrets never live in
config files. The ``sandbox.lambda_microvm.env`` config takes precedence."""

EGRESS_CONNECTORS_ENV_VAR: str = "OMNIGENT_LAMBDA_MICROVM_EGRESS_CONNECTORS"
"""Environment variable naming (comma-separated) the egress network-connector
ARNs to attach at ``run-microvm``. Attaching a VPC egress connector routes the
host's outbound traffic — including its dial-back to the server — over the
customer VPC instead of the public internet, so the server can sit on a private
address. The ``sandbox.lambda_microvm.egress_network_connectors`` config takes
precedence; unset means the account default (public internet egress)."""

MAX_LIFETIME_ENV_VAR: str = "OMNIGENT_LAMBDA_MICROVM_MAX_LIFETIME_S"
"""Environment variable overriding the requested MicroVM lifetime in seconds
(default :data:`_DEFAULT_MAX_LIFETIME_S`, 8 h — the platform maximum). Used to
derive the managed launch-token TTL."""

DEBUG_INGRESS_CONNECTORS_ENV_VAR: str = "OMNIGENT_LAMBDA_MICROVM_DEBUG_INGRESS"
"""DEBUG-ONLY environment variable naming (comma-separated) ingress
network-connector ARNs to attach at ``run-microvm`` so an operator can shell
into a managed VM to diagnose in-guest issues. Unset in production: an ingress
connector opens an inbound path to the guest, so this is a deliberate debugging
backdoor, not a normal knob. No config-file equivalent — env var only."""

# AWS Lambda MicroVMs cap lifetime at 8 hours.
_DEFAULT_MAX_LIFETIME_S: int = 8 * 60 * 60

# Slack added to the sandbox lifetime to derive the launch-token TTL, so a live
# MicroVM can always re-authenticate its tunnel across reconnects while a token
# leaked from a dead VM still expires. Mirrors the e2b/cwsandbox pattern.
_TOKEN_TTL_SLACK_S: int = 3600

# Idle policy defaults for run-microvm: an idle VM auto-suspends after 15 min of
# no inbound traffic, may stay suspended up to 30 min before termination, and
# auto-resumes on the next request. Tuned so a session that sits between turns
# sleeps to a snapshot instead of holding warm compute, and wakes on the next
# message via the managed wake path.
_DEFAULT_MAX_IDLE_S: int = 900
_DEFAULT_SUSPENDED_S: int = 1800

# boto3 service name for the Lambda MicroVMs control plane.
_SERVICE_NAME: str = "lambda-microvms"

# Env names the launcher owns in the /run payload: the per-launch identity,
# token, dial-back URL, and the sandbox marker. An operator passthrough
# (sandbox.lambda_microvm.env) naming one is rejected — it would otherwise
# collide with the launch identity. Mirrors the Kubernetes launcher's
# _RESERVED_ENV_NAMES guard.
_RESERVED_ENV_NAMES: frozenset[str] = frozenset(
    {
        HOST_ID_ENV_VAR,
        HOST_NAME_ENV_VAR,
        HOST_TOKEN_ENV_VAR,
        "OMNIGENT_SERVER",
        "IS_SANDBOX",
    }
)

# The image's container runs as root (see deploy/aws-lambda-microvm/Dockerfile,
# based on the official omnigent-host image), so start_host.sh's ${HOME}/workspace
# resolves under /root. Mirrors the Kubernetes launcher's _HOME_DIR: the launcher
# controls the image, so the in-sandbox workspace path is a known constant rather
# than something asked of the sandbox.
_MICROVM_HOME: str = "/root"


def resolve_max_lifetime_s() -> int:
    """
    Resolve the requested MicroVM lifetime in seconds.

    :data:`MAX_LIFETIME_ENV_VAR` overrides the 8 h default. The value is
    validated up front: a non-numeric, non-finite, non-positive, or
    over-the-8h-cap override fails fast here with a clear message rather than
    building an invalid ``maximumDurationInSeconds`` that ``run-microvm``
    rejects later with an opaque AWS error.

    :returns: The lifetime to request at ``run-microvm``.
    :raises click.ClickException: When the env override is not a positive,
        finite number of seconds at or below the 8 h platform cap.
    """
    raw = os.environ.get(MAX_LIFETIME_ENV_VAR)
    if raw is None:
        return _DEFAULT_MAX_LIFETIME_S
    try:
        value = float(raw)
    except ValueError as exc:
        raise click.ClickException(f"{MAX_LIFETIME_ENV_VAR} must be a number of seconds") from exc
    if not math.isfinite(value) or value <= 0:
        raise click.ClickException(
            f"{MAX_LIFETIME_ENV_VAR} must be a positive, finite number of seconds"
        )
    seconds = int(value)
    if seconds < 1:
        # A fractional override in (0, 1) passes the value > 0 check but
        # truncates to 0, which would build a non-positive lifetime.
        raise click.ClickException(
            f"{MAX_LIFETIME_ENV_VAR} must be at least 1 second (got {value})"
        )
    if seconds > _DEFAULT_MAX_LIFETIME_S:
        raise click.ClickException(
            f"{MAX_LIFETIME_ENV_VAR} ({seconds}s) exceeds the AWS Lambda MicroVMs "
            f"maximum of {_DEFAULT_MAX_LIFETIME_S}s (8h)"
        )
    return seconds


def managed_token_ttl_s() -> int:
    """
    Launch-token TTL for the managed path, derived from (and always above) the
    MicroVM lifetime so the token outlives the VM across tunnel reconnects.

    :returns: The token lifetime in seconds.
    """
    return resolve_max_lifetime_s() + _TOKEN_TTL_SLACK_S


def build_idle_policy() -> dict[str, object]:
    """
    Build the ``idlePolicy`` for ``run-microvm``.

    Auto-suspend on idle and auto-resume on the next request are what let a
    between-turns session sleep to a snapshot and wake cheaply. The snapshot
    thaw restores the running ``omnigent host`` process with its still-valid
    launch token (this provider sets ``resume_preserves_host = True``), so the
    managed wake path (:func:`omnigent.server.managed_hosts.resume_managed_host`)
    resumes the VM WITHOUT restarting the host — the host reconnects on its own.

    :returns: The idle-policy mapping.
    """
    return {
        "maxIdleDurationSeconds": _DEFAULT_MAX_IDLE_S,
        "suspendedDurationSeconds": _DEFAULT_SUSPENDED_S,
        "autoResumeEnabled": True,
    }


def build_run_microvm_kwargs(
    *,
    image_identifier: str,
    execution_role_arn: str,
    host_id: str,
    host_name: str,
    server_url: str,
    token: str,
    env_literals: dict[str, str],
    image_version: str | None = None,
    egress_network_connectors: Sequence[str] | None = None,
) -> dict[str, Any]:
    """
    Build the ``run-microvm`` request as a plain dict.

    Pure: no boto3 import, no I/O — the kwargs are a literal dict the caller
    hands to ``client.run_microvm``, which makes this the primary unit-test
    surface for the launch wiring.

    The host identity and launch token ride the MicroVM's ``runHookPayload`` —
    a per-launch string (max 16 KB) the platform delivers as the body of the
    ``/run`` lifecycle hook after the VM thaws from its snapshot. ``RunMicrovm``
    has no per-launch environment-variable parameter (build-time env is baked at
    ``create-microvm-image`` time), so the identity that varies per launch has
    to travel this channel. The image's hooks server reads the ``/run`` body and
    starts ``omnigent host`` with the identity, which dials back. The idle policy
    lets the VM auto-suspend/resume.

    :param image_identifier: The prebuilt MicroVM image (name or ARN).
    :param execution_role_arn: IAM role the running MicroVM assumes.
    :param host_id: Server-chosen host identity, delivered in the /run payload.
    :param host_name: Server-chosen host display name, delivered in the payload.
    :param server_url: URL the in-VM host dials back to, delivered in the payload
        (the hooks server exports it as ``OMNIGENT_SERVER`` for ``omnigent
        host``, so a stock image needs no baked server URL).
    :param token: The raw launch token, delivered in the /run payload.
    :param env_literals: Harness credential env (name → value) resolved from the
        server environment, merged into the /run payload's ``env`` map.
    :param image_version: Specific image version, or ``None`` for the latest.
    :returns: The ``run-microvm`` kwargs.
    """
    # Identity keys are spread LAST so they always win over operator passthrough
    # (_resolve_sandbox_env already rejects a passthrough naming a reserved key,
    # but the merge order is the structural backstop): a passthrough that slipped
    # a reserved name through must never clobber the per-launch identity/token.
    payload: dict[str, Any] = {
        **env_literals,
        HOST_ID_ENV_VAR: host_id,
        HOST_NAME_ENV_VAR: host_name,
        HOST_TOKEN_ENV_VAR: token,
        "OMNIGENT_SERVER": server_url,
        "IS_SANDBOX": "1",
    }
    kwargs: dict[str, Any] = {
        "imageIdentifier": image_identifier,
        "executionRoleArn": execution_role_arn,
        "idlePolicy": build_idle_policy(),
        "maximumDurationInSeconds": resolve_max_lifetime_s(),
        "runHookPayload": json.dumps(payload),
    }
    if image_version is not None:
        kwargs["imageVersion"] = image_version
    # Attach VPC egress connectors so the host reaches a private server (and
    # private resources) over the customer VPC instead of the public internet.
    if egress_network_connectors:
        kwargs["egressNetworkConnectors"] = list(egress_network_connectors)
    # Debug-only: attach a shell ingress connector so an operator can shell into
    # a managed VM to diagnose in-guest issues. Not for production.
    _dbg_ingress = os.environ.get(DEBUG_INGRESS_CONNECTORS_ENV_VAR)
    if _dbg_ingress:
        _dbg_arns = [a.strip() for a in _dbg_ingress.split(",") if a.strip()]
        # Only attach the key when the parse yields real ARNs — a value of just
        # separators/whitespace (e.g. ",") is truthy but parses to [], and an
        # explicit empty ingressNetworkConnectors is rejected by the API.
        if _dbg_arns:
            kwargs["ingressNetworkConnectors"] = _dbg_arns
    return kwargs


def _ensure_sdk() -> None:
    """
    Verify boto3 is importable, with an install hint when not.

    Called at the top of every launcher entry point because boto3 is an optional
    dependency — the base ``omnigent`` install does not pull it in.

    :raises click.ClickException: When ``boto3`` is not installed.
    """
    try:
        import boto3  # noqa: F401  # presence probe only
    except ImportError as exc:
        raise click.ClickException(
            "boto3 is required for the 'lambda_microvm' sandbox provider. "
            "Install it with `pip install 'omnigent[lambda-microvm]'`, then "
            "configure AWS credentials (profile, environment, or instance role)."
        ) from exc


class LambdaMicroVMSandboxLauncher(SandboxLauncher):
    """
    :class:`SandboxLauncher` for AWS Lambda MicroVMs.

    Server-managed only and entrypoint-as-host: :meth:`provision` reserves a
    MicroVM name, :meth:`start_host` calls ``run-microvm`` on a prebuilt image
    whose command runs ``omnigent host``, :meth:`resume` thaws a suspended VM,
    and :meth:`terminate` calls ``terminate-microvm``. All transport rides the
    ``lambda-microvms`` boto3 client, created lazily and cached.
    """

    provider: ClassVar[str] = "lambda_microvm"
    # Managed-only: no CLI bootstrap, no local→sandbox port forward.
    supports_cli_bootstrap: ClassVar[bool] = False
    supports_local_port_forward: ClassVar[bool] = False
    # Lambda MicroVMs snapshot-suspend an idle VM and thaw it in place with the
    # workspace intact — the reattachable-volume lifecycle the wake path needs.
    can_resume: ClassVar[bool] = True
    # A snapshot thaw restores the whole guest, including the running
    # ``omnigent host`` process and its still-valid launch token, so the host
    # reconnects on its own after resume — the wake path must not restart it.
    resume_preserves_host: ClassVar[bool] = True

    def __init__(
        self,
        *,
        region: str | None = None,
        image_identifier: str | None = None,
        image_version: str | None = None,
        execution_role_arn: str | None = None,
        env: Sequence[str] | None = None,
        egress_network_connectors: Sequence[str] | None = None,
    ) -> None:
        """
        Initialize the launcher.

        :param region: AWS region MicroVMs run in — the
            ``sandbox.lambda_microvm.region`` config. ``None`` resolves
            :data:`REGION_ENV_VAR` then the ambient boto3 region.
        :param image_identifier: Prebuilt MicroVM image (name or ARN) — the
            ``sandbox.lambda_microvm.image_identifier`` config. ``None`` resolves
            :data:`IMAGE_IDENTIFIER_ENV_VAR`; required at launch.
        :param image_version: Specific image version — the
            ``sandbox.lambda_microvm.image_version`` config. ``None`` resolves
            :data:`IMAGE_VERSION_ENV_VAR` then the account's latest.
        :param execution_role_arn: IAM role the MicroVM assumes — the
            ``sandbox.lambda_microvm.execution_role_arn`` config. ``None``
            resolves :data:`EXECUTION_ROLE_ENV_VAR`; required at launch.
        :param env: Names of server-process environment variables to inject into
            every MicroVM — the ``sandbox.lambda_microvm.env`` config. ``None``
            resolves :data:`SANDBOX_ENV_PASSTHROUGH_ENV_VAR`.
        :param egress_network_connectors: Egress network-connector IDs to attach
            — the ``sandbox.lambda_microvm.egress_network_connectors`` config.
            ``None`` resolves :data:`EGRESS_CONNECTORS_ENV_VAR`; unset means the
            account default (public internet egress).
        """
        self._region = region
        self._image_identifier = image_identifier
        self._image_version = image_version
        self._execution_role_arn = execution_role_arn
        self._env_names = tuple(env) if env is not None else None
        self._egress_connectors = (
            tuple(egress_network_connectors) if egress_network_connectors is not None else None
        )
        self._client: Any = None

    # ── client / resolution helpers ─────────────────────────

    def _get_client(self) -> Any:
        """
        Return the (lazily created, cached) ``lambda-microvms`` boto3 client.

        :returns: The boto3 client.
        :raises click.ClickException: When boto3 is not installed.
        """
        if self._client is None:
            _ensure_sdk()
            import boto3

            region = self._region or os.environ.get(REGION_ENV_VAR)
            self._client = boto3.client(_SERVICE_NAME, region_name=region)
        return self._client

    def _resolve_image_identifier(self) -> str:
        """
        Resolve the MicroVM image identifier: constructor → env → error.

        :returns: The image identifier.
        :raises click.ClickException: When neither the config nor the env var is
            set (Lambda MicroVMs cannot boot without a prebuilt image).
        """
        resolved = self._image_identifier or os.environ.get(IMAGE_IDENTIFIER_ENV_VAR)
        if not resolved:
            raise click.ClickException(
                "the 'lambda_microvm' provider needs a prebuilt MicroVM image — "
                "set sandbox.lambda_microvm.image_identifier (or "
                f"{IMAGE_IDENTIFIER_ENV_VAR}). Build one with `create-microvm-image` "
                "(see deploy/aws-lambda-microvm/README.md)."
            )
        return resolved

    def _resolve_execution_role_arn(self) -> str:
        """
        Resolve the execution role ARN: constructor → env → error.

        :returns: The execution role ARN.
        :raises click.ClickException: When neither the config nor the env var is
            set.
        """
        resolved = self._execution_role_arn or os.environ.get(EXECUTION_ROLE_ENV_VAR)
        if not resolved:
            raise click.ClickException(
                "the 'lambda_microvm' provider needs an execution role — set "
                f"sandbox.lambda_microvm.execution_role_arn (or {EXECUTION_ROLE_ENV_VAR})."
            )
        return resolved

    def _resolve_image_version(self) -> str | None:
        """Resolve the image version: constructor → env → ``None`` (latest)."""
        return self._image_version or os.environ.get(IMAGE_VERSION_ENV_VAR) or None

    def _resolve_egress_connectors(self) -> list[str]:
        """Resolve egress connector ARNs: constructor → env → ``[]`` (default egress)."""
        if self._egress_connectors is not None:
            return list(self._egress_connectors)
        return [
            arn.strip()
            for arn in os.environ.get(EGRESS_CONNECTORS_ENV_VAR, "").split(",")
            if arn.strip()
        ]

    def _resolve_sandbox_env(self) -> dict[str, str]:
        """
        Resolve the env vars to inject into created MicroVMs.

        Explicit constructor names win; otherwise
        :data:`SANDBOX_ENV_PASSTHROUGH_ENV_VAR` (comma-separated) applies; an
        empty resolution injects nothing. Values come from the server's own
        environment — a configured name that is unset there fails loud (silently
        launching without it would surface much later as an opaque in-VM harness
        auth failure).

        :returns: Name → value mapping for the MicroVM environment.
        :raises click.ClickException: When a configured name is not set in the
            server process environment.
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
                    f"sandbox env passthrough names '{name}', which is reserved for "
                    "the launch identity — remove it from sandbox.lambda_microvm.env "
                    f"/ {SANDBOX_ENV_PASSTHROUGH_ENV_VAR} (it is set per launch)."
                )
            value = os.environ.get(name)
            if value is None:
                raise click.ClickException(
                    f"sandbox env passthrough names '{name}' but it is not set in "
                    "the server's environment — set it (or remove it from "
                    f"sandbox.lambda_microvm.env / {SANDBOX_ENV_PASSTHROUGH_ENV_VAR})."
                )
            resolved[name] = value
        return resolved

    # ── lifecycle ───────────────────────────────────────────

    def prepare(self) -> None:
        """
        Local preflight: boto3 must be installed and the launch-critical config
        (image identifier, execution role) resolvable.

        AWS reachability is not pre-checked — the first ``run-microvm`` surfaces
        a credential/permission error with a clear message.

        :raises click.ClickException: When boto3 is missing or required config is
            absent.
        """
        _ensure_sdk()
        self._resolve_image_identifier()
        self._resolve_execution_role_arn()

    def provision(self, name: str) -> str:
        """
        Reserve a MicroVM name for a managed launch — no MicroVM is created here.

        Entrypoint-as-host: the MicroVM (which boots running ``omnigent host``)
        is materialized by :meth:`start_host`, not here. ``provision`` only
        returns the name, so the server can register the launch token against it
        BEFORE the VM exists — closing the host dial-back race by construction.

        :param name: Human-readable label, e.g. ``"managed-a1b2c3d4"``.
        :returns: The reserved name (unchanged).
        """
        return name

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
        Run a MicroVM whose container command is ``omnigent host``.

        The entrypoint-as-host override: ``run-microvm`` boots the prebuilt
        image with the identity + token delivered in ``runHookPayload``, and the
        host dials back over the launch-token tunnel.

        Repository cloning is not driven from here: the workspace is prepared
        inside the image's ``/run``-triggered ``start_host.sh`` (which clones
        ``OMNIGENT_REPO_URL`` when set). The *repo_url* / *repo_branch* ride the
        same payload so the entrypoint can clone before starting the host.
        Because this launcher controls the image, the resulting workspace path is
        a known constant — ``${HOME}/workspace`` (or the clone directory under
        it) — computed here without asking the sandbox, mirroring the Kubernetes
        launcher's ``start_host``. It must NOT be the returned ``microvmId``: the
        managed-launch framework persists this return value as the session's
        *workspace* (:class:`~omnigent.server.managed_hosts.ManagedHostLaunch`),
        never as a new sandbox id — ``host.launch_runner`` then validates it with
        ``Path(workspace).is_dir()`` **inside the guest**, so a non-path value
        (e.g. the MicroVM id) makes every launch fail with "workspace path does
        not exist" and the runner never starts.

        The real ``microvmId`` AWS assigns at ``run-microvm`` is surfaced to the
        managed-launch framework through :attr:`started_sandbox_id` (not the
        return value): ``provision`` can only RESERVE a name before the VM
        exists, and ``run-microvm`` accepts no caller-supplied name, so the id is
        unknown until here. The framework reads :attr:`started_sandbox_id` after
        this returns and overwrites the host row's ``sandbox_id`` with it, so
        later :meth:`terminate` / :meth:`resume` key off the id AWS knows.

        :param sandbox_id: The reserved name from :meth:`provision` — used only
            for logging (``run-microvm`` has no caller-supplied-name field). The
            real id is recorded on :attr:`started_sandbox_id` for the framework
            to persist.
        :param token: The raw launch token, delivered via the /run payload.
        :param host_id: Server-chosen host identity.
        :param host_name: Server-chosen host display name.
        :param server_url: URL the host dials back to.
        :param repo_url: Repository clone URL, or ``None`` for an empty
            workspace.
        :param repo_branch: Branch to clone, or ``None`` for the default branch.
        :param repo_name: Directory the clone lands in, or ``None``.
        :param on_stage: Progress observer; invoked with ``"starting"``.
        :returns: The absolute in-sandbox workspace path (the cloned repository
            directory when *repo_name* is set).
        :raises click.ClickException: When the run fails.
        """
        _ensure_sdk()
        from botocore.exceptions import BotoCoreError, ClientError

        image_identifier = self._resolve_image_identifier()
        execution_role_arn = self._resolve_execution_role_arn()
        env_literals = self._resolve_sandbox_env()
        # The image entrypoint reads these to clone the repo before starting the
        # host; a stock image with no repo leaves them unset.
        if repo_url is not None:
            env_literals = {**env_literals, "OMNIGENT_REPO_URL": repo_url}
            if repo_branch is not None:
                env_literals["OMNIGENT_REPO_BRANCH"] = repo_branch
            if repo_name is not None:
                env_literals["OMNIGENT_REPO_NAME"] = repo_name
        if on_stage is not None:
            on_stage("starting")
        kwargs = build_run_microvm_kwargs(
            image_identifier=image_identifier,
            execution_role_arn=execution_role_arn,
            host_id=host_id,
            host_name=host_name,
            server_url=server_url,
            token=token,
            env_literals=env_literals,
            image_version=self._resolve_image_version(),
            egress_network_connectors=self._resolve_egress_connectors() or None,
        )
        client = self._get_client()
        click.echo(f"▸ Running Lambda MicroVM '{sandbox_id}' from image {image_identifier}")
        try:
            response = client.run_microvm(**kwargs)
        except (BotoCoreError, ClientError) as exc:
            # AWS boundary: surface the provider reason (missing image, role
            # trust, quota) so the managed-launch 502 carries it verbatim.
            raise click.ClickException(f"Lambda MicroVM run failed: {exc}") from exc
        microvm_id = response.get("microvmId")
        if not microvm_id:
            raise click.ClickException(
                f"Lambda MicroVM run for '{sandbox_id}' returned no microvmId"
            )
        # Surface the provider-assigned id so the managed-launch framework can
        # persist it as the host row's sandbox_id — terminate/resume need the
        # real microvmId, not the reserved name passed in as sandbox_id.
        self.started_sandbox_id = microvm_id
        click.echo(f"  → running {microvm_id}")
        # The real workspace path — a known constant since this launcher
        # controls the image's HOME (see start_host.sh), not the run's return
        # value. host.launch_runner validates this path with Path(...).is_dir()
        # INSIDE the guest, so returning anything else (e.g. microvm_id) makes
        # every runner launch fail with "workspace path does not exist".
        workspace = f"{_MICROVM_HOME}/workspace"
        if repo_name:
            workspace = f"{workspace}/{repo_name}"
        return workspace

    def resume(self, sandbox_id: str) -> None:
        """
        Resume a suspended MicroVM in place, restoring its snapshot.

        The first real consumer of the base class's :meth:`resume` contract:
        Lambda MicroVMs thaw a suspended VM with memory + disk state intact, so a
        dormant managed host wakes under the SAME MicroVM id with its workspace
        preserved. Because the thaw restores the whole guest — including the
        running ``omnigent host`` process and its still-valid launch token — the
        host reconnects on its own; the wake path does NOT restart it (see
        :attr:`resume_preserves_host`).

        *sandbox_id* is the real ``microvmId`` the framework persisted from
        :attr:`started_sandbox_id` after :meth:`start_host`, so ``resume-microvm``
        resolves it.

        Idempotent from the caller's perspective: a MicroVM already running is
        treated as success.

        :param sandbox_id: The suspended MicroVM id to resume.
        :raises click.ClickException: When the resume fails.
        """
        _ensure_sdk()
        from botocore.exceptions import BotoCoreError, ClientError

        client = self._get_client()
        click.echo(f"▸ Resuming Lambda MicroVM '{sandbox_id}'")
        try:
            client.resume_microvm(microvmIdentifier=sandbox_id)
        except (BotoCoreError, ClientError) as exc:
            raise click.ClickException(
                f"Could not resume Lambda MicroVM '{sandbox_id}': {exc}"
            ) from exc

    def terminate(self, sandbox_id: str) -> None:
        """
        Terminate a MicroVM, releasing its compute.

        Idempotent from the caller's perspective: a MicroVM that no longer exists
        (already terminated or aged past the 8 h cap) is treated as success — the
        desired end state holds.

        *sandbox_id* is the real ``microvmId`` the framework persisted from
        :attr:`started_sandbox_id` after :meth:`start_host`, so
        ``terminate-microvm`` resolves it and the VM is actually released.

        :param sandbox_id: The MicroVM id to terminate.
        :raises click.ClickException: When termination fails for a MicroVM that
            still exists.
        """
        _ensure_sdk()
        from botocore.exceptions import BotoCoreError, ClientError

        client = self._get_client()
        try:
            client.terminate_microvm(microvmIdentifier=sandbox_id)
        except ClientError as exc:
            # A not-found MicroVM is idempotent success; anything else is a real
            # failure the best-effort teardown caller logs.
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("ResourceNotFoundException", "NotFoundException"):
                return
            raise click.ClickException(
                f"Could not terminate Lambda MicroVM '{sandbox_id}': {exc}"
            ) from exc
        except BotoCoreError as exc:
            raise click.ClickException(
                f"Could not terminate Lambda MicroVM '{sandbox_id}': {exc}"
            ) from exc

    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        """
        Unsupported: the host runs as the MicroVM's entrypoint, so there is no
        exec-in transport.

        :param sandbox_id: Unused.
        :param command: Unused.
        :param check: Unused.
        :raises SandboxCapabilityError: Always.
        """
        raise self._capability_error(
            "run a command via exec — the host runs as the MicroVM entrypoint"
        )
