"""Client contract for static sandboxes: the user's own VMs, saved by name."""

from __future__ import annotations

import pytest

from omnigent.community.sandbox.remote.client import RemoteSandboxError, _path_segment
from omnigent.community.sandbox.remote.launcher import RemoteSandboxLauncher
from omnigent.onboarding.sandboxes.base import SandboxGoneError, StaticSandboxProvider
from tests.community.sandbox.remote._shim import FakeService, make_client, make_config


def test_static_sandboxes_capability_defaults_off_for_older_services(
    service: FakeService,
) -> None:
    def launcher() -> RemoteSandboxLauncher:
        return RemoteSandboxLauncher(make_config(), client=make_client(service))

    assert launcher().capabilities.static_sandboxes is False
    service.declared_capabilities["static_sandboxes"] = True
    assert launcher().capabilities.static_sandboxes is True
    # An unreadable service declares the baseline, which has no static sandboxes.
    service.capabilities_status = 503
    assert launcher().capabilities.static_sandboxes is False


def test_the_launcher_is_a_static_sandbox_provider() -> None:
    assert isinstance(RemoteSandboxLauncher(make_config()), StaticSandboxProvider)


def test_get_static_sandbox_reads_one_entry(service: FakeService) -> None:
    service.vms = {"foo": "running"}
    service.static_labels = {"foo": "Checkout"}
    service.static_repo = "example/Sample-App"
    launcher = RemoteSandboxLauncher(make_config(), client=make_client(service))
    entry = launcher.get_static_sandbox("foo")
    assert entry is not None and entry["label"] == "Checkout"
    assert entry["default_repo_url"] == "https://github.com/example/Sample-App"
    assert entry["checkout_path"] == "/workspaces/web-app"
    assert launcher.get_static_sandbox("bar") is None
    assert service.paths("GET") == ["/v1/static-sandboxes/foo", "/v1/static-sandboxes/bar"]


def test_an_entry_without_a_primary_repo_has_empty_repo_fields(service: FakeService) -> None:
    service.vms = {"foo": "paused"}
    service.static_labels = {"foo": "foo"}
    service.static_repo = ""
    entry = make_client(service).get_static_sandbox("foo")
    assert entry is not None
    assert (entry["status"], entry["repo"], entry["default_repo_url"]) == ("paused", "", "")
    assert entry["checkout_path"] == ""


def test_static_checkout_path_comes_from_the_service(service: FakeService) -> None:
    service.vms = {"foo": "running"}
    service.static_labels = {"foo": "foo"}
    client = make_client(service)
    path = client.static_checkout_path("foo", "https://github.com/example/Sample-App.git")
    assert path == "/workspaces/web-app"
    assert service.last_query == {"repo_url": "https://github.com/example/Sample-App.git"}
    with pytest.raises(RemoteSandboxError) as exc:
        client.static_checkout_path("bar", "https://github.com/o/r")
    assert exc.value.status_code == 404


def test_resume_waits_for_the_vm_and_returns_the_entry(service: FakeService) -> None:
    service.vms = {"foo": "paused"}
    service.static_labels = {"foo": "foo"}
    launcher = RemoteSandboxLauncher(make_config(), client=make_client(service))
    assert launcher.resume_static_sandbox("foo")["status"] == "running"
    # The service waits for the VM, so the read deadline is lifted.
    assert service.timeout("/v1/static-sandboxes/foo/resume")["read"] is None


def test_resume_of_a_gone_vm_raises_sandbox_gone(service: FakeService) -> None:
    service.static_labels = {"foo": "foo"}
    launcher = RemoteSandboxLauncher(make_config(), client=make_client(service))
    with pytest.raises(SandboxGoneError, match="no longer exists"):
        launcher.resume_static_sandbox("foo")


def test_a_resume_timeout_keeps_the_status_and_code(service: FakeService) -> None:
    service.vms = {"foo": "paused"}
    service.static_labels = {"foo": "foo"}
    service.resume_error = (504, "SANDBOX_RESUME_TIMEOUT")
    launcher = RemoteSandboxLauncher(make_config(), client=make_client(service))
    with pytest.raises(RemoteSandboxError) as exc:
        launcher.resume_static_sandbox("foo")
    assert (exc.value.status_code, exc.value.code) == (504, "SANDBOX_RESUME_TIMEOUT")
    assert exc.value.retryable is False


