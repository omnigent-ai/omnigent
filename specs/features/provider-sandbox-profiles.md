---
service: omnigent
created: 2026-10-21
---

# Delegate sandbox profiles and access to the sandbox provider

## Intent

Let customers offer centrally managed sandbox profiles through their existing
sandbox plugin. The provider decides which profiles each user may use and
authorizes every launch without an Omnigent team directory.

## Context

The lower stack supplies a profile picker, local administrator catalog, and
session/host bindings. The existing sandbox-provider registry supplies plugins.

## Requirements

- R1: Providers can list authorized profile metadata for a trusted user and workspace.
- R2: Providers authorize and configure each selected revision before allocation or resume.
- R3: Provider-managed catalogs require an explicit selection and are read-only in Omnigent.
- R4: Forks, host reuse, wake, and replacement recheck access to the saved selection.
- R5: Keep local-profile behavior for providers that do not opt in.
- R6: Persist only profile references and execution identity for provider-managed sessions;
  do not store directory memberships, credentials, or provider configuration.

## Clarifications

- Q1: Where do Okta, Google Groups, and customer authorization rules live?
  A1: Inside the customer's sandbox provider; Omnigent has no team-provider API.
- Q2: What happens after a provider removes or changes a profile revision?
  A2: The provider decides whether that exact revision remains usable. Denial blocks
  subsequent launches and wakes; it does not terminate an already running sandbox.
- Q3: How are background launches identified?
  A3: Reuse the trusted execution identity accepted for the session. Forks must
  authorize the new caller; profile names and IDs never establish identity.

## Acceptance Criteria

- AC1: GIVEN two users WHEN profiles are listed THEN each sees only the provider's
  authorized metadata for their workspace.
- AC2: GIVEN a provider-managed catalog WHEN a request omits or forges a selection
  THEN it fails before allocation, including when the authorized list is empty.
- AC3: GIVEN revoked access or a retired revision WHEN a session launches, forks,
  wakes, or reuses a host THEN the provider can deny the operation.
- AC4: GIVEN provider failure WHEN listing or selecting THEN an error is returned
  without falling back to the local catalog or default configuration.
- AC5: GIVEN provider-managed profiles WHEN an administrator opens Settings or
  calls mutation endpoints THEN the profiles cannot be edited locally.
- AC6: GIVEN a provider without managed profiles WHEN its existing local profiles
  or default launch are used THEN their behavior is preserved.

## Constraints

Reuse the sandbox provider registry. Keep the contract small, validate metadata,
scope IDs to providers, and keep external error details out of client responses.
Provider I/O must have bounded timeouts. Each launch gets its own provider instance.

## Out of Scope

Identity/group management, membership APIs, directory synchronization, OAuth token
management, immediate termination on revocation, and production infrastructure deployment.

## Plan

### Approach

Add optional discovery and selection methods to SandboxHostLauncher. Route profile
discovery through configured providers, store reference snapshots in existing bindings,
and invoke selection again on fresh launchers before infrastructure calls. Keep the
local catalog for providers without this capability.

### Affected Components

Sandbox launcher contract, catalog, create/fork/wake/host admission, picker, Settings,
provider examples, and tests.

### Data Model Changes

No migration. Existing binding JSON carries the provider reference and execution identity.

### API Changes

The existing profile listing includes managed_providers and managed profile metadata.
Provider-managed launches require a profile selection; an omitted sandbox_provider
uses the deployment's default provider.

### Risks

Provider availability affects launching. A background launch acts for its saved
execution identity. Listing is advisory; selection is the enforcement boundary.

## Tasks

- [x] T1: Add provider contract and remove the team-provider prototype.
- [x] T2: Integrate provider discovery, references, and launch authorization.
- [x] T3: Adapt picker and Settings for read-only catalogs and required selection.
- [x] T4: Supply a small provider example and reproducible demo instructions.
- [x] T-val: Verify authorization, lifecycle, legacy regressions, and UI behavior;
  run pre-commit.
