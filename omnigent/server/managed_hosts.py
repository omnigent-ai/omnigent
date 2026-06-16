"""Server-launched sandbox hosts for ``host_type="managed"`` sessions.

The external host flow has a human run ``omnigent host`` on their own
machine. The managed flow replaces the human: when a session is created
with ``host_type="managed"``, the server provisions a cloud sandbox,
starts ``omnigent host`` inside it, and waits for that host to
register — after which the session rides the exact same host-launch
machinery an external host uses (binding token, ``host.launch_runner``
frame, runner tunnel).

The host's identity is DURABLE while its sandbox is not: the ``hosts``
row carries the managed columns (launch-token digest + expiry,
provider, sandbox id), and a relaunch overwrites them in place — a new
sandbox generation under the same ``host_id``, so session bindings
survive a sandbox dying at the provider's lifetime cap.

The sandbox host authenticates back with a dedicated launch token the
server mints per launch (see
:meth:`omnigent.stores.host_store.HostStore.register_managed_host` and
the managed-token branch in
:mod:`omnigent.server.routes.host_tunnel`) — the user's own
credentials never enter the sandbox.

How a deployment supplies the sandbox backend (two paths, one seam —
:class:`ManagedSandboxConfig` carries a launcher FACTORY, so embedding
deployments inject custom launchers the same way they inject custom
stores into ``create_app``):

1. **Server YAML** (OSS / self-hosted): :func:`parse_sandbox_config`
   builds the config from the ``sandbox:`` section
   (``omnigent server -c`` / ``OMNIGENT_CONFIG`` /
   ``<data_dir>/config.yaml``)::

       sandbox:
         provider: modal          # lakebox | modal | daytona | islo
         server_url: https://omnigent.example.com
         modal:                   # optional block
           image: docker.io/me/omnigent-host:latest  # default: official image
           secrets: [omnigent-llm]  # Modal secrets injected as sandbox env
                                     # (harness LLM keys, gateway URLs)
         daytona:                 # optional block (provider: daytona)
           image: docker.io/me/omnigent-host:latest  # default: official image
           env: [OPENAI_API_KEY, GIT_TOKEN]  # SERVER env var NAMES whose
                                             # values are injected as
                                             # sandbox env
         islo:                    # optional block (provider: islo)
           image: docker.io/me/omnigent-host:latest  # default: official image
           env: [OPENAI_API_KEY, GIT_TOKEN]  # SERVER env var NAMES injected
                                             # as sandbox env
           base_url: https://api.islo.dev    # optional API override
           gateway_profile: default          # optional Islo gateway profile
           snapshot_name: warm-host          # optional Islo snapshot name
           workdir: /root/workspace          # optional sandbox workdir
           vcpus: 2
           memory_mb: 4096
           disk_gb: 20

   The image defaults to the official prebaked host image
   (``ghcr.io/omnigent-ai/omnigent-host:latest``; see
   :data:`omnigent.onboarding.sandboxes.base.DEFAULT_HOST_IMAGE` and
   the per-provider env overrides), so ``provider`` + ``server_url``
   is a complete config. Provider credentials are NOT in this file
   (12-factor): the Modal launcher reads ``MODAL_TOKEN_ID`` /
   ``MODAL_TOKEN_SECRET`` (or ``~/.modal.toml``) and the Daytona
   launcher reads ``DAYTONA_API_KEY`` (plus optional
   ``DAYTONA_API_URL`` / ``DAYTONA_TARGET``), and the Islo launcher
   reads ``ISLO_API_KEY`` (plus optional ``ISLO_BASE_URL``) from the
   server process environment. ``modal``, ``daytona``, and ``islo``
   have managed-launch support; ``lakebox`` parses but rejects at
   launch.

2. **Direct construction** (embedding deployments): build
   :class:`ManagedSandboxConfig` with a custom
   :class:`~omnigent.onboarding.sandboxes.base.SandboxLauncher`
   factory and pass it to ``create_app(sandbox_config=…)``::

       ManagedSandboxConfig(
           server_url=public_url,
           launcher_factory=lambda: MySandboxLauncher(...),
           token_ttl_s=7 * 24 * 3600,
       )

   A managed-only launcher implements ``prepare`` / ``provision`` /
   ``run`` / ``terminate``; the CLI-bootstrap primitives default to
   capability errors and need no overrides.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import shlex
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import click
from fastapi import HTTPException

from omnigent.db.utils import now_epoch
from omnigent.host.identity import (
    HOST_ID_ENV_VAR,
    HOST_NAME_ENV_VAR,
    HOST_TOKEN_ENV_VAR,
)
from omnigent.stores.host_store import Host, HostStore

if TYPE_CHECKING:
    from omnigent.onboarding.sandboxes import SandboxLauncher

_logger = logging.getLogger(__name__)

# Providers the YAML `sandbox:` section accepts. Parsing accepts all
# known providers so a deployment can stage config ahead of support
# landing, but only PROVIDERS_WITH_MANAGED_LAUNCH can actually serve a
# managed session today. (Deployments that construct
# ManagedSandboxConfig directly are not constrained by either set —
# their launcher factory IS the support.)
SUPPORTED_SANDBOX_PROVIDERS: frozenset[str] = frozenset(
    {"lakebox", "modal", "daytona", "cwsandbox", "islo", "docker", "e2b"}
)
PROVIDERS_WITH_MANAGED_LAUNCH: frozenset[str] = frozenset(
    {"modal", "daytona", "cwsandbox", "islo", "docker", "e2b"}
)

# How long a managed launch waits for the sandboxed host to register
# before declaring failure. The image is pre-baked (no pip install at
# boot), so a healthy launch registers in seconds; the budget covers a
# cold registry pull of the image on first use.
MANAGED_HOST_ONLINE_TIMEOUT_S = 120
_ONLINE_POLL_INTERVAL_S = 1.0

# Launch-token lifetime for the YAML modal path: Modal's 24h sandbox
# cap plus an hour of slack, so a live sandbox can always
# re-authenticate its tunnel across reconnects, while a token leaked
# from a long-dead sandbox cannot. Scoped to the token, not the host:
# the host row is durable, and a relaunch mints a fresh token + expiry.
# Deployments injecting their own launcher choose their own TTL on
# ManagedSandboxConfig.
MODAL_MANAGED_TOKEN_TTL_S = 25 * 3600

# Launch-token lifetime for the YAML daytona path. Daytona sandboxes
# have no platform lifetime cap (idle auto-stop is disabled at
# provision), so the bound is policy, not platform: 7 days keeps a
# long-lived sandbox re-authenticating across tunnel reconnects while
# still expiring tokens of sandboxes nobody deleted. A relaunch (or a
# session past 7 days going through the dead-host relaunch path) mints
# a fresh token.
DAYTONA_MANAGED_TOKEN_TTL_S = 7 * 24 * 3600

# Launch-token lifetime for the YAML islo path. Islo sandboxes are
# deleted by managed-session teardown; use the same 7-day policy bound
# as Daytona for long-lived hosts and stale-token cleanup.
ISLO_MANAGED_TOKEN_TTL_S = 7 * 24 * 3600

# Launch-token lifetime for the YAML docker path. Containers have no
# platform lifetime cap, so the bound is policy (same 7-day bound as
# Daytona/Islo): a long-lived container keeps re-authenticating its
# tunnel across reconnects, while a token leaked from a removed
# container expires. A relaunch mints a fresh token.
DOCKER_MANAGED_TOKEN_TTL_S = 7 * 24 * 3600

# The cwsandbox launch-token TTL is NOT a constant: CW Sandbox's lifetime is
# operator-overridable (OMNIGENT_CWSANDBOX_MAX_LIFETIME_S), so the TTL is
# derived from the resolved lifetime at parse time via
# cwsandbox.managed_token_ttl_s() — always above the cap, so a live sandbox
# can re-authenticate its tunnel across reconnects while a leaked token can't.

# Where the in-sandbox host process logs — named in launch-failure
# errors so an operator knows where to look inside the sandbox.
_HOST_LOG_PATH = "/tmp/omnigent-host.log"

# How long a message POST waits for an in-flight managed launch to
# settle before giving up (see ManagedLaunchTracker). Covers the full
# launch pipeline: sandbox provision + host registration
# (MANAGED_HOST_ONLINE_TIMEOUT_S) plus runner spawn/connect, with
# slack. The wait resolves as soon as the launch settles — this bound
# only matters when the background launch is itself stuck.
MANAGED_LAUNCH_RENDEZVOUS_TIMEOUT_S = MANAGED_HOST_ONLINE_TIMEOUT_S + 60

# Session label recording the repository-URL workspace a managed
# session was created with (the raw ``<url>[#<branch>]`` request
# value). ``conversations.workspace`` is overwritten with the CLONED
# path at bind time, so this label is what a sandbox RELAUNCH parses
# to re-clone the repository into the fresh generation's workspace.
MANAGED_REPO_LABEL_KEY = "omnigent.sandbox.repo"


@dataclass
class ManagedLaunch:
    """
    One session's in-flight (or failed) managed-host launch.

    Created by :meth:`ManagedLaunchTracker.begin` when
    ``POST /v1/sessions`` schedules the background launch, and settled
    by the background task via :meth:`ManagedLaunchTracker.finish` /
    :meth:`ManagedLaunchTracker.fail`.

    :param settled: Set once the launch reaches a terminal state —
        either success (host bound, runner launched) or failure.
        Waiters (a message POST racing the provision) block on this.
    :param error: Failure detail once settled unsuccessfully, e.g.
        ``"managed sandbox launch failed: …"``. ``None`` while
        in flight and on success.
    """

    settled: asyncio.Event
    error: str | None = None


class ManagedLaunchTracker:
    """
    In-memory index of managed-host launches keyed by session id.

    ``POST /v1/sessions`` with ``host_type="managed"`` returns before
    the sandbox exists; this tracker is how the rest of the server
    observes that window. A message POST that arrives mid-provision
    waits on the session's :class:`ManagedLaunch` instead of failing
    with "no runner bound"; a launch failure is recorded here so the
    waiting POST (and any later one) reports the real reason.

    Successful launches are removed on settle — from then on the
    session looks like any host-bound session. Failed launches are
    retained (the session row never got a host, so the recorded error
    is the only trace of why) until the process restarts or a new
    launch for the same session begins.
    """

    def __init__(self) -> None:
        """Initialize the empty session-id → launch index."""
        self._by_session: dict[str, ManagedLaunch] = {}

    def begin(self, session_id: str) -> None:
        """
        Register a new in-flight launch for *session_id*.

        Replaces any prior entry (e.g. a retained failure from an
        earlier attempt).

        :param session_id: Session/conversation identifier,
            e.g. ``"conv_abc123"``.
        """
        self._by_session[session_id] = ManagedLaunch(settled=asyncio.Event())

    def get(self, session_id: str) -> ManagedLaunch | None:
        """
        Look up the launch state for *session_id*.

        :param session_id: Session/conversation identifier.
        :returns: The launch entry, or ``None`` when no managed launch
            is in flight or recorded as failed for this session.
        """
        return self._by_session.get(session_id)

    def finish(self, session_id: str) -> None:
        """
        Settle *session_id*'s launch as successful and forget it.

        Waiters holding the entry observe ``settled`` with
        ``error is None``; later readers find no entry and take the
        normal host-bound paths.

        :param session_id: Session/conversation identifier.
        """
        entry = self._by_session.pop(session_id, None)
        if entry is not None:
            entry.settled.set()

    def fail(self, session_id: str, error: str) -> None:
        """
        Settle *session_id*'s launch as failed, retaining the reason.

        :param session_id: Session/conversation identifier.
        :param error: Human-readable failure detail, e.g.
            ``"managed sandbox launch failed: spend limit reached"``.
        """
        entry = self._by_session.get(session_id)
        if entry is None:
            return
        entry.error = error
        entry.settled.set()


@dataclass
class ManagedSandboxConfig:
    """
    Everything the managed-host flow needs from a deployment.

    Built by :func:`parse_sandbox_config` from the server YAML, or
    constructed directly by embedding deployments to inject a custom
    launcher (see the module docstring).

    :param server_url: Public URL of THIS server that the sandboxed
        host dials back to, e.g. ``"https://omnigent.example.com"``
        (no trailing slash). Explicit — the server cannot reliably
        infer its own public URL behind proxies.
    :param launcher_factory: Zero-argument factory producing the
        :class:`~omnigent.onboarding.sandboxes.base.SandboxLauncher`
        each launch uses, e.g.
        ``lambda: ModalSandboxLauncher(image=…)``. Called per launch
        (launchers may cache provider handles internally). May raise
        ``HTTPException`` to report an unusable backend — the YAML
        path uses this for providers without managed support.
    :param token_ttl_s: Launch-token lifetime in seconds, e.g.
        ``90000`` (25h) for Modal. Must comfortably exceed the
        provider's maximum sandbox lifetime so a live sandbox can
        always re-authenticate its tunnel across reconnects.
    :param managed_launch_supported: Whether ``launcher_factory`` can
        actually serve a managed launch. The YAML path sets this from
        :data:`PROVIDERS_WITH_MANAGED_LAUNCH` — staged providers
        (``lakebox``) parse but get ``False``, since their factory
        rejects at launch. Defaults to ``True`` for
        directly-constructed configs (an embedding deployment's
        custom factory IS the support). Drives the unauthenticated
        ``managed_sandboxes_enabled`` capability flag on
        ``GET /v1/info``, which gates the web UI's sandbox option.
    :param provider: Short provider name surfaced to the web UI so the
        new-session sandbox option can be labeled per provider (e.g.
        ``"modal"`` → "Modal Sandbox", ``"lakebox"`` → "Databricks
        Sandbox"). The YAML path sets it from the parsed
        ``sandbox.provider``. ``None`` for directly-constructed
        embedding configs that don't name a provider — the UI then
        falls back to the generic "New Sandbox" label. Exposed (when
        managed launch is supported) on the unauthenticated
        ``GET /v1/info`` as ``sandbox_provider``.
    """

    server_url: str
    launcher_factory: Callable[[], SandboxLauncher]
    token_ttl_s: int
    managed_launch_supported: bool = True
    provider: str | None = None


@dataclass
class ManagedHostLaunch:
    """
    Result of a successful managed host launch.

    :param host_id: The registered host's identifier, e.g.
        ``"host_a1b2c3d4..."`` — feed this to the same launch-runner
        path an external ``host_id`` takes.
    :param workspace: Absolute workspace path created inside the
        sandbox, e.g. ``"/root/workspace"`` — or the cloned repository
        directory (e.g. ``"/root/workspace/myrepo"``) when the session
        requested a repository-URL workspace.
    """

    host_id: str
    workspace: str


@dataclass
class RepoWorkspace:
    """
    Parsed repository-URL workspace for a managed session.

    A managed create's ``workspace`` is a git repository URL with an
    optional ``#<branch>`` fragment (Docker build-context style): the
    URL fully describes what the server materializes inside the
    sandbox. Built by :func:`parse_repo_workspace` — construct via the
    parser, not directly, so every field has been validated.

    :param url: The clone URL with any fragment stripped, e.g.
        ``"https://github.com/org/repo.git"`` or
        ``"git@github.com:org/repo.git"``.
    :param branch: Branch to clone (``--branch … --single-branch``),
        e.g. ``"release-1.2"``, or ``None`` for the default branch.
    :param repo_name: Directory name the clone lands in under the
        sandbox workspace, derived from the URL's last path segment
        with ``.git`` stripped, e.g. ``"repo"``.
    """

    url: str
    branch: str | None
    repo_name: str


# A full 40-hex object id — rejected as a clone fragment: cloning a
# commit lands the agent on a detached HEAD it cannot push from.
_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

# Directory names a repo URL may resolve to. Conservative on purpose:
# the name is interpolated into an in-sandbox shell path.
_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# Characters git forbids in ref names (plus ``#``, which can never
# reach the fragment since the workspace splits on its FIRST ``#`` —
# a second ``#`` means the branch itself contains one, which the
# fragment form does not support).
_BRANCH_FORBIDDEN_CHARS = set(" \t~^:?*[\\#")


def is_repo_workspace(workspace: str) -> bool:
    """
    Return whether *workspace* is a repository-URL workspace.

    Used by the create-session schema to tell the managed form (a git
    URL) apart from the external form (an absolute host path) without
    fully parsing it.

    :param workspace: The raw request workspace, e.g.
        ``"https://github.com/org/repo"`` or ``"/Users/me/repo"``.
    :returns: ``True`` for the ``https://`` / ``git@`` URL forms.
    """
    return workspace.startswith(("https://", "git@"))


def _validate_clone_branch(fragment: str) -> str:
    """
    Validate a ``#<branch>`` fragment as a clonable branch name.

    :param fragment: The fragment text after the first ``#``, e.g.
        ``"release-1.2"``.
    :returns: The validated branch name, unchanged.
    :raises ValueError: When the fragment is empty, is a commit SHA
        (detached HEAD — pin commits via git worktree options
        instead), or violates git ref-name rules.
    """
    if not fragment:
        raise ValueError("the '#' fragment must name a branch, e.g. '#main'")
    if _COMMIT_SHA_RE.fullmatch(fragment):
        raise ValueError(
            "the '#' fragment must be a branch, not a commit SHA — a commit "
            "checkout would leave the agent on a detached HEAD it cannot push"
        )
    if (
        any(c in _BRANCH_FORBIDDEN_CHARS or ord(c) < 0x20 for c in fragment)
        or fragment.startswith(("-", "/"))
        or fragment.endswith(("/", "."))
        or ".." in fragment
        or "@{" in fragment
    ):
        raise ValueError(f"'{fragment}' is not a valid git branch name")
    return fragment


def _derive_repo_name(url: str) -> str:
    """
    Derive the clone directory name from a repository URL.

    :param url: The fragment-stripped clone URL, e.g.
        ``"https://github.com/org/repo.git"``.
    :returns: The last path segment with ``.git`` stripped, e.g.
        ``"repo"``.
    :raises ValueError: When no usable name can be derived (empty
        path, or a name that is not filesystem-safe).
    """
    last = url.rstrip("/").split("/")[-1]
    # scp-style URLs with a single-segment path ("git@host:repo.git")
    # have no "/" after the colon — take what follows it.
    if ":" in last:
        last = last.rsplit(":", 1)[-1]
    name = last[: -len(".git")] if last.endswith(".git") else last
    if not name or name in (".", "..") or not _REPO_NAME_RE.fullmatch(name):
        raise ValueError(
            f"could not derive a repository directory name from '{url}' — "
            "the URL must end in the repository name, e.g. "
            "'https://github.com/org/repo'"
        )
    return name


def parse_repo_workspace(workspace: str) -> RepoWorkspace:
    """
    Parse and validate a managed session's repository-URL workspace.

    Grammar (Docker build-context style)::

        <repo>[#<branch>]
        <repo> := https://<host>/<path>  |  git@<host>:<path>

    The fragment splits on the FIRST ``#``; branches containing ``#``
    are not supported in this form. Fails loud on anything malformed
    so a bad workspace 422s at validation instead of surfacing as a
    mid-provision clone error.

    :param workspace: The raw request workspace, e.g.
        ``"https://github.com/org/repo#release-1.2"``.
    :returns: The parsed, validated :class:`RepoWorkspace`.
    :raises ValueError: When the URL or branch fragment is malformed.
    """
    url, sep, fragment = workspace.partition("#")
    if any(ch.isspace() for ch in workspace):
        raise ValueError("a repository workspace must not contain whitespace")
    if url.startswith("https://"):
        host, slash, path = url[len("https://") :].partition("/")
        if not host or not slash or not path.strip("/"):
            raise ValueError(
                f"'{url}' is not a usable https repository URL — expected "
                "'https://<host>/<org>/<repo>'"
            )
    elif url.startswith("git@"):
        host, colon, path = url[len("git@") :].partition(":")
        if not host or not colon or not path.strip("/"):
            raise ValueError(
                f"'{url}' is not a usable ssh repository URL — expected 'git@<host>:<org>/<repo>'"
            )
    else:
        raise ValueError(
            f"'{url}' is not a supported repository URL — use "
            "'https://<host>/<org>/<repo>' or 'git@<host>:<org>/<repo>'"
        )
    branch = _validate_clone_branch(fragment) if sep else None
    return RepoWorkspace(url=url, branch=branch, repo_name=_derive_repo_name(url))


def _modal_launcher_factory(
    image: str | None,
    secrets: list[str] | None,
) -> Callable[[], SandboxLauncher]:
    """
    Build the launcher factory for the YAML ``provider: modal`` path.

    :param image: Registry image reference with omnigent pre-installed,
        e.g. ``"docker.io/me/omnigent-host:latest"``, or ``None`` to
        use the official prebaked host image (env-overridable; see
        :func:`omnigent.onboarding.sandboxes.modal._build_sandbox_image`).
    :param secrets: Modal secret names whose env vars (harness LLM
        credentials, gateway URLs) are injected into every sandbox,
        e.g. ``["omnigent-llm"]``, or ``None`` to resolve from the
        launcher's env-var fallback / inject nothing.
    :returns: A factory producing parameterized Modal launchers.
    """

    def _build() -> SandboxLauncher:
        """Construct the Modal launcher (lazy SDK import inside)."""
        from omnigent.onboarding.sandboxes.modal import ModalSandboxLauncher

        return ModalSandboxLauncher(image=image, secrets=secrets)

    return _build


def _unsupported_launcher_factory(provider: str) -> Callable[[], SandboxLauncher]:
    """
    Build a factory that rejects launch for a not-yet-supported provider.

    Lets a deployment stage ``sandbox:`` config for a provider before
    managed-launch support lands: parsing succeeds, and the clear 400
    only surfaces if a managed session is actually requested.

    :param provider: The configured provider name, e.g. ``"daytona"``.
    :returns: A factory that raises ``HTTPException`` 400 when called.
    """

    def _reject() -> SandboxLauncher:
        """Reject the launch with the provider named."""
        raise HTTPException(
            status_code=400,
            detail=(
                f"managed hosts are not yet supported for the "
                f"'{provider}' sandbox provider — only "
                f"{', '.join(sorted(PROVIDERS_WITH_MANAGED_LAUNCH))} is implemented"
            ),
        )

    return _reject


def parse_sandbox_config(raw: object) -> ManagedSandboxConfig | None:
    """
    Parse and validate the server config's ``sandbox:`` section.

    Fails loud on malformed config (an operator typo should stop server
    startup, not surface as a runtime 502 on the first managed session).

    :param raw: The raw ``sandbox`` value from the server config YAML,
        e.g. ``{"provider": "modal", "server_url": "https://…",
        "modal": {"image": "docker.io/me/omnigent-host:latest"}}``.
        ``None`` when the section is absent.
    :returns: The parsed config, or ``None`` when *raw* is ``None``
        (managed hosts not configured).
    :raises ValueError: When the section is present but malformed.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("server config 'sandbox' must be a mapping")
    provider = raw.get("provider")
    if provider not in SUPPORTED_SANDBOX_PROVIDERS:
        supported = ", ".join(sorted(SUPPORTED_SANDBOX_PROVIDERS))
        raise ValueError(
            f"server config 'sandbox.provider' must be one of: {supported} (got {provider!r})"
        )
    server_url = raw.get("server_url")
    if not isinstance(server_url, str) or not server_url.strip():
        raise ValueError(
            "server config 'sandbox.server_url' is required — the public URL "
            "of this server that sandboxed hosts connect back to"
        )
    if provider == "modal":
        launcher_factory = _modal_launcher_factory(
            _parse_modal_image(raw), _parse_modal_secrets(raw)
        )
        token_ttl_s = MODAL_MANAGED_TOKEN_TTL_S
    elif provider == "daytona":
        launcher_factory = _daytona_launcher_factory(
            _parse_daytona_image(raw), _parse_daytona_env(raw)
        )
        token_ttl_s = DAYTONA_MANAGED_TOKEN_TTL_S
    elif provider == "cwsandbox":
        from omnigent.onboarding.sandboxes.cwsandbox import managed_token_ttl_s

        launcher_factory = _cwsandbox_launcher_factory(
            _parse_cwsandbox_image(raw), _parse_cwsandbox_env(raw)
        )
        # Derived from OMNIGENT_CWSANDBOX_MAX_LIFETIME_S so the token always
        # outlives the (operator-overridable) sandbox lifetime.
        token_ttl_s = managed_token_ttl_s()
    elif provider == "islo":
        launcher_factory = _islo_launcher_factory(
            image=_parse_provider_image(raw, "islo"),
            env=_parse_provider_env(raw, "islo"),
            base_url=_parse_provider_string(raw, "islo", "base_url"),
            gateway_profile=_parse_provider_string(raw, "islo", "gateway_profile"),
            snapshot_name=_parse_provider_string(raw, "islo", "snapshot_name"),
            workdir=_parse_provider_string(raw, "islo", "workdir"),
            vcpus=_parse_provider_positive_int(raw, "islo", "vcpus"),
            memory_mb=_parse_provider_positive_int(raw, "islo", "memory_mb"),
            disk_gb=_parse_provider_positive_int(raw, "islo", "disk_gb"),
        )
        token_ttl_s = ISLO_MANAGED_TOKEN_TTL_S
    elif provider == "docker":
        launcher_factory = _docker_launcher_factory(
            image=_parse_provider_image(raw, "docker"),
            env=_parse_provider_env(raw, "docker"),
            network=_parse_provider_string(raw, "docker", "network"),
            resources=_parse_provider_mapping(raw, "docker", "resources"),
            security=_parse_provider_mapping(raw, "docker", "security"),
        )
        token_ttl_s = DOCKER_MANAGED_TOKEN_TTL_S
    elif provider == "e2b":
        from omnigent.onboarding.sandboxes.e2b import managed_token_ttl_s

        launcher_factory = _e2b_launcher_factory(
            _parse_e2b_template(raw), _parse_provider_env(raw, "e2b")
        )
        # Derived from OMNIGENT_E2B_MAX_LIFETIME_S so the token always
        # outlives the (operator-overridable) sandbox lifetime — mirrors
        # the cwsandbox path.
        token_ttl_s = managed_token_ttl_s()
    else:
        launcher_factory = _unsupported_launcher_factory(provider)
        # Never consulted (the factory rejects before any token is
        # minted); the conservative modal TTL keeps the field total.
        token_ttl_s = MODAL_MANAGED_TOKEN_TTL_S
    return ManagedSandboxConfig(
        server_url=server_url.strip().rstrip("/"),
        launcher_factory=launcher_factory,
        token_ttl_s=token_ttl_s,
        managed_launch_supported=provider in PROVIDERS_WITH_MANAGED_LAUNCH,
        provider=provider,
    )


def _parse_modal_image(raw: dict[str, object]) -> str | None:
    """
    Extract and validate the modal image from the raw ``sandbox`` dict.

    The ``modal`` section and its ``image`` field are OPTIONAL — when
    absent, sandboxes boot from the official prebaked host image
    (env-overridable; see
    :func:`omnigent.onboarding.sandboxes.modal._build_sandbox_image`).
    A present-but-malformed value still fails loud.

    :param raw: The raw ``sandbox`` mapping (provider already known to
        be ``"modal"``).
    :returns: The validated image reference, or ``None`` to use the
        official default.
    :raises ValueError: When ``sandbox.modal`` is present but not a
        mapping, or ``sandbox.modal.image`` is present but not a
        non-empty string.
    """
    modal_raw = raw.get("modal")
    if modal_raw is None:
        return None
    if not isinstance(modal_raw, dict):
        raise ValueError("server config 'sandbox.modal' must be a mapping")
    image = modal_raw.get("image")
    if image is None:
        return None
    if not isinstance(image, str) or not image.strip():
        raise ValueError(
            "server config 'sandbox.modal.image' must be a registry image "
            "reference with omnigent pre-installed, e.g. "
            "'docker.io/me/omnigent-host:latest' (omit it to use the "
            "official image)"
        )
    return image.strip()


def _parse_modal_secrets(raw: dict[str, object]) -> list[str] | None:
    """
    Extract and validate the modal secret names from the ``sandbox`` dict.

    ``sandbox.modal.secrets`` names the Modal secrets whose env vars
    (harness LLM credentials, gateway base URLs) are injected into
    every managed sandbox. OPTIONAL — absent means the launcher's
    env-var fallback applies (or nothing is injected). A
    present-but-malformed value fails loud.

    :param raw: The raw ``sandbox`` mapping (provider already known to
        be ``"modal"``).
    :returns: The validated secret names, e.g. ``["omnigent-llm"]``,
        or ``None`` when not configured.
    :raises ValueError: When ``sandbox.modal`` is present but not a
        mapping, or ``sandbox.modal.secrets`` is present but not a
        list of non-empty strings.
    """
    modal_raw = raw.get("modal")
    if modal_raw is None:
        return None
    if not isinstance(modal_raw, dict):
        raise ValueError("server config 'sandbox.modal' must be a mapping")
    secrets = modal_raw.get("secrets")
    if secrets is None:
        return None
    if not isinstance(secrets, list) or not all(
        isinstance(name, str) and name.strip() for name in secrets
    ):
        raise ValueError(
            "server config 'sandbox.modal.secrets' must be a list of Modal "
            "secret names, e.g. ['omnigent-llm']"
        )
    return [name.strip() for name in secrets]


def _daytona_launcher_factory(
    image: str | None,
    env: list[str] | None,
) -> Callable[[], SandboxLauncher]:
    """
    Build the launcher factory for the YAML ``provider: daytona`` path.

    :param image: Registry image reference with omnigent pre-installed,
        e.g. ``"docker.io/me/omnigent-host:latest"``, or ``None`` to
        use the official prebaked host image (env-overridable; see
        :class:`omnigent.onboarding.sandboxes.daytona.DaytonaSandboxLauncher`).
    :param env: Names of server-process environment variables (harness
        LLM credentials, gateway URLs, ``GIT_TOKEN``) injected into
        every sandbox, e.g. ``["OPENAI_API_KEY", "GIT_TOKEN"]``, or
        ``None`` to resolve from the launcher's env-var fallback /
        inject nothing.
    :returns: A factory producing parameterized Daytona launchers.
    """

    def _build() -> SandboxLauncher:
        """Construct the Daytona launcher (lazy SDK import inside)."""
        from omnigent.onboarding.sandboxes.daytona import DaytonaSandboxLauncher

        return DaytonaSandboxLauncher(image=image, env=env)

    return _build


def _docker_launcher_factory(
    *,
    image: str | None,
    env: list[str] | None,
    network: str | None,
    resources: dict[str, object] | None,
    security: dict[str, object] | None,
) -> Callable[[], SandboxLauncher]:
    """
    Build the launcher factory for the YAML ``provider: docker`` path.

    :param image: Registry image with omnigent pre-installed, or ``None``
        to use the official prebaked host image (env-overridable).
    :param env: SERVER-process env var NAMES injected into every sandbox
        (LLM credentials, ``GIT_TOKEN``), or ``None``.
    :param network: Docker network the spawned host containers join (the
        segmented sandbox network), or ``None`` for the launcher default.
    :param resources: ``containers.run`` resource kwargs (``mem_limit`` /
        ``nano_cpus`` / ``pids_limit``), or ``None``.
    :param security: ``containers.run`` security kwargs (``security_opt`` /
        ``cap_drop``), or ``None``.
    :returns: A factory producing parameterized Docker launchers.
    """

    def _build() -> SandboxLauncher:
        """Construct the Docker launcher (lazy SDK import inside)."""
        from omnigent.onboarding.sandboxes.docker import DockerSandboxLauncher

        return DockerSandboxLauncher(
            image=image,
            env=env,
            network=network,
            resources=resources,
            security=security,
        )

    return _build


def _parse_provider_mapping(
    raw: dict[str, object],
    provider: str,
    key: str,
) -> dict[str, object] | None:
    """
    Extract and validate a nested mapping field from a provider section.

    ``sandbox.<provider>.<key>`` is OPTIONAL — absent means ``None``. A
    present-but-non-mapping value fails loud (operator typo stops startup).

    :param raw: The raw ``sandbox`` mapping.
    :param provider: Provider section name, e.g. ``"docker"``.
    :param key: Field name within the section, e.g. ``"resources"``.
    :returns: The validated mapping (a shallow copy), or ``None``.
    :raises ValueError: When the section or the field is present but not a
        mapping.
    """
    section = _parse_provider_section(raw, provider)
    if section is None:
        return None
    value = section.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"server config 'sandbox.{provider}.{key}' must be a mapping")
    return dict(value)


def _parse_daytona_image(raw: dict[str, object]) -> str | None:
    """
    Extract and validate the daytona image from the ``sandbox`` dict.

    The ``daytona`` section and its ``image`` field are OPTIONAL —
    when absent, sandboxes boot from the official prebaked host image
    (env-overridable; see
    :mod:`omnigent.onboarding.sandboxes.daytona`). A
    present-but-malformed value still fails loud.

    :param raw: The raw ``sandbox`` mapping (provider already known to
        be ``"daytona"``).
    :returns: The validated image reference, or ``None`` to use the
        official default.
    :raises ValueError: When ``sandbox.daytona`` is present but not a
        mapping, or ``sandbox.daytona.image`` is present but not a
        non-empty string.
    """
    daytona_raw = raw.get("daytona")
    if daytona_raw is None:
        return None
    if not isinstance(daytona_raw, dict):
        raise ValueError("server config 'sandbox.daytona' must be a mapping")
    image = daytona_raw.get("image")
    if image is None:
        return None
    if not isinstance(image, str) or not image.strip():
        raise ValueError(
            "server config 'sandbox.daytona.image' must be a registry image "
            "reference with omnigent pre-installed, e.g. "
            "'docker.io/me/omnigent-host:latest' (omit it to use the "
            "official image)"
        )
    return image.strip()


def _parse_daytona_env(raw: dict[str, object]) -> list[str] | None:
    """
    Extract and validate the daytona env names from the ``sandbox`` dict.

    ``sandbox.daytona.env`` names the SERVER-process environment
    variables whose values (harness LLM credentials, gateway base
    URLs, ``GIT_TOKEN``) are injected into every managed sandbox —
    names only, so secret values never live in the config file.
    OPTIONAL — absent means the launcher's env-var fallback applies
    (or nothing is injected). A present-but-malformed value fails
    loud.

    :param raw: The raw ``sandbox`` mapping (provider already known to
        be ``"daytona"``).
    :returns: The validated env var names, e.g.
        ``["OPENAI_API_KEY", "GIT_TOKEN"]``, or ``None`` when not
        configured.
    :raises ValueError: When ``sandbox.daytona`` is present but not a
        mapping, or ``sandbox.daytona.env`` is present but not a list
        of non-empty strings.
    """
    daytona_raw = raw.get("daytona")
    if daytona_raw is None:
        return None
    if not isinstance(daytona_raw, dict):
        raise ValueError("server config 'sandbox.daytona' must be a mapping")
    env = daytona_raw.get("env")
    if env is None:
        return None
    if not isinstance(env, list) or not all(
        isinstance(name, str) and name.strip() for name in env
    ):
        raise ValueError(
            "server config 'sandbox.daytona.env' must be a list of server "
            "environment variable NAMES to inject, e.g. ['OPENAI_API_KEY', "
            "'GIT_TOKEN']"
        )
    return [name.strip() for name in env]


def _cwsandbox_launcher_factory(
    image: str | None,
    env: list[str] | None,
) -> Callable[[], SandboxLauncher]:
    """Build the launcher factory for the YAML ``provider: cwsandbox`` path."""

    def _build() -> SandboxLauncher:
        from omnigent.onboarding.sandboxes.cwsandbox import CWSandboxLauncher

        return CWSandboxLauncher(image=image, env=env)

    return _build


def _parse_cwsandbox_image(raw: dict[str, object]) -> str | None:
    """Extract and validate ``sandbox.cwsandbox.image`` (optional)."""
    section = raw.get("cwsandbox")
    if section is None:
        return None
    if not isinstance(section, dict):
        raise ValueError("server config 'sandbox.cwsandbox' must be a mapping")
    image = section.get("image")
    if image is None:
        return None
    if not isinstance(image, str) or not image.strip():
        raise ValueError(
            "server config 'sandbox.cwsandbox.image' must be a registry image "
            "reference with omnigent pre-installed (omit it to use the official image)"
        )
    return image.strip()


def _parse_cwsandbox_env(raw: dict[str, object]) -> list[str] | None:
    """Extract and validate ``sandbox.cwsandbox.env`` — server env var NAMES (optional)."""
    section = raw.get("cwsandbox")
    if section is None:
        return None
    if not isinstance(section, dict):
        raise ValueError("server config 'sandbox.cwsandbox' must be a mapping")
    env = section.get("env")
    if env is None:
        return None
    if not isinstance(env, list) or not all(
        isinstance(name, str) and name.strip() for name in env
    ):
        raise ValueError(
            "server config 'sandbox.cwsandbox.env' must be a list of server "
            "environment variable NAMES to inject, e.g. ['ANTHROPIC_API_KEY', 'GIT_TOKEN']"
        )
    return [name.strip() for name in env]


def _e2b_launcher_factory(
    template: str | None,
    env: list[str] | None,
) -> Callable[[], SandboxLauncher]:
    """
    Build the launcher factory for the YAML ``provider: e2b`` path.

    :param template: E2B template NAME the Omnigent host image was built
        into (``e2b template build``), or ``None`` to use the launcher's
        env-var fallback / the default template. Unlike the other
        providers' ``image`` field this is NOT a registry reference —
        E2B boots from templates (see
        :class:`omnigent.onboarding.sandboxes.e2b.E2BSandboxLauncher`).
    :param env: Names of server-process environment variables (harness
        LLM credentials, gateway URLs, ``GIT_TOKEN``) injected into
        every sandbox, e.g. ``["OPENAI_API_KEY", "GIT_TOKEN"]``, or
        ``None`` to resolve from the launcher's env-var fallback /
        inject nothing.
    :returns: A factory producing parameterized E2B launchers.
    """

    def _build() -> SandboxLauncher:
        """Construct the E2B launcher (lazy SDK import inside)."""
        from omnigent.onboarding.sandboxes.e2b import E2BSandboxLauncher

        return E2BSandboxLauncher(template=template, env=env)

    return _build


def _parse_e2b_template(raw: dict[str, object]) -> str | None:
    """
    Extract and validate the e2b template from the ``sandbox`` dict.

    ``sandbox.e2b.template`` names the pre-built E2B template the
    Omnigent host image was built into — NOT a registry image reference
    (the wording every other provider's ``image`` field uses), because
    E2B cannot boot an arbitrary registry image. OPTIONAL — when absent,
    the launcher resolves :data:`~omnigent.onboarding.sandboxes.e2b.TEMPLATE_ENV_VAR`
    then the default template. A present-but-malformed value fails loud.

    :param raw: The raw ``sandbox`` mapping (provider already known to
        be ``"e2b"``).
    :returns: The validated template name, or ``None`` to use the
        launcher's fallback / default.
    :raises ValueError: When ``sandbox.e2b`` is present but not a
        mapping, or ``sandbox.e2b.template`` is present but not a
        non-empty string.
    """
    section = _parse_provider_section(raw, "e2b")
    if section is None:
        return None
    template = section.get("template")
    if template is None:
        return None
    if not isinstance(template, str) or not template.strip():
        raise ValueError(
            "server config 'sandbox.e2b.template' must be the NAME of a pre-built "
            "E2B template the omnigent host image was built into (e.g. "
            "'omnigent-host'; see deploy/e2b/README.md) — NOT a registry image "
            "reference (omit it to use the default template)"
        )
    return template.strip()


def _islo_launcher_factory(
    *,
    image: str | None,
    env: list[str] | None,
    base_url: str | None,
    gateway_profile: str | None,
    snapshot_name: str | None,
    workdir: str | None,
    vcpus: int | None,
    memory_mb: int | None,
    disk_gb: int | None,
) -> Callable[[], SandboxLauncher]:
    """
    Build the launcher factory for the YAML ``provider: islo`` path.

    :param image: Registry image reference with omnigent pre-installed,
        e.g. ``"docker.io/me/omnigent-host:latest"``, or ``None`` to
        use the official prebaked host image (env-overridable; see
        :class:`omnigent.onboarding.sandboxes.islo.IsloSandboxLauncher`).
    :param env: Names of server-process environment variables injected
        into every sandbox, e.g. ``["OPENAI_API_KEY", "GIT_TOKEN"]``,
        or ``None`` to resolve from the launcher's env-var fallback /
        inject nothing.
    :param base_url: Optional Islo API base URL override.
    :param gateway_profile: Optional Islo gateway profile name.
    :param snapshot_name: Optional Islo snapshot name.
    :param workdir: Optional sandbox working directory.
    :param vcpus: Optional vCPU count.
    :param memory_mb: Optional memory allocation in MiB.
    :param disk_gb: Optional disk allocation in GiB.
    :returns: A factory producing parameterized Islo launchers.
    """

    def _build() -> SandboxLauncher:
        """Construct the Islo launcher."""
        from omnigent.onboarding.sandboxes.islo import IsloSandboxLauncher

        return IsloSandboxLauncher(
            image=image,
            env=env,
            base_url=base_url,
            gateway_profile=gateway_profile,
            snapshot_name=snapshot_name,
            workdir=workdir,
            vcpus=vcpus,
            memory_mb=memory_mb,
            disk_gb=disk_gb,
        )

    return _build


def _parse_provider_section(raw: dict[str, object], provider: str) -> dict[str, object] | None:
    """
    Extract a provider-specific optional config block.

    :param raw: The raw ``sandbox`` mapping.
    :param provider: Provider block name, e.g. ``"islo"``.
    :returns: The provider mapping, or ``None`` when omitted.
    :raises ValueError: When the block is present but not a mapping.
    """
    section = raw.get(provider)
    if section is None:
        return None
    if not isinstance(section, dict):
        raise ValueError(f"server config 'sandbox.{provider}' must be a mapping")
    return section


def _parse_provider_image(raw: dict[str, object], provider: str) -> str | None:
    """
    Extract and validate a provider image from the raw ``sandbox`` dict.

    :param raw: The raw ``sandbox`` mapping.
    :param provider: Provider block name, e.g. ``"islo"``.
    :returns: The validated image reference, or ``None`` to use the
        official default.
    :raises ValueError: When the provider block or image value is
        malformed.
    """
    section = _parse_provider_section(raw, provider)
    if section is None:
        return None
    image = section.get("image")
    if image is None:
        return None
    if not isinstance(image, str) or not image.strip():
        raise ValueError(
            f"server config 'sandbox.{provider}.image' must be a registry image "
            "reference with omnigent pre-installed, e.g. "
            "'docker.io/me/omnigent-host:latest' (omit it to use the "
            "official image)"
        )
    return image.strip()


def _parse_provider_env(raw: dict[str, object], provider: str) -> list[str] | None:
    """
    Extract and validate provider env passthrough names.

    :param raw: The raw ``sandbox`` mapping.
    :param provider: Provider block name, e.g. ``"islo"``.
    :returns: Validated environment variable names, or ``None`` when
        not configured.
    :raises ValueError: When the provider block or env list is
        malformed.
    """
    section = _parse_provider_section(raw, provider)
    if section is None:
        return None
    env = section.get("env")
    if env is None:
        return None
    if not isinstance(env, list) or not all(
        isinstance(name, str) and name.strip() for name in env
    ):
        raise ValueError(
            f"server config 'sandbox.{provider}.env' must be a list of server "
            "environment variable NAMES to inject, e.g. ['OPENAI_API_KEY', "
            "'GIT_TOKEN']"
        )
    return [name.strip() for name in env]


def _parse_provider_string(raw: dict[str, object], provider: str, key: str) -> str | None:
    """
    Extract and validate an optional provider string field.

    :param raw: The raw ``sandbox`` mapping.
    :param provider: Provider block name, e.g. ``"islo"``.
    :param key: Field name under the provider block.
    :returns: The stripped string, or ``None`` when omitted.
    :raises ValueError: When the field is present but not a non-empty
        string.
    """
    section = _parse_provider_section(raw, provider)
    if section is None:
        return None
    value = section.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"server config 'sandbox.{provider}.{key}' must be a non-empty string")
    return value.strip()


