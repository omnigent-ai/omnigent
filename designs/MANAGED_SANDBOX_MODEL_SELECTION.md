# Model and provider selection for managed sandboxes

Operators can bind each harness to a named provider and expose a short model
list in both session composers. For example, Claude Code can use Databricks
Unity Gateway while Codex and Pi continue using Bifrost. This configuration is
shared by the `kubernetes` and `agent_sandbox` providers.

Provider and model-policy edits apply to **new sessions**. Existing sessions
keep their accepted configuration across model changes, runner restarts,
sandbox wake/replacement, and server restarts. Credentials are refreshed through
saved references; tokens are never part of the snapshot.

## Configuration

Add providers and harness bindings to the existing `sandbox.host_config`.
Configure server-side gateway inventory access in `sandbox.model_discovery`.
The catalog credential must represent the same model entitlement as the Pod's
inference credential. Both the server and Pod must reach their respective URLs.

The model names below are placeholders for exact IDs returned by your gateway.
Use a sandbox image containing the same inference-profile support as the server.

```yaml
sandbox:
  provider: agent_sandbox # or kubernetes
  server_url: https://omnigent.example.com
  kubernetes:
    secret_name: harness-credentials

  # Resolved on the server; never installed in the sandbox.
  model_discovery:
    bifrost:
      base_url: https://bifrost.example.com/v1
      api_key_ref: env:BIFROST_CATALOG_KEY
      # auth_command is an alternative to api_key_ref.

  host_config:
    providers:
      bifrost:
        kind: gateway
        anthropic:
          base_url: https://bifrost.example.com/anthropic
          api_key_ref: env:BIFROST_INFERENCE_KEY
        openai:
          base_url: https://bifrost.example.com/v1
          api_key_ref: env:BIFROST_INFERENCE_KEY
          wire_api: responses
      unity:
        kind: databricks
        connection: databricks

    inference:
      harnesses:
        claude-native:
          provider: unity
          default_model: workspace-claude-primary
          model_allowlist: [workspace-claude-primary, workspace-claude-fast]
        claude-sdk:
          provider: bifrost
          default_model: claude-primary
          model_allowlist: [claude-primary, claude-fast]
        codex-native:
          provider: bifrost
          default_model: gpt-primary
          model_allowlist: [gpt-primary, gpt-fast]
        pi-native:
          provider: bifrost
          default_model: gpt-primary
          model_allowlist: [gpt-primary, gpt-fast, claude-primary]
```

`BIFROST_CATALOG_KEY` belongs in the server environment.
`BIFROST_INFERENCE_KEY` belongs in the Kubernetes Secret projected into the Pod.
Use `api_key_ref` or `auth_command`; inline keys are rejected. Endpoints must be
literal HTTP(S) URLs without embedded credentials, query strings, fragments, or
environment substitutions, so the accepted endpoint can be saved reliably.

`connection: databricks` uses the session owner's existing Databricks connection
and credential broker. It is mutually exclusive with the existing local
`profile:` selector. Connect the account before choosing its Unity harness.
The workspace identity is pinned: disconnecting or reconnecting to a different
workspace makes the old session unavailable until its original access is
restored. Other harnesses' Bifrost routes continue to work without a Databricks
connection.

With multiple sandbox providers, shared configuration and per-provider overrides
follow the existing `sandbox.providers` merge behavior. Each selected provider
has its own target identity and configuration revision.

## Model policy and transport

The visible catalog is the intersection of gateway availability, the harness's
supported protocol/capabilities, and `model_allowlist` when present.

- An omitted allowlist adds no restriction. An empty list permits no models.
  A singleton list stays restricted and visible as such.
- An explicit list preserves operator order. Its default must belong to the
  compatible live catalog. An unavailable default blocks creation or switching
  instead of silently choosing a different model.
- Provider tier aliases resolve to exact IDs, with cycle detection. Literal
  slashes and dots in gateway IDs remain intact.
- Discovery failure and a successful empty intersection have distinct error
  states. Neither falls back to public models or the server's ambient providers.
- An explicit binding is authoritative. Conflicting agent authentication or a
  legacy executor Databricks profile is rejected. Smart Routing cannot replace
  the bound provider.
- Unconfigured targets and harnesses retain existing behavior. These settings
  govern Omnigent model selection; they are not a network boundary for arbitrary
  code running in a sandbox.

| Harness | Gateway transport |
| --- | --- |
| Claude native / SDK | Anthropic Messages |
| Codex native / SDK | OpenAI Responses |
| Pi native / SDK | Configured Anthropic or OpenAI transport |
| OpenAI Agents SDK | Configured OpenAI transport |
| Qwen, OpenCode native, Jcode | OpenAI Chat Completions |
| `acp` or `acp:<slug>` | Catalog policy; the installed ACP CLI owns its authentication and transport |

Configure native and SDK harnesses separately. Exact `acp:<slug>` bindings take
precedence over an explicitly configured generic `acp` binding. The provider
binding does not configure authentication for arbitrary ACP programs; retain
the program's existing login/configuration. A namespace in a model ID does not
select another provider.

Connected Unity providers materialize Anthropic and Responses endpoints.
Qwen, OpenCode, and Jcode therefore need a separate named Chat-compatible gateway
entry rather than this connection shorthand. OpenCode and Jcode resolve their
auth commands on process launch; restart/resume obtains a fresh token. They do
not gain continuous credential refresh from this feature. Pi SDK uses one
credential across both families; configure both gateway endpoints to accept
that same credential.

