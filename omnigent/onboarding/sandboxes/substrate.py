"""Managed hosts in Agent Substrate v1 Actors; no Kubernetes exec transport."""

from __future__ import annotations

import hashlib
import json
import re
import ssl
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urlsplit

import click
import httpx
import yaml

from omnigent.onboarding.sandboxes.base import SandboxGoneError, SandboxHostLauncher
from omnigent.onboarding.sandboxes.types import RepoWorkspace, SandboxCapabilities

_HOME = "/home/omnigent"
_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_TIMEOUT_S = 120
_CREATE_REJECTIONS = frozenset(
    {
        "UNAUTHENTICATED",
        "PERMISSION_DENIED",
        "INVALID_ARGUMENT",
        "FAILED_PRECONDITION",
    }
)


@dataclass(frozen=True)
class SubstrateConfig:
    """Operator connection settings; credentials are read from projected files."""

    endpoint: str
    atespace: str
    actor_template: str
    api_ca_file: str
    api_token_file: str
    router_url: str
    router_ca_file: str
    router_identity_file: str
    egress_policy_file: str

    @classmethod
    def parse(cls, raw: object) -> SubstrateConfig:
        names = {field.name for field in fields(cls)}
        if not isinstance(raw, dict) or set(raw) != names:
            raise ValueError(f"sandbox.substrate requires exactly: {', '.join(sorted(names))}")
        return cls(**raw)

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or not value.strip() or "\0" in value
            for value in (getattr(self, field.name) for field in fields(self))
        ):
            raise ValueError("sandbox.substrate settings must be nonempty strings")
        for name in (self.atespace, self.actor_template):
            if not _NAME.fullmatch(name):
                raise ValueError("Substrate atespace and template must be DNS labels")
        endpoint = urlsplit(f"https://{self.endpoint}")
        if (
            not endpoint.hostname
            or not endpoint.port
            or endpoint.path
            or endpoint.username is not None
            or endpoint.password is not None
            or endpoint.query
            or endpoint.fragment
        ):
            raise ValueError("sandbox.substrate.endpoint must be a TLS hostname:port")
        _https_origin(self.router_url)


