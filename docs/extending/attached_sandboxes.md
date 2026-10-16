# Prototype: reusable sandboxes through existing providers

A reusable sandbox is a normal host created by a provider or attached later. Its
`sandbox_attached` marker makes it reusable in host pickers and records that
Omnigent does not own the box's lifecycle. Ordinary per-session sandbox hosts
remain hidden. One provider may offer both kinds.

```mermaid
flowchart LR
    A[New sandbox: ad hoc or static] --> P[Existing provider: provision]
    P --> H[Existing host and session machinery]
    P --> O[Save lifecycle ownership on host]
    O --> K[Static hosts appear in picker for reuse]
    B[Attach existing devbox] --> C[Provider: authorize caller]
    C --> R[Existing provider: resume and start_host]
    R --> H
    H --> S[Runner for each session]
```

Only hosts marked reusable appear as sandbox entries. The new-session picker can
wake a sleeping static host before browsing directories. Once online, the same
host can launch multiple independent session runners. Existing sessions reuse
the normal resume path. Deleting one session leaves the host and other sessions
alone. Reaping, startup failure and missing-box recovery never delete or replace
an attached box. Omnigent also leaves its keepalive policy to the customer.

## Create through the normal sandbox option

A provider opts its new sandboxes into reuse with the existing capability object:

```python
from dataclasses import replace

@property
def capabilities(self):
    return replace(super().capabilities, reusable_hosts=True, resume_stopped=True)
```

The regular new-session provider option becomes **New static sandbox**.
By default it calls the existing `provision` and `start_host` methods, including
repository setup. The resulting host is saved with the same ownership marker
and appears in host pickers for later sessions. No separate Add static sandbox dialog or creation
API is needed. The capability only selects the initial policy: changing provider
configuration later cannot turn a saved static box into an ad hoc one.

`provision` must create a fresh box. For a controller that finds the user's box
and creates it only when absent, implement this optional method on the same
provider instead:

```python
from omnigent.onboarding.sandboxes.types import SandboxInfo

def get_or_create_for_user(self, *, repos=()):
    # The client already carries the identity supplied to bind_principal.
    box = self.controller.get_or_create(repos=repos)
    return SandboxInfo(sandbox_id=box.id, workspace_path=box.workspace)
```

With `reusable_hosts=True`, the normal provider option calls this hook for each
new managed session, including forks. The controller authorizes the bound user,
finds or creates the box, and prepares the requested repositories/workspace.
It must return a stable, canonical sandbox ID and a usable workspace path, and
make concurrent create-or-get requests idempotent. Its HTTP endpoints and policy
stay in the provider plugin/controller.

Omnigent uses the existing attachment registration and wake path. An already
online host keeps its ID and credential, with no second host process. Sleeping
boxes wake under the same registration. Explicit attachment and controller
selection of the same box converge on that registration; another user's host
or an ad hoc allocation cannot be adopted. A failure never deletes the box or
falls back to `provision`. The default hook returns `None`, preserving fresh
creation for providers that do not implement create-or-get.

No discovery dialog or new REST endpoint is needed for this flow. Users can
choose the provider option again or select the resulting host in the picker.
Discovery is a separate feature if users must choose among several existing
boxes. The existing registry can still expose separate static/ad hoc providers.

## Provider contract

Keep the customer controller and HTTP client in the existing
[provider plugin](sandbox_providers.md). No generic HTTP adapter, second provider
interface, discovery service or external provider store is required upstream.

The attachment authorization hook is optional and rejects requests by default:

```python
def attach_for_user(self, sandbox_id: str, *, owner: str) -> None:
    # Customer code: authenticate to your controller and authorize this caller
    # for this exact box. Service credentials alone are not user authorization.
    if not self.controller.can_control(owner, sandbox_id):
        raise PermissionError("not your sandbox")
```

The server also calls the optional `bind_principal(principal)` hook before any
user-scoped provider operation. It defaults to a no-op for existing providers.
An HTTP provider can use it to configure its controller client's user header.
Creation and attachment use the authenticated account identity; later wake,
startup, status, relaunch, termination, keepalive and background cleanup use the
saved host owner. The value is an opaque account identifier, not necessarily an
email address, and never an owner supplied in the request body. It is identity
context, not a forwarded user access token: the controller must authenticate the
server and authorize the operation for that principal.

The launcher factory must return a fresh instance, since the bound identity must
never be shared across users. Background cleanup groups hosts by workspace,
provider and owner. Deployment-wide capability inspection and operator-owned
warm-pool tooling remain unbound; constructors and capability properties must
not require an end-user identity.