## Configuration lifetime

```mermaid
flowchart TD
  YAML[Static sandbox profile] --> Resolve[Provider and model resolution]
  Future[Future Harnesses settings API] -.-> Resolve
  Resolve --> Preview[Prelaunch model choices and revision]
  Preview --> Create[Create and revalidate revision]
  Create --> Saved[Immutable session snapshot]
  Saved --> Catalog[Existing-session model choices]
  Saved --> Launch[Host configuration and private runner file]
  Launch --> Runtime[Harness-specific provider adapter]
```

`GET /v1/sandbox-providers/{provider}/harnesses/{harness}/model-options`
previews the catalog without provisioning a Pod. Optional `agent_id` supplies
agent context after access checks. The response includes model rows, provider
label, default, status, and a configuration revision; it includes no credential
values or authentication commands.

The new-session composer submits `inference_configuration_revision` with the
chosen model. A changed revision returns HTTP 409 before session rows or bundle
artifacts are created. The composer refreshes choices and retains the draft;
it requires the user to submit again.

Creation saves the actual harness identity, target/revision, provider settings,
model policy, credential references, and workspace identity in a dedicated
text column on Omnigent's conversation metadata. Server-only discovery settings
are also saved there. An additive database migration creates the nullable column;
the compact AP-owned session-overrides field is unchanged. Children inherit the full configuration and
resolve their own harness binding; forks retain it even when resetting model
settings. Configured sessions cannot switch agents or fork into another harness
or another owner's credential scope; create a new session for those changes.
Same-harness forks validate their model against the saved policy before writes.

Launch and wake overlay the saved providers and bindings onto current sandbox
lifecycle settings. Unbound harnesses on a profile-enabled target save that
unbound baseline. Legacy sessions without snapshots do not acquire newly added
inference bindings on restore; their pre-existing provider-default behavior is
unchanged. The host sends only runtime settings to a private runner
configuration file, selected through `OMNIGENT_INFERENCE_CONFIG`. It never
sends server discovery settings. Runner initialization verifies that the
process configuration matches the session; assigning a session to a different
profile's runner is rejected.

Existing-session catalogs resolve against this snapshot and current gateway
availability. Create, model PATCH, and prompt dispatch all validate the same
policy. Model reset chooses the saved default. Rejected native model changes
restore the previous stored selection. Native adapters retain their actual
harness identity and translate only their own framework provider prefixes.

The shared code is in `omnigent/inference_config.py`; server discovery and
materialization are in `omnigent/server/inference_catalog.py`. The route boundary
is `omnigent/server/routes/sandbox_inference.py`.

## Future Harnesses page

The planned page can select an existing host or a sandbox provider's future-host
profile, then open a harness's gear to edit provider/default/model policy. For a
sandbox, this edits the host template before any Pod exists. It does not require
baking credentials into an image or rebuilding the image for each edit.

The future settings API can replace the static configuration loader with a
persisted source while reusing validation, model discovery, provider resolution,
and session snapshots. Show “Applies to new sessions” beside Save. Retain stable
target IDs and revision checks; keep YAML-managed targets read-only until the
operator explicitly enables UI management. The page and dynamic persistence
are outside this implementation. Existing-host UI configuration will need the
same snapshot/materialization contract added to that create path.

## Relationship to the Pi and ACP changes

The ordering is **Pi #7713 → ACP #7716 → this feature**. Both prerequisites are
merged into the base of this branch. They supersede the original community
proposals #6184 and #6693, respectively.

Pi supplies curated native settings, enabled models, alias handling, and
credential isolation. ACP supplies the generic session picker, catalog/default
handling, and model-switch plumbing. This feature composes those paths with
managed-sandbox discovery, explicit per-harness bindings, availability
intersection, both composers, and durable configuration lifetime. It does not
require changes or another merge from either original community PR.

## Verification

Automated coverage includes configuration validation, transport compatibility,
owner/workspace isolation, both create shapes, stale revision rejection before
writes, model-switch rejection/reset, children/forks, persistence/reopen,
runner-profile mismatch, and UI refresh/error behavior. The maintained opt-in
Kubernetes e2e test uses actual native harnesses against two synthetic gateways:
`tests/e2e/integrations/deploy/kubernetes/test_inference_profiles.py`.

To verify with a configured OSS deployment:

1. Put the inference key in the Pod Secret and the catalog key in the server
   environment. Add the YAML above with real endpoints/model IDs, then restart
   the server. Use matching server and sandbox-image versions.
2. In New Session, select Kubernetes or Agent Sandbox. Select each configured
   harness and confirm the provider label and short model list. Viewing choices
   should not create a Pod.
3. Choose a nondefault model and start a session. Send a short prompt; inspect
   the gateway's request log to confirm the exact provider and model. Switch to
   the other allowed model in the session composer and repeat.
4. Change one harness's provider/default/list and restart the server. The old
   session should retain its choices; a new session should use the edited list.
   Stop/wake or replace the old sandbox and confirm its original route remains.
5. Remove an allowed model from the gateway inventory or revoke access. The
   composer should show an availability error and sending a new prompt should
   fail without rerouting.

Synthetic gateways verify routing and lifecycle behavior. Real Unity OAuth,
customer-specific Bifrost entitlements, and arbitrary ACP CLI authentication
require validation in the operator's deployment.
