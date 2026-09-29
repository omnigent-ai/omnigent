"""Tests for env-driven credential collection in ``omnigent.onboarding.provider_selection``.

``test_provider_selection.py`` covers single-key providers. This module covers
providers with multi-field auth modes (default mode selection, optional fields,
missing-variable reporting).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from click import ClickException

from omnigent.onboarding import provider_selection
from omnigent.onboarding.provider_selection import (
    _collect_env_credentials,
    resolve_provider_from_model,
)
from omnigent.onboarding.providers import AuthField

_ENV_NAMES = ("ZZ_KEY_ID", "ZZ_SECRET", "ZZ_REGION")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(f"OMNIGENT_{name}", raising=False)


def _fields() -> list[AuthField]:
    return [
        AuthField("zz_key_id", "Key id", secret=False, required=True),
        AuthField("zz_secret", "Secret", secret=True, required=True),
        AuthField("zz_region", "Region", secret=False, required=False),
    ]


def _stub_provider_config(
    monkeypatch: pytest.MonkeyPatch, *, default_mode: str, modes: dict[str, list[AuthField]]
) -> None:
    config = SimpleNamespace(
        default_mode=default_mode,
        auth_modes=[
            SimpleNamespace(mode_id=mode, fields=fields) for mode, fields in modes.items()
        ],
    )
    monkeypatch.setattr(provider_selection, "get_provider_config", lambda provider: config)


def test_collect_reads_required_fields_and_skips_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZZ_KEY_ID", "id-1")
    monkeypatch.setenv("OMNIGENT_ZZ_SECRET", "s3cret")
    monkeypatch.setenv("ZZ_REGION", "eu-1")

    creds = _collect_env_credentials("zz", _fields())

    # The OMNIGENT_-prefixed spelling is accepted; the optional field is not read.
    assert creds == {"zz_key_id": "id-1", "zz_secret": "s3cret"}


def test_collect_lists_every_missing_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ClickException) as excinfo:
        _collect_env_credentials("zz", _fields())

    message = excinfo.value.message
    assert "provider 'zz'" in message
    assert "ZZ_KEY_ID, ZZ_SECRET" in message
    assert "ZZ_REGION" not in message


def test_collect_treats_empty_values_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ZZ_KEY_ID", "id-1")
    monkeypatch.setenv("ZZ_SECRET", "")

    with pytest.raises(ClickException, match="ZZ_SECRET"):
        _collect_env_credentials("zz", _fields())


def test_resolve_uses_default_auth_mode_for_multi_field_providers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_provider_config(
        monkeypatch,
        default_mode="keys",
        modes={
            "iam": [AuthField("zz_role", "Role", secret=False, required=True)],
            "keys": _fields(),
        },
    )
    monkeypatch.setenv("ZZ_KEY_ID", "id-1")
    monkeypatch.setenv("ZZ_SECRET", "s3cret")

    selection = resolve_provider_from_model("zz/some-model")

    assert selection.provider == "zz"
    assert selection.model == "zz/some-model"
    assert selection.credentials == {"zz_key_id": "id-1", "zz_secret": "s3cret"}


def test_resolve_falls_back_to_first_mode_when_default_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_provider_config(
        monkeypatch,
        default_mode="does-not-exist",
        modes={"only": [AuthField("zz_key_id", "Key id", secret=False, required=True)]},
    )
    monkeypatch.setenv("ZZ_KEY_ID", "id-1")

    assert resolve_provider_from_model("zz/m").credentials == {"zz_key_id": "id-1"}


def test_resolve_reports_missing_variables_for_default_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_provider_config(monkeypatch, default_mode="keys", modes={"keys": _fields()})

    with pytest.raises(ClickException, match="ZZ_KEY_ID, ZZ_SECRET"):
        resolve_provider_from_model("zz/m")
