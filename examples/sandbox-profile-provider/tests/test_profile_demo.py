"""Public contract and file-provider checks; no Kubernetes infrastructure is used."""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from pydantic import ValidationError

from omnigent.community.sandbox.profile_demo import MAX_CATALOG_BYTES, FileCatalogSandboxLauncher
from omnigent.onboarding.sandboxes.base import SandboxCapabilityError
from omnigent.onboarding.sandboxes.kubernetes import KubernetesSandboxLauncher
from omnigent.onboarding.sandboxes.profiles import (
    ProviderSandboxProfile,
    SandboxProfileAccessDenied,
    SandboxProfileChanged,
    SandboxProfileContext,
)
from omnigent.onboarding.sandboxes.registry import (
    instantiate,
    plugin_state,
    reset_plugin_state_for_tests,
)
from omnigent.onboarding.sandboxes.types import SandboxConfigError, SandboxError

ALICE = SandboxProfileContext(user_id="alice", workspace_id=1)


@pytest.fixture
def catalog_file(tmp_path: Path) -> Path:
    target = tmp_path / "catalog.json"
    shutil.copyfile(Path(__file__).parents[1] / "catalog.json", target)
    return target


@pytest.fixture
def launcher(catalog_file: Path) -> FileCatalogSandboxLauncher:
    reset_plugin_state_for_tests()
    result = instantiate("profile_demo", config={"catalog_file": str(catalog_file)})
    assert isinstance(result, FileCatalogSandboxLauncher)
    return result