def _https_origin(value: str) -> None:
    parsed = urlsplit(value)
    if (
        any(character.isspace() for character in value)
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Substrate requires an HTTPS origin without credentials, path, or query")
    _ = parsed.port


class ControlError(click.ClickException):
    """A sanitized Control failure whose status can be handled without guessing."""

    def __init__(self, operation: str, code: str) -> None:
        self.code = code
        super().__init__(f"Substrate {operation} failed ({code})")


class ControlClient:
    """Bounded unary calls using the complete, pinned upstream descriptors."""

    def __init__(self, config: SubstrateConfig) -> None:
        self.config = config

    def call(self, operation: str, payload: Mapping[str, object]) -> dict[str, Any]:
        try:
            import grpc
        except ImportError:
            raise click.ClickException("Install omnigent[substrate] on the server") from None
        from google.protobuf.json_format import MessageToDict, ParseDict
        from google.protobuf.message_factory import GetMessageClass

        from omnigent.api.substrate.v1 import ateapi_pb2

        method = ateapi_pb2.DESCRIPTOR.services_by_name["Control"].methods_by_name[operation]
        request = ParseDict(dict(payload), GetMessageClass(method.input_type)())
        response_type = GetMessageClass(method.output_type)
        try:
            credentials = grpc.ssl_channel_credentials(Path(self.config.api_ca_file).read_bytes())
            token = Path(self.config.api_token_file).read_text().strip()
            if not token or any(char.isspace() for char in token):
                raise ValueError("invalid token file")
            # Reopen per operation so projected token and CA rotation takes effect.
            with grpc.secure_channel(
                self.config.endpoint,
                credentials,
                options=(("grpc.lb_policy_name", "round_robin"),),
            ) as channel:
                response = channel.unary_unary(
                    f"/substrate.ateapi.v1.Control/{operation}",
                    request_serializer=lambda message: message.SerializeToString(),
                    response_deserializer=response_type.FromString,
                )(request, timeout=_TIMEOUT_S, metadata=(("authorization", f"Bearer {token}"),))
            return MessageToDict(response)
        except grpc.RpcError as exc:
            raise ControlError(operation, exc.code().name) from None
        except (OSError, ValueError):
            raise click.ClickException(
                "Cannot read Substrate Control trust or credential files"
            ) from None


@dataclass(frozen=True)
class ActorHandle:
    """Persisted before allocation, then bound to the returned resource UID."""

    deployment: str
    atespace: str
    name: str
    uid: str = ""
    allocation_error: str = ""

    def encode(self) -> str:
        return ":".join(
            ("st1", self.deployment, self.atespace, self.name, self.uid, self.allocation_error)
        )

    @classmethod
    def parse(cls, value: str, config: SubstrateConfig) -> ActorHandle:
        parts = value.split(":")
        if len(parts) != 6 or parts[0] != "st1":
            raise click.ClickException("Invalid Substrate Actor handle")
        handle = cls(*parts[1:])
        if (
            handle.deployment != _deployment(config)
            or handle.atespace != config.atespace
            or not re.fullmatch(r"omnigent-[0-9a-f]{32}", handle.name)
            or (handle.uid and not re.fullmatch(r"[0-9a-f-]{36}", handle.uid))
            or (handle.allocation_error and handle.allocation_error not in _CREATE_REJECTIONS)
            or (handle.uid and handle.allocation_error)
        ):
            raise click.ClickException("Substrate handle does not match this deployment")
        return handle

    @property
    def ref(self) -> dict[str, str]:
        return {"atespace": self.atespace, "name": self.name}


def _deployment(config: SubstrateConfig) -> str:
    return hashlib.sha256(config.endpoint.encode()).hexdigest()[:16]


def validate_template(template: Mapping[str, Any]) -> None:
    """Reject templates that cannot safely cold-boot this activation protocol."""
    snapshot = template.get("snapshotConfig", {})
    containers = template.get("containers", [])
    if (
        snapshot.get("preferredFidelity") != "SNAPSHOT_FIDELITY_VOLUMES"
        or snapshot.get("goldenSnapshotConfig", {}).get("mode") != "GOLDEN_SNAPSHOT_MODE_DISABLED"
        or len(containers) != 1
    ):
        raise click.ClickException(
            "Substrate requires one container, VOLUMES fidelity, and disabled golden snapshots"
        )
    container = containers[0]
    volumes = {volume["name"]: volume for volume in template.get("volumes", [])}
    mounts = {
        mount["mountPath"]: volumes.get(mount["name"], {})
        for mount in container.get("volumeMounts", [])
    }
    from omnigent.host.substrate_bootstrap import TOKEN_DIR

    if any(TOKEN_DIR.is_relative_to(Path(path)) for path in mounts):
        raise click.ClickException(
            "Substrate bootstrap requires an ephemeral credential directory"
        )
    identity = mounts.get("/run/substrate", {}).get("systemInfo", {}).get("dataSources", [])
    metadata = [
        item for source in identity for item in source.get("actorMetadata", {}).get("items", [])
    ]
    if (
        container.get("command") != ["python3", "-m", "omnigent.host.substrate_bootstrap"]
        or container.get("args")
        or "@sha256:" not in container.get("image", "")
        or "durableDir" not in mounts.get(_HOME, {})
        or {"field": "ACTOR_METADATA_FIELD_UID", "path": "uid"} not in metadata
        or container.get("wakeupProbe", {}).get("httpGet") != {"path": "/readyz", "port": 80}
    ):
        raise click.ClickException(
            "Substrate template does not match the Omnigent bootstrap contract"
        )
    environment = {entry["name"]: entry.get("value", "") for entry in container.get("env", [])}
    if environment.get("HOME") != _HOME or environment.get("IS_SANDBOX") != "1":
        raise click.ClickException("Substrate template must set HOME and IS_SANDBOX")
    if any(name.startswith("OMNIGENT_HOST_") for name in environment):
        raise click.ClickException(
            "Substrate templates must not contain a host identity or credential"
        )


def _normalize_policy(raw: object) -> dict[str, Any]:
    """Apply public protocol defaults before comparing an admitted policy."""
    from google.protobuf.json_format import MessageToDict, ParseDict, ParseError

    from omnigent.api.substrate.v1 import ateapi_pb2

    try:
        if not isinstance(raw, dict) or set(raw) != {"rules"}:
            raise ValueError("expected rules")
        policy = ParseDict(raw, ateapi_pb2.EgressPolicyTemplate())
        if not policy.rules:
            raise ValueError("expected nonempty rules")
        for rule in policy.rules:
            protocols = [
                name for name in ("http", "https", "tls_passthrough") if rule.HasField(name)
            ]
            if len(protocols) != 1:
                raise ValueError("expected one protocol")
            name = protocols[0]
            protocol = getattr(rule, name)
            if not protocol.HasField("ports"):
                if name == "tls_passthrough":
                    raise ValueError("TLS passthrough requires explicit ports")
                protocol.ports.numbers.append(80 if name == "http" else 443)
            ports = protocol.ports
            if bool(ports.numbers) == ports.HasField("all"):
                raise ValueError("expected port numbers or all")
            if any(not 1 <= port <= 65535 for port in ports.numbers):
                raise ValueError("invalid port")
            protocol.hostnames.sort()
            ports.numbers.sort()
        result = MessageToDict(policy)
        result["rules"].sort(key=lambda rule: json.dumps(rule, sort_keys=True))
        return result
    except (ValueError, ParseError):
        raise click.ClickException(
            "Invalid Substrate egress policy; TLS passthrough requires explicit ports"
        ) from None


class SubstrateSandboxLauncher(SandboxHostLauncher):
    provider: ClassVar[str] = "substrate"

    def __init__(
        self, config: SubstrateConfig | None = None, *, client: ControlClient | None = None
    ) -> None:
        if config is None:
            raise click.ClickException(
                "Substrate supports server-managed sessions; configure sandbox.substrate"
            )
        self.config = config
        self.client = client or ControlClient(config)
        self._unattempted: dict[str, bool] = {}

    @property
    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            managed_launch=True,
            resume_stopped=True,
            programmatic_terminate=True,
            multi_repo=True,
            git_clone_options=True,
            managed_idle_suspend=True,
        )

    def _template(self, reference: Mapping[str, str]) -> dict[str, Any]:
        template = self.client.call("GetActorTemplate", {"actorTemplate": dict(reference)})
        validate_template(template)
        return template

    def prepare(self) -> None:
        self._template({"atespace": self.config.atespace, "name": self.config.actor_template})
        self._policy()
        self._router_context()

    def provision(self, name: str) -> str:
        del name
        handle = ActorHandle(
            _deployment(self.config), self.config.atespace, f"omnigent-{uuid.uuid4().hex}"
        )
        self._unattempted[handle.name] = True
        return handle.encode()

    def _actor(self, handle: ActorHandle) -> dict[str, Any]:
        if handle.allocation_error:
            raise ControlError("CreateActor", handle.allocation_error)
        try:
            actor = self.client.call("GetActor", {"actor": handle.ref})
        except ControlError as exc:
            if exc.code == "NOT_FOUND":
                raise SandboxGoneError("Substrate Actor no longer exists") from None
            raise
        if handle.uid and actor["metadata"]["uid"] != handle.uid:
            raise SandboxGoneError("Substrate Actor generation no longer exists")
        if not handle.uid and actor["actorTemplate"] != {
            "atespace": self.config.atespace,
            "name": self.config.actor_template,
        }:
            raise click.ClickException(
                "Reserved Substrate Actor has a different template; refusing adoption"
            )
        return actor

    def materialize(self, sandbox_id: str) -> str:
        handle = ActorHandle.parse(sandbox_id, self.config)
        if handle.allocation_error:
            return sandbox_id
        if handle.uid:
            self._actor(handle)
            return sandbox_id
        first_attempt = self._unattempted.pop(handle.name, False)
        try:
            actor = self.client.call(
                "CreateActor",
                {
                    "actor": {
                        "metadata": handle.ref,
                        "actorTemplate": {
                            "atespace": self.config.atespace,
                            "name": self.config.actor_template,
                        },
                    }
                },
            )
        except ControlError as exc:
            if first_attempt and exc.code in _CREATE_REJECTIONS:
                # These v1 creation refusals occur before an Actor is stored.
                return replace(handle, allocation_error=exc.code).encode()
            if exc.code not in {"ALREADY_EXISTS", "DEADLINE_EXCEEDED", "UNAVAILABLE"}:
                raise
            actor = self._actor(handle)
        return replace(handle, uid=actor["metadata"]["uid"]).encode()

    def _policy(self) -> dict[str, Any]:
        try:
            raw = yaml.safe_load(Path(self.config.egress_policy_file).read_text())
        except (OSError, yaml.YAMLError):
            raise click.ClickException("Cannot read Substrate egress policy file") from None
        return _normalize_policy(raw)

    def _ensure_policy(self, handle: ActorHandle, server_url: str) -> None:
        policy = self._policy()
        server = urlsplit(server_url)
        rules = [rule.get("tlsPassthrough", {}) for rule in policy["rules"]]
        if not any(
            rule.get("hostnames") == [server.hostname]
            and (rule.get("ports", {}).get("numbers") == [server.port or 443])
            and "all" not in rule.get("ports", {})
            for rule in rules
        ):
            raise click.ClickException(
                "Substrate egress needs an exact-host/port TLS passthrough rule for Omnigent"
            )
        expected = {"metadata": {**handle.ref, "name": "default"}, **policy}
        try:
            self.client.call(
                "CreateActorEgressPolicy", {"actor": handle.ref, "egressPolicy": expected}
            )
        except ControlError as exc:
            if exc.code not in {"ALREADY_EXISTS", "DEADLINE_EXCEEDED", "UNAVAILABLE"}:
                raise
            existing = self.client.call(
                "GetActorEgressPolicy", {"actor": handle.ref, "name": "default"}
            )
            if _normalize_policy({"rules": existing.get("rules", [])}) != policy:
                raise click.ClickException(
                    "Substrate Actor egress policy differs; refusing to overwrite it"
                ) from None

    def _router_context(self) -> ssl.SSLContext:
        try:
            context = ssl.create_default_context(cafile=self.config.router_ca_file)
            context.load_cert_chain(self.config.router_identity_file)
            return context
        except (OSError, ssl.SSLError):
            raise click.ClickException(
                "Cannot load Substrate router trust or client identity"
            ) from None

    def _bootstrap(
        self, handle: ActorHandle, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        try:
            with httpx.Client(
                verify=self._router_context(), timeout=10, trust_env=False
            ) as client:
                response = client.request(
                    "POST" if payload is not None else "GET",
                    self.config.router_url.rstrip("/") + path,
                    headers={"ate-target-actor": f"{handle.atespace}/{handle.name}"},
                    json=payload,
                )
                response.raise_for_status()
                return response.json()
        except (httpx.HTTPError, ValueError):
            raise click.ClickException("Substrate actor activation transport failed") from None

    def start_host(
        self,
        sandbox_id: str,
        *,
        token: str,
        host_id: str,
        host_name: str,
        server_url: str,
        repos: Sequence[RepoWorkspace] = (),
        host_config: dict[str, object] | None = None,
        on_stage: Callable[[str], None] | None = None,
    ) -> str:
        _https_origin(server_url)
        handle = ActorHandle.parse(sandbox_id, self.config)
        actor = self._actor(handle)
        if not handle.uid:
            raise click.ClickException("Substrate Actor UID must be persisted before activation")
        self._template(actor["actorTemplate"])
        self._ensure_policy(handle, server_url)
        self.resume(sandbox_id)
        if on_stage:
            on_stage("cloning" if repos else "starting")
        generation = hashlib.sha256(token.encode()).hexdigest()
        payload = {
            "version": 1,
            "actor_uid": handle.uid,
            "generation": generation,
            "host_id": host_id,
            "host_name": host_name,
            "token": token,
            "server_url": server_url,
            "repos": [asdict(repo) for repo in repos],
            "host_config": host_config,
        }
        deadline = time.monotonic() + _TIMEOUT_S
        # A lost response is ambiguous: resend only the identical assignment.
        for attempt in range(2):
            try:
                self._bootstrap(handle, "/activate", payload)
                break
            except click.ClickException:
                if attempt:
                    raise
        while time.monotonic() < deadline:
            status = self._bootstrap(handle, "/status")
            if status.get("generation") != generation:
                raise click.ClickException("Substrate activation generation does not match")
            if status.get("stage") == "prepared":
                workspace = f"{_HOME}/workspace"
                return f"{workspace}/{repos[0].repo_name}" if len(repos) == 1 else workspace
            if status.get("stage") == "failed":
                raise click.ClickException("Substrate workspace preparation failed")
            time.sleep(0.2)
        raise click.ClickException("Substrate workspace preparation timed out")

    def resume_keeps_host(self, sandbox_id: str) -> bool:
        handle = ActorHandle.parse(sandbox_id, self.config)
        actor = self._actor(handle)
        state = actor.get("status", {}).get("state")
        if state == "ACTOR_STATE_SUSPENDED":
            # VOLUMES limits exported snapshots, not local process retention.
            self.resume(sandbox_id)
        elif state != "ACTOR_STATE_RUNNING":
            return False
        status = self._bootstrap(handle, "/status")
        if status.get("stage") == "failed" and status.get("retryable") is not True:
            raise click.ClickException(
                "Substrate bootstrap cleanup is incomplete; refusing reactivation"
            )
        keeps_host = status.get("stage") in {"preparing", "prepared"}
        if keeps_host:
            generation = status.get("generation")
            if not isinstance(generation, str) or not re.fullmatch(r"[0-9a-f]{64}", generation):
                raise click.ClickException(
                    "Substrate bootstrap has no valid activation generation"
                )
            self.preserved_host_token_hash = generation
        return keeps_host

    def resume(self, sandbox_id: str) -> None:
        handle = ActorHandle.parse(sandbox_id, self.config)
        actor = self._actor(handle)
        self._template(actor["actorTemplate"])
        if actor.get("status", {}).get("state") != "ACTOR_STATE_RUNNING":
            self.client.call("ResumeActor", {"actor": handle.ref})

    def wait_for_idle(self, sandbox_id: str, generation: str) -> None:
        handle = ActorHandle.parse(sandbox_id, self.config)
        self._actor(handle)
        deadline = time.monotonic() + _TIMEOUT_S
        while time.monotonic() < deadline:
            status = self._bootstrap(handle, "/status")
            if status.get("generation") != generation:
                raise click.ClickException("Substrate activation changed during quiescence")
            if status.get("stage") == "idle":
                return
            if status.get("stage") == "failed":
                raise click.ClickException("Substrate host could not finish idle cleanup")
            time.sleep(0.2)
        raise click.ClickException("Substrate host quiescence timed out")

    def stop(self, sandbox_id: str) -> None:
        handle = ActorHandle.parse(sandbox_id, self.config)
        actor = self._actor(handle)
        self._template(actor["actorTemplate"])
        self.client.call(
            "SuspendActor",
            {"actor": handle.ref, "options": {"mode": "DURABILITY_MODE_SYNCHRONOUS"}},
        )
        actor = self._actor(handle)
        snapshots = actor.get("status", {}).get("snapshots", [])
        latest = max(snapshots, key=lambda snapshot: snapshot["generation"], default={})
        if (
            actor.get("status", {}).get("state") != "ACTOR_STATE_SUSPENDED"
            or latest.get("durableSnapshot", {}).get("state") != "SNAPSHOT_STORAGE_STATE_COMPLETED"
        ):
            raise click.ClickException("Substrate snapshot has not reached durable storage")

    def is_running(self, sandbox_id: str) -> bool | None:
        handle = ActorHandle.parse(sandbox_id, self.config)
        state = self._actor(handle).get("status", {}).get("state")
        if state == "ACTOR_STATE_RUNNING":
            return True
        if state == "ACTOR_STATE_SUSPENDED":
            return False
        raise click.ClickException(
            "Substrate Actor is not running or suspended; inspect its status"
        )

    def terminate(self, sandbox_id: str) -> None:
        handle = ActorHandle.parse(sandbox_id, self.config)
        if handle.allocation_error:
            return
        try:
            actor = self._actor(handle)
        except SandboxGoneError:
            if not handle.uid:
                raise click.ClickException(
                    "Substrate allocation is not settled; retaining its cleanup reservation"
                ) from None
            return
        try:
            self.client.call(
                "DeleteActor", {"actor": handle.ref, "options": {"uid": actor["metadata"]["uid"]}}
            )
        except ControlError as exc:
            if exc.code != "NOT_FOUND":
                raise
