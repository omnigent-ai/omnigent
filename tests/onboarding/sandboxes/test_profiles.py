"""Public provider contracts are strict, immutable, and optional for existing launchers."""

from dataclasses import FrozenInstanceError

import pytest
from pydantic import ValidationError

from omnigent.onboarding.sandboxes.base import SandboxCapabilityError
from omnigent.onboarding.sandboxes.kubernetes import KubernetesSandboxLauncher
from omnigent.onboarding.sandboxes.profiles import (
    ProviderSandboxProfile,
    SandboxProfileAccessDenied,
    SandboxProfileChanged,
    SandboxProfileContext,
)
from omnigent.onboarding.sandboxes.types import SandboxError

ALICE = SandboxProfileContext(user_id="alice", workspace_id=1)


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