@pytest.mark.parametrize(
    "invalid",
    [
        {"id": ""},
        {"id": "Uppercase"},
        {"id": "-leading"},
        {"id": "a" * 65},
        {"id": "path/segment"},
        {"id": "trailing\n"},
        {"name": ""},
        {"name": "n" * 101},
        {"description": "d" * 501},
        {"description": None},
        {"revision": ""},
        {"revision": "r" * 65},
        {"revision": 1},
        {"config": {}},
        {"users": ["alice"]},
    ],
)
def test_public_profile_rejects_invalid_metadata(invalid: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        ProviderSandboxProfile.model_validate(
            {"id": "valid_id", "name": "Valid", "revision": "opaque:v1", **invalid}
        )


def test_public_contract_is_frozen_and_revision_is_opaque() -> None:
    profile = ProviderSandboxProfile(
        id="a" * 62 + "_-", name="n" * 100, description="d" * 500, revision=" opaque:v1 "
    )
    assert profile.revision == " opaque:v1 "
    with pytest.raises(ValidationError, match="frozen"):
        profile.name = "Changed"
    with pytest.raises(FrozenInstanceError):
        ALICE.workspace_id = 2
    assert ProviderSandboxProfile(id="x", name="X", revision="1").description == ""
    assert issubclass(SandboxProfileAccessDenied, SandboxError)
    assert issubclass(SandboxProfileChanged, SandboxError)


def test_default_launcher_has_no_managed_profile_capability() -> None:
    launcher = KubernetesSandboxLauncher()
    assert not launcher.manages_profiles
    with pytest.raises(SandboxCapabilityError):
        launcher.list_profiles(ALICE)
    with pytest.raises(SandboxCapabilityError):
        launcher.select_profile(ALICE, ProviderSandboxProfile(id="x", name="X", revision="1"))


def test_installed_plugin_filters_both_identity_dimensions(
    launcher: FileCatalogSandboxLauncher,
) -> None:
    assert "profile_demo" not in plugin_state().load_errors
    assert launcher.manages_profiles
    with patch.object(KubernetesSandboxLauncher, "_load_clients", side_effect=AssertionError):
        assert [p.id for p in launcher.list_profiles(ALICE)] == ["demo-small", "demo-large"]
        assert [p.id for p in launcher.list_profiles(SandboxProfileContext("alice", 0))] == [
            "demo-small",
            "demo-large",
        ]
        assert [p.id for p in launcher.list_profiles(SandboxProfileContext("bob", 1))] == [
            "demo-small"
        ]
        assert launcher.list_profiles(SandboxProfileContext("alice", 2)) == []
        assert launcher.list_profiles(SandboxProfileContext("unknown", 1)) == []
    assert set(launcher.list_profiles(ALICE)[0].model_dump()) == {
        "id",
        "name",
        "description",
        "revision",
    }


def test_selection_configures_the_launcher_before_fake_lifecycle(
    launcher: FileCatalogSandboxLauncher,
) -> None:
    def check_settings(current: KubernetesSandboxLauncher, *args: object) -> str:
        del args
        assert current is launcher
        assert current._resolve_namespace() == "omnigent-demo"
        assert current._resolve_image() == "ghcr.io/omnigent-ai/omnigent-host:latest"
        assert current._resources == {
            "requests": {"cpu": "1", "memory": "2Gi"},
            "limits": {"cpu": "1", "memory": "2Gi"},
        }
        return "fake-job"

    with (
        patch.object(KubernetesSandboxLauncher, "_load_clients", side_effect=AssertionError),
        patch.object(KubernetesSandboxLauncher, "prepare", autospec=True) as prepare,
        patch.object(KubernetesSandboxLauncher, "provision", autospec=True) as provision,
        patch.object(KubernetesSandboxLauncher, "resume", autospec=True) as resume,
    ):
        prepare.side_effect = provision.side_effect = resume.side_effect = check_settings
        selected = launcher.list_profiles(ALICE)[0]
        launcher.select_profile(ALICE, selected)
        prepare.assert_not_called()
        provision.assert_not_called()
        resume.assert_not_called()
        launcher.prepare()
        assert launcher.provision("demo") == "fake-job"
        launcher.select_profile(ALICE, selected)
        launcher.resume("fake-job")


@pytest.mark.parametrize("revocation", ["users", "workspaces", "deleted"])
def test_selection_rechecks_access(
    launcher: FileCatalogSandboxLauncher, catalog_file: Path, revocation: str
) -> None:
    selected = launcher.list_profiles(ALICE)[0]
    catalog = json.loads(catalog_file.read_text())
    if revocation == "deleted":
        catalog["profiles"].pop(0)
    else:
        catalog["profiles"][0][revocation] = []
    catalog_file.write_text(json.dumps(catalog))
    assert selected.id not in {p.id for p in launcher.list_profiles(ALICE)}
    with pytest.raises(SandboxProfileAccessDenied):
        launcher.select_profile(ALICE, selected)
    assert launcher._image_ref is None


@pytest.mark.parametrize(
    "change",
    [
        {"image": "ghcr.io/omnigent-ai/omnigent-host:changed"},
        {"resources": {"requests": {"cpu": "2"}}},
        {"egress": []},
    ],
)
def test_config_changes_invalidate_selection(
    launcher: FileCatalogSandboxLauncher, catalog_file: Path, change: dict[str, object]
) -> None:
    selected = launcher.list_profiles(ALICE)[0]
    catalog = json.loads(catalog_file.read_text())
    catalog["profiles"][0]["config"].update(change)
    catalog_file.write_text(json.dumps(catalog))
    with pytest.raises(SandboxProfileChanged):
        launcher.select_profile(ALICE, selected)
    refreshed = launcher.list_profiles(ALICE)[0]
    assert refreshed.revision != selected.revision
    launcher.select_profile(ALICE, refreshed)
    assert launcher._resolve_namespace() == "omnigent-demo"


def test_reselection_clears_previous_overrides(
    launcher: FileCatalogSandboxLauncher, catalog_file: Path
) -> None:
    catalog = json.loads(catalog_file.read_text())
    catalog["profiles"][0]["config"]["egress"] = []
    catalog["profiles"][1]["config"].pop("resources")
    catalog_file.write_text(json.dumps(catalog))
    small, large = launcher.list_profiles(ALICE)
    launcher.select_profile(ALICE, small)
    assert launcher._profile_egress == []
    launcher.select_profile(ALICE, large)
    assert launcher._profile_egress is None
    assert launcher._resources is None


@pytest.mark.parametrize("catalog_state", ["revoked", "deleted", "unreadable"])
def test_fresh_launcher_deletes_in_provider_namespace_without_profile_access(
    catalog_file: Path, catalog_state: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = {"catalog_file": str(catalog_file), "namespace": "customer-sandboxes"}
    selected_launcher = instantiate("profile_demo", config=config)
    selected_launcher.select_profile(ALICE, selected_launcher.list_profiles(ALICE)[0])
    assert isinstance(selected_launcher, FileCatalogSandboxLauncher)
    assert selected_launcher._resolve_namespace() == "customer-sandboxes"
    catalog = json.loads(catalog_file.read_text())
    if catalog_state == "revoked":
        catalog["profiles"][0]["users"] = []
    if catalog_state == "deleted":
        catalog["profiles"].pop(0)
    catalog_file.write_text(
        "invalid JSON" if catalog_state == "unreadable" else json.dumps(catalog)
    )

    core, batch = Mock(), Mock()
    monkeypatch.setitem(sys.modules, "kubernetes", SimpleNamespace(client=Mock()))
    monkeypatch.setattr("omnigent.onboarding.sandboxes.kubernetes._ensure_sdk", lambda: None)
    monkeypatch.setattr(KubernetesSandboxLauncher, "_load_core", lambda _self: core)
    monkeypatch.setattr(KubernetesSandboxLauncher, "_load_batch", lambda _self: batch)
    monkeypatch.setattr(
        KubernetesSandboxLauncher,
        "_delete_with_retry",
        lambda _self, _kind, _name, delete: delete(),
    )
    cleanup = instantiate("profile_demo", config=config)
    with patch.object(FileCatalogSandboxLauncher, "_read_catalog", side_effect=AssertionError):
        cleanup.terminate("fake-job")
    assert batch.delete_namespaced_job.call_args.args[:2] == ("fake-job", "customer-sandboxes")
    assert core.delete_namespaced_pod.call_args.args[:2] == ("fake-job", "customer-sandboxes")
    assert core.delete_namespaced_secret.call_args.args[1] == "customer-sandboxes"


def test_namespace_cannot_be_overridden_by_a_profile(
    launcher: FileCatalogSandboxLauncher, catalog_file: Path
) -> None:
    catalog = json.loads(catalog_file.read_text())
    catalog["profiles"][0]["config"]["namespace"] = "other-namespace"
    catalog_file.write_text(json.dumps(catalog))
    with pytest.raises(SandboxConfigError):
        launcher.list_profiles(ALICE)


@pytest.mark.parametrize("bad_catalog", ["invalid", "duplicate", "oversized", "invalid_config"])
def test_bad_catalog_fails_closed(
    launcher: FileCatalogSandboxLauncher, catalog_file: Path, bad_catalog: str
) -> None:
    selected = launcher.list_profiles(ALICE)[0]
    catalog = json.loads(catalog_file.read_text())
    if bad_catalog == "duplicate":
        catalog["profiles"].append(catalog["profiles"][0])
    if bad_catalog == "invalid_config":
        catalog["profiles"][0]["config"]["secret_name"] = "private-setting"
    payload = json.dumps(catalog)
    if bad_catalog == "invalid":
        payload = "{not json"
    if bad_catalog == "oversized":
        payload += " " * MAX_CATALOG_BYTES
    catalog_file.write_text(payload)
    with pytest.raises(SandboxConfigError) as error:
        launcher.list_profiles(ALICE)
    assert "private-setting" not in str(error.value)
    with pytest.raises(SandboxConfigError):
        launcher.select_profile(ALICE, selected)


def test_fifo_is_rejected_without_blocking(
    launcher: FileCatalogSandboxLauncher, catalog_file: Path
) -> None:
    catalog_file.unlink()
    os.mkfifo(catalog_file)
    with pytest.raises(SandboxConfigError, match="local file"):
        launcher.list_profiles(ALICE)
