"""A dormant OpenShell managed host stays inside the in-place resume framework.

A managed host launched on the ``openshell`` provider that has gone dormant
(sandbox stopped, persistent volume retained, ``sandbox_id`` recorded on the
host row) must be wakeable in place, exactly like the Kubernetes launcher's
hosts: the server's wake gate
:func:`omnigent.server.managed_hosts.host_resume_supported` feeds both
``SessionResponse.host_resumable`` (which the web SPA uses to render a dormant
host as a wakeable "asleep" state instead of the terminal ``host_offline``
dead-end) and :func:`omnigent.server.managed_hosts.resume_managed_host` (the
send-message wake path).

A driver that reports ``resume_stopped=False`` unconditionally makes that gate
always False, turning every dormant host on it into a terminal dead end even
when the compute backend retains the sandbox and can restart it. The second
half of the contract is that
:class:`~omnigent.onboarding.sandboxes.types.SandboxCapabilities` can express
a snapshot-backed (warm) restore, so a resume-capable driver can tell the
server that waking restores caches rather than cold-starting.

These tests drive the genuine server path — YAML ``sandbox:`` section →
provider-recorded launcher factory → wake gate — with the real OpenShell
launcher (its gRPC client is lazy, so no gateway is needed to read declared
capabilities).

Run directly; no live server, gateway, or LLM key is needed::

    pytest tests/e2e/test_openshell_resume_gate.py -v
"""

from __future__ import annotations

import dataclasses

from omnigent.db.utils import now_epoch
from omnigent.onboarding.sandboxes.types import SandboxCapabilities
from omnigent.server.managed_hosts import (
    ManagedSandboxDeployment,
    host_resume_supported,
    parse_sandbox_config,
)
from omnigent.stores.host_store import HostStore

_OWNER = "owner@example.com"


def _openshell_deployment() -> ManagedSandboxDeployment:
    """Parse a real ``sandbox: {provider: openshell}`` server config section.

    This is the same path a self-hosted deployment's YAML takes, so the
    launcher the wake gate consults is the real ``OpenShellSandboxLauncher``,
    not a test double — the gate sees exactly the capabilities a production
    server would.
    """
    deployment = parse_sandbox_config(
        {
            "server_url": "https://omnigent.example.com",
            "provider": "openshell",
        }
    )
    assert deployment is not None
    return deployment


def test_dormant_openshell_host_is_wakeable_in_place(db_uri: str) -> None:
    """A dormant OpenShell host with a recorded sandbox must pass the wake gate.

    The host row below is what a stopped-but-resumable OpenShell host looks
    like after launch: provider recorded, ``sandbox_id`` bound (the volume
    survives an idle-stop), deployment config unchanged. For a provider whose
    backend can resume a stopped sandbox in place, ``host_resume_supported``
    must return True — that is what renders the session's dormant host as a
    wakeable "asleep" state and lets ``resume_managed_host`` wake it under the
    same sandbox id when the next message arrives.
    """
    deployment = _openshell_deployment()
    host_store = HostStore(db_uri)
    host = host_store.register_managed_host(
        host_id="9c1f6a2b8d4e5f60718293a4b5c6d7e8",
        name="managed-openshell-dormant",
        user_id=_OWNER,
        token="tok-openshell-resume-gate",
        provider="openshell",
        sandbox_id="osb-dormant-1",
        token_expires_at=now_epoch() + 3600,
    )

    assert host_resume_supported(host, deployment), (
        "OpenShell-backed dormant host is not wakeable: the OpenShell driver "
        "reports SandboxCapabilities.resume_stopped=False unconditionally, so "
        "host_resume_supported() is always False for an OpenShell host — the "
        "open-session snapshot renders the terminal host_offline dead-end "
        "instead of the wakeable 'asleep' state, and resume_managed_host() "
        "silently no-ops instead of waking the sandbox in place."
    )


def test_sandbox_capabilities_can_express_snapshot_restore() -> None:
    """The capability model must be able to distinguish a warm (snapshot) restore.

    A backend that suspends with a snapshot restores dependencies and caches
    on resume; one that merely restarts a stopped sandbox comes back cold.
    Without a snapshot field on ``SandboxCapabilities`` a driver cannot
    advertise the difference, so the server cannot distinguish a cold restart
    from a snapshot restore when it wakes a dormant host.
    """
    field_names = {f.name for f in dataclasses.fields(SandboxCapabilities)}
    assert "snapshot_restore" in field_names, (
        "SandboxCapabilities models no snapshot capability (fields: "
        f"{sorted(field_names)}); a driver whose backend restores from a "
        "suspend+snapshot cannot advertise that a resume brings back warm "
        "state, so the server cannot distinguish a cold restart from a "
        "snapshot restore."
    )
    # Cold restart stays the default: a driver that says nothing new keeps
    # advertising a plain (cold) resume.
    assert SandboxCapabilities().snapshot_restore is False
    # A suspend+snapshot backend can advertise the warm restore.
    warm = SandboxCapabilities(resume_stopped=True, snapshot_restore=True)
    assert warm.snapshot_restore is True
