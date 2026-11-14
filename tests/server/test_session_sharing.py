"""Unit tests for the grant policy in :mod:`omnigent.server.session_sharing`.

The share route's HTTP behaviour is covered by
``tests/server/routes/test_sessions_sharing_mode.py``. These tests pin each check
at the helper layer, using plain ``app.state`` stand-ins.
"""

from __future__ import annotations

import contextlib
import re
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server import session_sharing
from omnigent.server.auth import LEVEL_EDIT, LEVEL_MANAGE, LEVEL_READ, SharingMode
from omnigent.server.session_sharing import (
    require_public_grant_allowed,
    require_sharing_mode_allows_grant,
)
from omnigent.server.sharing_settings import PublicSharingMaxLevel

ALL_LEVELS = (LEVEL_READ, LEVEL_EDIT, LEVEL_MANAGE)
READ_ONLY_MODES = (SharingMode.READ_ONLY, SharingMode.RESTRICTED_READ_ONLY)
HOME_OR_ROOT_WORKSPACES = ("/", "/root", "/home/alice")
ORDINARY_WORKSPACE = "/home/alice/project"


def _state(
    mode: SharingMode = SharingMode.ON,
    *,
    public: bool = True,
    ceiling: PublicSharingMaxLevel = PublicSharingMaxLevel.READ,
) -> Any:
    """An ``app.state`` stand-in carrying the live policy callables."""
    return SimpleNamespace(
        sharing_mode=lambda: mode,
        public_sharing=lambda: public,
        public_sharing_max_level=lambda: ceiling,
    )


def _check_mode(state: Any, level: int, workspace: str | None = ORDINARY_WORKSPACE) -> None:
    require_sharing_mode_allows_grant(state, level=level, load_workspace=lambda: workspace)


@pytest.mark.parametrize("level", ALL_LEVELS)
def test_off_refuses_every_level(level: int) -> None:
    with pytest.raises(OmnigentError, match="Sharing has been disabled") as refused:
        _check_mode(_state(SharingMode.OFF), level)
    assert refused.value.code == ErrorCode.FORBIDDEN


@pytest.mark.parametrize("level", ALL_LEVELS)
def test_on_allows_every_level_even_in_a_home_workspace(level: int) -> None:
    _check_mode(_state(SharingMode.ON), level, workspace="/")


@pytest.mark.parametrize("mode", READ_ONLY_MODES)
def test_read_only_modes_allow_read(mode: SharingMode) -> None:
    _check_mode(_state(mode), LEVEL_READ)


@pytest.mark.parametrize("mode", READ_ONLY_MODES)
@pytest.mark.parametrize("level", [LEVEL_EDIT, LEVEL_MANAGE])
def test_read_only_modes_cap_grants_at_read(mode: SharingMode, level: int) -> None:
    with pytest.raises(OmnigentError, match="limited to read-only access") as refused:
        _check_mode(_state(mode), level)
    assert refused.value.code == ErrorCode.FORBIDDEN


@pytest.mark.parametrize("workspace", HOME_OR_ROOT_WORKSPACES)
def test_restricted_refuses_home_or_root_workspace_even_for_read(workspace: str) -> None:
    with pytest.raises(OmnigentError, match="cannot be shared") as refused:
        _check_mode(_state(SharingMode.RESTRICTED_READ_ONLY), LEVEL_READ, workspace)
    assert refused.value.code == ErrorCode.FORBIDDEN


def test_restricted_workspace_check_runs_before_read_only_cap() -> None:
    with pytest.raises(OmnigentError, match="cannot be shared"):
        _check_mode(_state(SharingMode.RESTRICTED_READ_ONLY), LEVEL_EDIT, "/home/alice")


@pytest.mark.parametrize("workspace", [ORDINARY_WORKSPACE, None])
def test_restricted_allows_read_in_ordinary_or_unknown_workspace(workspace: str | None) -> None:
    # An unknown working directory is not treated as home or root, matching the share route.
    state = _state(SharingMode.RESTRICTED_READ_ONLY)
    _check_mode(state, LEVEL_READ, workspace)
    with pytest.raises(OmnigentError, match="limited to read-only access"):
        _check_mode(state, LEVEL_EDIT, workspace)


