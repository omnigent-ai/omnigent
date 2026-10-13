---
service: omnigent
created: 2026-10-12
---

# Customer-defined sandbox profiles

## Intent

Customers can define named sandbox configurations in Settings and select one
when creating a managed session. The controller receives the accepted resource
and network configuration, which survives sandbox wake and replacement.

## Context

Managed launch currently selects one deployment configuration per provider.
The new-chat composer already offers configured providers. Profiles add several
configurations per provider using the existing launcher lifecycle.

## Requirements

- R1: Administrators can create, edit, and disable workspace-scoped profiles.
- R2: Authenticated users can select an enabled profile for a managed session.
- R3: Validate provider support, configuration, and revision before allocation.
- R4: Persist the accepted configuration and preserve it across wake/replacement.
- R5: Shared-host sessions inherit the same profile; reject mismatched placement.
- R6: Kubernetes and direct agent-sandbox launches apply image/resource overrides
  and optional native Kubernetes NetworkPolicy egress rules before workload start.
- R7: Existing launches without a profile retain their current behavior.

## Clarifications

- Q1: Which customer controller should the first integration target?
  A1: Default to existing Kubernetes / agent-sandbox pending user steering.
- Q2: Who authors profiles?
  A2: Administrators; other users select published profiles. Okta follows later.

## Acceptance Criteria

- AC1: GIVEN an admin WHEN a valid profile is saved THEN it survives server restart
  and appears in the matching provider's new-chat picker.
- AC2: GIVEN a non-admin WHEN a profile write is attempted THEN it is denied.
- AC3: GIVEN a selected profile WHEN creating a session THEN the controller
  receives the accepted configuration and the session retains its revision.
- AC4: GIVEN a changed profile WHEN an existing sandbox wakes or is replaced
  THEN its saved configuration is reused, provided the profile remains enabled.
- AC5: GIVEN invalid, stale, disabled, or unsupported configuration WHEN launch
  is requested THEN it fails before allocating a workload.
- AC6: GIVEN a profiled host WHEN another session attaches THEN it inherits the
  same profile or an incompatible requested profile is rejected.

## Constraints

Use existing SQLAlchemy storage, named sessions, bounded payloads, and paginated
catalog reads. NetworkPolicy enforcement requires a supporting cluster network
plugin; Kubernetes allow policies combine. No claim of read-only internet or
preventing private-data disclosure. Empty egress rules deny DNS/control/model
traffic too; admins must allow required destinations explicitly.

## Out of Scope

Okta groups, arbitrary Pod manifests, live profile changes to running sandboxes,
warm-pool profile matching, and a network-policy designer beyond a JSON editor.
Transferring a profiled session to another host is unsupported; forks can launch
on new hosts, and replacements retain the original host identity.

## Plan

### Approach

Add a persisted admin profile catalog, a small Settings editor and launch picker,
and immutable session/host bindings. Pass the saved configuration through the
shared managed-launch and wake paths into a provider hook. Use native
NetworkPolicy for egress enforcement in supported Kubernetes providers.

### Affected Components

Server profile routes/store; managed session creation, fork, host admission,
wake/replacement; Kubernetes provider; Settings and new-chat composer.

### Data Model Changes

Add workspace-scoped profile and immutable launch-binding tables with bounded
configuration JSON. Keep provider credentials in existing deployment config.

### API Changes

Profile catalog CRUD; optional sandbox_profile_id and sandbox_profile_revision
on managed session creation and fork.

### Risks

Incorrect network rules can prevent host registration. Existing cluster policies
may grant additional traffic. Warm pools are rejected until profile matching is
implemented. Hash-addressed NetworkPolicies persist for safe reuse; unused
policies can be removed by cluster administrators after all matching Pods stop.

## Tasks

- [x] T1: Implement catalog persistence and authenticated/admin API.
- [x] T2: Implement supported provider configuration and network policies.
- [x] T3: Preserve configuration through launch, wake, replacement, and sharing.
- [x] T4: Add Settings editor and new-chat profile picker.
- [x] T5: Document usage and verification entry points.
- [x] T-val: Run focused tests, lint/type checks, and an isolated browser demo;
  distinguish mocked controller evidence from real cluster enforcement.
