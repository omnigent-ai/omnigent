# Provider-owned sandbox profiles

A customer sandbox provider owns its configurations, revisions and access policy.
Omnigent displays approved choices and passes the selected ID and exact revision
to that provider before launch, reuse, wake or replacement. Customers can use
Okta, Google Groups, or their existing policy service inside the controller;
Omnigent does not synchronize groups or manage identities.

There is no admin configuration editor. **No database migration, table, column,
index, or backfill is required.** Session references use protected labels in the
existing conversation store. A capable controller also retains the assignment on
its sandbox resource, so it survives deletion of every Omnigent session.

## Enable the feature

Install a provider through the existing
[`omnigent.sandbox_providers` entry point](sandbox_providers.md#creating-a-community-sandbox-provider).
Enable `OMNIGENT_FEATURES=sandbox_profiles` on the server, preserving any other
feature overrides. Both the server flag and the provider's `manages_profiles`
capability are required. Ordinary providers keep their deployment defaults.
Older servers and flag-off clients do not display or query profile choices.
Complete the [rollout checks](#rollout-rollback-and-verification) before first
opt-in on an existing deployment.

The [example package](../../examples/sandbox-profile-provider/README.md) includes
both a file-catalog Kubernetes adapter and an HTTP controller adapter. Install
from the same checkout as the server until the contract ships in a release;
pin that release as the minimum SDK version when distributing your plugin.

## Provider contract

```python
from typing import ClassVar
from omnigent.onboarding.sandboxes.base import SandboxHostLauncher
from omnigent.onboarding.sandboxes.profiles import (
    ProviderSandboxProfile, SandboxProfileContext,
    SandboxProfileAccessDenied, SandboxProfileChanged,
)

class CustomerLauncher(SandboxHostLauncher):
    manages_profiles: ClassVar[bool] = True
    supports_sandbox_profile_lookup: ClassVar[bool] = True

    def list_profiles(self, context: SandboxProfileContext) -> list[ProviderSandboxProfile]:
        if context.sandbox_id is not None:
            # Read the actual resource's immutable assignment; authorize this caller.
            sandbox = self.controller.get_sandbox(context.sandbox_id, context)
            return [sandbox.public_profile]
        return self.controller.authorized_profiles(context)

    def select_profile(self, context: SandboxProfileContext, profile: ProviderSandboxProfile) -> None:
        # Reauthorize exact ID/revision, and verify the resource assignment when scoped.
        self.selected = self.controller.authorize_exact(profile, context)
        # prepare/provision/resume/start_host use this launcher's selected configuration.
```

This sketch omits the existing provisioning and cleanup methods. See the
[complete HTTP adapter](../../examples/sandbox-profile-provider/src/omnigent/community/sandbox/profile_demo/controller.py)
for configuration, credentials, request limits and lifecycle wiring.

Declare the same capability in the plugin's `SandboxProviderMetadata`:

```python
from omnigent.onboarding.sandboxes.registry import SandboxProviderMetadata

SandboxProviderMetadata(
    name="customer",
    launcher_class="omnigent.community.sandbox.customer:CustomerLauncher",
    manages_profiles=True,
)
```

This default-false metadata lets Omnigent check support without importing or
constructing a launcher. Embedding deployments that construct
`ManagedSandboxConfig` directly also set `manages_profiles=True`. The launcher
capability and metadata must agree; a mismatch rejects profile launches.
Restart server replicas after changing the declaration. User authorization is never cached:
resolution, admission and binding can each recheck it as a launch progresses.
Implement bounded controller timeouts and make authorization calls idempotent.

`SandboxProfileContext` is a frozen dataclass:

| Field | Meaning |
| --- | --- |
| `user_id: str` | Trusted execution identity from server authentication. |
| `workspace_id: int` | Trusted tenant scope; OSS uses workspace 0. |
| `sandbox_id: str \| None = None` | Existing controller resource, only for opted-in sandbox lookup. |

The context is not a credential. Do not trust equivalent browser headers. Bind
user/workspace claims to the authenticated server-to-controller connection and
authorize them on the controller. Anonymous and reserved local/public identities
cannot select profiles. A fork authorizes its new owner; a saved session uses its
recorded execution identity.

The frozen, strict `ProviderSandboxProfile` model contains public metadata only:

| Field | Contract |
| --- | --- |
| `id` | Stable `[a-z0-9][a-z0-9_-]{0,63}` identifier, unique within a provider. |
| `name` | Nonempty display name, maximum 100 characters. |
| `description` | Optional description, maximum 500 characters. |
| `revision` | Nonempty opaque string, maximum 64 characters. |

Keep launch configuration, credentials, directory membership and access lists out
of this model. Change the revision whenever effective configuration changes;
never reuse it for different settings. Keep historical revisions if existing
sandboxes and replacement launches must remain usable after a catalog update.
A rename alone need not change the revision.

## Controller-owned assignment

`supports_sandbox_profile_lookup` defaults to false. Enabling it extends the
**existing** `list_profiles` and `select_profile` methods with the optional
`sandbox_id` context; it does not introduce a new plugin method or require a new
controller endpoint.

For a sandbox-scoped listing, return exactly one profile: that resource's actual
ID and revision, even when the current catalog offers a newer revision. Perform
current authorization and return only public metadata. Do not pick a default or
infer the assignment from the current catalog. Scoped selection must verify that
the requested reference matches this exact resource. The server rechecks the host
record after lookup to detect a changed resource, provider, owner or deletion.

Persist the immutable assignment **with the sandbox at creation** in your
controller's existing metadata/tags. For example:

```json
{
  "sandbox_id": "sb-123",
  "sandbox_profile": {
    "id": "large-build", "revision": "2026.10-v3",
    "name": "Large build", "description": "Approved build environment"
  }
}
```

Your controller can return this field in its existing GET sandbox response.
Alternatively, your plugin can read existing resource tags. Map the adapter to
those APIs. The example's `/profiles` and `/sandboxes` URLs are illustrative,
not a mandatory customer protocol. Omnigent's existing host detail API gains an
optional expansion: `GET /v1/hosts/{id}?include=sandbox_profile`. After host access
checks it returns the public assignment, or `sandbox_profile: null` and a
sanitized `sandbox_profile_error`. No user/workspace identity is exposed.
Launch admission performs the same discovery, so recovery does not depend on
whether a client requested the expansion.

Without the lookup capability, Omnigent can recover a host's reference from
surviving sessions using existing host metadata queries. Conflicting references
are rejected. After the last session is deleted, a provider without controller
lookup cannot recover the assignment; it must not silently launch with defaults.

## Authorization, credentials and failures

`list_profiles` returns only currently authorized choices, at most 1,000 unique
profiles. `select_profile` must independently check current permission and the
exact revision, then configure **this launcher instance**. Both run synchronously
in worker threads. Bound all external calls with timeouts, response-size limits
and bounded pagination. Discovery and selection must not allocate infrastructure.

Each provider may return at most 1,000 unique profiles. Omnigent loads and sorts
the complete authorized catalog before applying search and pagination; the
20-result picker cap limits the response, not the work performed by the provider.

| Provider result | Omnigent behavior |
| --- | --- |
| Empty catalog | No available profiles; launch remains blocked. |
| `SandboxProfileAccessDenied` | Sanitized 403; no default fallback. |
| `SandboxProfileChanged` | Sanitized 409; choose again for a new session. |
| `OwnerCredentialMissingError` | Actionable 409 asking the user to connect their account. |
| `SandboxGoneError` during scoped discovery/resume | A surviving session can authorize its saved revision for replacement; an orphan with no saved reference cannot be recreated implicitly. |
| Other provider exception | Generic 503; private errors are not returned to clients. |

Providers using connected user accounts declare `owner_credential_provider` and
implement the existing `bind_owner_credential(resolve)` hook. Omnigent binds the
workspace-scoped owner resolver before discovery/selection. There is no fallback
to a service credential if the user's connection is missing. Providers using a
customer service credential leave that capability unset and authorize the trusted
context themselves. The HTTP example reads its service token from an environment
variable, uses HTTPS outside loopback, disables redirects, sets timeouts, and
caps each response at 1 MiB.

Cleanup and status checks use stable provider configuration and sandbox ID,
independent of profile availability. Revoking access blocks subsequent launches
and reuse; it does not terminate already running sandboxes. Handle immediate
infrastructure revocation in your controller if your policy requires it.

## User experience and lifecycle

The composer and fork dialog show a searchable picker for participating providers.
Search covers authorized IDs, names and descriptions before pagination. Each menu
shows at most **20 results**, with a bounded scroll area and truncated text; users
refine the search to find later matches. A selected profile is fetched separately
by ID, so being outside the first page does not invalidate it. Query cancellation
and separate search keys keep older responses from replacing newer results.
While capability discovery or a catalog refresh is pending or failed, sandbox
launch is disabled, including for ordinary providers whose participation is not
yet known. A background refresh briefly disables Start even with a selected
profile. Retry after an error or select an ordinary connected host to continue.

| Operation | Reference and behavior |
| --- | --- |
| New session, including attachments | Explicit authorized ID/revision; reject before allocating if unavailable. |
| Fresh managed fork / branch clone | Default to the source profile for the same provider, resolve the currently offered revision and show an update notice if changed. User can search and choose another. Revoked/missing source blocks until a valid choice is made. |
| Fork to an ordinary provider or local host | Do not copy the source profile. |
| Native `/clear` / implicit `/fork` | Create an unprofiled replacement. Binding to a managed runner reauthorizes and adopts its host's reference before detaching the source; local forks stay unprofiled. |
| Side chat | Keep the source's profile and runner; reject unrelated runner adoption. |
| Existing host reuse | Authorize the controller's actual assignment, check ownership and profile compatibility, then accept it for the session. |
| Restart / wake | Reauthorize the exact saved revision. Never upgrade an existing session to the current catalog revision. |
| Daemon recovery inside a profiled sandbox | Replace a stale runner atomically on the same host, retaining its profile. Failed launches restore the previous binding. A fresh persisted heartbeat blocks replacement for up to 90 seconds after an ungraceful disconnect; retry after it expires. Ordinary deployments retain their existing recovery behavior. |
| Missing sandbox replacement | Reauthorize the saved revision before allocating a replacement. |
| Last session deleted | Its labels are deleted; capable controllers retain the sandbox assignment. |
| Host deletion | Existing host metadata marks deletion; surviving sessions can bind an authorized replacement. |
| Failed allocation / runner startup | Roll back the attempted host/runner binding and clean up fresh infrastructure. Retain a preaccepted selection for retry. |
| Programmatic managed create | `sys_session_create(placement="managed")` accepts `sandbox_provider`, `sandbox_profile_id`, and `sandbox_profile_revision`, with the same authorization as the composer. Explicit profile selections require the existing `/v1/info` response to advertise `features.sandbox_profiles: true`; otherwise no create request is sent. Provider-only launches retain their existing behavior. |
| Scheduled fresh sandbox | Creating/updating a task rejects a default provider requiring profiles before saving. Use a connected host or an ordinary default provider. Already-saved unsupported tasks fail closed at launch. |

Omnigent stores the accepted provider/ID/revision, public display snapshot and
execution identity under reserved `sandbox_profile:*` session labels. Generic
create, update, delete and fork-label APIs cannot forge or modify them. Public
session responses hide that namespace and expose a typed `sandbox_profile`
summary. No private configuration is stored. Host affinity uses the existing
conversation `host_id`. Capable controllers remain authoritative for sandbox
assignments; a missing sandbox can be replaced from its bound session's reference.
Ordinary hosts skip
profile discovery entirely. Legacy host recovery reads at most 201 session IDs
through the existing host index and rejects histories over 200 before reading
profile references. Each reference read uses only the seven canonical label keys.
Enable controller assignment lookup for large histories or orphan recovery.
Runner-only adoption reads host IDs through the existing runner index in one
statement snapshot, so concurrent rotation cannot hide a host between pages.
This read has a separate safety budget of 10,000 currently attached sessions
(10,001 rows detect overflow). Conflicting hosts or overflow fail closed; launch
a new runner on overflow. The reconnect liveness write uses the same budget,
locks the selected bindings and rejects overflow before updating any rows.
Ordinary runners with 201 or more attached sessions
remain usable within that budget in mixed deployments.
Rotation and rollback still bound their
source lookup to 201 rows and leave the binding intact if no source is found.
Ordinary-only deployments skip runner profile discovery.

Incomplete legacy labels remain readable with `sandbox_profile: null`; they
do not impose profile affinity on ordinary metadata binding operations and
cannot authorize execution. Saved identities must match the trusted execution
owner before any owner credential resolution or provider authorization call.

Single-database deployments commit labels and metadata together. Split-database
deployments accept immutable labels before binding metadata. If that second
transaction fails, the reference can remain while the host/runner stay unbound;
a retry must use that same reference. Rollback retains the reference in split
mode so a metadata failure cannot leave a live binding without its profile.
There is no cross-database atomicity claim or unsafe fallback to default settings.

## Rollout, rollback and verification

Deploy with the flag off and keep providers in ordinary mode while validating
authenticated identity mapping, historical revisions and cleanup in staging.
No schema deployment or schema downgrade is needed. Enable the flag before setting
`manages_profiles = True`; a profile-requiring provider with the flag off is a
configuration error, rejected before session or schedule persistence.

**Before the first opt-in on an existing deployment, audit the reserved labels.**
Older servers allowed editors to write `sandbox_profile:*`. A complete old label
set is indistinguishable from a server-accepted reference, including when it names
the correct owner. Provider reauthorization alone does not prove that owner chose
the profile. Every replica and other label writer must run a build that rejects
the reserved prefix before this check begins; stop older writers and keep profiles
disabled throughout the audit. Do not audit midway through a rolling upgrade.

In the conversation database, inspect every workspace's `conversation_labels` in
primary-key order `(workspace_id, conversation_id, key)`. Read at most 200 rows per
transaction, continue after the last `(conversation_id, key)` for that workspace,
and check `key.startswith("sandbox_profile:")` in the audit process. Use the
database's native UUID cursor representation. Do not add a prefix filter that
forces each page to scan an unbounded portion of the tenant. Back up and review
any matches, then remove those specific full-primary-key rows in bounded batches
through an administrator's maintenance procedure; the public label API deliberately
rejects this namespace. Complete a second audit with **zero matching labels**
before enabling the feature. Do not run this cleanup against an already-enabled
deployment: its accepted references are legitimate and must be preserved.

Upgrade every server replica and launch client before opting a provider in. Older
clients cannot supply a required selection and receive 422; there is no implicit
default profile. Keep providers used by older clients and fresh-sandbox schedules
ordinary until those callers support selection. Inventory existing schedules
before changing the default provider. Ordinary controllers retain their launch
behavior with either flag setting. The reserved label namespace applies globally.

To roll back, stop new profiled launches and drain their running sandboxes before
disabling the flag or using an older server. A flag-off server does not enforce
profile authorization, so disabling it is not an access-revocation mechanism.
Omnigent rejects unselected profile launches before invoking the launcher. Custom
callers invoking a launcher directly must enforce selection themselves; the
file-backed example inherits lifecycle methods without its own selection guard.
Keep the controller and stable provider configuration available for cleanup and
preserve controller assignments and surviving session labels for later recovery.

Use the [local walkthrough](../../examples/sandbox-profile-provider/README.md) to:

1. Compare Alice's and Bob's catalogs, then revoke access between listing and launch.
2. Search beyond 20 results and fork a profiled session; verify its default and changed revision notice.
3. Create a controller sandbox, restart the controller with the same resource state,
   and recover the exact assignment without a session reference.
4. Verify an ordinary provider with the flag off still sends its original launch payload.

The simulator and browser recordings do not provision real infrastructure or
exercise an identity provider. Test real resource limits, identity mapping,
controller persistence and revocation against your customer's staging controller.
