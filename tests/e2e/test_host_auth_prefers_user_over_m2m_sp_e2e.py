"""Host credential resolution must prefer the user over an M2M service principal.

When ``~/.databrickscfg`` contains both a ``[DEFAULT]`` U2M user profile
(``auth_type = databricks-cli``) and a second M2M service-principal profile
(``client_id`` / ``client_secret``), both pointing at the **same** workspace
host, the host credential resolver must honour the configured profile preference
rather than silently picking the M2M SP because it is first in file order.

Two sub-symptoms are guarded:

1. **M2M SP selected over user**: ``_resolve_databricks_auth_for_host`` takes
   the first host-matching profile that successfully authenticates.  An M2M SP
   always authenticates (client_id + client_secret are static), so a plain
   file-order walk lets it win even when ``[DEFAULT]`` (U2M) is also present.
   The host then registers as the SP, which is a different identity to the
   SSO-signed-in app user, so the user sees no live host, the "Connect New
   Host" dropdown is empty, and "Run on this machine" silently no-ops.

2. **DATABRICKS_CONFIG_PROFILE ignored in host= path**: When
   ``omnigent login <apps-url>`` stores a Databricks Apps workspace pointer,
   ``_make_auth_token_factory`` calls ``_resolve_databricks_auth(host=…)``
   instead of ``_resolve_databricks_auth(profile=…)``.  The ``host=`` code
   path must consult ``DATABRICKS_CONFIG_PROFILE`` when the named profile
   matches the requested host, so exporting
   ``DATABRICKS_CONFIG_PROFILE=DEFAULT`` forces the user profile.

Both sub-symptoms are exercised here without spawning a real server or host
daemon — the credential-resolution layer is the exact failure point.

Run with::

    pytest tests/e2e/test_host_auth_prefers_user_over_m2m_sp_e2e.py -v
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Helpers / stubs
# ---------------------------------------------------------------------------


class _StubSdkConfig:
    """Minimal stand-in for ``databricks.sdk.config.Config``.

    Real ``Config`` probes host metadata at construction time, which fails
    offline for placeholder hosts.  The production seam ``_sdk_config``
    exists precisely so tests can replace it.

    :param host: Workspace host the config resolves to.
    :param token: Bearer token returned by :meth:`authenticate`.
    :param raises: When set to an exception class, :meth:`authenticate`
        raises it instead of succeeding.  Simulates an SP whose
        ``client_secret`` is wrong / expired.
    """

    def __init__(
        self,
        host: str,
        token: str,
        raises: type[Exception] | None = None,
    ) -> None:
        self.host = host
        self._token = token
        self._raises = raises

    def authenticate(self) -> dict[str, str]:
        """Return Authorization headers, or raise if configured to fail."""
        if self._raises is not None:
            raise self._raises("stub authentication failure")
        return {"Authorization": f"Bearer {self._token}"}


def _write_two_profile_cfg(
    tmp_path: Path,
    *,
    workspace_host: str = "https://myworkspace.cloud.databricks.com",
    sp_profile: str = "sp-profile",
    user_profile: str = "DEFAULT",
) -> Path:
    """Write a ``~/.databrickscfg`` with one M2M SP and one U2M user profile.

    Both profiles point at *workspace_host* — this is the exact layout the
    bug report describes.

    The M2M SP profile (``[sp-profile]``) appears **before** the DEFAULT
    (U2M) profile in file order, which is the scenario where the bug bites:
    ``_databrickscfg_profiles_for_host`` returns them in file order, and
    ``_resolve_databricks_auth_for_host`` takes the first one that
    authenticates — the M2M SP always authenticates.

    :param tmp_path: Temporary directory for the config file.
    :param workspace_host: The shared workspace URL.
    :param sp_profile: Section name for the M2M service principal.
    :param user_profile: Section name for the U2M user (``DEFAULT`` in the
        real-world scenario).
    :returns: The path to the written config file.
    """
    cfg_path = tmp_path / "databrickscfg"
    # sp-profile (M2M) comes first in file order — the bug manifests here.
    lines = [
        f"[{sp_profile}]",
        f"host = {workspace_host}",
        "client_id = svc-client-id-fake",
        "client_secret = svc-client-secret-fake",
        "",
    ]
    if user_profile == "DEFAULT":
        # ConfigParser treats [DEFAULT] as the implicit defaults section, so
        # we append it last.  This matches what ``databricks auth login``
        # produces.
        lines += [
            "[DEFAULT]",
            f"host = {workspace_host}",
            "auth_type = databricks-cli",
            "",
        ]
    else:
        lines += [
            f"[{user_profile}]",
            f"host = {workspace_host}",
            "auth_type = databricks-cli",
            "",
        ]
    cfg_path.write_text("\n".join(lines))
    return cfg_path


# ---------------------------------------------------------------------------
# Sub-symptom 1: M2M SP wins over U2M user in file-order walk
# ---------------------------------------------------------------------------


def test_profiles_for_host_includes_m2m_sp_and_default_for_same_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both the M2M SP profile and DEFAULT appear for the shared workspace.

    This is the precondition for the bug: when both profiles match the
    host, the resolver sees them and (absent a fix) picks the first one —
    which is the M2M SP.
    """
    from omnigent.inner.databricks_executor import _databrickscfg_profiles_for_host

    cfg_path = _write_two_profile_cfg(tmp_path)
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg_path))

    matches = _databrickscfg_profiles_for_host("https://myworkspace.cloud.databricks.com")

    # Both profiles match the shared workspace host.
    assert "sp-profile" in matches, f"expected sp-profile in matches, got {matches}"
    assert "DEFAULT" in matches, f"expected DEFAULT in matches, got {matches}"


