
def test_schema_ahead_of_client_reports_upgrade_client() -> None:
    """A database written by a NEWER omnigent (revision absent from this
    build) must not be misreported as stale with retry advice that cannot
    run — Alembic cannot resolve the unknown revision (#4963)."""
    from omnigent.db import utils as db_utils

    # Drive _initialize_or_verify_schema with stubs: current revision is
    # unknown to the script map (written by a newer build).
    calls = {"migrated": False}

    def _fake_head(uri):
        return "f7a8b9c0d1e2"

    def _fake_current(eng):
        return "za2b3c4d5e6f"

    def _fake_unknown(rev, uri):
        return True

    def _fail_migrate(*a, **k):
        calls["migrated"] = True
        raise AssertionError("upgrade must not be attempted for an unknown revision")

    orig_head, orig_cur = db_utils._get_head_db_revision, db_utils._get_current_db_revision
    orig_unknown = getattr(db_utils, "_revision_unknown_to_this_client", None)
    orig_migrate = db_utils._run_migrations
    db_utils._get_head_db_revision = _fake_head
    db_utils._get_current_db_revision = _fake_current
    if orig_unknown is not None:
        db_utils._revision_unknown_to_this_client = _fake_unknown
    db_utils._run_migrations = _fail_migrate
    try:
        import pytest

        with pytest.raises(RuntimeError) as excinfo:
            db_utils._initialize_or_verify_schema(None, "sqlite:///:memory:")
        msg = str(excinfo.value)
        assert "newer omnigent" in msg
        assert "Upgrade omnigent" in msg
        assert calls["migrated"] is False, "no migration may be attempted"
    finally:
        db_utils._get_head_db_revision = orig_head
        db_utils._get_current_db_revision = orig_cur
        if orig_unknown is not None:
            db_utils._revision_unknown_to_this_client = orig_unknown
        db_utils._run_migrations = orig_migrate
