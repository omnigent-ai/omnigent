"""The comment level (5) ranks between read and edit despite its number.

Every path that orders levels must use the rank, or a commenter would pass
edit/manage/owner checks. Stored level-5 grants arrive with the schema change
that admits them, along with their store-level coverage.
"""

from __future__ import annotations

import pytest

from omnigent.entities import ResolvedAccess
from omnigent.server.auth import (
    LEVEL_COMMENT,
    LEVEL_EDIT,
    LEVEL_MANAGE,
    LEVEL_OWNER,
    LEVEL_READ,
    level_rank,
    level_satisfies,
)
from omnigent.server.permissions import resolved_allows

_ALLOWED = (LEVEL_READ, LEVEL_COMMENT)
_DENIED = (LEVEL_EDIT, LEVEL_MANAGE, LEVEL_OWNER)


def test_rank_orders_comment_between_read_and_edit() -> None:
    ordered = sorted(
        (LEVEL_OWNER, LEVEL_EDIT, LEVEL_COMMENT, LEVEL_MANAGE, LEVEL_READ), key=level_rank
    )
    assert ordered == [LEVEL_READ, LEVEL_COMMENT, LEVEL_EDIT, LEVEL_MANAGE, LEVEL_OWNER]


@pytest.mark.parametrize("granted", [None, 0, 6, 99])
def test_missing_or_unknown_level_satisfies_nothing(granted: int | None) -> None:
    assert not level_satisfies(granted, LEVEL_READ)


@pytest.mark.parametrize("required", [0, 6, 99])
def test_unknown_required_level_fails_closed(required: int) -> None:
    assert not level_satisfies(LEVEL_OWNER, required)


@pytest.mark.parametrize("required", _ALLOWED)
def test_comment_grant_satisfies_read_and_comment(required: int) -> None:
    assert level_satisfies(LEVEL_COMMENT, required)


@pytest.mark.parametrize("required", _DENIED)
def test_comment_grant_does_not_satisfy_edit_or_above(required: int) -> None:
    assert not level_satisfies(LEVEL_COMMENT, required)


@pytest.mark.parametrize("granted", (LEVEL_EDIT, LEVEL_MANAGE, LEVEL_OWNER))
def test_editors_and_above_satisfy_comment(granted: int) -> None:
    assert level_satisfies(granted, LEVEL_COMMENT)


def test_read_grant_does_not_satisfy_comment() -> None:
    assert not level_satisfies(LEVEL_READ, LEVEL_COMMENT)


@pytest.mark.parametrize(
    "required,expected", [(lvl, True) for lvl in _ALLOWED] + [(lvl, False) for lvl in _DENIED]
)
def test_resolved_allows_ranks_comment(required: int, expected: bool) -> None:
    access = ResolvedAccess(
        is_admin=False, user_grant_level=LEVEL_COMMENT, public_grant_level=None
    )
    assert resolved_allows(access, required) is expected