def _parse_provider_positive_int(raw: dict[str, object], provider: str, key: str) -> int | None:
    """
    Extract and validate an optional positive integer provider field.

    :param raw: The raw ``sandbox`` mapping.
    :param provider: Provider block name, e.g. ``"islo"``.
    :param key: Field name under the provider block.
    :returns: The integer, or ``None`` when omitted.
    :raises ValueError: When the field is present but is not a positive
        integer.
    """
    section = _parse_provider_section(raw, provider)
    if section is None:
        return None
    value = section.get(key)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"server config 'sandbox.{provider}.{key}' must be a positive integer")
    return value


async def launch_managed_host(
    *,
    config: ManagedSandboxConfig,
    owner: str,
    host_store: HostStore,
    repo: RepoWorkspace | None = None,
    on_stage: Callable[[str], None] | None = None,
) -> ManagedHostLaunch:
    """
    Provision a sandbox, start a host in it, and wait until it registers.

    Sequence: provision sandbox → pre-register the host row with its
    launch-token digest (so the credential resolves by the time the
    host dials the tunnel) → optionally clone the requested repository
    → start ``omnigent host`` inside the sandbox with the token +
    identity in its environment → poll the hosts table until the host
    is online. Any failure after provisioning terminates the sandbox
    and deletes the host row (which revokes the token) before
    re-raising.

    :param config: The deployment's sandbox config (YAML-parsed or
        directly constructed with a custom launcher factory).
    :param owner: User the managed host acts for — the session
        creator, e.g. ``"alice@example.com"`` (or the reserved local
        user on single-user servers).
    :param host_store: Persistent host registrations — receives the
        pre-registered host row and is polled for the sandbox host
        coming online.
    :param repo: Parsed repository-URL workspace to clone into the
        sandbox as the session's working directory, or ``None`` for
        an empty workspace. Private repositories authenticate via the
        host image's git credential helper when the sandbox env
        carries ``GIT_TOKEN`` (injected through Modal secrets — see
        deploy/modal/README.md "Git credentials").
    :param on_stage: Progress observer invoked as the launch pipeline
        advances, with the stage just entered: ``"cloning"`` (when
        *repo* is set) then ``"starting"``. May be called from a
        worker thread (the sandbox exec steps run via
        ``asyncio.to_thread``), so it must be thread-safe. ``None``
        disables progress reporting.
    :returns: The registered host id + in-sandbox workspace path
        (the cloned repository directory when *repo* is set).
    :raises HTTPException: 400 when the configured provider lacks
        managed-launch support; 502 when provisioning, cloning, host
        startup, or registration fails.
    """
    launcher = config.launcher_factory()
    host_id = f"host_{uuid.uuid4().hex}"
    # Visible label in the host picker; (owner, name) is the hosts
    # table PK, so embed the host_id's leading hex for uniqueness
    # across a user's managed sandboxes.
    host_name = f"managed-{host_id[len('host_') : len('host_') + 8]}"
    try:
        await asyncio.to_thread(launcher.prepare)
        sandbox_id = await asyncio.to_thread(launcher.provision, host_name)
    except click.ClickException as exc:
        raise HTTPException(
            status_code=502,
            detail=f"managed sandbox launch failed: {exc.message}",
        ) from exc
    workspace = await _arm_and_start_host(
        launcher=launcher,
        config=config,
        host_store=host_store,
        host_id=host_id,
        host_name=host_name,
        owner=owner,
        sandbox_id=sandbox_id,
        repo=repo,
        on_stage=on_stage,
    )
    return ManagedHostLaunch(host_id=host_id, workspace=workspace)


