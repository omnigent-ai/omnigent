"""
One-time initialization of the D1 schema for an Omnigent deploy.

Why this exists: Omnigent normally creates its schema by running Alembic
migrations on boot. One migration uses ``batch_alter_table`` +
``drop_constraint``, which needs the dialect to reflect a *named* unique
constraint — something the third-party D1 dialect can't fully do (it recovers
constraint names from ``sqlite_master``, which the dialect doesn't parse). So
on a brand-new D1 the on-boot migration fails partway.

Workaround (verified): build the head schema directly from the ORM metadata
(``create_all``, which only needs DDL — no reflection) and stamp
``alembic_version`` to head so the server treats the DB as up to date.

Run ONCE against a fresh D1, using the deploy image so omnigent + the dialect +
the shim are all present:

    docker build -t omnigent-cf .
    docker run --rm -i \
      -e DATABASE_URL="cloudflare_d1://<ACCOUNT_ID>:<D1_API_TOKEN>@<DATABASE_ID>" \
      --entrypoint /opt/venv/bin/python omnigent-cf - < bootstrap-d1.py

(The upstream changes discussed in the README would make this step unnecessary —
the normal on-boot migration would just work.)
"""

import os

from sqlalchemy import inspect, text

import omnigent.db.utils as db_utils
from omnigent.db import Base

url = os.environ["DATABASE_URL"]
engine = db_utils._create_engine(url)
head = db_utils._get_head_db_revision(url)

Base.metadata.create_all(bind=engine)  # checkfirst=True; pure DDL, no reflection
with engine.begin() as conn:
    conn.execute(
        text(
            "CREATE TABLE IF NOT EXISTS alembic_version "
            "(version_num VARCHAR(32) NOT NULL PRIMARY KEY)"
        )
    )
    conn.execute(text("DELETE FROM alembic_version"))
    conn.execute(text("INSERT INTO alembic_version (version_num) VALUES (:v)"), {"v": head})

tables = sorted(t for t in inspect(engine).get_table_names() if not t.startswith("_cf_"))
print(f"D1 schema bootstrapped to {head}: {len(tables)} tables {tables}")
