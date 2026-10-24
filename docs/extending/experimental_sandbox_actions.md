# Experimental sandbox actions

**Status: Python SDK preview for design feedback.** The contract and external
example are runnable. No public route, session handoff, runner lifecycle, UI,
or harness tool invokes these actions. Installing a provider does not expose
its actions to users or agents. This is not a production operator workflow.
The experimental API may change before a supported integration ships.

## Provider contract

An installed package registers through the existing
[`omnigent.sandbox_providers` entry point](sandbox_providers.md).
Its launcher optionally implements two hooks:

- `experimental_actions` declares the actions it supports.
- `invoke_experimental_action(request)` executes a validated request.

Existing providers inherit an empty action list and an unsupported-operation
error. The preview does not change `SandboxCapabilities` or require providers
to implement fork, merge, or promotion.

Types and the dispatch helper live in
`omnigent.onboarding.sandboxes.experimental`:

| Type | Contract |
| --- | --- |
| `SandboxAction` | Versioned `name`, Pydantic `parameters_model`, required `target_roles`, and `creates_child`. |
| `SandboxActionTarget` | Resolved `session_id`, `host_id`, `provider`, and `sandbox_id` for one role. |
| `SandboxActionRequest` | `action`, bounded `operation_id`, role-to-target mapping, and `parameters`. |
| `SandboxActionResult` | JSON `data` describing an ordinary result. |
| `SandboxChildResult` | A new `child: SandboxInfo` plus JSON `data`. |

Names use `<provider>.<action>.v<version>`, such as `example.inspect.v1` and
`example.create_child.v1`. Names belong to that provider; matching verbs do
not establish common semantics across providers. Contract changes require a
new action version. An action cannot claim another provider's namespace.

Call `invoke_sandbox_action(launcher, request)` to validate and dispatch.
It checks the declared action, target roles/provider, parameter schema, and
result shape. Parameter models declare `ConfigDict(strict=True, extra="forbid")`
to reject coercion and unknown fields. Validated values are normalized to JSON
before provider dispatch. Result data
must be finite JSON values. Put runtime references in declared target roles,
not arbitrary sandbox IDs hidden in parameters. Do not return credentials.

For example, `example.inspect.v1` accepts a `source` target and no parameters.
`example.create_child.v1` accepts the same target and `{"name": "candidate"}`,
and declares `creates_child=True`. The child result makes the new resource
visible to a future core handoff; it does not create a session or register a
host. Returned child handles must support the provider's declared lifecycle
operations, including independent, idempotent termination.

## Authority and retries

The helper is a local SDK boundary, not an authorization service. A Python
caller can construct targets; validation does not prove permission to use
them. Production core must resolve each target from its own session/host
records under the authenticated user's workspace and permissions. A caller
must not supply authoritative provider sandbox IDs or borrowed host identity.

Authorize every role before provider effects. Copying runtime state needs
source-runtime authority beyond the read access used for conversation forks.
An initial integration should require ownership of the affected managed hosts
and sessions. Delegated permissions and agent approvals need separate design.
Provider packages are trusted operator-installed code; this API is not an
isolation boundary around plugin code.

The core allocates `operation_id` before calling the provider. The provider
must bind it to the action version, resolved targets, and validated parameters.
Identical retries recover the same result; conflicting reuse fails. A timeout
can mean the operation succeeded: retry with the same identity rather than
allocating another child or repeating a destructive effect.

Provider idempotency does not supply core durability. Production needs a
durable operation reservation and recovery record before effects, including
owner, source generation, parameters, returned handles, and pending cleanup.
The existing `ManagedLaunchTracker` is in memory and does not provide that
record. The preview implements neither a durable operation store nor a server
reconciler.

## Proposed production handoff

These steps describe missing integration work, not behavior enabled here:

1. Authorize all targets and validate capabilities and parameters. Reserve the
   operation and intended session/host identities durably before allocation.
2. Invoke the provider with the reserved operation ID. Persist its result;
   reconcile an uncertain outcome using the same identity. Recheck source
   generation and cancellation before committing a handoff.
3. For a child result, register a distinct managed host identity and credential.
   Start the host against the retained workspace without re-cloning or
   resetting captured contents. A live capture must prevent copied processes
   from reconnecting with the source's host, runner, or harness identity.
4. Bind the intended child session, launch its runner under a fresh identity,
   and await host registration and runner readiness. Only then publish a
   usable session. Provider allocation alone is not readiness.
5. Persist the outcome before cleanup. On failure or cancellation, record and
   retry cleanup of exactly the unused child. Preserve usable survivors and
   transcript/audit records independently of compute lifetime.

Existing pieces to reuse include `HostStore.register_managed_host`,
`_register_and_start_host`, `_bind_and_launch_managed_runner`, and
`_wait_for_managed_runner_tunnel`. Their current startup path assumes ordinary
provisioning; retained-workspace startup and crash recovery require explicit
integration. `terminate_managed_host` and `ManagedSandboxReaper` already retain
known failed cleanup for retries. They cannot recover an unrecorded child from
a lost provider response without the new operation record.

## Decisions left open

- **State to inherit:** fresh allocation, filesystem capture, and live process
  capture have different guarantees. Specify capture scope and consistency;
  unsupported modes must fail explicitly. A current runtime capture cannot
  represent an arbitrary historical conversation response.
- **Session relationship:** existing conversation fork creates a top-level
  session; ordinary subagents inherit their parent's runner. Neither behavior
  changes here. Independent child placement needs ownership, parent completion
  routing, recovery, and descendant cleanup work.
- **Resolution:** an action result does not promote a child into the original
  session or establish safe merge into an active runtime. Selection needs a
  committed survivor binding before cleanup; merge needs reviewed scope,
  conflict preconditions, and process consistency. No memory, conversation,
  or external-service merge is implied.

## Run the example

From a checkout with the normal development environment installed:

```bash
uv pip install --no-deps -e examples/sandbox-actions
uv run --no-sync python -m omnigent.community.sandbox.action_example
```

The [external example](../../examples/sandbox-actions/README.md) uses installed
entry-point discovery and a fake in-memory controller. It checks inspection,
child results, retry identity, conflicting requests, and independent cleanup.
It creates no remote resources and starts no host. Passing it verifies the SDK
contract; it does not validate authorization, durable recovery, or production
session handoff.
