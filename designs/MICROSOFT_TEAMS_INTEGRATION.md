# Microsoft Teams integration

Status: proposed

## Context

Omnigent does not have a Microsoft Teams transport. A URL or deep-link helper can
make a session easier to open, but it cannot establish who invoked an action,
which Omnigent principal that person represents, or whether that principal may
access the referenced organization, team, agent, or session.

Teams offers several integration shapes:

- incoming webhooks and Workflows for outbound channel messages;
- Microsoft Graph APIs for activity notifications and chat messages;
- bots and message extensions for authenticated, interactive applications; and
- tabs for embedding a web application.

Those surfaces have materially different identity and permission properties.
Choosing one is therefore an authorization decision, not only a user-interface
choice.

Omnigent already has the boundaries needed by a delegated client:

- RFC 8628 device grants bind a non-browser client to an authenticated Omnigent
  user and issue short-lived, revocable delegated tokens;
- delegated tokens are restricted to the session API allowlist;
- organization/team access is resolved server-side from the authenticated user;
  and
- model credentials are selected from the validated `ActorContext` for every
  invocation.

The Teams integration must reuse those boundaries rather than introduce a
Teams-specific identity or permission model inside Omnigent.

## Decision

Build Teams as a **lightweight authenticated client**, delivered as a Teams bot
in personal scope first.

The client will support focused Omnigent workflows—account linking, creating or
selecting a session, exchanging messages, receiving bounded status/completion
notifications, and opening the canonical web session. It will not embed or
reimplement the full Omnigent web UI.

This choice is intentionally between the two extremes:

- **Not notification-only.** Notifications are useful, but a webhook or deep
  link alone does not close the actor identity boundary and cannot safely
  support later actions.
- **Not a full collaboration client.** A tab or complete Teams-native session
  browser would duplicate the web application and substantially expand the
  authorization, state synchronization, and support surface before the core
  workflow is validated.

The integration will live outside the core server, following the existing
first-party Slack client boundary. It consumes public Omnigent HTTP/SSE APIs;
it does not add Teams concepts to `AgentSpec`, session resources, or the runner.

## Initial product boundary

### Included

The first usable release is a personal Teams bot with:

- `connect`, `status`, `logout`, `new`, and `help` commands;
- one active Omnigent session per personal Teams conversation;
- message forwarding and streamed response updates;
- Adaptive Cards for login, errors, completion, and links to the canonical web
  session; and
- optional proactive messages only for work initiated through that linked
  personal conversation.

The implementation may coalesce streamed updates to respect Teams throttling,
but Omnigent remains the canonical transcript.

### Deferred

- team/channel and group-chat installation scopes;
- automatic mapping of a Teams team or channel to an Omnigent organization or
  team;
- organization-wide broadcast notifications;
- message extensions, link unfurling, tabs, meetings, and activity-feed
  notifications;
- browsing or administering organizations, teams, members, credentials, or
  users from Teams;
- arbitrary subscription rules for sessions initiated outside the linked
  conversation; and
- Microsoft Graph reads of tenant directory, channel, chat, or message data.

These are separate product and permission decisions. They must not be enabled
only by adding manifest scopes.

## Architecture

```text
Teams client
    | Teams activity + SSO token
    v
Teams/Bot Framework service
    | signed bot activity
    v
integrations/teams
    |-- validate activity and Entra principal
    |-- map Teams principal to one encrypted Omnigent device grant
    |-- map personal conversation to active Omnigent session
    |-- translate activities, SSE events, and Adaptive Cards
    |
    | delegated Omnigent bearer token
    v
Omnigent HTTP/SSE APIs
    |-- authenticate Omnigent user
    |-- enforce organization/team/session permissions
    |-- derive validated ActorContext
    v
runner / credential broker
```

The Teams bridge is a confidential, operator-hosted service for Bot Framework
credentials, but it is an OAuth public client from Omnigent's perspective. It
uses the existing Omnigent device authorization flow and delegated scope rather
than a shared service account.

