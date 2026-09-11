#!/usr/bin/env bash
set -euo pipefail

compose_file="deploy/cockroachdb/docker-compose.yml"

# READ COMMITTED is opt-in on 23.2. The setting persists in the named volume,
# and setting it again makes a fresh or restarted local cluster deterministic.
docker compose -f "${compose_file}" exec -T crdb-23-2-28 \
  cockroach sql --insecure \
  --execute="SET CLUSTER SETTING sql.txn.read_committed_isolation.enabled = true"

for target in "23.2.28:26323" "24.3.20:26324" "25.2.10:26325" "25.4.5:26326"; do
  version="${target%%:*}"
  port="${target##*:}"
  echo "Testing CockroachDB ${version} on SQL port ${port}"
  OMNIGENT_TEST_DB_URI="cockroachdb+psycopg://root@localhost:${port}/defaultdb?sslmode=disable" \
    uv run --extra all --extra cockroachdb pytest tests/stores tests/db \
      -m "not databricks" \
      -n 4 \
      --dist=loadfile \
      --timeout=300
done
