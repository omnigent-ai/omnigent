"""
Tests for the AWS Lambda MicroVMs (entrypoint-as-host) sandbox launcher.

The launcher talks to AWS only through a lazily-created ``lambda-microvms`` boto3
client, so the SDK-driven tests inject a small recording fake in place of that
client (no real AWS, no boto3 needed). The pure request-builder
(:func:`build_run_microvm_kwargs`) and the TTL / idle-policy helpers are tested
directly, since they carry the launch wiring and need no client at all.
"""

from __future__ import annotations

import json
from typing import Any

import click
import pytest

from omnigent.host.identity import (
    HOST_ID_ENV_VAR,
    HOST_NAME_ENV_VAR,
    HOST_TOKEN_ENV_VAR,
)
from omnigent.onboarding.sandboxes.lambda_microvm import (
    DEBUG_INGRESS_CONNECTORS_ENV_VAR,
    IMAGE_IDENTIFIER_ENV_VAR,
    MAX_LIFETIME_ENV_VAR,
    LambdaMicroVMSandboxLauncher,
    build_idle_policy,
    build_run_microvm_kwargs,
    managed_token_ttl_s,
    resolve_max_lifetime_s,
)

_RUN_KW = {
    "image_identifier": "omnigent-host",
    "execution_role_arn": "arn:aws:iam::123456789012:role/omnigent-microvm-exec",
    "host_id": "host_abcdef",
    "host_name": "managed-abcdef",
    "server_url": "https://srv.example.com",
    "token": "launch-token-xyz",
    "env_literals": {"ANTHROPIC_API_KEY": "sk-test"},
}


# ── pure builder / helper tests (no boto3) ──────────────────


def test_build_run_microvm_kwargs_injects_identity_token_and_server() -> None:
    """Identity, launch token, and server URL ride the /run hook payload.

    RunMicrovm has no per-launch environmentVariables parameter, so the
    per-launch identity travels in runHookPayload (a JSON string the platform
    delivers as the /run hook body).
    """
    kwargs = build_run_microvm_kwargs(**_RUN_KW)
    assert "environmentVariables" not in kwargs
    env = json.loads(kwargs["runHookPayload"])
    assert env[HOST_ID_ENV_VAR] == "host_abcdef"
    assert env[HOST_NAME_ENV_VAR] == "managed-abcdef"
    assert env[HOST_TOKEN_ENV_VAR] == "launch-token-xyz"
    assert env["OMNIGENT_SERVER"] == "https://srv.example.com"
    assert env["IS_SANDBOX"] == "1"
    # Harness credential passthrough is merged into the same payload.
    assert env["ANTHROPIC_API_KEY"] == "sk-test"


def test_build_run_microvm_kwargs_sets_image_role_and_idle_policy() -> None:
    """The request names the image + execution role and carries an idle policy."""
    kwargs = build_run_microvm_kwargs(**_RUN_KW)
    assert kwargs["imageIdentifier"] == "omnigent-host"
    assert kwargs["executionRoleArn"] == "arn:aws:iam::123456789012:role/omnigent-microvm-exec"
    assert kwargs["idlePolicy"]["autoResumeEnabled"] is True
    assert kwargs["maximumDurationInSeconds"] == resolve_max_lifetime_s()
    # No version requested → the key is omitted (account's latest is used).
    assert "imageVersion" not in kwargs


def test_build_run_microvm_kwargs_includes_version_when_set() -> None:
    """A pinned image version is threaded into the request."""
    kwargs = build_run_microvm_kwargs(**_RUN_KW, image_version="2.1")
    assert kwargs["imageVersion"] == "2.1"


def test_build_idle_policy_enables_auto_resume() -> None:
    """The idle policy auto-suspends on idle; autoResumeEnabled stays on as a
    backstop (this host receives no inbound endpoint traffic, so every real
    wake is the server's explicit resume-microvm)."""
    policy = build_idle_policy()
    assert policy["autoResumeEnabled"] is True
    assert policy["maxIdleDurationSeconds"] > 0
    assert policy["suspendedDurationSeconds"] > 0


