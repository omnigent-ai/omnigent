"""Native warm-pool allocation with owner-specific activation after claiming.

Unallocated pods run the bootstrap without a managed-host identity. Claims
transfer one Sandbox to an allocation; its UID travels in the opaque provider
handle so controller repair cannot silently replace a durable workspace.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import click

from omnigent.host.identity import HOST_ID_ENV_VAR, HOST_NAME_ENV_VAR, HOST_TOKEN_ENV_VAR
from omnigent.host.warm_bootstrap import ACTIVATION_DIR_ENV_VAR, POD_UID_ENV_VAR
from omnigent.onboarding.sandboxes.agent_sandbox import (
    API_GROUP,
    API_VERSION,
    SANDBOX_PLURAL,
    AgentSandboxLauncher,
    _shutdown_time,
    build_sandbox_manifest,
    initial_shutdown_window_s,
    resolve_shutdown_window_s,
    resolve_workspace_volume,
)
from omnigent.onboarding.sandboxes.base import SandboxGoneError
from omnigent.onboarding.sandboxes.kubernetes import (
    _AGENT_LABEL,
    _HOME_DIR,
    _POD_READY_REQUEST_TIMEOUT_S,
    _api_reason,
    _ensure_sdk,
    _new_pod_name,
    _render_workspace_prep_command,
    _resolve_pod_ready_timeout_s,
    _terminal_failure,
    build_job_manifest,
)
from omnigent.onboarding.sandboxes.types import RepoWorkspace

_logger = logging.getLogger(__name__)
EXTENSION_GROUP = "extensions.agents.x-k8s.io"
CLAIMS = "sandboxclaims"
POOLS = "sandboxwarmpools"
TEMPLATES = "sandboxtemplates"
BOOTSTRAP_CONTAINER = "bootstrap"
PROFILE_ANNOTATION = "omnigent.ai/warm-profile"
SHARED_POOL_ANNOTATION = "omnigent.ai/warm-pool-shared"
_ACTIVATION_PATH = "/run/omnigent-activation"
_HANDLE_PREFIX = "wp1:"
_POLL_S = 0.5
_EXEC_TIMEOUT_S = 15


@dataclass(frozen=True)
class WarmPoolHandle:
    namespace: str
    claim_name: str
    claim_uid: str
    sandbox_name: str
    sandbox_uid: str

    def encode(self) -> str:
        value = _HANDLE_PREFIX + ":".join(
            (
                self.namespace,
                self.claim_name,
                uuid.UUID(self.claim_uid).hex,
                self.sandbox_name,
                uuid.UUID(self.sandbox_uid).hex,
            )
        )
        if len(value) > 256:
            raise click.ClickException("Warm allocation resource names exceed the host ID limit.")
        return value

    @classmethod
    def parse(cls, value: str) -> WarmPoolHandle:
        try:
            prefix, namespace, claim, claim_uid, sandbox, sandbox_uid = value.split(":")
            if prefix != "wp1" or not all((namespace, claim, sandbox)):
                raise ValueError
            return cls(
                namespace, claim, str(uuid.UUID(claim_uid)), sandbox, str(uuid.UUID(sandbox_uid))
            )
        except ValueError:
            raise click.ClickException("Invalid warm-pool allocation handle.") from None


def _bootstrap_command(mode: str) -> list[str]:
    return ["python3", "-m", "omnigent.host.warm_bootstrap", mode]


def _shared_profile(spec: dict[str, Any]) -> bool:
    metadata = spec["podTemplate"].get("metadata", {})
    annotations = metadata.get("annotations", {})
    if SHARED_POOL_ANNOTATION not in annotations:
        return False
    if annotations[SHARED_POOL_ANNOTATION] != "true" or _AGENT_LABEL in metadata.get("labels", {}):
        raise click.ClickException(
            "Invalid shared warm-pool profile: the shared marker must be 'true' "
            "and the omnigent.ai/agent label must be absent."
        )
    return True


def _contains(expected: Any, actual: Any, *, field: str = "") -> bool:
    """Compare declared fields while tolerating Kubernetes defaults/admission additions."""
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(
            (key in actual and _contains(value, actual[key], field=key))
            or (key not in actual and (key, value) in (("readOnly", False), ("value", "")))
            for key, value in expected.items()
        )
    if isinstance(expected, list):
        if not isinstance(actual, list):
            return False
        if field == "tolerations":
            if not all(isinstance(item, dict) for item in [*expected, *actual]):
                return False
            # Admission may append tolerations; declared entries must keep their semantics.
            defaults = {
                "key": "",
                "operator": "Equal",
                "value": "",
                "effect": "",
                "tolerationSeconds": None,
            }
            observed = [{**defaults, **item} for item in actual]
            return all({**defaults, **item} in observed for item in expected)
        if expected and all(isinstance(item, dict) and "name" in item for item in expected):
            names = [item.get("name") for item in actual if isinstance(item, dict)]
            if len(names) != len(set(names)):
                return False
            return all(
                any(_contains(item, candidate) for candidate in actual) for item in expected
            )
        return expected == actual
    if expected == actual:
        return True
    if field in {"cpu", "memory", "storage", "ephemeral-storage", "sizeLimit"}:
        from kubernetes.utils.quantity import parse_quantity

        try:
            return parse_quantity(str(expected)) == parse_quantity(str(actual))
        except ValueError:
            return False
    return False


class AgentSandboxWarmPoolLauncher(AgentSandboxLauncher):
    """Opt-in claim allocation; direct Sandbox handles retain their original behavior."""

    def __init__(self, *, warm_pool: str | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._warm_pool = warm_pool
        self._agent_name: str | None = None

    def prepare_for_launch(self, *, agent_name: str | None = None) -> None:
        self._agent_name = agent_name

    def template_spec(
        self,
        *,
        agent_name: str | None = None,
        host_config: dict[str, object] | None = None,
        shared: bool = False,
    ) -> dict[str, Any]:
        """Render the static pod profile from the same config as direct Sandboxes."""
        if shared and agent_name is not None:
            raise click.ClickException("A shared warm pool cannot have an agent classifier.")
        job = build_job_manifest(
            job_name="warm-template",
            namespace=self._resolve_namespace(),
            image=self._resolve_image(),
            service_account=self._resolve_service_account(),
            host_id="",
            host_name="",
            server_url="http://localhost",
            token_secret_name="unused",
            harness_secret=self._resolve_secret(),
            env_literals=self._resolve_sandbox_env(),
            node_selector=self._node_selector,
            workspace=f"{_HOME_DIR}/workspace",
            resources=self._resources,
            pvc_mounts=self._pvc_mounts,
            secret_mounts=self._secret_mounts,
            tolerations=self._tolerations,
            agent_name=agent_name,
            runtime_class=self._runtime_class,
            home_size_limit=self._home_size_limit,
            host_config=host_config,
        )
        sandbox: dict[str, Any] = build_sandbox_manifest(
            job,
            shutdown_time=_shutdown_time(initial_shutdown_window_s()),
            workspace_volume=resolve_workspace_volume(),
        )
        spec = sandbox["spec"]
        for key in ("shutdownTime", "shutdownPolicy", "operatingMode"):
            spec.pop(key, None)
        pod = spec["podTemplate"]["spec"]
        bootstrap = pod.pop("initContainers")[0]
        bootstrap["name"] = BOOTSTRAP_CONTAINER
        bootstrap["command"] = _bootstrap_command("prepare")
        host = pod["containers"][0]
        host["command"] = _bootstrap_command("host")
        host["env"] = [
            item
            for item in host["env"]
            if item["name"] not in {HOST_ID_ENV_VAR, HOST_NAME_ENV_VAR, HOST_TOKEN_ENV_VAR}
        ]
        for container in (bootstrap, host):
            reserved = {POD_UID_ENV_VAR, ACTIVATION_DIR_ENV_VAR}
            if any(item["name"] in reserved for item in container["env"]):
                raise click.ClickException(
                    "OMNIGENT_POD_UID and OMNIGENT_ACTIVATION_DIR "
                    "are reserved for warm activation."
                )
            container["env"].append(
                {"name": POD_UID_ENV_VAR, "valueFrom": {"fieldRef": {"fieldPath": "metadata.uid"}}}
            )
            container["env"].append({"name": ACTIVATION_DIR_ENV_VAR, "value": _ACTIVATION_PATH})
            container["volumeMounts"].append(
                {
                    "name": "activation",
                    "mountPath": _ACTIVATION_PATH,
                    **({"readOnly": True} if container is host else {}),
                }
            )
            container["readinessProbe"] = {
                "exec": {"command": _bootstrap_command("ready")},
                "periodSeconds": 1,
            }
        pod["containers"] = [bootstrap, host]
        pod["volumes"].append(
            {"name": "activation", "emptyDir": {"medium": "Memory", "sizeLimit": "4Mi"}}
        )
        pod["dnsPolicy"] = "ClusterFirst"
        annotations = {SHARED_POOL_ANNOTATION: "true"} if shared else {}
        if shared:
            spec["podTemplate"]["metadata"]["annotations"] = annotations
        digest = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
        spec["podTemplate"]["metadata"]["annotations"] = {
            **annotations,
            PROFILE_ANNOTATION: digest,
        }
        return spec

    def _get(self, group: str, plural: str, namespace: str, name: str) -> dict[str, Any]:
        return cast(
            dict[str, Any],
            self._load_custom().get_namespaced_custom_object(
                group,
                API_VERSION,
                namespace,
                plural,
                name,
                _request_timeout=_POD_READY_REQUEST_TIMEOUT_S,
            ),
        )

    def _validate_profile(
        self,
        spec: dict[str, Any],
        *,
        agent_name: str | None,
        host_config: dict[str, object] | None = None,
        shared: bool = False,
    ) -> None:
        expected_agent = None if shared else agent_name
        expected = self.template_spec(
            agent_name=expected_agent, host_config=host_config, shared=shared
        )
        labels = spec["podTemplate"].get("metadata", {}).get("labels", {})
        if (
            _shared_profile(spec) != shared
            or labels.get(_AGENT_LABEL) != expected_agent
            or not _contains(expected, spec)
        ):
            raise click.ClickException(
                "Warm-pool template does not match sandbox.kubernetes/workspace configuration. "
                "Generate a new versioned template and pool with this server configuration."
            )

    def _validate_pod(
        self,
        pod: Any,
        handle: WarmPoolHandle,
        *,
        agent_name: str | None,
        shared: bool = False,
    ) -> None:
        from kubernetes import client

        expected_agent = None if shared else agent_name
        profile = self.template_spec(agent_name=expected_agent, shared=shared)
        expected = profile["podTemplate"]
        for claim in profile.get("volumeClaimTemplates", []):
            name = claim["metadata"]["name"]
            volumes = expected["spec"]["volumes"]
            volumes[:] = [volume for volume in volumes if volume["name"] != name]
            volumes.append(
                {
                    "name": name,
                    "persistentVolumeClaim": {"claimName": f"{name}-{handle.sandbox_name}"},
                }
            )
        with client.ApiClient() as api:
            actual = api.sanitize_for_serialization(pod)
        classifier = actual.get("metadata", {}).get("labels", {}).get(_AGENT_LABEL)
        identity_keys = {HOST_ID_ENV_VAR, HOST_NAME_ENV_VAR, HOST_TOKEN_ENV_VAR}
        has_identity = any(
            env.get("name") in identity_keys
            for container in actual.get("spec", {}).get("containers", [])
            for env in container.get("env", [])
        )
        if (
            _shared_profile({"podTemplate": actual}) != shared
            or classifier != expected_agent
            or has_identity
            or not _contains(expected, actual)
        ):
            raise click.ClickException(
                "Warm Pod does not match its configured profile; create a new versioned pool."
            )

    def provision(self, name: str) -> str:
        if self._warm_pool is None:
            return super().provision(name)
        _ensure_sdk()
        from kubernetes.client.rest import ApiException

        namespace = self._resolve_namespace()
        claim_name = _new_pod_name(name)
        claim_uid: str | None = None
        try:
            pool = self._get(EXTENSION_GROUP, POOLS, namespace, self._warm_pool)
            template = self._get(
                EXTENSION_GROUP, TEMPLATES, namespace, pool["spec"]["sandboxTemplateRef"]["name"]
            )
            classifier = (
                template["spec"]["podTemplate"]
                .get("metadata", {})
                .get("labels", {})
                .get(_AGENT_LABEL)
            )
            shared = _shared_profile(template["spec"])
            if not shared and classifier != self._agent_name:
                _logger.info(
                    "No warm profile for agent %r; using direct Sandbox launch", self._agent_name
                )
                return super().provision(name)
            self._validate_profile(template["spec"], agent_name=self._agent_name, shared=shared)
            claim = cast(
                dict[str, Any],
                self._load_custom().create_namespaced_custom_object(
                    EXTENSION_GROUP,
                    API_VERSION,
                    namespace,
                    CLAIMS,
                    {
                        "apiVersion": f"{EXTENSION_GROUP}/{API_VERSION}",
                        "kind": "SandboxClaim",
                        "metadata": {
                            "name": claim_name,
                            "labels": {"app.kubernetes.io/managed-by": "omnigent"},
                        },
                        "spec": {
                            "warmPoolRef": {"name": self._warm_pool},
                            "lifecycle": {
                                "shutdownPolicy": "Delete",
                                "shutdownTime": _shutdown_time(initial_shutdown_window_s()),
                            },
                        },
                    },
                    _request_timeout=_POD_READY_REQUEST_TIMEOUT_S,
                ),
            )
            claim_uid = claim["metadata"]["uid"]
            deadline = time.monotonic() + _resolve_pod_ready_timeout_s(self._pod_ready_timeout_s)
            while time.monotonic() < deadline:
                claim = self._get(EXTENSION_GROUP, CLAIMS, namespace, claim_name)
                if claim["metadata"]["uid"] != claim_uid or claim["metadata"].get(
                    "deletionTimestamp"
                ):
                    raise SandboxGoneError("Warm claim was removed during allocation.")
                sandbox_name = claim.get("status", {}).get("sandbox", {}).get("name")
                if sandbox_name:
                    try:
                        sandbox = self._get(API_GROUP, SANDBOX_PLURAL, namespace, sandbox_name)
                    except ApiException as exc:
                        if exc.status != 404:
                            raise
                        time.sleep(_POLL_S)
                        continue
                    handle = WarmPoolHandle(
                        namespace, claim_name, claim_uid, sandbox_name, sandbox["metadata"]["uid"]
                    )
                    self._verify_assignment(handle, claim, sandbox)
                    self._validate_profile(
                        sandbox["spec"], agent_name=self._agent_name, shared=shared
                    )
                    _logger.info(
                        "Allocated %s Sandbox %s from warm pool %s",
                        sandbox["metadata"]
                        .get("labels", {})
                        .get("agents.x-k8s.io/launch-type", "unknown"),
                        sandbox_name,
                        self._warm_pool,
                    )
                    return handle.encode()
                time.sleep(_POLL_S)
            raise click.ClickException("Timed out waiting for warm-pool claim assignment.")
        except BaseException as exc:
            if claim_uid is not None:
                with contextlib.suppress(Exception):
                    self._delete_claim(namespace, claim_name, claim_uid)
            if not isinstance(exc, Exception) or isinstance(exc, click.ClickException):
                raise
            raise click.ClickException(
                f"Warm-pool allocation failed ({_api_reason(exc)})."
            ) from exc
        finally:
            self._close_clients()

    @staticmethod
    def _verify_assignment(
        handle: WarmPoolHandle, claim: dict[str, Any], sandbox: dict[str, Any]
    ) -> None:
        if (
            claim["metadata"]["uid"] != handle.claim_uid
            or claim["metadata"].get("deletionTimestamp")
            or claim.get("status", {}).get("sandbox", {}).get("name") != handle.sandbox_name
            or sandbox["metadata"]["uid"] != handle.sandbox_uid
            or sandbox["metadata"].get("deletionTimestamp")
            or not any(
                owner.get("uid") == handle.claim_uid and owner.get("controller") is True
                for owner in sandbox["metadata"].get("ownerReferences", [])
            )
        ):
            raise SandboxGoneError(
                "The assigned warm Sandbox no longer exists; its workspace cannot be resumed."
            )

    def _allocation(self, handle: WarmPoolHandle) -> dict[str, Any]:
        from kubernetes.client.rest import ApiException

        try:
            claim = self._get(EXTENSION_GROUP, CLAIMS, handle.namespace, handle.claim_name)
            sandbox = self._get(API_GROUP, SANDBOX_PLURAL, handle.namespace, handle.sandbox_name)
        except ApiException as exc:
            if exc.status == 404:
                raise SandboxGoneError("The assigned warm Sandbox no longer exists.") from exc
            raise
        self._verify_assignment(handle, claim, sandbox)
        return sandbox

    def _pod(self, handle: WarmPoolHandle, sandbox: dict[str, Any]) -> Any:
        from kubernetes.client.rest import ApiException

        name = (
            sandbox["metadata"].get("annotations", {}).get("agents.x-k8s.io/pod-name")
            or handle.sandbox_name
        )
        try:
            pod: Any = self._load_core().read_namespaced_pod(
                name, handle.namespace, _request_timeout=_POD_READY_REQUEST_TIMEOUT_S
            )
        except ApiException as exc:
            if exc.status != 404:
                raise
            selector = sandbox.get("status", {}).get("selector")
            if not selector:
                return None
            candidates: Any = self._load_core().list_namespaced_pod(
                handle.namespace,
                label_selector=selector,
                _request_timeout=_POD_READY_REQUEST_TIMEOUT_S,
            )
            owned = [
                item
                for item in candidates.items
                if any(
                    owner.uid == handle.sandbox_uid and owner.controller
                    for owner in item.metadata.owner_references or []
                )
            ]
            if not owned:
                return None
            if len(owned) != 1:
                raise click.ClickException(
                    "Multiple Pods are owned by the warm Sandbox."
                ) from None
            pod = owned[0]
        if pod.metadata.deletion_timestamp:
            return None
        if not any(
            owner.uid == handle.sandbox_uid and owner.controller
            for owner in pod.metadata.owner_references or []
        ):
            raise SandboxGoneError("Warm Sandbox Pod ownership changed.")
        return pod

    def _patch_deadline(self, handle: WarmPoolHandle, *, boot: bool) -> None:
        self._load_custom().patch_namespaced_custom_object(
            API_GROUP,
            API_VERSION,
            handle.namespace,
            SANDBOX_PLURAL,
            handle.sandbox_name,
            {
                "metadata": {"uid": handle.sandbox_uid},
                "spec": {
                    "operatingMode": "Running",
                    "shutdownPolicy": "Retain",
                    "shutdownTime": _shutdown_time(
                        initial_shutdown_window_s() if boot else resolve_shutdown_window_s()
                    ),
                },
            },
            _request_timeout=_POD_READY_REQUEST_TIMEOUT_S,
        )

    def _exec(
        self,
        handle: WarmPoolHandle,
        pod_name: str,
        mode: str,
        payload: dict[str, Any] | None = None,
    ) -> str:
        from kubernetes import client
        from kubernetes.stream import stream

        # stream() mutates its ApiClient's request transport; keep it isolated.
        self._load_clients()
        assert self._api_client is not None
        with client.ApiClient(configuration=self._api_client.configuration) as api:
            core = client.CoreV1Api(api)
            connection = stream(
                core.connect_get_namespaced_pod_exec,
                pod_name,
                handle.namespace,
                container=BOOTSTRAP_CONTAINER,
                command=["python3", "-m", "omnigent.host.warm_bootstrap", mode],
                stdin=payload is not None,
                stdout=True,
                stderr=True,
                tty=False,
                _preload_content=False,
                _request_timeout=_EXEC_TIMEOUT_S,
            )
            try:
                if payload is not None:
                    connection.write_stdin(json.dumps(payload) + "\n")
                connection.run_forever(timeout=_EXEC_TIMEOUT_S)
                if connection.is_open() or connection.returncode != 0:
                    raise click.ClickException(
                        f"Warm bootstrap {mode} did not complete successfully."
                    )
                output = connection.read_stdout()
                if len(output) > 4096:
                    raise click.ClickException("Invalid warm bootstrap status response.")
                return output
            finally:
                connection.close()

    def _status(self, handle: WarmPoolHandle, pod_name: str) -> dict[str, Any]:
        try:
            status = json.loads(self._exec(handle, pod_name, "status"))
            if not isinstance(status, dict) or status.get("stage") not in {
                "waiting",
                "bound",
                "preparing",
                "prepared",
                "failed",
            }:
                raise ValueError
            if status["stage"] == "waiting":
                if status.get("generation") is not None:
                    raise ValueError
            elif not isinstance(status.get("generation"), str) or not status["generation"]:
                raise ValueError
            return status
        except ValueError:
            raise click.ClickException("Invalid warm bootstrap status response.") from None

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
        agent_name: str | None = None,
        on_stage: Callable[[str], None] | None = None,
    ) -> str:
        if not sandbox_id.startswith(_HANDLE_PREFIX):
            return super().start_host(
                sandbox_id,
                token=token,
                host_id=host_id,
                host_name=host_name,
                server_url=server_url,
                repos=repos,
                host_config=host_config,
                agent_name=agent_name,
                on_stage=on_stage,
            )
        _ensure_sdk()
        handle = WarmPoolHandle.parse(sandbox_id)
        workspace = f"{_HOME_DIR}/workspace"
        generation = hashlib.sha256(token.encode()).hexdigest()[:32]
        deadline = time.monotonic() + _resolve_pod_ready_timeout_s(self._pod_ready_timeout_s)
        if on_stage:
            on_stage("starting")
        try:
            sandbox = self._allocation(handle)
            shared = _shared_profile(sandbox["spec"])
            self._validate_profile(
                sandbox["spec"], agent_name=agent_name, host_config=host_config, shared=shared
            )
            self._patch_deadline(handle, boot=True)
            # The registered host now owns cleanup. Remove the unactivated-claim TTL.
            self._load_custom().patch_namespaced_custom_object(
                EXTENSION_GROUP,
                API_VERSION,
                handle.namespace,
                CLAIMS,
                handle.claim_name,
                {"metadata": {"uid": handle.claim_uid}, "spec": {"lifecycle": None}},
                _request_timeout=_POD_READY_REQUEST_TIMEOUT_S,
            )
            activated_uid: str | None = None
            while time.monotonic() < deadline:
                sandbox = self._allocation(handle)
                pod = self._pod(handle, sandbox)
                if pod is None:
                    time.sleep(_POLL_S)
                    continue
                failure = _terminal_failure(pod)
                if failure:
                    raise click.ClickException(f"Warm Sandbox startup failed: {failure[1]}")
                if activated_uid is not None and pod.metadata.uid != activated_uid:
                    raise click.ClickException(
                        "Warm Sandbox Pod changed during activation; retry the session."
                    )
                self._validate_pod(pod, handle, agent_name=agent_name, shared=shared)
                # Failed preparation makes readiness false; still read its error state.
                containers = pod.status.container_statuses or []
                if not any(
                    item.name == BOOTSTRAP_CONTAINER and item.state and item.state.running
                    for item in containers
                ):
                    time.sleep(_POLL_S)
                    continue
                status = self._status(handle, pod.metadata.name)
                if status.get("stage") == "waiting":
                    if not containers or not all(item.ready for item in containers):
                        time.sleep(_POLL_S)
                        continue
                    payload = {
                        "version": 1,
                        "pod_uid": pod.metadata.uid,
                        "generation": generation,
                        "host_id": host_id,
                        "host_name": host_name,
                        "token": token,
                        "server_url": server_url,
                        "prepare_command": _render_workspace_prep_command(
                            workspace, repos, server_url, host_id, host_config
                        ),
                    }
                    activated_uid = pod.metadata.uid
                    try:
                        self._exec(handle, pod.metadata.name, "activate", payload)
                    except Exception:
                        # Delivery may have succeeded before the connection failed.
                        observed = self._status(handle, pod.metadata.name)
                        if observed.get("generation") != generation:
                            raise
                    if on_stage and repos:
                        on_stage("cloning")
                elif status.get("generation") != generation:
                    raise click.ClickException(
                        "Warm Sandbox is already activated for another host generation."
                    )
                elif status.get("stage") == "prepared":
                    return f"{workspace}/{repos[0].repo_name}" if len(repos) == 1 else workspace
                elif status.get("stage") == "failed":
                    raise click.ClickException("Warm Sandbox workspace preparation failed.")
                time.sleep(_POLL_S)
            raise click.ClickException(
                "Timed out waiting for warm Sandbox activation and workspace preparation."
            )
        finally:
            self._close_clients()

    def resume(self, sandbox_id: str) -> None:
        if not sandbox_id.startswith(_HANDLE_PREFIX):
            return super().resume(sandbox_id)
        _ensure_sdk()
        handle = WarmPoolHandle.parse(sandbox_id)
        try:
            sandbox = self._allocation(handle)
            pod = self._pod(handle, sandbox)
            if pod is not None:
                from kubernetes import client
                from kubernetes.client.rest import ApiException

                try:
                    self._load_core().delete_namespaced_pod(
                        pod.metadata.name,
                        handle.namespace,
                        body=client.V1DeleteOptions(
                            preconditions=client.V1Preconditions(uid=pod.metadata.uid)
                        ),
                        _request_timeout=_POD_READY_REQUEST_TIMEOUT_S,
                    )
                except ApiException as exc:
                    # Idle expiry may remove the Pod after the ownership check.
                    if exc.status != 404:
                        raise
            # Wake happens in start_host, after the server arms the fresh token.
        finally:
            self._close_clients()

    def keep_alive(self, sandbox_id: str) -> bool | None:
        if not sandbox_id.startswith(_HANDLE_PREFIX):
            return super().keep_alive(sandbox_id)
        _ensure_sdk()
        handle = WarmPoolHandle.parse(sandbox_id)
        try:
            self._allocation(handle)
            self._patch_deadline(handle, boot=False)
            return True
        except Exception as exc:
            _logger.warning(
                "Could not extend warm Sandbox %s (%s)", handle.sandbox_name, _api_reason(exc)
            )
            return False
        finally:
            self._close_clients()

    def is_running(self, sandbox_id: str) -> bool | None:
        if not sandbox_id.startswith(_HANDLE_PREFIX):
            return super().is_running(sandbox_id)
        _ensure_sdk()
        handle = WarmPoolHandle.parse(sandbox_id)
        try:
            pod = self._pod(handle, self._allocation(handle))
            return pod is not None and pod.status.phase == "Running"
        except SandboxGoneError:
            return False
        finally:
            self._close_clients()

    def _delete_claim(self, namespace: str, name: str, uid: str) -> None:
        from kubernetes.client.rest import ApiException

        try:
            self._load_custom().delete_namespaced_custom_object(
                EXTENSION_GROUP,
                API_VERSION,
                namespace,
                CLAIMS,
                name,
                body={
                    "apiVersion": "v1",
                    "kind": "DeleteOptions",
                    "propagationPolicy": "Foreground",
                    "preconditions": {"uid": uid},
                },
                _request_timeout=_POD_READY_REQUEST_TIMEOUT_S,
            )
        except ApiException as exc:
            if exc.status == 404:
                return
            if exc.status == 409:
                try:
                    claim = self._get(EXTENSION_GROUP, CLAIMS, namespace, name)
                except ApiException as missing:
                    if missing.status == 404:
                        return
                    raise
                if claim["metadata"]["uid"] != uid:
                    return
            raise

    def terminate(self, sandbox_id: str) -> None:
        if not sandbox_id.startswith(_HANDLE_PREFIX):
            return super().terminate(sandbox_id)
        _ensure_sdk()
        handle = WarmPoolHandle.parse(sandbox_id)
        try:
            self._delete_claim(handle.namespace, handle.claim_name, handle.claim_uid)
        finally:
            self._close_clients()


def main() -> None:
    """Generate operator-owned pool/template manifests from server configuration."""
    import yaml

    from omnigent.server.managed_hosts import parse_sandbox_config

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--replicas", type=int, default=1)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--agent-name", help="Require this built-in agent's admission profile")
    scope.add_argument(
        "--shared", action="store_true", help="Share unlabeled Pods across agents and harnesses"
    )
    args = parser.parse_args()
    raw = yaml.safe_load(args.config.read_text())
    deployment = parse_sandbox_config(raw.get("sandbox"))
    if deployment is None:
        parser.error("server configuration must enable sandbox.provider: agent_sandbox")
    config = deployment.for_provider("agent_sandbox")
    if config is None:
        parser.error("server configuration must enable sandbox.provider: agent_sandbox")
    launcher = cast(AgentSandboxWarmPoolLauncher, config.launcher_factory())
    from omnigent.onboarding.sandboxes.kubernetes import _validate_k8s_name_env

    _validate_k8s_name_env(args.name, env_var="--name", kind="label")
    if args.replicas < 0:
        parser.error("--replicas must be non-negative")
    metadata = {"name": args.name, "namespace": launcher._resolve_namespace()}
    template = {
        "apiVersion": f"{EXTENSION_GROUP}/{API_VERSION}",
        "kind": "SandboxTemplate",
        "metadata": metadata,
        "spec": {
            **launcher.template_spec(agent_name=args.agent_name, shared=args.shared),
            "networkPolicyManagement": "Unmanaged",
        },
    }
    pool = {
        "apiVersion": f"{EXTENSION_GROUP}/{API_VERSION}",
        "kind": "SandboxWarmPool",
        "metadata": metadata,
        "spec": {
            "replicas": args.replicas,
            "sandboxTemplateRef": {"name": args.name},
            "updateStrategy": {"type": "Recreate"},
        },
    }
    print(yaml.safe_dump_all([template, pool], sort_keys=False), end="")


if __name__ == "__main__":
    main()
