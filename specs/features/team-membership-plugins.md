---
service: omnigent
created: 2026-10-12
---

# Read external team membership through plugins

## Intent

Let customers query membership from their existing directory through an
installed provider plugin. The directory remains the source of truth.

## Context

Omnigent already authenticates requests and loads installed Python plugins by
entry point. This prototype adds a read-only membership contract without
changing authentication or sandbox authorization.

## Requirements

- R1: A plugin lists teams for an existing authenticated user identity.
- R2: Users can list their own teams and check membership by provider team ID.
- R3: Missing, failing, or timed-out providers produce errors, never false membership.
- R4: Okta and Google Groups examples implement the same contract.
- R5: Persist no users, teams, memberships, tokens, or synchronization state.

## Clarifications

- Q1: What is the smallest useful surface?
  A1: Self-service APIs and an installable Python provider contract; no new UI.
- Q2: Who maps identity and manages credentials?
  A2: The provider uses the existing authenticated user ID; the customer handles
  differing identity conventions and credentials outside Omnigent.

## Acceptance Criteria

- AC1: GIVEN a configured plugin WHEN an authenticated user lists teams THEN
  only that authenticated identity is passed to the plugin.
- AC2: GIVEN a complete team list WHEN membership is checked THEN the response
  matches the stable provider ID; display names do not grant membership.
- AC3: GIVEN missing auth or a provider failure WHEN a lookup is requested THEN
  it fails without returning an empty list or false membership.
- AC4: GIVEN Okta or paginated Google responses WHEN their plugins run THEN
  they return the same team shape, and pagination failures return no partial result.
- AC5: GIVEN no configured plugin WHEN the server starts THEN existing features
  are unchanged and membership endpoints report unavailable.

## Constraints

Use existing auth and HTTP libraries. Credentials stay on the server; responses
must not echo provider error bodies. One selected plugin per server process.
Membership semantics follow the directory listing API, without inventing nested
group expansion. Requests have a bounded total timeout.

## Out of Scope

Identity provisioning, sign-in changes, directory writes, invitation flows,
membership databases, caches, background sync, arbitrary-user HTTP lookup,
team administration UI, sandbox access policies, and OAuth token management.

## Plan

### Approach

Use a single async list method on an env-selected Python entry-point plugin.
Derive membership from the complete list. Ship optional reference adapters in
one small example package, installed separately from the application.

### Affected Components

Provider contract/loader, server routes and app registration, example plugins,
and generated OpenAPI documentation.

### Data Model Changes

None.

### API Changes

`GET /v1/me/teams` and `GET /v1/me/team-membership?team_id=...`.

### Risks

Provider latency/failure is visible to callers. Existing identities must match
directory identities or be translated by the customer plugin. Externally supplied
Google access tokens expire and must be refreshed outside this prototype.

## Tasks

- [x] T1: Add the provider contract and explicit plugin loader.
- [x] T2: Add authenticated listing and membership endpoints.
- [x] T3: Add optional Okta/Google reference adapters and setup documentation.
- [x] T-val: Verify plugin discovery, auth, failures, ID matching, and provider
  pagination through mocked upstream APIs; run required repository checks.