def test_build_idle_policy_suspends_for_the_full_lifetime() -> None:
    """
    A suspended VM survives to the lifetime cap, not less.

    ``suspendedDurationSeconds`` is when Lambda TERMINATES a suspended VM, so
    anything below the lifetime silently kills sessions idle longer than it and
    the next wake fails with ResourceNotFoundException.
    """
    policy = build_idle_policy()
    assert policy["suspendedDurationSeconds"] == resolve_max_lifetime_s()
    assert policy["maxIdleDurationSeconds"] < policy["suspendedDurationSeconds"]


@pytest.mark.parametrize("lifetime_s", [3600, 7200])
def test_build_idle_policy_follows_lowered_lifetime(
    monkeypatch: pytest.MonkeyPatch, lifetime_s: int
) -> None:
    """A lowered lifetime override propagates into the idle policy.

    A fixed 8 h suspendedDurationSeconds would contradict a lowered
    maximumDurationInSeconds — the policy must track the resolved lifetime.
    """
    monkeypatch.setenv(MAX_LIFETIME_ENV_VAR, str(lifetime_s))
    policy = build_idle_policy()
    assert policy["suspendedDurationSeconds"] == lifetime_s
    assert policy["maxIdleDurationSeconds"] <= lifetime_s


def test_build_idle_policy_caps_idle_below_tiny_lifetime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lifetime below the 900 s idle default caps the idle window to it.

    Otherwise a 600 s VM would carry a 900 s idle timer — longer than its own
    life — which the API may reject and operators cannot reason about.
    """
    monkeypatch.setenv(MAX_LIFETIME_ENV_VAR, "600")
    policy = build_idle_policy()
    assert policy["maxIdleDurationSeconds"] == 600
    assert policy["suspendedDurationSeconds"] == 600


def test_build_run_microvm_kwargs_idle_policy_matches_lifetime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The built request's idle policy agrees with its maximumDurationInSeconds."""
    monkeypatch.setenv(MAX_LIFETIME_ENV_VAR, "3600")
    kwargs = build_run_microvm_kwargs(**_RUN_KW)
    assert kwargs["maximumDurationInSeconds"] == 3600
    assert kwargs["idlePolicy"]["suspendedDurationSeconds"] == 3600


def test_build_run_microvm_kwargs_rejects_oversized_payload() -> None:
    """A launch payload over the 16 KB runHookPayload cap fails fast.

    Passthrough credentials and repo fields aggregate into one string with a
    hard AWS-side 16,384-byte limit; the builder pre-checks so the failure is a
    pointed ClickException, not a run-time ValidationException.
    """
    oversized = {**_RUN_KW, "env_literals": {"BIG_SECRET": "x" * 17_000}}
    with pytest.raises(click.ClickException, match="runHookPayload"):
        build_run_microvm_kwargs(**oversized)


def test_build_run_microvm_kwargs_counts_utf8_bytes_not_chars() -> None:
    """The payload cap is measured in UTF-8 bytes, not characters."""
    # ~9k chars of a 2-byte codepoint → >16 KB encoded (plus JSON escaping).
    oversized = {**_RUN_KW, "env_literals": {"NOTE": "é" * 9_000}}
    with pytest.raises(click.ClickException, match="runHookPayload"):
        build_run_microvm_kwargs(**oversized)


def test_resolve_max_lifetime_defaults_to_eight_hours() -> None:
    """Absent an override, the requested lifetime is the 8 h platform cap."""
    assert resolve_max_lifetime_s() == 8 * 60 * 60