@pytest.mark.parametrize(
    "mode,expected_loads",
    [
        (SharingMode.ON, 0),
        (SharingMode.READ_ONLY, 0),
        (SharingMode.OFF, 0),
        (SharingMode.RESTRICTED_READ_ONLY, 1),
    ],
)
def test_workspace_is_loaded_only_under_restricted_mode(
    mode: SharingMode, expected_loads: int
) -> None:
    loads: list[bool] = []

    def load_workspace() -> str:
        loads.append(True)
        return ORDINARY_WORKSPACE

    with contextlib.suppress(OmnigentError):
        require_sharing_mode_allows_grant(
            _state(mode), level=LEVEL_READ, load_workspace=load_workspace
        )
    assert len(loads) == expected_loads


def test_read_only_levels_constant_is_the_overlay_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    assert frozenset({LEVEL_READ}) == session_sharing.READ_ONLY_MODE_LEVELS
    # A downstream deployment widens read-only modes by patching this constant.
    monkeypatch.setattr(
        session_sharing, "READ_ONLY_MODE_LEVELS", frozenset({LEVEL_READ, LEVEL_EDIT})
    )
    _check_mode(_state(SharingMode.READ_ONLY), LEVEL_EDIT)
    with pytest.raises(OmnigentError, match="limited to read-only access"):
        _check_mode(_state(SharingMode.READ_ONLY), LEVEL_MANAGE)


@pytest.mark.parametrize("level", ALL_LEVELS)
def test_public_kill_switch_refuses_every_level(level: int) -> None:
    state = _state(public=False, ceiling=PublicSharingMaxLevel.EDIT)
    with pytest.raises(OmnigentError, match="Public access has been disabled") as refused:
        require_public_grant_allowed(state, level=level)
    assert refused.value.code == ErrorCode.FORBIDDEN


def test_public_kill_switch_is_checked_before_the_ceiling() -> None:
    # MANAGE is also above the READ ceiling, but the kill switch's message wins.
    with pytest.raises(OmnigentError, match="Public access has been disabled") as refused:
        require_public_grant_allowed(_state(public=False), level=LEVEL_MANAGE)
    assert refused.value.code == ErrorCode.FORBIDDEN


def test_public_read_is_allowed_when_enabled() -> None:
    require_public_grant_allowed(_state(), level=LEVEL_READ)


@pytest.mark.parametrize(
    "ceiling,level",
    [
        (PublicSharingMaxLevel.READ, LEVEL_EDIT),
        (PublicSharingMaxLevel.READ, LEVEL_MANAGE),
        (PublicSharingMaxLevel.EDIT, LEVEL_MANAGE),
    ],
)
def test_public_ceiling_refuses_levels_above_it(
    ceiling: PublicSharingMaxLevel, level: int
) -> None:
    expected = f"Public access is limited to {ceiling.value} (level {ceiling.level})"
    with pytest.raises(OmnigentError, match=re.escape(expected)) as refused:
        require_public_grant_allowed(_state(ceiling=ceiling), level=level)
    assert refused.value.code == ErrorCode.INVALID_INPUT


@pytest.mark.parametrize("level", [LEVEL_READ, LEVEL_EDIT])
def test_public_edit_ceiling_allows_up_to_edit(level: int) -> None:
    require_public_grant_allowed(_state(ceiling=PublicSharingMaxLevel.EDIT), level=level)


def test_hand_built_state_defaults_to_on_and_public_read() -> None:
    state = SimpleNamespace()
    _check_mode(state, LEVEL_EDIT)
    require_public_grant_allowed(state, level=LEVEL_READ)
    with pytest.raises(OmnigentError, match=re.escape("limited to read (level 1)")) as refused:
        require_public_grant_allowed(state, level=LEVEL_EDIT)
    assert refused.value.code == ErrorCode.INVALID_INPUT