Existing lifecycle methods handle the rest:

| Method | Customer implementation |
| --- | --- |
| `get_or_create_for_user(repos=...)` | Optionally authorize/select/create the user's box and return `SandboxInfo` with its ID and prepared workspace. Called only for reusable providers. |
| `resume(sandbox_id)` | Wake that existing box; never silently allocate a replacement. |
| `start_host(sandbox_id, token=..., host_id=..., ...)` | Ask the controller to start/reconnect `omnigent host` with the supplied identity and token; return its workspace path. |
| `provision` | Create a fresh box when `get_or_create_for_user` returns `None`. Attachment and successful controller selection never call this method. |
| `terminate` | Used for ad hoc sandboxes. Never called for a saved reusable box, including when startup fails. |

Declare the existing `resume_stopped` capability. The default `attach_for_user`
rejects requests, so installed providers do not opt in accidentally. Providers
must reject boxes managed by an ad hoc allocation system and missing or foreign
IDs. Use stable, canonical IDs; aliases for the same box are not supported.
Each box has one host owner per workspace. Separate launchers are constructed
on later wakes, so methods must resolve the box by ID without relying on an
earlier attachment call's in-memory state.

HTTP is only the customer's control channel. Agents still run through the
existing outbound host/runner connections. The controller must honor the supplied
host identity and token; returning an HTTP endpoint alone is insufficient.
Reusable hosts route by their canonical host ID on creation and every wake.
A controller-selected box can override a create's requested host ID; clients
must use the session's bound `host_id` once it is available.

## Workspace policy

The controller can choose an existing checkout, a per-session worktree, or
another directory. Omnigent only needs the chosen path to start the session's
runner there; being a static sandbox does not require a particular policy.
The create-or-get hook can return a different prepared directory for every
session started through the provider option, even when the host is already
online. Direct selection of a saved host still uses existing directory/worktree
settings and does not call this hook. Enforcing controller workspace policy on
that path remains a separate integration. `start_host` alone cannot provide
per-session preparation: a running host can serve many sessions without restarting.

## Try it

After the [schema/application rollout](../sandbox-attachment-rollout.md), install
and configure an opt-in provider on an isolated server. Open New session, select
**New static sandbox** and submit a prompt. Once the session starts, open
New session again: the created host should now be available to select. Create
a second session on that host, then delete the first; the second must stay bound
and the provider must not delete the box.

For a provider with `get_or_create_for_user`, select **New static sandbox** for
both sessions. They must have the same `host_id`, distinct runner IDs, and the
controller's returned workspace paths. Only the first request should create the
box/start its host. Repeat after sleeping the box and confirm it wakes with the
same host ID. Test a controller refusal too: the session must fail without
calling fresh provisioning or deleting the box.

To attach a pre-existing box instead, authenticate as its owner and call:

```http
POST /v1/hosts/attach
Content-Type: application/json

{"provider":"static","sandbox_id":"my-existing-box","name":"My devbox"}
```

This waits for the ordinary host connection and returns `{"host_id":"..."}`.
Retries reuse the same host and keep its original name. An authorized but failed
startup leaves the attachment available for retry. `GET /v1/hosts` and the host
detail response expose `sandbox_attached`; older servers omit it, preserving the
old picker behavior. `POST /v1/hosts/{host_id}/wake` wakes an attachment, and
`DELETE /v1/hosts/{host_id}` removes the attachment and revokes its credential
without deleting the box. Detachment unbinds its sessions; it does not stop
processes already running on the customer's machine.

Open New session, pick **My devbox**, choose a directory and start a session.
Repeat for a second session, then delete the first: the second stays bound to
the same host. Sleep the box through the controller, then click its **Asleep**
entry to wake it. Ordinary ad hoc sandbox hosts must never appear in that menu.
Use separate directories or the existing worktree option for concurrent edits.

## Prototype limits

Creating a static box uses the normal provider option. Attaching a pre-existing
box and detaching a host use the API; there is no discovery or Add dialog. The
new-session picker has the wake action; other pickers reuse an
attachment once it is online. Automations retain their existing sandbox
restrictions. Cross-replica wake coordination retains the existing resume
implementation's limits; it is not a new distributed lease mechanism.

Self-authenticating hosts need an adapter for this token contract, or a subsequent
change must support their startup mode in the existing host path. This prototype
has not been validated against a real customer controller.