Use Bot Framework replies and proactive bot messages for transport. Do not use
Microsoft Graph `chatMessage` creation for normal bot traffic: Graph requires a
delegated send permission, while application message creation is restricted to
migration scenarios. Do not use incoming webhooks or Workflows as the primary
transport: they are outbound/channel-oriented, do not establish an Omnigent
actor, and Workflows are owned by individual users and can become orphaned.

The first release requests no Microsoft Graph permissions. Any future Graph
permission requires a separate decision documenting the exact endpoint,
least-privilege delegated or application scope, data retention, and tenant
administrator consent impact.

## Identity binding

Teams identity and Omnigent identity are distinct and are bound explicitly.

1. The bridge validates every incoming Bot Framework activity using Microsoft's
   supported authentication middleware. Transport authentication proves the
   activity came through the configured bot channel; it does not authorize an
   Omnigent action.
2. Teams SSO obtains and validates an Entra token for the invoking user. The
   stable external principal is `(tenant_id, object_id)` from validated token
   claims. The activity's display name, email, UPN, and unverified client fields
   are never identity keys.
3. The user starts Omnigent's existing device authorization flow. Approval
   occurs in an authenticated Omnigent browser session and names the client and
   delegated scope.
4. The bridge stores a binding from `(bot_app_id, tenant_id, object_id,
   omnigent_server_url)` to that Omnigent grant. Access and refresh tokens are
   encrypted at rest, rotated through the existing token endpoint, and deleted
   after revocation or logout.
5. Every Omnigent request uses the linked user's delegated bearer token. The
   server-derived Omnigent user is the actor; Teams claims are correlation
   metadata only and never populate or override `ActorContext`.

A deployment may restrict accepted Entra tenant IDs. A missing or disallowed
`tenant_id`, a missing `object_id`, an unlinked account, token validation
failure, or revoked/expired Omnigent grant fails closed and yields a login or
access-denied card.

Email-address matching is explicitly prohibited. It is mutable, can differ
between systems, and would silently collapse two independently authenticated
identities into one authority.

## Authorization semantics

Teams membership grants no Omnigent permission.

- A Teams tenant is not an Omnigent organization.
- A Teams team is not an Omnigent team.
- A Teams channel, chat, or conversation is not an Omnigent session ACL.
- Installing the app grants no read, create, run, share, or administrative
  access in Omnigent.

For every user action, the bridge calls Omnigent with that user's delegated
token. Omnigent then applies the same current checks used by web/API clients:

- agent visibility comes from owner, organization, and Omnigent team access;
- team membership is resolved from the authenticated Omnigent user at request
  time;
- session access and mutations are checked against the session and agent
  resources; and
- model execution receives only the server-validated actor, preserving
  actor-aware credential selection.

The bridge must not cache a positive Omnigent authorization decision across
requests. It may cache non-authoritative presentation data, but a request after
team removal, ownership change, grant revocation, or session access loss must be
rejected by the server.

A conversation/session mapping is only a routing hint. Before forwarding a
message, rendering protected details, or sending a proactive notification, the
bridge performs an authenticated Omnigent request for the mapped resource. On
`401` it refreshes once and otherwise requires relinking; on `403` or `404` it
clears the mapping and reveals no protected resource details.

## Teams scope rules

Personal scope is the only supported scope initially. It provides an
unambiguous invoking user and is supported by Teams bot SSO.

When group chat is considered later, each activity must still be authorized as
the individual user who invoked it. A grant owned by one participant must never
be used for another participant. Shared output must be treated as disclosure to
every chat participant and therefore needs a product-level sharing rule, not
just individual access.

Channel scope is further deferred because Teams bot SSO is not supported there
and channel visibility does not correspond to Omnigent resource visibility. A
future channel design must define an explicit Omnigent resource binding, who may
create it, how membership drift is handled, and what content is safe to post to
the whole channel.

## Stored state and secrets

The bridge stores only the state required for routing and revocation:

- validated Teams principal key and tenant;
- Omnigent server URL and encrypted delegated tokens;
- Omnigent grant identifier and expiry metadata;
- personal conversation reference; and
- active Omnigent session ID owned by that conversation's linked user.

Bot credentials, Entra application credentials, and the token-encryption key
come from deployment secret management and are never stored in the database or
logged. Raw Teams SSO tokens, Omnigent bearer tokens, message bodies, model
credentials, and Adaptive Card submissions must not appear in logs. Audit and
error correlation uses opaque request, activity, session, grant, and actor IDs.

Deleting a binding revokes the Omnigent grant before removing local token state.
Uninstall and conversation-removal events delete routing state. A failed revoke
still deletes local credentials and is logged without token values.

## Failure and delivery behavior

- A Teams acknowledgement is sent within the platform response window; long
  work continues asynchronously and updates the bot message.
- Duplicate and retried activities are idempotent by activity ID.
- Only the owner of the linked personal conversation may continue its active
  session.
- Proactive messages require a stored conversation reference and installed app.
  Teams `403` blocked/uninstalled responses disable that destination.
- No notification contains prompt text, assistant content, tool arguments, or
  elicitation values by default. It contains status, a session label, and an
  Omnigent link after a fresh authorization check.
- Transport, authentication, and authorization failures are distinct. The user
  gets a bounded remediation message; internal exceptions and credentials are
  not returned.

## Implementation sequence

### 1. Personal bot identity and account-linking foundation

Create `integrations/teams` with the app manifest, Bot Framework ingress,
validated Entra principal extraction, encrypted binding store, and Omnigent
device-flow connect/status/logout path. It must include tenant allowlisting,
activity replay/idempotency handling, token refresh/revocation, and tests proving
that email/UPN and unsigned activity fields cannot select an Omnigent grant.

This slice is complete when a real personal Teams user can link and unlink a
real Omnigent account against an auth-enabled server, while every protected
Omnigent call is attributable to the linked user.

### 2. Personal conversation session bridge

After the identity foundation lands, add setup/new-session selection, personal
conversation-to-session routing, message/SSE translation, bounded Adaptive Card
updates, and canonical web links. Reuse the existing server-side permission
checks and actor-aware model credential path; do not add Teams-specific bypasses
or a service account.

This slice is complete when two linked Teams users cannot read, continue, or
receive details about each other's sessions, including after Omnigent team
membership removal or delegated-grant revocation.

## Consequences

- Users perform one explicit Omnigent account-linking consent even when Teams
  SSO succeeds. This extra step is necessary because Entra and Omnigent may use
  different identity providers and tenant policies.
- Personal chat provides a useful end-to-end workflow without solving shared
  disclosure semantics prematurely.
- The bridge can reuse the device-grant, permission, actor, SSE, and credential
  boundaries already exercised by the Slack integration.
- Channel notifications arrive later, but they will not accidentally establish
  an authorization model through deployment configuration.
- A separate service and Teams app registration must be operated, monitored,
  and secured.

## References

- [Teams app capabilities and scopes](https://learn.microsoft.com/en-us/microsoftteams/platform/concepts/design/understand-use-cases)
- [Teams bot SSO](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/authentication/bot-sso-overview)
- [Teams proactive bot messages](https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/conversations/send-proactive-messages)
- [Teams webhooks and connectors](https://learn.microsoft.com/en-us/microsoftteams/platform/webhooks-and-connectors/what-are-webhooks-and-connectors)
- [Microsoft Graph chat message creation](https://learn.microsoft.com/en-us/graph/api/chatmessage-post?view=graph-rest-1.0)
- [Omnigent device authorization](DEVICE_AUTH.md)
- [Omnigent organization/team permissions](ORGANIZATIONS_TEAMS_PERMISSIONS.md)
- [Actor-aware model credential broker](ACTOR_AWARE_MODEL_CREDENTIAL_BROKER.md)
- [Slack client architecture](../integrations/slack/DESIGN.md)