async def relaunch_managed_host(
    *,
    config: ManagedSandboxConfig,
    host: Host,
    host_store: HostStore,
    repo: RepoWorkspace | None = None,
    on_stage: Callable[[str], None] | None = None,
) -> ManagedHostLaunch:
    """
    Provision a NEW sandbox generation for an existing managed host.

    The host identity is durable while its sandbox is not: when the
    sandbox dies (the provider's lifetime cap, a crash, a manual
    terminate), the host row and the sessions bound to it remain.
    This relaunch keeps that identity — terminate the old sandbox
    (best-effort; it is usually already gone), provision a fresh one,
    and re-arm the SAME host row with a new token + sandbox id (which
    atomically revokes the previous generation's token).

    The new sandbox starts from the image — workspace contents of the
    dead generation are gone. Passing *repo* re-clones the session's
    repository so the workspace is restored to its create-time state.

    Unlike a first launch, a failure here keeps the host row (only the
    new sandbox is torn down and the armed token revoked), so the
    session binding survives and a later attempt can retry.

    :param config: The deployment's sandbox config.
    :param host: The existing managed host row to relaunch
        (``sandbox_provider`` set; callers guard on that).
    :param host_store: Persistent host registrations.
    :param repo: Repository to re-clone as the workspace, or ``None``
        for an empty workspace.
    :param on_stage: Progress observer forwarded to
        :func:`_arm_and_start_host`; see :func:`launch_managed_host`.
        ``None`` disables progress reporting.
    :returns: The (unchanged) host id + fresh in-sandbox workspace.
    :raises HTTPException: 400 when the host's recorded provider no
        longer matches the configured launcher; 502 when
        provisioning, cloning, host startup, or registration fails.
    """
    launcher = _launcher_for_teardown(host, config)
    if launcher is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"the '{host.sandbox_provider}' sandbox provider this host "
                "was launched with is no longer configured on this server"
            ),
        )
    # The old generation is normally already dead (that is why we are
    # here), but terminate defensively so a transient tunnel outage
    # can never leave two live sandboxes claiming one host identity.
    await _terminate_sandbox_best_effort(launcher, host)
    try:
        await asyncio.to_thread(launcher.prepare)
        sandbox_id = await asyncio.to_thread(launcher.provision, host.name)
    except click.ClickException as exc:
        raise HTTPException(
            status_code=502,
            detail=f"managed sandbox relaunch failed: {exc.message}",
        ) from exc
    workspace = await _arm_and_start_host(
        launcher=launcher,
        config=config,
        host_store=host_store,
        host_id=host.host_id,
        host_name=host.name,
        owner=host.owner,
        sandbox_id=sandbox_id,
        repo=repo,
        on_stage=on_stage,
        keep_host_on_failure=True,
    )
    return ManagedHostLaunch(host_id=host.host_id, workspace=workspace)