def test_max_lifetime_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lifetime env var overrides the default."""
    monkeypatch.setenv(MAX_LIFETIME_ENV_VAR, "3600")
    assert resolve_max_lifetime_s() == 3600


def test_max_lifetime_env_rejects_non_numeric(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-numeric lifetime override fails loud."""
    monkeypatch.setenv(MAX_LIFETIME_ENV_VAR, "soon")
    with pytest.raises(click.ClickException):
        resolve_max_lifetime_s()


@pytest.mark.parametrize("bad", ["0", "-1", "-3600", "inf", "nan", "0.5"])
def test_max_lifetime_env_rejects_non_positive_or_non_finite(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    """Zero, negative, non-finite, and sub-second (truncates to 0) overrides fail
    fast rather than building an invalid maximumDurationInSeconds that run-microvm
    rejects opaquely later."""
    monkeypatch.setenv(MAX_LIFETIME_ENV_VAR, bad)
    with pytest.raises(click.ClickException):
        resolve_max_lifetime_s()


def test_max_lifetime_env_rejects_over_eight_hour_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """An override above the 8 h platform cap fails fast with a clear message."""
    monkeypatch.setenv(MAX_LIFETIME_ENV_VAR, str(8 * 60 * 60 + 1))
    with pytest.raises(click.ClickException, match="exceeds"):
        resolve_max_lifetime_s()


def test_max_lifetime_env_accepts_exact_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 8 h cap itself is valid (boundary)."""
    monkeypatch.setenv(MAX_LIFETIME_ENV_VAR, str(8 * 60 * 60))
    assert resolve_max_lifetime_s() == 8 * 60 * 60


def test_managed_token_ttl_exceeds_lifetime() -> None:
    """The launch-token TTL always outlives the sandbox so it can reconnect."""
    assert managed_token_ttl_s() > resolve_max_lifetime_s()


# ── SDK-driven lifecycle tests (recording fake client) ──────


class _FakeMicroVMClient:
    """Recording stand-in for the ``lambda-microvms`` boto3 client."""

    def __init__(self, *, run_id: str = "microvm-0001") -> None:
        self._run_id = run_id
        self.run_calls: list[dict[str, Any]] = []
        self.resumed: list[str] = []
        self.terminated: list[str] = []
        # When set, terminate_microvm raises this to simulate an AWS error.
        self.terminate_error: Exception | None = None
        # When set, resume_microvm raises this to simulate an AWS error.
        self.resume_error: Exception | None = None

    def run_microvm(self, **kwargs: Any) -> dict[str, str]:
        self.run_calls.append(kwargs)
        return {"microvmId": self._run_id}

    def resume_microvm(self, *, microvmIdentifier: str) -> dict[str, str]:
        if self.resume_error is not None:
            raise self.resume_error
        self.resumed.append(microvmIdentifier)
        return {}

    def terminate_microvm(self, *, microvmIdentifier: str) -> dict[str, str]:
        if self.terminate_error is not None:
            raise self.terminate_error
        self.terminated.append(microvmIdentifier)
        return {}


def _launcher_with_fake(
    monkeypatch: pytest.MonkeyPatch, client: _FakeMicroVMClient, **kwargs: Any
) -> LambdaMicroVMSandboxLauncher:
    """Build a launcher whose ``_get_client`` returns the recording fake."""
    launcher = LambdaMicroVMSandboxLauncher(
        image_identifier="omnigent-host",
        execution_role_arn="arn:aws:iam::123456789012:role/omnigent-microvm-exec",
        **kwargs,
    )
    monkeypatch.setattr(launcher, "_get_client", lambda: client)
    return launcher


def test_provision_reserves_name_without_calling_aws(monkeypatch: pytest.MonkeyPatch) -> None:
    """Entrypoint-as-host: provision only reserves the name; no MicroVM is run."""
    client = _FakeMicroVMClient()
    launcher = _launcher_with_fake(monkeypatch, client)
    assert launcher.provision("managed-abc") == "managed-abc"
    assert client.run_calls == []


def test_start_host_runs_microvm_and_returns_workspace_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """start_host runs the MicroVM and returns the in-sandbox workspace path.

    NOT the provider's microvmId: the managed-launch framework persists this
    return value only as the session workspace (never a new sandbox id), and
    host.launch_runner validates it with Path(...).is_dir() inside the guest —
    so returning the opaque microvm_id here breaks every runner launch with
    "workspace path does not exist".
    """
    client = _FakeMicroVMClient(run_id="microvm-run-42")
    launcher = _launcher_with_fake(monkeypatch, client)
    workspace = launcher.start_host(
        "managed-abc",
        token="tok",
        host_id="host_x",
        host_name="managed-x",
        server_url="https://srv.example.com",
    )
    assert workspace == "/root/workspace"
    assert len(client.run_calls) == 1
    call = client.run_calls[0]
    assert call["imageIdentifier"] == "omnigent-host"
    assert json.loads(call["runHookPayload"])[HOST_TOKEN_ENV_VAR] == "tok"
    # run_microvm's returned microvmId is not the return value (see docstring
    # above) — it is surfaced on started_sandbox_id for the framework to persist
    # as the host row's sandbox_id, so terminate/resume key off the real id.
    assert launcher.started_sandbox_id == "microvm-run-42"


def test_start_host_records_real_microvm_id_for_framework(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The provider-assigned microvmId lands on started_sandbox_id (not the
    reserved provision name), so the managed-launch framework can persist the id
    AWS actually knows for later terminate / resume."""
    client = _FakeMicroVMClient(run_id="microvm-real-99")
    launcher = _launcher_with_fake(monkeypatch, client)
    assert launcher.started_sandbox_id is None
    launcher.start_host(
        "managed-reserved",
        token="tok",
        host_id="host_x",
        host_name="managed-x",
        server_url="https://srv.example.com",
    )
    assert launcher.started_sandbox_id == "microvm-real-99"
    assert launcher.started_sandbox_id != "managed-reserved"


def test_start_host_returns_clone_dir_when_repo_name_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repo workspace's returned path is the clone directory, not the bare
    workspace root — the runner's cwd must match where entrypoint.sh cloned."""
    client = _FakeMicroVMClient()
    launcher = _launcher_with_fake(monkeypatch, client)
    workspace = launcher.start_host(
        "managed-abc",
        token="tok",
        host_id="host_x",
        host_name="managed-x",
        server_url="https://srv.example.com",
        repo_url="https://github.com/org/repo.git",
        repo_name="repo",
    )
    assert workspace == "/root/workspace/repo"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("..", "repo"),
        (".", "repo"),
        ("-rf", "repo"),
        ("../../etc", "etc"),
        ("nested/path/proj", "proj"),
        ("normal-name", "normal-name"),
    ],
)
def test_start_host_workspace_matches_entrypoint_sanitization(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: str
) -> None:
    """The returned workspace path applies the SAME repo-name sanitization as
    start_host.sh — a divergent path fails Path(...).is_dir() inside the guest
    and no runner ever launches."""
    client = _FakeMicroVMClient()
    launcher = _launcher_with_fake(monkeypatch, client)
    workspace = launcher.start_host(
        "managed-abc",
        token="tok",
        host_id="host_x",
        host_name="managed-x",
        server_url="https://srv.example.com",
        repo_url="https://github.com/org/repo.git",
        repo_name=raw,
    )
    assert workspace == f"/root/workspace/{expected}"


