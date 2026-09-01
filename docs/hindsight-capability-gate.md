# Hindsight capability gate

Assessment date: 2026-08-29

Wulo keeps Hindsight personal-memory writes disabled until every lifecycle
capability below has current evidence for the exact API endpoint, live server
version, and installed client version. Read-only recall and reflect are unaffected.

## Current assessment

The installed Python client is pinned to `hindsight-client==0.8.3`. The current
upstream HTTP API documentation is version 0.9.2.

| Capability | Status | Evidence |
| --- | --- | --- |
| Individual record deletion | Failed | The upstream API explicitly does not support deleting an individual memory unit. It supports reversible invalidation and deleting a whole source document. |
| Whole-bank deletion | Passed upstream | `DELETE /v1/default/banks/{bank_id}` deletes the bank, memories, entities, documents, and profile; upstream integration tests verify absence. |
| Tenant partitioning | Unverified locally | Banks isolate data, but the built-in API-key extension is one shared service credential. Production requires a private endpoint plus a tenant extension or a dedicated deployment, and a cross-tenant behavioral test. |
| Memory retention / TTL | Failed | Hindsight exposes retention for operation, audit, trace, and model-history records, but no expiry policy for memory units. |
| Export | Passed upstream | API 0.9.2 provides async document/bank export with operation tracking and authenticated download. A deployment must expose the feature flag and pass a live round trip. |
| Backup deletion | Unverified | This is controlled by the database/object-store backup policy, not the Hindsight API. A restore-and-delete drill is required. |
| Idempotent capture | Passed upstream with constraints | Async retain accepts a caller UUID and rejects conflicting reuse. Synchronous retain ignores the operation ID; Wulo must use async retain with a stable ID and deterministic document ID. |

**Decision:** the gate is closed. Do not create a passing report and do not set
`OMNIGENT_HINDSIGHT_WRITES_ENABLED=1` until individual logical-record deletion,
memory retention, tenant isolation, live export, and backup deletion are proven
for the deployed service.

## Runtime gate

The legacy `hindsight_retain` built-in is disabled unconditionally and is planned
for removal in version 0.70. Its tool context has no authenticated workspace or
account subject, and its synchronous SDK call has no retry-safe operation ID.
It must not be used for personal memory, even with a passing capability report.

A future governed Hindsight provider must call the capability validator with the
version fetched from the live endpoint and requires both:

1. `OMNIGENT_HINDSIGHT_WRITES_ENABLED=1`
2. `OMNIGENT_HINDSIGHT_CAPABILITY_REPORT=/absolute/path/to/report.json`

The report is valid for at most 30 days and is bound to the normalized API URL,
live server version, and installed client version. Every required capability
must have `status` set to `passed` and non-empty evidence. Agent configuration
cannot override this server-owned gate.

```json
{
  "schema_version": 1,
  "provider": "hindsight",
  "api_url_sha256": "sha256-of-normalized-api-url",
  "client_version": "0.8.3",
  "server_version": "0.9.2",
  "checked_at": 1787961600,
  "valid_until": 1788566400,
  "capabilities": {
    "individual_record_deletion": {"status": "passed", "evidence": "test run or runbook reference"},
    "bank_deletion": {"status": "passed", "evidence": "test run or runbook reference"},
    "tenant_partitioning": {"status": "passed", "evidence": "test run or architecture reference"},
    "memory_retention": {"status": "passed", "evidence": "policy and expiry test reference"},
    "export": {"status": "passed", "evidence": "round-trip test reference"},
    "backup_deletion": {"status": "passed", "evidence": "restore-and-delete drill reference"},
    "idempotent_capture": {"status": "passed", "evidence": "retry/conflict test reference"}
  }
}
```

The endpoint digest is `sha256(normalized_api_url)`, where normalization removes
a trailing slash and lowercases the authority. URLs containing credentials,
queries, or fragments are rejected; non-loopback endpoints must use HTTPS.