async def _arm_and_start_host(
    *,
    launcher: SandboxLauncher,
    config: ManagedSandboxConfig,
    host_store: HostStore,
    host_id: str,
    host_name: str,
    owner: str,
    sandbox_id: str,
    repo: RepoWorkspace | None = None,
    on_stage: Callable[[str], None] | None = None,
    keep_host_on_failure: bool = False,
) -> str:
    """
    Arm the credential, start the in-sandbox host, and await its
    registration — tearing the sandbox down on any failure.

    The credential is registered BEFORE the host process starts, so
    the token is resolvable by the time the host first dials the
    tunnel. A failure in any later step terminates the sandbox and
    revokes the armed token before re-raising — by deleting the host
    row (first launch: the row would otherwise be an unusable picker
    ghost) or, on a relaunch, by clearing the credential columns only
    (the durable row keeps the session binding alive for a retry).

    :param launcher: The launcher holding the provisioned sandbox.
    :param config: The deployment's sandbox config.
    :param host_store: Persistent host registrations.
    :param host_id: Server-chosen host identity, e.g.
        ``"host_a1b2c3d4..."``.
    :param host_name: Server-chosen host display name, e.g.
        ``"managed-a1b2c3d4"``.
    :param owner: User the managed host acts for, e.g.
        ``"alice@example.com"``.
    :param sandbox_id: The provisioned sandbox, e.g. ``"sb-a1b2c3"``.
    :param repo: Repository to clone as the workspace, or ``None``
        for an empty workspace.
    :param on_stage: Progress observer forwarded to
        :func:`_start_host_in_sandbox`; see
        :func:`launch_managed_host`. ``None`` disables progress
        reporting.
    :param keep_host_on_failure: ``True`` on a relaunch — failure
        cleanup terminates the new sandbox and revokes the token but
        keeps the host row. ``False`` (first launch) deletes the row.
    :returns: The absolute in-sandbox workspace path.
    :raises HTTPException: 502 when cloning, host startup, or
        registration fails.
    """
    token = secrets.token_urlsafe(32)
    record = await asyncio.to_thread(
        host_store.register_managed_host,
        host_id=host_id,
        name=host_name,
        owner=owner,
        token=token,
        provider=launcher.provider,
        sandbox_id=sandbox_id,
        token_expires_at=now_epoch() + config.token_ttl_s,
    )
    try:
        workspace = await asyncio.to_thread(
            _start_host_in_sandbox,
            launcher,
            sandbox_id,
            token=token,
            host_id=host_id,
            host_name=host_name,
            server_url=config.server_url,
            repo=repo,
            on_stage=on_stage,
        )
        await _wait_for_host_online(host_store, host_id)
    except Exception as exc:
        # Broad on purpose: any post-provision failure — launcher CLI
        # errors, provider SDK exceptions (e.g. Modal's
        # SandboxTerminated), raw network errors from the in-sandbox
        # exec — must tear down the sandbox and revoke the armed token,
        # or the sandbox leaks running until the provider's lifetime
        # cap. Cleanup-then-reraise at a system boundary, not a
        # swallow: every path below re-raises as an HTTPException.
        if keep_host_on_failure:
            await _terminate_sandbox_best_effort(launcher, record)
            await asyncio.to_thread(host_store.revoke_launch_token, host_id)
        else:
            await terminate_managed_host(record, host_store, config)
        if isinstance(exc, HTTPException):
            raise
        message = exc.message if isinstance(exc, click.ClickException) else str(exc)
        raise HTTPException(
            status_code=502,
            detail=f"managed sandbox host startup failed: {message}",
        ) from exc
    return workspace