def test_start_host_threads_host_config_write_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """host_config rides the /run payload as the rendered write command.

    There is no exec transport to install the config from the server side, so
    the shared marker-aware write command travels as OMNIGENT_HOST_CONFIG_CMD
    and start_host.sh runs it before starting the host.
    """
    client = _FakeMicroVMClient()
    launcher = _launcher_with_fake(monkeypatch, client)
    launcher.start_host(
        "managed-abc",
        token="tok",
        host_id="host_x",
        host_name="managed-x",
        server_url="https://srv.example.com",
        host_config={"providers": {"gw": {"type": "openai"}}},
    )
    env = json.loads(client.run_calls[0]["runHookPayload"])
    assert env["OMNIGENT_HOST_CONFIG_CMD"].startswith("python3 -c ")


def test_start_host_omits_host_config_cmd_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No host_config → no OMNIGENT_HOST_CONFIG_CMD in the payload (and no
    16 KB budget spent on it)."""
    client = _FakeMicroVMClient()
    launcher = _launcher_with_fake(monkeypatch, client)
    launcher.start_host(
        "managed-abc",
        token="tok",
        host_id="host_x",
        host_name="managed-x",
        server_url="https://srv.example.com",
    )
    env = json.loads(client.run_calls[0]["runHookPayload"])
    assert "OMNIGENT_HOST_CONFIG_CMD" not in env


def test_start_host_threads_repo_env_when_requested(monkeypatch: pytest.MonkeyPatch) -> None:
    """A repo workspace is passed to the image entrypoint via the /run payload."""
    client = _FakeMicroVMClient()
    launcher = _launcher_with_fake(monkeypatch, client)
    launcher.start_host(
        "managed-abc",
        token="tok",
        host_id="host_x",
        host_name="managed-x",
        server_url="https://srv.example.com",
        repo_url="https://github.com/org/repo.git",
        repo_branch="main",
        repo_name="repo",
    )
    env = json.loads(client.run_calls[0]["runHookPayload"])
    assert env["OMNIGENT_REPO_URL"] == "https://github.com/org/repo.git"
    assert env["OMNIGENT_REPO_BRANCH"] == "main"
    assert env["OMNIGENT_REPO_NAME"] == "repo"


def test_resume_calls_resume_microvm(monkeypatch: pytest.MonkeyPatch) -> None:
    """resume thaws the suspended MicroVM under the same id."""
    client = _FakeMicroVMClient()
    launcher = _launcher_with_fake(monkeypatch, client)
    launcher.resume("microvm-run-42")
    assert client.resumed == ["microvm-run-42"]


def test_resume_treats_already_running_conflict_as_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ConflictException (VM already RUNNING) is idempotent success.

    resume-microvm requires the SUSPENDED state and conflicts on a running VM.
    The wake path resumes with force=True when persisted liveness may be stale,
    so a redundant resume against a live VM is a normal event — it must not
    become a 502.
    """
    from botocore.exceptions import ClientError

    client = _FakeMicroVMClient()
    client.resume_error = ClientError(
        {"Error": {"Code": "ConflictException", "Message": "MicroVM is not suspended"}},
        "ResumeMicrovm",
    )
    launcher = _launcher_with_fake(monkeypatch, client)
    # Does not raise.
    launcher.resume("microvm-already-running")


