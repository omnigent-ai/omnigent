# Custom agents settings

Settings gains a Custom agents section, styled like Harnesses. The prototype is
<https://agentic-ux-2026-omnigent.vercel.app/customize/agents>.

## First PR: list, create, review, delete

The section requires both the default-off `custom_agents_settings_ui` release
flag and `/v1/info.agent_install`. Older servers hide the navigation entry and
send direct links to General settings. When those gates pass but
`/v1/info.agent_detail` is absent or false, the section shows a server-update
notice and makes no agent requests.

- List server agents and owned agents separately, using the same catalogs as the picker.
  Show one current owned identity per exact name, including historical duplicate responses.
- Page through `GET /v1/agents` and `GET /v1/agents?scope=user`.
- Reuse the new-session creation form and bundle builder. Save with multipart
  `POST /v1/agents`, without creating a session.
- Review the available summary: description, harness, version, timestamps,
  MCP server summaries, and skills, through `GET /v1/agents/{id}`. The detail
  response's `user_owned` field controls deletion; server agents are read-only.
  Older detail responses without ownership information keep deletion disabled
  and show an update notice.
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
an editor. Direct detail links issue one by-ID query, with a missing-agent message on
404 and an explicit retry for other failures. They never scan the agent lists.
Search only covers loaded rows and says so while more pages exist.

## Configuration editing API

`/v1/info.agent_config_edit` advertises owner-only configuration reads and partial
updates. Settings uses one inline form to preview and edit owned agents when this
capability is true. Owned agents open directly in the form; Save changes stays
inactive until a field changes. Existing list/detail responses and multipart installation are unchanged.

1. `GET /v1/agents/{id}/config` returns description, model, harness, literal
   instructions, and MCP declarations keyed by name. Save the response's `ETag` when available.
   Header/env credential names are listed in `configured_header_names` and
   `configured_env_names`; values are never returned. Responses use `no-store`.
2. `PATCH /v1/agents/{id}` accepts JSON and optional `If-Match: <ETag>`. Send only
   changed fields. A successful response returns the updated configuration and
   ETag; an unchanged patch retains the version and timestamp. A supplied stale
   tag returns 412. Reload before retrying a conflict.
   Without a tag, the patch applies to the latest configuration: the last successful
   save of a field wins. Omitted fields are preserved, and the database still rejects
   changes that race with the write itself.

The editor saves without `If-Match` when a proxy strips `ETag`. If a proxy strips
`If-Match` from the request instead, the backend accepts the same partial edit
without a stale-draft check. Neither revision header must survive the proxy.
Keep the existing `custom_agents_settings_ui` flag off until both backend and UI
support this behavior.
Either can deploy first while the flag is off. With the flag enabled, a newer UI
against an older backend can receive 428 without a tag. The editor retains the
draft and explains that the server needs an update, without offering reload as a
fix. Saving can be retried after the update without discarding the draft.

```json
{
  "description": "Reviews migrations",
  "instructions": "Check compatibility and rollback steps.",
  "mcp_servers": {
    "docs": {"url": "https://docs.example.com/mcp"},
    "old-tool": null
  }
}
```

This is a typed merge-style PATCH, not RFC 6902 or a generic RFC 7396 endpoint.
Omitted fields preserve their original contents, including advanced YAML options,
credentials, skills, and bundled files. `description: null` clears the description;
`instructions: null` or `""` clears instructions without reviving a bundled
`AGENTS.md`. Nonempty model/harness strings replace those fields on compatible
Omnigent executors. Agent name and executor type are immutable.

`mcp_servers` merges by name: null deletes one declaration, an object patches it,
and `{}` changes nothing. New servers require a complete HTTP or stdio declaration.
Existing transport changes and managed-registry edits are unsupported. Credential
maps merge by key: null deletes one key, a null map clears all keys, and `{}`
preserves the map. `args` replaces the entire array; `[]` clears it. Do not send
`[REDACTED]` placeholders. Unknown fields, inappropriate nulls, and invalid final
configurations are rejected before persistence. Existing MCP policy checks apply.

Both endpoints require ownership, even if a session using the agent is shared.
They support standard `config.yaml` bundles; other formats return 409 with
`agent_config_not_editable`. Edited YAML loses comments and formatting; untouched
files retain their contents and executable status. Instruction replacements keep
old instruction assets because other bundled agents may reference them. Existing
archive limits still apply. Symlinks and hardlinks remain disallowed by the
existing bundle validator.

Updates keep the agent ID and switch the bundle reference with a database revision
check. Concurrent session MCP edits also reject conflicts. An explicit multipart
reinstallation still replaces the whole bundle. Sessions resolve the new revision
on their next turn; active turns are not interrupted, session overrides still take
precedence, and running native processes are not migrated between harnesses.
An identical reinstall after deletion within the same second may reuse an ETag:
the current schema has no separate incarnation counter, and the configuration and
ownership are identical. Deleting without recreating always fails the pending edit.

To verify against an isolated development server, install an agent, GET its config,
then PATCH only its description using the returned ETag. Verify its ID is unchanged,
other fields remain intact, and replaying the old ETag returns 412. Clear instructions
and confirm an existing `AGENTS.md` does not reappear. The route and bundle tests
cover this without model-provider credentials:

```sh
uv run --no-sync pytest tests/server/routes/test_agent_install.py tests/server/test_agent_config.py tests/stores/test_agent_store.py --reruns 0
```

The editor gates on capability and ownership, retains drafts on error, and
requires explicit reload/discard confirmation on 412. It sends only changed fields
and credential keys. Existing MCP names/transports and managed servers are read-only.
Model/harness compatibility remains server-validated because the projection does
not expose executor-specific editability. Cancel and the form's back button confirm
dirty drafts; browser refresh warns, but global history/settings navigation can discard an unsaved draft. Names are immutable; use a different name to create a separate agent;
run counts and global sorting require separate server support.

Verification entry points and steps are in
[the feature map](../feature-map/custom-agents.md).
