# Docker policy store — behavior + verification

Reference for the `deploy/docker` compose stack. This is the canonical
Docker policy-store verification doc; keep `README.md` operator-facing
and `config.yaml.example` focused on config keys.

For the policy model itself (phases, ordering, actions), see
[`docs/POLICIES.md`](../../docs/POLICIES.md).

## Behavior to preserve

- `DATABASE_URL` selects the policy store database. There is no
  policy-store-specific environment variable.
- `main()` runs Alembic `upgrade head` before `build_app()` constructs
  `SqlAlchemyPolicyStore`.
- `build_app()` passes the same policy store to runtime initialization
  and `create_app()`.
- `config.yaml` can register `policy_modules` so API-created Python
  policies can reference custom handlers through the registry allowlist.
- `config.yaml` can declare a `policies:` block; the entrypoint parses it
  through `parse_default_policies`, matching the CLI `omnigent server`
  path. These merge with the API-backed defaults below.
- Persisted defaults created through `/v1/policies` join the admin
  policy layer for every session; session-scoped policies from
  `/v1/sessions/{session_id}/policies` run before agent and admin
  policies.

## Completion criteria for a Docker policy-store change

- `/health` returns success after `docker compose up -d --build`.
- `/openapi.json` includes default and session policy routes.
- A default policy created through `/v1/policies` can be listed,
  enforced through `/v1/sessions/{session_id}/policies/evaluate`, and
  still exists after `docker compose restart omnigent`.
- A session-scoped policy created through
  `/v1/sessions/{session_id}/policies` can be listed for that session.
- The `policies` table exists in the compose Postgres database.

## Local smoke test

```bash
cd deploy/docker
./bootstrap.sh
OMNIGENT_AUTH_ENABLED=0 docker compose down -v
OMNIGENT_AUTH_ENABLED=0 docker compose up -d --build
curl -fsS http://localhost:8000/health
curl -fsS http://localhost:8000/openapi.json \
  | jq -r '.paths | keys[] | select(test("polic"))'

docker compose exec -T postgres sh -lc \
'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "
INSERT INTO users (id, is_admin) VALUES ('\''local'\'', true)
ON CONFLICT (id) DO UPDATE SET is_admin = true;
"'

BASE=http://localhost:8000
DEFAULT_HANDLER=omnigent.policies.builtins.safety.max_tool_calls_per_session
SESSION_HANDLER=omnigent.policies.builtins.safety.ask_on_os_tools

curl -fsS -X POST "$BASE/v1/policies" \
  -H 'content-type: application/json' \
  -d "{\"name\":\"local_default_limit\",\"type\":\"python\",\"handler\":\"$DEFAULT_HANDLER\",\"factory_params\":{\"limit\":0}}"
curl -fsS "$BASE/v1/policies" | jq '.data'

AGENT_ID=$(curl -fsS "$BASE/v1/agents" | jq -r '.data[0].id // empty')
test -n "$AGENT_ID"
SESSION_ID=$(curl -fsS -X POST "$BASE/v1/sessions" \
  -H 'content-type: application/json' \
  -d "{\"agent_id\":\"$AGENT_ID\",\"title\":\"policy crud smoke\"}" \
  | jq -r '.id')
curl -fsS -X POST "$BASE/v1/sessions/$SESSION_ID/policies" \
  -H 'content-type: application/json' \
  -d "{\"name\":\"local_session_policy\",\"type\":\"python\",\"handler\":\"$SESSION_HANDLER\"}"
curl -fsS "$BASE/v1/sessions/$SESSION_ID/policies" | jq '.data'

curl -fsS -X POST "$BASE/v1/sessions/$SESSION_ID/policies/evaluate" \
  -H 'content-type: application/json' \
  -d '{"event":{"type":"PHASE_TOOL_CALL","target":"","data":{"name":"Bash","arguments":{}},"context":{}}}' \
  | jq -e '.result == "POLICY_ACTION_DENY"'

docker compose restart omnigent
curl -fsS "$BASE/v1/policies" | jq '.data'
docker compose exec -T postgres sh -lc \
'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "\d+ policies"'
```