def _start_host_in_sandbox(
    launcher: SandboxLauncher,
    sandbox_id: str,
    *,
    token: str,
    host_id: str,
    host_name: str,
    server_url: str,
    repo: RepoWorkspace | None = None,
    on_stage: Callable[[str], None] | None = None,
) -> str:
    """
    Create the workspace and start ``omnigent host`` inside a sandbox.

    When *repo* is set, the repository is cloned into
    ``<workspace>/<repo_name>`` before the host starts, and that
    directory becomes the returned workspace.

    The host runs detached (``setsid``-backgrounded with its output in
    :data:`_HOST_LOG_PATH`) so it outlives the exec session that
    spawned it. Identity + credential ride the process environment —
    nothing is written to the sandbox's config files.

    :param launcher: The provider launcher holding the sandbox.
    :param sandbox_id: The provisioned sandbox, e.g. ``"sb-a1b2c3"``.
    :param token: The raw launch token the host authenticates with.
    :param host_id: Server-chosen host identity, e.g.
        ``"host_a1b2c3d4..."``.
    :param host_name: Server-chosen host display name, e.g.
        ``"managed-a1b2c3d4"``.
    :param server_url: URL of this server the host dials back to,
        e.g. ``"https://omnigent.example.com"``.
    :param repo: Repository to clone as the workspace, or ``None``
        for an empty workspace.
    :param on_stage: Progress observer invoked with ``"cloning"``
        before the repository clone (when *repo* is set) and
        ``"starting"`` before the host process launches. Runs on
        this (worker) thread, so it must be thread-safe. ``None``
        disables progress reporting.
    :returns: The absolute in-sandbox workspace path, e.g.
        ``"/root/workspace"`` (or ``"/root/workspace/myrepo"`` when
        *repo* is set).
    :raises click.ClickException: If a sandbox command fails, the
        clone fails, or the sandbox's ``$HOME`` cannot be resolved.
    """
    # The image (and the user it runs as) is operator-supplied, so the
    # home directory isn't knowable statically — ask the sandbox.
    home = launcher.run(sandbox_id, 'printf %s "$HOME"').stdout.strip()
    if not home:
        raise click.ClickException(
            f"could not resolve $HOME inside sandbox '{sandbox_id}' — "
            "the configured image must provide a usable shell environment"
        )
    workspace = f"{home}/workspace"
    launcher.run(sandbox_id, f"mkdir -p {shlex.quote(workspace)}")
    if repo is not None:
        if on_stage is not None:
            on_stage("cloning")
        workspace = _clone_repo_workspace(launcher, sandbox_id, repo, workspace)
    # "starting" covers from here through host registration — the
    # caller's _wait_for_host_online poll resolves it.
    if on_stage is not None:
        on_stage("starting")
    env_prefix = " ".join(
        f"{key}={shlex.quote(value)}"
        for key, value in (
            (HOST_TOKEN_ENV_VAR, token),
            (HOST_ID_ENV_VAR, host_id),
            (HOST_NAME_ENV_VAR, host_name),
        )
    )
    launcher.run(
        sandbox_id,
        # setsid + nohup + redirects detach the host from the exec
        # session: the exec's bash exits immediately (the trailing echo
        # gives it a clean foreground completion) while the host keeps
        # running for the sandbox's lifetime.
        f"{env_prefix} setsid nohup omnigent host "
        f"--server {shlex.quote(server_url)} "
        f"> {_HOST_LOG_PATH} 2>&1 < /dev/null & echo launched",
    )
    return workspace