def test_resume_raises_on_real_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-conflict AWS error still surfaces as a ClickException."""
    from botocore.exceptions import ClientError

    client = _FakeMicroVMClient()
    client.resume_error = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}},
        "ResumeMicrovm",
    )
    launcher = _launcher_with_fake(monkeypatch, client)
    with pytest.raises(click.ClickException):
        launcher.resume("microvm-gone")


def test_can_resume_flag_is_true() -> None:
    """The launcher advertises resume support so the wake path engages it."""
    assert LambdaMicroVMSandboxLauncher.can_resume is True


def test_resume_preserves_host_flag_is_true() -> None:
    """A snapshot thaw restores the running host + its token, so the wake path
    must NOT restart the host (that would start a second host / fresh VM)."""
    assert LambdaMicroVMSandboxLauncher.resume_preserves_host is True


def test_terminate_calls_terminate_microvm(monkeypatch: pytest.MonkeyPatch) -> None:
    """terminate releases the MicroVM's compute."""
    client = _FakeMicroVMClient()
    launcher = _launcher_with_fake(monkeypatch, client)
    launcher.terminate("microvm-run-42")
    assert client.terminated == ["microvm-run-42"]


def test_terminate_is_idempotent_on_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """A MicroVM already gone (ResourceNotFoundException) is idempotent success."""
    from botocore.exceptions import ClientError

    client = _FakeMicroVMClient()
    client.terminate_error = ClientError(
        {"Error": {"Code": "ResourceNotFoundException", "Message": "gone"}},
        "TerminateMicrovm",
    )
    launcher = _launcher_with_fake(monkeypatch, client)
    # Does not raise.
    launcher.terminate("microvm-already-gone")


