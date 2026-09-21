"""Live native allocation against an explicitly configured test deployment.

Set OMNIGENT_WARM_POOL_E2E=1 plus OMNIGENT_WARM_POOL_SERVER_URL,
OMNIGENT_WARM_POOL_KUBECONFIG, OMNIGENT_WARM_POOL_CONTEXT,
OMNIGENT_WARM_POOL_NAMESPACE, and OMNIGENT_WARM_POOL_NAME. The server must
accept unauthenticated test requests and use this otherwise-idle warm pool.
The real host and claude-sdk runner start without submitting a model prompt.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import tarfile
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("OMNIGENT_WARM_POOL_E2E") != "1",
        reason="set OMNIGENT_WARM_POOL_E2E=1 and the explicit test deployment settings",
    ),
    pytest.mark.timeout(1080),
]


def _setting(suffix: str) -> str:
    name = f"OMNIGENT_WARM_POOL_{suffix}"
    value = os.environ.get(name, "").strip()
    assert value, f"{name} must explicitly select the test deployment"
    return value


def _bundle() -> bytes:
    spec = (
        b"spec_version: 1\nname: warm-pool-e2e\n"
        b"executor:\n  type: omnigent\n  config:\n    harness: claude-sdk\n"
        b"prompt: Help the user inspect their sandbox workspace.\n"
    )
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        entry = tarfile.TarInfo("config.yaml")
        entry.size = len(spec)
        archive.addfile(entry, io.BytesIO(spec))
    return buffer.getvalue()


def _owned_by(resource: dict[str, Any], uid: str) -> bool:
    return any(
        owner.get("uid") == uid and owner.get("controller") is True
        for owner in resource["metadata"].get("ownerReferences", [])
    )


def test_managed_session_adopts_ready_pod_and_preserves_workspace_on_wake() -> None:
    """Adopt a spare, then wake the same workspace despite slow shell startup."""
    server_url = _setting("SERVER_URL").rstrip("/")
    kubeconfig = Path(_setting("KUBECONFIG")).expanduser().resolve()
    assert kubeconfig.is_file(), "The explicit test kubeconfig must exist"
    context = _setting("CONTEXT")
    namespace = _setting("NAMESPACE")
    pool_name = _setting("NAME")

    def kubectl(*args: str) -> str:
        return subprocess.run(
            [
                "kubectl",
                "--kubeconfig",
                str(kubeconfig),
                "--context",
                context,
                "--namespace",
                namespace,
                *args,
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout

    def resource(kind: str, name: str) -> dict[str, Any]:
        return json.loads(kubectl("get", kind, name, "-o", "json"))

    def resources(kind: str) -> list[dict[str, Any]]:
        return json.loads(kubectl("get", kind, "-o", "json"))["items"]

    pool = resource("sandboxwarmpools.extensions.agents.x-k8s.io", pool_name)
    pool_uid = pool["metadata"]["uid"]
    replicas = pool["spec"]["replicas"]
    assert replicas > 0, "The test pool must maintain at least one spare Sandbox"

    def ready_spares() -> dict[str, str]:
        sandbox_uids = {
            sandbox["metadata"]["uid"]
            for sandbox in resources("sandboxes.agents.x-k8s.io")
            if _owned_by(sandbox, pool_uid) and not sandbox["metadata"].get("deletionTimestamp")
        }
        return {
            pod["metadata"]["uid"]: pod["metadata"]["name"]
            for pod in resources("pods")
            if any(_owned_by(pod, uid) for uid in sandbox_uids)
            and not pod["metadata"].get("deletionTimestamp")
            and any(
                condition.get("type") == "Ready" and condition.get("status") == "True"
                for condition in pod.get("status", {}).get("conditions", [])
            )
        }

    def wait_for_spares(*, previous: dict[str, str] | None = None) -> dict[str, str]:
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            current = resource("sandboxwarmpools.extensions.agents.x-k8s.io", pool_name)
            assert current["metadata"]["uid"] == pool_uid, "The configured pool was replaced"
            spares = ready_spares()
            if (
                current.get("status", {}).get("readyReplicas", 0) >= replicas
                and len(spares) >= replicas
                and (previous is None or spares.keys() - previous.keys())
            ):
                return spares
            time.sleep(1)
        pytest.fail("The configured pool did not provide the expected ready spare Pods")

    before = wait_for_spares()
    previous_claims = {
        claim["metadata"]["uid"] for claim in resources("sandboxclaims.extensions.agents.x-k8s.io")
    }
    with httpx.Client(base_url=server_url, timeout=15) as client:
        response = client.post(
            "/v1/sessions",
            data={
                "metadata": json.dumps(
                    {
                        "title": f"Warm pool E2E {uuid.uuid4().hex[:8]}",
                        "host_type": "managed",
                        "sandbox_provider": "agent_sandbox",
                    }
                )
            },
            files={"bundle": ("agent.tar.gz", _bundle(), "application/gzip")},
            timeout=60,
        )
        response.raise_for_status()
        session_id = response.json()["session_id"]
        session_path = f"/v1/sessions/{session_id}"
        try:
            deadline = time.monotonic() + 420
            while time.monotonic() < deadline:
                response = client.get(session_path)
                response.raise_for_status()
                session = response.json()
                assert (session.get("sandbox_status") or {}).get("stage") != "failed", (
                    f"Managed launch failed for session {session_id}"
                )
                if session.get("host_online") and session.get("runner_online"):
                    break
                time.sleep(1)
            else:
                pytest.fail(f"Managed host/runner did not register for session {session_id}")

            host = client.get(f"/v1/hosts/{session['host_id']}")
            host.raise_for_status()
            assert host.json()["sandbox_provider"] == "agent_sandbox"
            assert host.json()["status"] == "online"

            claims = [
                claim
                for claim in resources("sandboxclaims.extensions.agents.x-k8s.io")
                if claim["metadata"]["uid"] not in previous_claims
                and claim.get("spec", {}).get("warmPoolRef", {}).get("name") == pool_name
            ]
            assert len(claims) == 1, "Run this E2E with an otherwise-idle dedicated pool"
            claim = claims[0]
            sandbox = resource("sandboxes.agents.x-k8s.io", claim["status"]["sandbox"]["name"])
            assert _owned_by(sandbox, claim["metadata"]["uid"])
            allocated = [
                pod
                for pod in resources("pods")
                if _owned_by(pod, sandbox["metadata"]["uid"])
                and not pod["metadata"].get("deletionTimestamp")
            ]
            assert len(allocated) == 1
            pod = allocated[0]
            pod_uid, pod_name = pod["metadata"]["uid"], pod["metadata"]["name"]
            assert pod_uid in before, "The session cold-started instead of claiming a ready Pod"
            assert before[pod_uid] == pod_name
            bound_host_id = kubectl(
                "exec",
                pod_name,
                "-c",
                "host",
                "--",
                "python3",
                "-c",
                "from omnigent.host.warm_bootstrap import _load_activation, _state_dir; "
                "print(_load_activation(_state_dir()).host_id)",
            ).strip()
            assert bound_host_id == session["host_id"], "The selected Pod belongs to another host"

            marker = f"/home/omnigent/workspace/WARM-POOL-E2E-{uuid.uuid4().hex}"
            content = "warm pool workspace is writable\n"
            kubectl(
                "exec",
                pod_name,
                "-c",
                "host",
                "--",
                "python3",
                "-c",
                "from pathlib import Path; import sys; Path(sys.argv[1]).write_text(sys.argv[2])",
                marker,
                content,
            )
            assert kubectl("exec", pod_name, "-c", "host", "--", "cat", marker) == content
            replenished = wait_for_spares(previous=before)
            assert pod_uid not in replenished, "Allocated Pods must leave the spare pool"

            home = next(volume for volume in pod["spec"]["volumes"] if volume["name"] == "home")
            pvc_name = home["persistentVolumeClaim"]["claimName"]
            pvc_uid = resource("pvc", pvc_name)["metadata"]["uid"]
            host_id = session["host_id"]
            sandbox_name = sandbox["metadata"]["name"]
            sandbox_uid = sandbox["metadata"]["uid"]
            kubectl(
                "exec",
                pod_name,
                "-c",
                "host",
                "--",
                "python3",
                "-c",
                "from pathlib import Path; p = Path.home() / '.bash_profile'; "
                "p.write_text((p.read_text() if p.exists() else '') + '\\nsleep 2\\n')",
            )
            sandbox = resource("sandboxes.agents.x-k8s.io", sandbox_name)
            assert sandbox["metadata"]["uid"] == sandbox_uid
            kubectl(
                "patch",
                "sandboxes.agents.x-k8s.io",
                sandbox_name,
                "--type=merge",
                "--patch",
                json.dumps(
                    {
                        "metadata": {
                            "uid": sandbox_uid,
                            "resourceVersion": sandbox["metadata"]["resourceVersion"],
                        },
                        "spec": {
                            "shutdownPolicy": "Retain",
                            "shutdownTime": "2000-01-01T00:00:00Z",
                        },
                    }
                ),
            )
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                response = client.get(session_path)
                response.raise_for_status()
                session = response.json()
                if (
                    not any(p["metadata"]["uid"] == pod_uid for p in resources("pods"))
                    and not session.get("host_online")
                    and not session.get("runner_online")
                ):
                    break
                time.sleep(1)
            else:
                pytest.fail("The allocated Pod and its host/runner did not suspend")
            assert resource("pvc", pvc_name)["metadata"]["uid"] == pvc_uid
            response = client.post(
                f"{session_path}/events",
                json={"type": "retry_session", "data": {}},
                timeout=420,
            )
            response.raise_for_status()
            deadline = time.monotonic() + 420
            while time.monotonic() < deadline:
                response = client.get(session_path)
                response.raise_for_status()
                session = response.json()
                assert (session.get("sandbox_status") or {}).get("stage") != "failed", (
                    f"Managed wake failed for session {session_id}"
                )
                if session.get("host_online") and session.get("runner_online"):
                    break
                time.sleep(1)
            else:
                pytest.fail("Managed host/runner did not register after wake")
            assert session["host_id"] == host_id
            assert (
                resource("sandboxclaim", claim["metadata"]["name"])["metadata"]["uid"]
                == claim["metadata"]["uid"]
            )
            assert resource("sandbox", sandbox_name)["metadata"]["uid"] == sandbox_uid
            assert resource("pvc", pvc_name)["metadata"]["uid"] == pvc_uid
            resumed = [p for p in resources("pods") if _owned_by(p, sandbox_uid)]
            assert len(resumed) == 1
            assert resumed[0]["metadata"]["uid"] != pod_uid
            assert any(
                c.get("type") == "Ready" and c.get("status") == "True"
                for c in resumed[0].get("status", {}).get("conditions", [])
            ), "The retained shell profile must not affect readiness"
            resumed_home = next(v for v in resumed[0]["spec"]["volumes"] if v["name"] == "home")
            assert resumed_home["persistentVolumeClaim"]["claimName"] == pvc_name
            assert (
                kubectl("exec", resumed[0]["metadata"]["name"], "-c", "host", "--", "cat", marker)
                == content
            )
        finally:
            response = client.delete(session_path, timeout=30)
            response.raise_for_status()
