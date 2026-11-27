# Layered personal merge settings for Polly

Keep the existing built-in Polly agent and its human-merge workflow. This
proposal defines a future settings contract and the prerequisites for a
dedicated action. It introduces no configuration or runtime behavior.

## Settings contract

Use an authenticated, caller-scoped `GET/PUT /v1/settings/polly-merge` backed
by the existing [user settings store](../omnigent/stores/user_settings_store.py).
Require a verified account for these routes, including on deployments where
other settings may fall back to a reserved local user. Derive the account and
workspace from the request context.

Keep the portable base and local account override as separate typed records.
Ship a base of `enabled: false` and `repositories: []`. A future sync path can
supply a reviewed portable base for an account. Limit base writes to that
separately authorized path. The settings API reads both layers and updates
only the caller's override. An omitted or null override field inherits the
current base field. Preserve explicit `false` and the empty list.

An effective enablement requires the base to enable the feature and the
override to permit it or inherit it. Use exact canonical GitHub repository
identities, including the host, with one normalized representation for
comparison. Reject wildcard, prefix, and URL-pattern entries. An explicit
override list replaces the inherited list and must be a subset of the base
on write. Intersect it again on read and dispatch so a later base reduction
takes effect immediately. Reject attempts to enable a disabled base. Server
policy can deny any request, regardless of the settings.

## Action prerequisites

The [interactive merge routes](../omnigent/server/routes/sessions/routes_resources.py)
check session edit access and accept a caller-supplied head. The
[runner helper](../omnigent/runner/github_resource.py) supports conditional
merge via `--match-head-commit`. Neither establishes that a human authorized
Polly to merge that reviewed commit. The
[agent tool policy gate](../omnigent/runner/tool_dispatch.py) accepts generic
permission verdicts, including an unspecified result. Such a result provides
no positive evidence of a human decision bound to a particular merge.

Before exposing an action, the server needs an authenticated confirmation
bound to the authorizing account, workspace, session, exact repository, PR
number, reviewed head, and squash method. A trusted confirmation surface must
display those details and record the user's decision. Expire the decision and
consume it atomically for the matching action to prevent replay. Agent text,
delegated tool calls, and generic policy allows cannot supply that decision.

At dispatch, verify from server-owned session metadata that the caller is
the top-level built-in Polly session and that its owner is the authorizing
account. Re-read the effective settings and apply server policy, honoring
denies and required approvals. Resolve effective GitHub branch protections
and rulesets for the current base, verify required checks and approvals
for the reviewed head, and require stack readiness. Fail closed when any
required fact is unavailable. Submit only a squash merge conditional on the
reviewed head, with GitHub enforcing current requirements and without bypass
privileges. Decline the action when a repository requires a merge queue
that cannot preserve those guarantees.

Keep the [built-in instructions](../examples/polly/config.yaml) and current
guardrails in force while the prerequisites are outstanding. Before changing
the instructions, verify that workers cannot invoke the action or bypass it
through shell, terminal, or connector paths.

## Delivery slices

1. Add the settings schema, persistence, and authenticated API. Test account
   and workspace isolation, disabled defaults, inheritance, exact identities,
   override narrowing, and immediate revocation after a base change.
2. Add the trusted confirmation and policy integration. Test expired, replayed,
   mismatched, denied, and delegated attempts.
3. Add repository readiness verification and head-conditional squash, then
   document the action in the built-in bundle. Test moved heads, failed or
   missing requirements, server denies, and attempts through other tool paths.
