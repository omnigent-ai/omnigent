"""Substrate lifecycle uses the public wire contract and generation-bound handles."""

from __future__ import annotations

import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import click
import pytest
import yaml
from google.protobuf.json_format import ParseDict
from google.protobuf.message_factory import GetMessageClass

from omnigent.api.substrate.v1 import ateapi_pb2
from omnigent.onboarding.sandboxes.base import SandboxGoneError
from omnigent.onboarding.sandboxes.substrate import (
    ActorHandle,
    ControlClient,
    ControlError,
    SubstrateConfig,
    SubstrateSandboxLauncher,
    _normalize_policy,
    validate_template,
)
from omnigent.server.managed_hosts import parse_sandbox_config

_UID = "4a93f8f1-ddf3-4f2c-9c31-566dc240da4f"
_OVERLAY = Path(__file__).resolve().parents[3] / "deploy/kubernetes/overlays/substrate-runners"
_TEMPLATE = yaml.safe_load((_OVERLAY / "actor-template.yaml.example").read_text())
_TEMPLATE["metadata"] = {"atespace": "runners", "name": "host-v1"}
_TEMPLATE["containers"][0]["image"] = "registry.example.com/host@sha256:" + "a" * 64


@pytest.fixture
def config(tmp_path: Path) -> SubstrateConfig:
    policy = tmp_path / "policy.yaml"
    policy.write_text(
        "rules:\n- tlsPassthrough:\n    hostnames: [omnigent.example.com]\n"
        "    ports: {numbers: [443]}\n"
    )
    return SubstrateConfig.parse(
        {
            "endpoint": "api.example.com:443",
            "atespace": "runners",
            "actor_template": "host-v1",
            "api_ca_file": "ca.pem",
            "api_token_file": "token",
            "router_url": "https://router.example.com",
            "router_ca_file": "router-ca.pem",
            "router_identity_file": "router.pem",
            "egress_policy_file": str(policy),
        }
    )


