"""The limits module is the one source of truth, and it agrees with the schema."""

from __future__ import annotations

from sqlalchemy import String, Table

from omnigent.db.db_models import BoundedString, SqlInstalledSkill, SqlSkillSource
from omnigent.skills import limits

STORED_VALUE_LIMIT_BYTES = 16_384


def _string_lengths(table: Table) -> dict[str, int]:
    """Every text column's declared limit, and nothing declared as a plain string.

    A plain ``String`` would carry the same limit but enforce it nowhere on
    SQLite, so the type itself is part of what this pins.
    """
    plain = [c.name for c in table.c if type(c.type) is String]
    assert plain == [], plain
    return {c.name: c.type.impl.length for c in table.c if isinstance(c.type, BoundedString)}


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
