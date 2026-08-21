

def test_build_alembic_config_percent_encoded_uri() -> None:
    """A correctly percent-encoded DATABASE_URL (e.g. p%40ss) must survive
    Alembic's ConfigParser-backed storage: % is interpolation syntax there,
    so it needs configparser-style doubling (#4959)."""
    from omnigent.db.utils import _build_alembic_config

    uri = "postgresql+psycopg://user:p%40ssword@db.example.com:5432/app"
    config = _build_alembic_config(uri)
    # Round-trips exactly: alembic/env.py reads via get_main_option, which
    # reverses the doubling.
    assert config.get_main_option("sqlalchemy.url") == uri