def test_resolve_auth_for_host_must_not_select_m2m_sp_over_user_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Host auth resolution must NOT pick the M2M SP ahead of the U2M user.

    Both ``[sp-profile]`` (M2M, client_id/secret) and ``[DEFAULT]``
    (U2M, auth_type=databricks-cli) point at the same workspace host.
    The M2M SP profile appears first in file order and its stub always
    authenticates successfully.  The resolver must prefer the U2M user
    profile (either by honouring ``DATABRICKS_CONFIG_PROFILE``, by
    deprioritising M2M SP profiles, or by preferring the explicit/DEFAULT
    profile) rather than silently registering the host as the SP.

    On a broken resolver this test fails: ``constructed[0]`` is
    ``{"profile": "sp-profile"}`` (M2M SP selected) rather than
    ``{"profile": "DEFAULT"}`` (U2M user selected).
    """
    from omnigent.inner import databricks_executor

    cfg_path = _write_two_profile_cfg(tmp_path)
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg_path))
    monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "DEFAULT")

    constructed: list[dict[str, Any]] = []

    def _fake_sdk_config(**kwargs: Any) -> _StubSdkConfig:
        constructed.append(dict(kwargs))
        profile = kwargs.get("profile", "")
        if profile == "sp-profile":
            # M2M SP — authenticates fine with client credentials.
            return _StubSdkConfig(
                host="https://myworkspace.cloud.databricks.com",
                token="sp-token-fake",
            )
        # DEFAULT / user profile — also authenticates (U2M OAuth token).
        return _StubSdkConfig(
            host="https://myworkspace.cloud.databricks.com",
            token="user-token-fake",
        )

    monkeypatch.setattr(databricks_executor, "_sdk_config", _fake_sdk_config)

    auth, host = databricks_executor._resolve_databricks_auth(
        host="https://myworkspace.cloud.databricks.com"
    )

    # The resolved token must be the USER's token, not the SP's.
    resolved_token = auth.current_token()
    assert resolved_token == "user-token-fake", (
        f"Host registered as M2M service principal (token={resolved_token!r}) "
        f"instead of the U2M user (expected 'user-token-fake'). "
        f"Profile construction order: {constructed}. "
        "An M2M SP in ~/.databrickscfg must not shadow the [DEFAULT] U2M user "
        "profile just because it comes first in file order."
    )
    assert host == "https://myworkspace.cloud.databricks.com"


def test_resolve_auth_for_host_config_profile_env_is_honoured_in_host_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """DATABRICKS_CONFIG_PROFILE must be consulted when resolving by host.

    When ``omnigent login <apps-url>`` stores a Databricks Apps workspace
    pointer, ``_make_auth_token_factory`` calls
    ``_resolve_databricks_auth(host=…)`` instead of the profile path.
    The user exports ``DATABRICKS_CONFIG_PROFILE=DEFAULT`` to force their
    U2M profile; the ``host=`` code path must honour it.

    On a broken resolver this test fails: the first-in-file-order M2M SP
    profile is selected instead of DEFAULT.
    """
    from omnigent.inner import databricks_executor

    cfg_path = _write_two_profile_cfg(tmp_path)
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg_path))
    # The user sets DATABRICKS_CONFIG_PROFILE=DEFAULT explicitly to force
    # their own (U2M) profile over the M2M SP.
    monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "DEFAULT")

    selected_profiles: list[str] = []

    def _fake_sdk_config(**kwargs: Any) -> _StubSdkConfig:
        profile = kwargs.get("profile", "")
        selected_profiles.append(profile or "<no-profile>")
        return _StubSdkConfig(
            host="https://myworkspace.cloud.databricks.com",
            token=f"token-for-{profile or 'ambient'}",
        )

    monkeypatch.setattr(databricks_executor, "_sdk_config", _fake_sdk_config)

    auth, _host = databricks_executor._resolve_databricks_auth(
        host="https://myworkspace.cloud.databricks.com"
    )

    # The first profile tried must be DEFAULT (from DATABRICKS_CONFIG_PROFILE),
    # NOT sp-profile (from file-order walk).
    assert selected_profiles, "No SDK Config construction was attempted"
    first_selected = selected_profiles[0]
    assert first_selected == "DEFAULT", (
        f"DATABRICKS_CONFIG_PROFILE=DEFAULT was ignored in the host= path. "
        f"First profile tried: {first_selected!r}, all tried: {selected_profiles}. "
        "When the env-named profile matches the requested host it must be "
        "tried before any file-order walk."
    )
    resolved_token = auth.current_token()
    assert resolved_token == "token-for-DEFAULT", (
        f"Expected token for DEFAULT profile, got {resolved_token!r}"
    )


# ---------------------------------------------------------------------------
# Sub-symptom 2: M2M SP profiles should be deprioritised / skipped in the
# file-order walk (complementary guard for when DATABRICKS_CONFIG_PROFILE
# is NOT set).
# ---------------------------------------------------------------------------


def test_resolve_auth_for_host_skips_m2m_sp_profiles_without_explicit_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without DATABRICKS_CONFIG_PROFILE, M2M SP profiles must not be selected.

    When no explicit profile env var is set the resolver walks
    ``~/.databrickscfg`` looking for a profile whose host matches.  If that
    walk picks an M2M SP profile (client_id + client_secret) and
    authenticates as a service principal, the user's desktop app (signed in
    via SSO as a person) will see no live host.

    The resolver must either skip profiles that look like M2M SPs (have
    ``client_id``/``client_secret`` but no ``auth_type = databricks-cli``)
    or prefer the U2M ``[DEFAULT]`` profile when one is present.

    On a broken resolver this test fails: the first-in-file M2M SP is
    selected.
    """
    from omnigent.inner import databricks_executor

    cfg_path = _write_two_profile_cfg(tmp_path)
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg_path))
    # No DATABRICKS_CONFIG_PROFILE — the pure file-order-walk scenario.
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)

    selected_profiles: list[str] = []

    def _fake_sdk_config(**kwargs: Any) -> _StubSdkConfig:
        profile = kwargs.get("profile", "")
        selected_profiles.append(profile or "<no-profile>")
        return _StubSdkConfig(
            host="https://myworkspace.cloud.databricks.com",
            token=f"token-for-{profile or 'ambient'}",
        )

    monkeypatch.setattr(databricks_executor, "_sdk_config", _fake_sdk_config)

    auth, _host = databricks_executor._resolve_databricks_auth(
        host="https://myworkspace.cloud.databricks.com"
    )

    resolved_token = auth.current_token()
    assert resolved_token != "token-for-sp-profile", (
        f"M2M service-principal profile 'sp-profile' was selected (token={resolved_token!r}). "
        f"Profile construction order: {selected_profiles}. "
        "When both an M2M SP and a U2M user profile exist for the same workspace "
        "host, the host must register under the user (U2M), not the service principal. "
        "The M2M SP profile is first in the ~/.databrickscfg file-order list and always "
        "authenticates successfully, so a simple file-order walk selects it."
    )