class Control:
    def __init__(self) -> None:
        self.actor: dict[str, Any] | None = None
        self.policy: dict[str, Any] | None = None
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.error: str | None = None

    def call(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        descriptor = ateapi_pb2.DESCRIPTOR.services_by_name["Control"].methods_by_name[operation]
        ParseDict(payload, GetMessageClass(descriptor.input_type)())
        self.calls.append((operation, copy.deepcopy(payload)))
        if self.error:
            raise ControlError(operation, self.error)
        if operation == "GetActorTemplate":
            return copy.deepcopy(_TEMPLATE)
        if operation == "CreateActor":
            if self.actor:
                raise ControlError(operation, "ALREADY_EXISTS")
            self.actor = {
                **copy.deepcopy(payload["actor"]),
                "status": {"state": "ACTOR_STATE_SUSPENDED"},
            }
            self.actor["metadata"]["uid"] = _UID
            return copy.deepcopy(self.actor)
        if self.actor is None:
            raise ControlError(operation, "NOT_FOUND")
        if operation == "CreateActorEgressPolicy":
            if self.policy:
                raise ControlError(operation, "ALREADY_EXISTS")
            self.policy = copy.deepcopy(payload["egressPolicy"])
            return self.policy
        if operation == "GetActorEgressPolicy":
            assert self.policy
            return self.policy
        if operation == "ResumeActor":
            self.actor["status"]["state"] = "ACTOR_STATE_RUNNING"
            return {"actor": self.actor}
        if operation == "SuspendActor":
            self.actor["status"] = {
                "state": "ACTOR_STATE_SUSPENDED",
                "snapshots": [
                    {
                        "generation": 1,
                        "durableSnapshot": {"state": "SNAPSHOT_STORAGE_STATE_COMPLETED"},
                    }
                ],
            }
            return {"actor": self.actor}
        if operation == "DeleteActor":
            assert payload["options"]["uid"] == _UID
            self.actor = None
            return {}
        assert operation == "GetActor"
        return copy.deepcopy(self.actor)


def test_control_uses_verified_tls_and_rereads_projected_token(
    config: SubstrateConfig,
    tmp_path: Path,
) -> None:
    import grpc

    from omnigent.inner.egress.ca import ensure_ca
    from omnigent.inner.egress.certs import HostCertCache

    ca, key = ensure_ca(tmp_path / "ca")
    certificate, private_key = HostCertCache(ca, key)._generate("localhost")
    token_file = tmp_path / "api-token"
    token_file.write_text("first-token")
    seen: list[str] = []

    def get_template(request: Any, context: Any) -> Any:
        seen.append(dict(context.invocation_metadata())["authorization"])
        assert request.actor_template.name == "host-v1"
        return ParseDict(_TEMPLATE, ateapi_pb2.ActorTemplate())

    with ThreadPoolExecutor(max_workers=1) as executor:
        server = grpc.server(executor)
        server.add_generic_rpc_handlers(
            (
                grpc.method_handlers_generic_handler(
                    "substrate.ateapi.v1.Control",
                    {
                        "GetActorTemplate": grpc.unary_unary_rpc_method_handler(
                            get_template,
                            request_deserializer=ateapi_pb2.GetActorTemplateRequest.FromString,
                            response_serializer=lambda value: value.SerializeToString(),
                        )
                    },
                ),
            )
        )
        port = server.add_secure_port(
            "localhost:0", grpc.ssl_server_credentials(((private_key, certificate),))
        )
        server.start()
        try:
            client = ControlClient(
                replace(
                    config,
                    endpoint=f"localhost:{port}",
                    api_ca_file=str(ca),
                    api_token_file=str(token_file),
                )
            )
            reference = {"actorTemplate": {"atespace": "runners", "name": "host-v1"}}
            assert client.call("GetActorTemplate", reference)["metadata"]["name"] == "host-v1"
            token_file.write_text("rotated-token")
            client.call("GetActorTemplate", reference)
            assert seen == ["Bearer first-token", "Bearer rotated-token"]
        finally:
            server.stop(None).wait()


def test_configuration_and_registry(config: SubstrateConfig) -> None:
    parsed = parse_sandbox_config(
        {
            "provider": "substrate",
            "server_url": "https://omnigent.example.com",
            "substrate": asdict(config),
        }
    )
    assert parsed is not None
    launcher = parsed.configs[0].launcher_factory()
    assert launcher.provider == "substrate"
    assert launcher.capabilities.managed_launch
    assert not launcher.capabilities.cli_bootstrap
    assert not launcher.capabilities.resume_preserves_host
    with pytest.raises(click.ClickException, match="server-managed"):
        SubstrateSandboxLauncher()


@pytest.mark.parametrize(
    "patch",
    [
        {"unknown": "x"},
        {"api_token_file": ""},
        {"router_url": "http://router.example.com"},
        {"endpoint": "api.example.com"},
        {"atespace": "../other"},
    ],
)
def test_reject_invalid_settings(config: SubstrateConfig, patch: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        SubstrateConfig.parse({**asdict(config), **patch})
    if set(patch).issubset(asdict(config)):
        with pytest.raises(ValueError):
            SubstrateConfig(**{**asdict(config), **patch})


def test_policy_defaults_match_admitted_policy() -> None:
    assert _normalize_policy(
        {"rules": [{"https": {"hostnames": ["model.example.com"]}}]}
    ) == _normalize_policy(
        {"rules": [{"https": {"hostnames": ["model.example.com"], "ports": {"numbers": [443]}}}]}
    )
    with pytest.raises(click.ClickException, match="explicit ports"):
        _normalize_policy({"rules": [{"tlsPassthrough": {"hostnames": ["omnigent.example.com"]}}]})


def test_template_requires_cold_boot_contract() -> None:
    ParseDict(_TEMPLATE, ateapi_pb2.ActorTemplate())
    validate_template(_TEMPLATE)
    altered = copy.deepcopy(_TEMPLATE)
    altered["snapshotConfig"]["preferredFidelity"] = "SNAPSHOT_FIDELITY_MEMORY"
    with pytest.raises(click.ClickException, match="VOLUMES"):
        validate_template(altered)


def test_lifecycle_and_retry_keep_identity(
    config: SubstrateConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    control = Control()
    launcher = SubstrateSandboxLauncher(config, client=control)
    reserved = launcher.provision("test")
    assert control.calls == []
    concrete = launcher.materialize(reserved)
    assert concrete == launcher.materialize(reserved)
    assert len(concrete) < 256
    handle = ActorHandle.parse(concrete, config)
    assert handle.uid == _UID
    activation: dict[str, Any] = {}

    def bootstrap(
        _handle: ActorHandle, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        if payload is not None:
            activation.update(payload)
        return {"stage": "prepared", "generation": activation["generation"]}

    monkeypatch.setattr(launcher, "_bootstrap", bootstrap)
    workspace = launcher.start_host(
        concrete,
        token="secret-test-token",
        host_id="host-test",
        host_name="test",
        server_url="https://omnigent.example.com",
    )
    assert workspace == "/home/omnigent/workspace"
    assert launcher.is_running(concrete)
    assert "secret-test-token" not in repr(control.calls)
    assert activation["actor_uid"] == _UID
    launcher.stop(concrete)
    assert launcher.is_running(concrete) is False
    launcher.resume(concrete)
    launcher.terminate(concrete)
    launcher.terminate(concrete)


@pytest.mark.parametrize(
    "code", ["PERMISSION_DENIED", "UNAUTHENTICATED", "UNAVAILABLE", "DEADLINE_EXCEEDED"]
)
def test_transient_and_auth_errors_are_not_missing(config: SubstrateConfig, code: str) -> None:
    control = Control()
    launcher = SubstrateSandboxLauncher(config, client=control)
    handle = launcher.materialize(launcher.provision("test"))
    control.error = code
    with pytest.raises(ControlError, match=code):
        launcher.resume(handle)
    with pytest.raises(ControlError, match=code):
        launcher.terminate(handle)


@pytest.mark.parametrize(
    "code", ["UNAUTHENTICATED", "PERMISSION_DENIED", "INVALID_ARGUMENT", "FAILED_PRECONDITION"]
)
def test_definitive_create_rejection_can_be_retired(config: SubstrateConfig, code: str) -> None:
    control = Control()
    control.error = code
    launcher = SubstrateSandboxLauncher(config, client=control)
    settled = launcher.materialize(launcher.provision("test"))
    assert ActorHandle.parse(settled, config).allocation_error == code
    with pytest.raises(ControlError, match=code):
        launcher.start_host(
            settled,
            token="token",
            host_id="host",
            host_name="host",
            server_url="https://omnigent.example.com",
        )
    control.error = None
    assert launcher.materialize(settled) == settled
    assert control.actor is None
    launcher.terminate(settled)
    assert [operation for operation, _ in control.calls] == ["CreateActor"]


def test_lost_create_response_is_not_settled_by_a_later_auth_error(
    config: SubstrateConfig,
) -> None:
    control = Control()
    original = control.call
    lose_response = True

    def call(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        nonlocal lose_response
        result = original(operation, payload)
        if operation == "CreateActor" and lose_response:
            lose_response = False
            control.error = "PERMISSION_DENIED"
            raise ControlError(operation, "UNAVAILABLE")
        return result

    control.call = Mock(side_effect=call)
    launcher = SubstrateSandboxLauncher(config, client=control)
    reserved = launcher.provision("test")
    with pytest.raises(ControlError, match="PERMISSION_DENIED"):
        launcher.materialize(reserved)
    assert control.actor is not None
    with pytest.raises(ControlError, match="PERMISSION_DENIED"):
        launcher.materialize(reserved)
    control.error = None
    concrete = launcher.materialize(reserved)
    assert ActorHandle.parse(concrete, config).uid == _UID
    launcher.terminate(concrete)
    assert control.actor is None


def test_credential_directory_cannot_be_a_durable_volume() -> None:
    template = copy.deepcopy(_TEMPLATE)
    template["volumes"].append({"name": "tmp", "durableDir": {}})
    template["containers"][0]["volumeMounts"].append(
        {
            "name": "tmp",
            "mountPath": "/run/omnigent-bootstrap",
        }
    )
    with pytest.raises(click.ClickException, match="ephemeral credential"):
        validate_template(template)


def test_unsettled_allocation_retains_cleanup_reservation(config: SubstrateConfig) -> None:
    launcher = SubstrateSandboxLauncher(config, client=Control())
    reserved = launcher.provision("test")
    with pytest.raises(click.ClickException, match="not settled"):
        launcher.terminate(reserved)


@pytest.mark.parametrize(
    "stage,keeps_host",
    [("waiting", False), ("preparing", True), ("prepared", True), ("failed", False)],
)
@pytest.mark.parametrize("suspended", [False, True])
def test_wake_does_not_rotate_live_host_credential(
    config: SubstrateConfig,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    keeps_host: bool,
    suspended: bool,
) -> None:
    control = Control()
    launcher = SubstrateSandboxLauncher(config, client=control)
    handle = launcher.materialize(launcher.provision("test"))
    launcher.resume(handle)
    if suspended:
        assert control.actor is not None
        control.actor["status"]["state"] = "ACTOR_STATE_SUSPENDED"
    monkeypatch.setattr(
        launcher,
        "_bootstrap",
        lambda *_: {
            "stage": stage,
            "generation": "a" * 64,
            "retryable": True,
        },
    )
    assert launcher.resume_keeps_host(handle) is keeps_host
    assert launcher.preserved_host_token_hash == ("a" * 64 if keeps_host else None)


def test_uid_replacement_is_never_deleted(config: SubstrateConfig) -> None:
    control = Control()
    launcher = SubstrateSandboxLauncher(config, client=control)
    handle = launcher.materialize(launcher.provision("test"))
    assert control.actor
    control.actor["metadata"]["uid"] = "cc6b1881-d38c-4c8a-9b26-2e56471550e5"
    with pytest.raises(SandboxGoneError):
        launcher.resume(handle)
    launcher.terminate(handle)
    assert control.actor is not None


def test_policy_must_allow_wss_before_resume(config: SubstrateConfig) -> None:
    control = Control()
    launcher = SubstrateSandboxLauncher(config, client=control)
    handle = launcher.materialize(launcher.provision("test"))
    with pytest.raises(click.ClickException, match="passthrough"):
        launcher.start_host(
            handle,
            token="secret",
            host_id="host",
            host_name="host",
            server_url="https://different.example.com",
        )
    assert "ResumeActor" not in [operation for operation, _ in control.calls]


def test_snapshot_pending_is_not_reported_durable(config: SubstrateConfig) -> None:
    control = Control()
    launcher = SubstrateSandboxLauncher(config, client=control)
    handle = launcher.materialize(launcher.provision("test"))
    original = control.call

    def call(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = original(operation, payload)
        if operation == "SuspendActor":
            assert control.actor
            control.actor["status"]["snapshots"].append(
                {"generation": 2, "durableSnapshot": {"state": "SNAPSHOT_STORAGE_STATE_PENDING"}}
            )
        return result

    control.call = Mock(side_effect=call)
    with pytest.raises(click.ClickException, match="durable"):
        launcher.stop(handle)
