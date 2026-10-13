"""A fake omnigent-shim with a persistent VM, shared by the static-sandbox tests."""

from __future__ import annotations

import re

import httpx

from omnigent.community.sandbox.remote.client import RemoteSandboxClient
from omnigent.community.sandbox.remote.launcher import RemoteSandboxLauncher
from omnigent.server import vm_hosts
from omnigent.server.managed_hosts import ManagedSandboxConfig, ManagedSandboxDeployment
from omnigent.stores.host_store import HostStore
from tests.community.sandbox.remote._shim import FakeService, make_client, make_config

OWNER = "alice"
WS = "foo"


class VmShim(FakeService):
    """The fake shim, plus a host that dials back when started."""

    def __init__(self, host_store: HostStore) -> None:
        super().__init__()
        self.host_store = host_store
        self.connect_on_start = True
        self.started_tokens: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        response = super().handler(request)
        path = request.url.path
        if path.endswith("/exec") and request.method == "POST" and response.status_code < 400:
            script = self.requests[-1][2]["script"]  # type: ignore[index]
            match = re.search(r"OMNIGENT_HOST_TOKEN=(\S+) OMNIGENT_HOST_ID=(\S+)", script)
            if match is not None:
                token, host_id = match.groups()
                self.started_tokens.append(token)
                if self.connect_on_start:
                    # The host reports the name its row already has.
                    row = self.host_store.get_host(host_id)
                    self.host_store.upsert_on_connect(
                        host_id=host_id,
                        name=row.name if row is not None else vm_hosts.static_host_name(WS),
                        user_id=OWNER,
                        managed_token=token,
                    )
        return response

    def deletes_of_vms(self) -> list[str]:
        return [p for p in self.paths("DELETE") if p.startswith("/v1/sandboxes/")]

    def creates(self) -> list[str]:
        return [p for p in self.paths("POST") if p == "/v1/sandboxes" or p.endswith("/start")]


def make_shim(host_store: HostStore) -> VmShim:
    """A shim offering static sandboxes, with the running VM ``foo`` saved."""
    service = VmShim(host_store)
    service.declared_capabilities["static_sandboxes"] = True
    service.vms = {WS: "running"}
    service.static_labels = {WS: "Checkout web app"}
    return service


class _ShimLauncher(RemoteSandboxLauncher):
    """The real launcher, with its client built against the fake shim."""

    def __init__(self, shim: VmShim) -> None:
        super().__init__(make_config())
        self._shim = shim

    def _unbound_client(self) -> RemoteSandboxClient:
        return make_client(self._shim, self._config, principal=None)

    def _api(self) -> RemoteSandboxClient:
        if self._client is None:
            assert self._principal is not None
            self._client = make_client(self._shim, self._config, principal=self._principal)
        return self._client


def make_deployment(shim: VmShim) -> ManagedSandboxDeployment:
    """A one-provider ``remote`` deployment whose launchers talk to *shim*."""
    return ManagedSandboxDeployment.single(
        ManagedSandboxConfig(
            server_url="http://omnigent.mesh:8080",
            launcher_factory=lambda: _ShimLauncher(shim),
            token_ttl_s=3600,
            provider="remote",
        )
    )