def test_terminate_raises_on_real_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-not-found AWS error surfaces as a ClickException."""
    from botocore.exceptions import ClientError

    client = _FakeMicroVMClient()
    client.terminate_error = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "nope"}},
        "TerminateMicrovm",
    )
    launcher = _launcher_with_fake(monkeypatch, client)
    with pytest.raises(click.ClickException):
        launcher.terminate("microvm-run-42")


def test_no_exec_transport() -> None:
    """Entrypoint-as-host: the launcher inherits SandboxHostLauncher directly
    (like Kubernetes) and carries NO exec transport — there is no run()."""
    launcher = LambdaMicroVMSandboxLauncher(
        image_identifier="omnigent-host",
        execution_role_arn="arn:aws:iam::123456789012:role/x",
    )
    assert not hasattr(launcher, "run")


# ── config resolution (no client) ──────────────────────────


def test_prepare_requires_image_identifier(monkeypatch: pytest.MonkeyPatch) -> None:
    """Preflight fails loud when no MicroVM image is configured."""
    monkeypatch.delenv(IMAGE_IDENTIFIER_ENV_VAR, raising=False)
    # boto3 present in the test env; the missing-image check is what should fire.
    launcher = LambdaMicroVMSandboxLauncher(
        execution_role_arn="arn:aws:iam::123456789012:role/x",
    )
    with pytest.raises(click.ClickException, match="image"):
        launcher.prepare()


def test_sandbox_env_passthrough_reads_server_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Configured env NAMES resolve to the server process's values."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-live")
    launcher = LambdaMicroVMSandboxLauncher(
        image_identifier="omnigent-host",
        execution_role_arn="arn:aws:iam::123456789012:role/x",
        env=["OPENAI_API_KEY"],
    )
    assert launcher._resolve_sandbox_env() == {"OPENAI_API_KEY": "sk-live"}


def test_sandbox_env_passthrough_fails_loud_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured env NAME that is unset in the server env fails loud."""
    monkeypatch.delenv("MISSING_KEY", raising=False)
    launcher = LambdaMicroVMSandboxLauncher(
        image_identifier="omnigent-host",
        execution_role_arn="arn:aws:iam::123456789012:role/x",
        env=["MISSING_KEY"],
    )
    with pytest.raises(click.ClickException, match="MISSING_KEY"):
        launcher._resolve_sandbox_env()


@pytest.mark.parametrize(
    "reserved",
    ["OMNIGENT_SERVER", "IS_SANDBOX", HOST_ID_ENV_VAR, HOST_NAME_ENV_VAR, HOST_TOKEN_ENV_VAR],
)
def test_sandbox_env_passthrough_rejects_reserved_identity_names(
    monkeypatch: pytest.MonkeyPatch, reserved: str
) -> None:
    """A passthrough naming a reserved identity key fails loud — it would
    otherwise clobber the per-launch identity in the /run payload."""
    monkeypatch.setenv(reserved, "attacker-controlled")
    launcher = LambdaMicroVMSandboxLauncher(
        image_identifier="omnigent-host",
        execution_role_arn="arn:aws:iam::123456789012:role/x",
        env=[reserved],
    )
    with pytest.raises(click.ClickException, match="reserved"):
        launcher._resolve_sandbox_env()


