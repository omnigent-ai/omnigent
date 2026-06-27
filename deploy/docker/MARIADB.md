# Running Omnigent with MariaDB

This directory contains `docker-compose.mariadb.yaml` for running the omnigent server locally against **MariaDB 11** instead of PostgreSQL.

## Why MariaDB (not vanilla MySQL)?

- **Partial indexes**: MariaDB 10.5.2+ supports `CREATE UNIQUE INDEX ... WHERE ...` on InnoDB, which the omnigent schema requires for the agents table. MariaDB 11 satisfies this.
- **Open-source**: MariaDB is GPL-licensed with no commercial restrictions.
- **Drop-in compatible**: Uses the same `mysql+pymysql://` SQLAlchemy driver as MySQL 8.

## Quickstart

```bash
cd deploy/docker

# 1. Set required env vars (copy from example if you haven't already)
cp .env.example .env
# Edit .env and add:
#   MARIADB_PASSWORD=<choose a password>
#   MARIADB_ROOT_PASSWORD=<choose a root password>

# 2. Start MariaDB + omnigent server (builds the image on first run)
docker compose -f docker-compose.mariadb.yaml up -d --build

# 3. Check logs — first boot prints the admin password
docker compose -f docker-compose.mariadb.yaml logs omnigent

# 4. Open the UI
open http://localhost:8000
```

## Connecting a Runner

After the server is up and you've logged in, the UI will display a command to start a local runner. The runner runs **on your local machine** (not in Docker) so it has access to your filesystem, terminal, and tools. It connects back to the server via WebSocket.

## Connection String

```
mysql+pymysql://omnigent:<password>@localhost:3306/omnigent?charset=utf8mb4
```

- `mysql+pymysql://` — SQLAlchemy dialect using the PyMySQL driver
- `charset=utf8mb4` — required for full Unicode + emoji support in MariaDB

## Known Limitations vs PostgreSQL

| Feature | PostgreSQL | MariaDB |
|---------|-----------|---------|
| Full-text search | ILIKE fallback | LIKE fallback (utf8mb4 case-insensitive) |
| Upsert | `ON CONFLICT DO UPDATE` | `ON DUPLICATE KEY UPDATE` |
| Partial indexes | Native | Requires MariaDB 10.5.2+ (MariaDB 11 ✓) |

**No FTS**: MariaDB does not use the SQLite FTS5 virtual table. Search falls back to a `LIKE %query%` scan on the `data` column. This is slower on large conversation histories but functionally correct. Adding native `FULLTEXT` index support is a future improvement.

## Code Changes Made

These files were modified to add MariaDB dialect support:

| File | Change |
|------|--------|
| `pyproject.toml` | Added `mysql` optional extra with `PyMySQL>=1.1,<2` |
| `deploy/docker/Dockerfile` | Added `PyMySQL` install in server-builder stage |
| `omnigent/stores/permission_store/sqlalchemy_store.py` | `grant()` and `ensure_user()`: added `mysql`/`mariadb` upsert branch |
| `omnigent/stores/conversation_store/sqlalchemy_store.py` | 3 upsert sites + search query + dialect gate expansions |
| `omnigent/db/migrations/versions/o1a2b3c4d5e6_mysql_mariadb_partial_index.py` | Fixes agents.name unique partial index for MariaDB |

## Stopping / Resetting

```bash
# Stop containers (data persists in volumes)
docker compose -f docker-compose.mariadb.yaml down

# Full reset including database volume
docker compose -f docker-compose.mariadb.yaml down -v
```