# ---------------------------------------------------------------------------
# Selection-order guard: user (U2M) profiles outrank M2M SP profiles
# ---------------------------------------------------------------------------


def test_host_profile_selection_order_puts_user_profiles_before_sp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The selection order tries the U2M user profile before the M2M SP.

    ``_databrickscfg_profiles_for_host`` returns matches in file order
    with DEFAULT appended last, so an SP section written first would win a
    naive first-successful-auth walk.  The selection-order layer must
    reorder: user (U2M) profiles first, M2M SP profiles last.
    """
    from omnigent.inner.databricks_executor import (
        _databrickscfg_host_matches_and_sp_sections,
        _order_profiles_by_identity_preference,
    )

    cfg_path = _write_two_profile_cfg(tmp_path)
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg_path))
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)

    ordered = _order_profiles_by_identity_preference(
        *_databrickscfg_host_matches_and_sp_sections("https://myworkspace.cloud.databricks.com")
    )

    sp_idx = ordered.index("sp-profile") if "sp-profile" in ordered else -1
    default_idx = ordered.index("DEFAULT") if "DEFAULT" in ordered else -1
    assert sp_idx != -1 and default_idx != -1, (
        f"Expected both 'sp-profile' and 'DEFAULT' in the order, got {ordered}"
    )
    assert default_idx < sp_idx, (
        f"User profile DEFAULT (idx={default_idx}) must be tried before the M2M "
        f"SP profile (idx={sp_idx}); got {ordered}. An SP that authenticates "
        "first registers the host under the wrong identity."
    )
