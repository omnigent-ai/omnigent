"""Exercise a real Substrate deployment from the server's test container.

Requires a disposable, single-workspace Omnigent deployment and access to its
isolated database and mounted Substrate credentials. Creates its own session,
checks automatic idle suspension and authenticated host filesystem RPCs across
wake, and deletes only that session/host. It does not qualify model inference.
Run as a module from the checkout; see the Substrate overlay runbook.
"""

from __future__ import annotations

import argparse
import ssl
import uuid
from pathlib import Path
from urllib.parse import quote

import httpx
import yaml

from omnigent.onboarding.sandboxes.substrate import (
    ActorHandle,
    ControlError,
    SubstrateSandboxLauncher,
)
from omnigent.server.managed_hosts import parse_sandbox_config
from omnigent.stores.host_store import HostStore
from tests._helpers.polling import poll_until
from tests._helpers.session import send_user_text


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--database-uri", required=True)
    parser.add_argument("--ca", type=Path)
    parser.add_argument("--agent-id")
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args()
    deployment = parse_sandbox_config(yaml.safe_load(args.config.read_text())["sandbox"])
    assert deployment is not None
    entry = deployment.for_provider("substrate")
    assert entry is not None
    launcher = entry.launcher_factory()
    assert isinstance(launcher, SubstrateSandboxLauncher)
    store = HostStore(args.database_uri)
    context = ssl.create_default_context(cafile=str(args.ca) if args.ca else None)
    session_id = host_id = None
    handle = None
    with httpx.Client(
        base_url=args.server, verify=context, timeout=args.timeout, trust_env=False
    ) as client:
        info = client.get("/v1/info").raise_for_status().json()
        assert "substrate" in info["sandbox_providers"]
        agents = client.get("/v1/agents").raise_for_status().json()["data"]
        agent_id = args.agent_id or agents[0]["id"]
        try:
            session = (
                client.post(
                    "/v1/sessions",
                    json={
                        "agent_id": agent_id,
                        "host_type": "managed",
                        "sandbox_provider": "substrate",
                    },
                )
                .raise_for_status()
                .json()
            )
            session_id = session["id"]

            def ready_session() -> bool:
                nonlocal host_id
                current = client.get(f"/v1/sessions/{session_id}").raise_for_status().json()
                if (current.get("sandbox_status") or {}).get("stage") == "failed":
                    raise AssertionError(
                        "Managed session launch failed; inspect server diagnostics"
                    )
                host_id = current.get("host_id")
                return bool(
                    host_id
                    and current.get("host_online") is True
                    and current.get("runner_online") is True
                    and (current.get("sandbox_status") or {}).get("stage") in {None, "ready"}
                )

            poll_until(ready_session, timeout_s=args.timeout, interval_s=0.5)
            assert host_id
            host = store.get_host(host_id)
            assert host and host.sandbox_id and host.sandbox_provider == "substrate"
            handle = ActorHandle.parse(host.sandbox_id, launcher.config)
            marker = f"snapshot-check-{uuid.uuid4().hex}"
            workspace = "/home/omnigent/workspace"
            client.post(
                f"/v1/hosts/{host_id}/directories", json={"path": f"{workspace}/{marker}"}
            ).raise_for_status()
            print("Host connected over WSS; workspace marker created", flush=True)

            def automatically_suspended() -> bool:
                # Browsing must not renew the work-only idle clock.
                client.get(f"/v1/sessions/{session_id}").raise_for_status()
                state = store.sandbox_operation(host_id)
                if state and state.get("phase") == "unknown":
                    raise AssertionError("Automatic suspension became unresolved")
                return bool(
                    state
                    and state.get("phase") == "suspended"
                    and launcher.is_running(host.sandbox_id) is False
                )

            poll_until(automatically_suspended, timeout_s=args.timeout, interval_s=0.5)
            print("Automatic idle suspension confirmed while polling the session", flush=True)
            poll_until(
                lambda: (
                    client.get(f"/v1/hosts/{host_id}").raise_for_status().json()["status"]
                    == "offline"
                ),
                timeout_s=args.timeout,
            )
            send_user_text(client, session_id=session_id, text="Resume this test session.")
            poll_until(ready_session, timeout_s=args.timeout)
            refreshed = store.get_host(host_id)
            assert refreshed and refreshed.sandbox_id is not None
            assert refreshed.sandbox_id == host.sandbox_id
            assert ActorHandle.parse(refreshed.sandbox_id, launcher.config).uid == handle.uid
            listing = (
                client.get(f"/v1/hosts/{host_id}/filesystem/{quote(workspace, safe='')}")
                .raise_for_status()
                .json()
            )
            assert any(
                item.get("name") == marker or item.get("path") == f"{workspace}/{marker}"
                for item in listing["data"]
            )
            print(
                "Durable snapshot wake retained Actor UID, host ID, and workspace marker",
                flush=True,
            )
        finally:
            if session_id:
                client.delete(f"/v1/sessions/{session_id}").raise_for_status()
            if host_id:
                response = client.delete(f"/v1/hosts/{host_id}")
                assert response.status_code in {200, 204, 404}
        if handle is not None:

            def removed() -> bool:
                try:
                    launcher.client.call("GetActor", {"actor": handle.ref})
                except ControlError as error:
                    if error.code == "NOT_FOUND":
                        return True
                    raise
                return False

            poll_until(removed, timeout_s=args.timeout, interval_s=0.5)
            print("Actor cleanup confirmed", flush=True)
    print("PASS", flush=True)


if __name__ == "__main__":
    main()
