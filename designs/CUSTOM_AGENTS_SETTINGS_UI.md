# Custom agents settings

Settings gains a Custom agents section, styled like Harnesses. The prototype is
<https://agentic-ux-2026-omnigent.vercel.app/customize/agents>.

## First PR: list, create, review, delete

The section requires both the default-off `custom_agents_settings_ui` release
flag and `/v1/info.agent_install`. Older servers hide the navigation entry and
send direct links to General settings.

- List server agents and owned agents separately; retain duplicate names by ID.
- Page through `GET /v1/agents` and `GET /v1/agents?scope=user`.
- Reuse the new-session creation form and bundle builder. Save with multipart
  `POST /v1/agents`, without creating a session.
- Review the available summary: description, harness, version, timestamps,
  MCP server summaries, and skills. Server agents are read-only.
- Delete owned agents; an `agent_in_use` response requires a second explicit
  confirmation before `DELETE /v1/agents/{id}?force=true`.
- Refresh management and picker caches after successful mutations.

The API in PR #8677 supplies installation, owner-scoped listing, deletion,
revisions, retention, and picker discovery. This PR supplies Settings navigation,
management pages, capability gating, and confirmation/error states. It introduces
no alternative agent store or mutation API. The upload helper matches #8680's
import helper so the two UI changes can converge on one implementation.

`POST /v1/agents` replaces the entire bundle for an existing owner/name. The form
states this before saving. Summary responses do not contain the original model,
instructions, or unredacted MCP configuration, so this PR cannot safely prefill
an editor. Direct detail links traverse list pages because there is no by-ID
read endpoint. Search only covers loaded rows and says so while more pages exist.

## Follow-up: configuration editing

Add a by-ID editable configuration API with an explicit update contract before
building the editor. Updates must preserve omitted fields, MCP secrets, and
bundle assets. Renaming needs defined identity/collision semantics. Run counts
and sorting across all agents need server support before those prototype
controls can be implemented accurately.

Verification entry points and steps are in
[the feature map](../feature-map/custom-agents.md).