def _clone_repo_workspace(
    launcher: SandboxLauncher,
    sandbox_id: str,
    repo: RepoWorkspace,
    workspace: str,
) -> str:
    """
    Clone a session's repository inside the sandbox.

    Public repositories clone anonymously; private ones authenticate
    through the host image's env-driven git credential helper when the
    sandbox carries ``GIT_TOKEN`` (see deploy/modal/README.md "Git
    credentials"). ``--single-branch`` keeps branch-pinned clones fast
    on large repos; ``--`` separates options from the (user-supplied,
    already-validated) URL so it can never be parsed as a flag.

    :param launcher: The provider launcher holding the sandbox.
    :param sandbox_id: The provisioned sandbox, e.g. ``"sb-a1b2c3"``.
    :param repo: The parsed repository workspace to clone.
    :param workspace: The sandbox workspace root the clone lands
        under, e.g. ``"/root/workspace"``.
    :returns: The cloned repository directory, e.g.
        ``"/root/workspace/myrepo"``.
    :raises click.ClickException: When the clone fails (bad URL, no
        such branch, missing/insufficient credentials, …), with the
        repository named for the create-session error surface.
    """
    clone_dir = f"{workspace}/{repo.repo_name}"
    branch_args = (
        f"--branch {shlex.quote(repo.branch)} --single-branch " if repo.branch is not None else ""
    )
    try:
        launcher.run(
            sandbox_id,
            f"git clone {branch_args}-- {shlex.quote(repo.url)} {shlex.quote(clone_dir)}",
        )
    except click.ClickException as exc:
        # Provider boundary: re-raise with the repository named so the
        # create-session 502 says WHAT failed to clone, not just that a
        # sandbox command exited non-zero.
        raise click.ClickException(
            f"failed to clone repository '{repo.url}'"
            f"{f' (branch {repo.branch!r})' if repo.branch else ''}: {exc.message}"
        ) from exc
    return clone_dir


