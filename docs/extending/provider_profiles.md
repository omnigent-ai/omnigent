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

| Operation | Reference and behavior |
| --- | --- |
| New session, including attachments | Explicit authorized ID/revision; reject before allocating if unavailable. |
| Fresh managed fork / branch clone | Default to the source profile for the same provider, resolve the currently offered revision and show an update notice if changed. User can search and choose another. Revoked/missing source blocks until a valid choice is made. |
| Fork to an ordinary provider or local host | Do not copy the source profile. |
| Native `/clear` / implicit `/fork` | Reauthorize the saved reference and bind the replacement to the trusted runner/host before detaching the source. |
| Side chat | Keep the source's profile and runner; reject unrelated runner adoption. |
| Existing host reuse | Authorize the controller's actual assignment, check ownership and profile compatibility, then accept it for the session. |
| Restart / wake | Reauthorize the exact saved revision. Never upgrade an existing session to the current catalog revision. |
| Missing sandbox replacement | Reauthorize the saved revision before allocating a replacement. |
| Last session deleted | Its labels are deleted; capable controllers retain the sandbox assignment. |
| Host deletion | Existing host metadata marks deletion; surviving sessions can bind an authorized replacement. |
| Failed allocation / runner startup | Roll back the attempted host/runner binding and clean up fresh infrastructure. Retain a preaccepted selection for retry. |
| Scheduled fresh sandbox | No scheduler profile picker yet; providers requiring selection reject an unprofiled scheduled launch. |

Omnigent stores the accepted provider/ID/revision, public display snapshot and
execution identity under reserved `sandbox_profile:*` session labels. Generic
create, update, delete and fork-label APIs cannot forge or modify them. Public
session responses hide that namespace and expose a typed `sandbox_profile`
summary. No private configuration is stored. Host affinity uses the existing
conversation `host_id`; native runner lookup pages the existing runner index.

Single-database deployments commit labels and metadata together. Split-database
deployments accept immutable labels before binding metadata. If that second
transaction fails, the reference can remain while the host/runner stay unbound;
a retry must use that same reference. Rollback retains the reference in split
mode so a metadata failure cannot leave a live binding without its profile.
There is no cross-database atomicity claim or unsafe fallback to default settings.

## Rollout, rollback and verification

Deploy with the flag off, install/configure the provider, then validate authenticated
identity mapping, historical revisions and cleanup before enabling the flag.
No schema deployment or schema downgrade is needed.

To roll back, stop new profiled launches and drain their running sandboxes before
disabling the flag or using an older server. A flag-off server does not enforce
profile authorization, so disabling it is not an access-revocation mechanism.
Profile-capable launchers reject unselected launches at their boundary. Keep the
controller and stable provider configuration available for cleanup and preserve
controller assignments and surviving session labels for later recovery.

Use the [local walkthrough](../../examples/sandbox-profile-provider/README.md) to:

1. Compare Alice's and Bob's catalogs, then revoke access between listing and launch.
2. Search beyond 20 results and fork a profiled session; verify its default and changed revision notice.
3. Create a controller sandbox, restart the controller with the same resource state,
   and recover the exact assignment without a session reference.
4. Verify an ordinary provider with the flag off still sends its original launch payload.

The simulator and browser recordings do not provision real infrastructure or
exercise an identity provider. Test real resource limits, identity mapping,
controller persistence and revocation against your customer's staging controller.