def test_diagnose_host_returns_the_structured_report(service: FakeService) -> None:
    service.vms = {"foo": "running"}
    service.static_labels = {"foo": "foo"}
    service.diagnostics = {
        "host_process_running": None,
        "fatal_reason": "omnigent is not on PATH on the VM",
        "omnigent_on_path": False,
    }
    launcher = RemoteSandboxLauncher(make_config(), client=make_client(service))
    report = launcher.diagnose_host("foo", "http://omnigent.mesh:8080")
    assert report == {
        "sandbox_id": "foo",
        "status": "running",
        "host_process_running": None,
        "omnigent_on_path": False,
        "omnigent_version": "0.17.0",
        "server_host": "omnigent.mesh",
        "server_resolves": True,
        "log_tail": "",
        "fatal_reason": "omnigent is not on PATH on the VM",
    }
    assert service.body("/v1/static-sandboxes/foo/diagnostics") == {
        "server_url": "http://omnigent.mesh:8080"
    }
    service.vms["foo"] = "paused"
    with pytest.raises(RemoteSandboxError) as exc:
        launcher.diagnose_host("foo", "http://omnigent.mesh:8080")
    assert (exc.value.status_code, exc.value.code) == (409, "SANDBOX_PAUSED")


def test_exec_on_a_paused_static_sandbox_is_refused(service: FakeService) -> None:
    service.vms = {"foo": "paused"}
    with pytest.raises(RemoteSandboxError) as exc:
        make_client(service).run_script("foo", "true")
    assert (exc.value.status_code, exc.value.code) == (409, "SANDBOX_PAUSED")


def test_list_available_vms_returns_normalized_entries(service: FakeService) -> None:
    service.vms = {"foo": "running", "android-build": "paused"}
    vms = make_client(service).list_available_vms()
    assert vms == [
        {"sandbox_id": "foo", "status": "running", "repo": "", "last_activity_at": None},
        {"sandbox_id": "android-build", "status": "paused", "repo": "", "last_activity_at": None},
    ]
    assert service.principals[-1] == ("/v1/vms/available", "alice")


def test_put_lists_and_deletes_a_static_sandbox(service: FakeService) -> None:
    service.vms = {"foo": "paused"}
    client = make_client(service)
    entry = client.put_static_sandbox("foo", "Checkout web app")
    assert entry["label"] == "Checkout web app"
    assert entry["status"] == "paused"
    assert service.body("/v1/static-sandboxes/foo") == {"label": "Checkout web app"}
    assert [e["sandbox_id"] for e in client.list_static_sandboxes()] == ["foo"]
    assert client.delete_static_sandbox("foo") is True
    assert client.delete_static_sandbox("foo") is False
    assert client.list_static_sandboxes() == []


def test_a_deleted_vm_lists_as_gone(service: FakeService) -> None:
    service.static_labels = {"foo": "Checkout"}
    [entry] = make_client(service).list_static_sandboxes()
    assert entry["status"] == "gone"


def test_put_surfaces_the_service_refusal(service: FakeService) -> None:
    client = make_client(service)
    with pytest.raises(RemoteSandboxError) as missing:
        client.put_static_sandbox("nope", "x")
    assert missing.value.status_code == 404
    service.vms = {"omnigent-abc": "running"}
    with pytest.raises(RemoteSandboxError) as refused:
        client.put_static_sandbox("omnigent-abc", "x")
    assert refused.value.status_code == 400


def test_unprovisioned_service_surfaces_503(service: FakeService) -> None:
    service.static_status = 503
    with pytest.raises(RemoteSandboxError) as exc:
        make_client(service).list_static_sandboxes()
    assert exc.value.status_code == 503


def test_sandbox_id_is_quoted_into_one_path_segment(service: FakeService) -> None:
    assert _path_segment("../sandboxes/x") == "..%2Fsandboxes%2Fx"
    with pytest.raises(RemoteSandboxError):
        make_client(service).delete_static_sandbox("..")


def test_link_static_host_keeps_the_first_link(service: FakeService) -> None:
    service.vms = {"foo": "running"}
    service.static_labels = {"foo": "foo"}
    client = make_client(service)
    assert client.list_static_sandboxes()[0]["omnigent_host_id"] is None
    assert client.link_static_host("foo", "a" * 32)["omnigent_host_id"] == "a" * 32
    # A second replica's candidate loses; it gets the winning id back.
    assert client.link_static_host("foo", "b" * 32)["omnigent_host_id"] == "a" * 32
    assert service.body("/v1/static-sandboxes/foo/host") == {"omnigent_host_id": "a" * 32}


def test_link_static_host_without_an_entry_is_404(service: FakeService) -> None:
    with pytest.raises(RemoteSandboxError) as exc:
        make_client(service).link_static_host("foo", "a" * 32)
    assert exc.value.status_code == 404