async def _wait_for_host_online(host_store: HostStore, host_id: str) -> None:
    """
    Poll the hosts table until the sandbox host registers, or time out.

    :param host_store: Persistent host registrations.
    :param host_id: The launched host's identifier.
    :raises HTTPException: 502 when the host does not come online
        within :data:`MANAGED_HOST_ONLINE_TIMEOUT_S`.
    """
    deadline = time.monotonic() + MANAGED_HOST_ONLINE_TIMEOUT_S
    while time.monotonic() < deadline:
        if await asyncio.to_thread(host_store.is_online, host_id):
            return
        await asyncio.sleep(_ONLINE_POLL_INTERVAL_S)
    raise HTTPException(
        status_code=502,
        detail=(
            f"managed host did not come online within "
            f"{MANAGED_HOST_ONLINE_TIMEOUT_S}s — check {_HOST_LOG_PATH} "
            "inside the sandbox"
        ),
    )


def _launcher_for_teardown(
    host: Host,
    config: ManagedSandboxConfig | None,
) -> SandboxLauncher | None:
    """
    Resolve the launcher that can terminate a managed host's sandbox.

    The deployment's CURRENT launcher factory is only usable when its
    provider matches the provider recorded on the host row at launch —
    a config change between launch and teardown must not aim a
    different provider's terminate at a stale sandbox id.

    :param host: The managed host being torn down.
    :param config: The deployment's current sandbox config, or ``None``
        when the ``sandbox:`` section has been removed since launch.
    :returns: A launcher whose provider matches the row, or ``None``
        when no matching launcher is available.
    """
    if config is None:
        return None
    try:
        launcher = config.launcher_factory()
    except HTTPException:
        # The YAML path's unsupported-provider factory raises; there is
        # no launcher to terminate with.
        return None
    if launcher.provider != host.sandbox_provider:
        return None
    return launcher


