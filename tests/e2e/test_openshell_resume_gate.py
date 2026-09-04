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
when the compute backend retains the sandbox and can restart it. The inverse
failure is just as real: advertising resume on an SDK that cannot drive it
(``SandboxClient.start`` arrives in openshell 0.0.105) would render every
dormant host as wakeable while each wake fails, so the capability is gated on
the installed SDK actually exposing the primitive. The second
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
import sys
import types

import pytest

from omnigent.db.utils import now_epoch
from omnigent.onboarding.sandboxes.types import SandboxCapabilities
from omnigent.server.managed_hosts import (
    ManagedSandboxDeployment,
    host_resume_supported,
    parse_sandbox_config,
)
from omnigent.stores.host_store import Host, HostStore

_OWNER = "owner@example.com"


def _install_fake_sdk(monkeypatch: pytest.MonkeyPatch, *, with_resume: bool) -> None:
    """Install a minimal ``openshell`` module, with or without ``start``.

    The wake gate's capability probe inspects the real installed SDK's
    ``SandboxClient``; substituting one here pins which SDK generation the
    test simulates, independent of what the test environment installs.
    """

    class _SandboxClient:
        def start(self, name: str, *, workspace: str) -> None:  # pragma: no cover
            raise AssertionError("the gate must not invoke the primitive")

    if not with_resume:
        del _SandboxClient.start
    module = types.ModuleType("openshell")
    module.SandboxClient = _SandboxClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "openshell", module)


def _register_dormant_host(db_uri: str) -> Host:
    """Store the host row a stopped-but-volume-retaining launch leaves behind."""
    host_store = HostStore(db_uri)
    return host_store.register_managed_host(
        host_id="9c1f6a2b8d4e5f60718293a4b5c6d7e8",
        name="managed-openshell-dormant",
        user_id=_OWNER,
        token="tok-openshell-resume-gate",
        provider="openshell",
        sandbox_id="osb-dormant-1",
        token_expires_at=now_epoch() + 3600,
    )


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


def test_dormant_openshell_host_is_wakeable_in_place(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dormant OpenShell host with a recorded sandbox must pass the wake gate.

    The host row is what a stopped-but-resumable OpenShell host looks like
    after launch: provider recorded, ``sandbox_id`` bound (the volume
    survives an idle-stop), deployment config unchanged. With an SDK that
    exposes the resume primitive, ``host_resume_supported`` must return True
    — that is what renders the session's dormant host as a wakeable "asleep"
    state and lets ``resume_managed_host`` wake it under the same sandbox id
    when the next message arrives.
    """
    _install_fake_sdk(monkeypatch, with_resume=True)
    deployment = _openshell_deployment()
    host = _register_dormant_host(db_uri)

    assert host_resume_supported(host, deployment), (
        "OpenShell-backed dormant host is not wakeable even though the "
        "installed SDK exposes the resume primitive (SandboxClient.start): "
        "the open-session snapshot renders the terminal host_offline "
        "dead-end instead of the wakeable 'asleep' state, and "
        "resume_managed_host() silently no-ops instead of waking the "
        "sandbox in place."
    )


def test_dormant_openshell_host_stays_offline_without_sdk_primitive(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An SDK predating the resume primitive must not advertise the wake.

    If the gate said True here, the SPA would render the dormant host as
    wakeable and every send-message wake would fail with the upgrade hint —
    capability advertisement decoupled from actual capability. The honest
    answer on an old SDK is the pre-resume-framework ``host_offline`` state.
    """
    _install_fake_sdk(monkeypatch, with_resume=False)
    deployment = _openshell_deployment()
    host = _register_dormant_host(db_uri)

    assert not host_resume_supported(host, deployment), (
        "host_resume_supported() advertises an in-place wake on an SDK with "
        "no resume primitive (SandboxClient.start requires "
        "openshell>=0.0.105); every wake would fail with the upgrade hint "
        "instead of the host rendering as honestly offline."
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
