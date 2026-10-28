"""The limits module is the one source of truth, and it agrees with the schema."""

from __future__ import annotations

from sqlalchemy import String, Table

from omnigent.db.db_models import SqlInstalledSkill, SqlSkillSource
from omnigent.skills import limits

STORED_VALUE_LIMIT_BYTES = 16_384


def _string_lengths(table: Table) -> dict[str, int]:
    return {c.name: c.type.length for c in table.c if isinstance(c.type, String)}


def test_column_bounds_match_the_schema() -> None:
    assert _string_lengths(SqlInstalledSkill.__table__) == {
        "name": limits.MAX_NAME_CHARS,
        "display_name": limits.MAX_DISPLAY_NAME_CHARS,
        "description": limits.MAX_DESCRIPTION_CHARS,
        "source_url": limits.MAX_URL_CHARS,
        "ref": limits.MAX_REF_CHARS,
        "commit": limits.MAX_COMMIT_CHARS,
        "path": limits.MAX_PATH_CHARS,
        "principal": limits.MAX_PRINCIPAL_CHARS,
        "owner_user_id": limits.MAX_USER_ID_CHARS,
        "bundle_location": limits.MAX_LOCATION_CHARS,
        "installed_by": limits.MAX_USER_ID_CHARS,
    }
    assert _string_lengths(SqlSkillSource.__table__) == {
        "url": limits.MAX_URL_CHARS,
        "ref": limits.MAX_REF_CHARS,
        "name": limits.MAX_DISPLAY_NAME_CHARS,
        "created_by": limits.MAX_USER_ID_CHARS,
    }
    # The widest column stays under the stored-value limit at four bytes a character.
    widest = max(
        *_string_lengths(SqlInstalledSkill.__table__).values(),
        *_string_lengths(SqlSkillSource.__table__).values(),
    )
    assert widest * 4 < STORED_VALUE_LIMIT_BYTES


def test_the_delivery_budget_is_two_principals_worth() -> None:
    assert limits.MAX_DELIVERY_BYTES == 2 * limits.MAX_DELIVERY_BYTES_PER_SCOPE
    assert limits.MAX_INSTALLED_PER_OWNER <= limits.MAX_PAGE