async def terminate_managed_host(
    host: Host,
    host_store: HostStore,
    config: ManagedSandboxConfig | None,
) -> None:
    """
    Terminate a managed host's sandbox and delete its host row.

    Deleting the row is both teardown and revocation in one operation:
    the host disappears from the picker AND its launch token stops
    resolving. Best-effort on the sandbox side: termination failures
    (or a missing/mismatched launcher after a config change) are
    logged, not raised — the provider's lifetime cap reaps stragglers,
    and the caller (session delete / launch-failure cleanup) must not
    be blocked by provider hiccups.

    :param host: The managed host to tear down (``sandbox_provider`` /
        ``sandbox_id`` set; callers guard on that).
    :param host_store: Store holding the host row.
    :param config: The deployment's current sandbox config (supplies
        the launcher for the provider-side terminate), or ``None``
        when managed hosts are no longer configured.
    """
    launcher = _launcher_for_teardown(host, config)
    await _terminate_sandbox_best_effort(launcher, host)
    await asyncio.to_thread(host_store.delete_host, host.host_id)


async def _terminate_sandbox_best_effort(
    launcher: SandboxLauncher | None,
    host: Host,
) -> None:
    """
    Terminate a managed host's sandbox without touching its row.

    Best-effort by design: termination failures (or a
    missing/mismatched launcher after a config change) are logged, not
    raised — the provider's lifetime cap reaps stragglers, and callers
    (session delete, launch-failure cleanup, relaunch) must not be
    blocked by provider hiccups.

    :param launcher: Provider-matched launcher from
        :func:`_launcher_for_teardown`, or ``None`` when no matching
        launcher is available (logged, nothing terminated).
    :param host: The host whose ``sandbox_id`` names the sandbox.
    """
    if launcher is not None and host.sandbox_id is not None:
        try:
            await asyncio.to_thread(launcher.terminate, host.sandbox_id)
        except Exception:  # noqa: BLE001 — deliberate broad catch: this is a
            # provider-API boundary on a cleanup path. The provider SDK can
            # fail here in many shapes (auth/config ClickException, network
            # errors, SDK-internal exceptions), the sandbox may already be
            # gone past its lifetime cap, and NONE of those may block the
            # caller's remaining cleanup (deleting the host row / revoking
            # the launch token), which only we can do.
            _logger.warning(
                "Failed to terminate managed sandbox %s (provider=%s) for host %s",
                host.sandbox_id,
                host.sandbox_provider,
                host.host_id,
                exc_info=True,
            )
    else:
        _logger.warning(
            "No launcher available for managed sandbox provider %s; "
            "sandbox %s must be deleted with the provider's own tooling",
            host.sandbox_provider,
            host.sandbox_id,
        )