def test_build_run_microvm_kwargs_identity_wins_over_passthrough() -> None:
    """Even if a passthrough value for a reserved key reaches the builder, the
    launch identity wins in the payload (merge-order backstop to the rejection)."""
    kw = {
        **_RUN_KW,
        "env_literals": {"OMNIGENT_SERVER": "https://evil.example", "IS_SANDBOX": "0"},
    }
    kwargs = build_run_microvm_kwargs(**kw)
    payload = json.loads(kwargs["runHookPayload"])
    assert payload["OMNIGENT_SERVER"] == _RUN_KW["server_url"]
    assert payload["IS_SANDBOX"] == "1"
    assert payload[HOST_TOKEN_ENV_VAR] == _RUN_KW["token"]


def test_build_run_microvm_kwargs_attaches_egress_connectors() -> None:
    """VPC egress connectors, when provided, ride the run request so the host
    reaches a private server over the customer VPC instead of the internet."""
    arns = ["arn:aws:lambda:us-east-1:123456789012:network-connector:vpc-egress"]
    kwargs = build_run_microvm_kwargs(**_RUN_KW, egress_network_connectors=arns)
    assert kwargs["egressNetworkConnectors"] == arns
    # Omitted by default (account-default public egress).
    assert "egressNetworkConnectors" not in build_run_microvm_kwargs(**_RUN_KW)


def test_debug_ingress_attaches_parsed_arns(monkeypatch: pytest.MonkeyPatch) -> None:
    """A real DEBUG_INGRESS value attaches the parsed connector ARNs."""
    monkeypatch.setenv(
        DEBUG_INGRESS_CONNECTORS_ENV_VAR,
        "arn:aws:lambda:us-east-1:123456789012:network-connector:shell , arn:...:nc2",
    )
    kwargs = build_run_microvm_kwargs(**_RUN_KW)
    assert kwargs["ingressNetworkConnectors"] == [
        "arn:aws:lambda:us-east-1:123456789012:network-connector:shell",
        "arn:...:nc2",
    ]


@pytest.mark.parametrize("blank", [",", " ", ", ,", ""])
def test_debug_ingress_separators_only_omits_key(
    monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    """A DEBUG_INGRESS value of only separators/whitespace is truthy but parses
    to [] — the key must be OMITTED (an explicit empty ingressNetworkConnectors
    is rejected by the run-microvm API), not sent as an empty list."""
    monkeypatch.setenv(DEBUG_INGRESS_CONNECTORS_ENV_VAR, blank)
    kwargs = build_run_microvm_kwargs(**_RUN_KW)
    assert "ingressNetworkConnectors" not in kwargs


# ── SDK availability (real botocore, no network) ────────────


def test_installed_botocore_ships_lambda_microvms_service_model() -> None:
    """The locked botocore carries the ``lambda-microvms`` service model.

    The extra's floor (boto3/botocore >= 1.43.35, the service's GA release)
    exists precisely so ``boto3.client("lambda-microvms")`` can resolve; this
    guards the pin against a future lockfile downgrade that would pass the
    import probe and fail at client construction.
    """
    botocore = pytest.importorskip("botocore")
    del botocore  # presence probe; the session below does the real check
    import botocore.session

    assert "lambda-microvms" in botocore.session.get_session().get_available_services()


def test_ensure_sdk_rejects_botocore_without_service_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_ensure_sdk fails loud (with an upgrade hint) on a pre-GA botocore."""
    pytest.importorskip("botocore")
    import botocore.session

    from omnigent.onboarding.sandboxes import lambda_microvm as mod

    class _OldSession:
        def get_available_services(self) -> list[str]:
            return ["s3", "lambda"]  # no lambda-microvms

    monkeypatch.setattr(botocore.session, "get_session", lambda: _OldSession())
    with pytest.raises(click.ClickException, match=r"1\.43\.35"):
        mod._ensure_sdk()
