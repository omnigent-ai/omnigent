# Databricks AI Gateway model discovery for Claude Code

Research snapshot: 2026-07-17. This note covers the Databricks-authenticated,
`ucode`-configured Claude Code path. Sources are first-party Databricks,
Anthropic, and `databricks/ucode` documentation or code.

## Bottom line

The desired behavior is already implemented in current `ucode`:

- It dynamically lists the user's Unity Catalog model services.
- It maps the newest discovered Claude model in each family to Claude Code's
  `fable`, `opus`, `sonnet`, and `haiku` aliases.
- It writes those mappings through Claude Code's
  `ANTHROPIC_DEFAULT_*_MODEL` variables and deliberately does **not** set
  `ANTHROPIC_MODEL`.
- Therefore `/model opus` can remain provider-neutral; the user does not need
  to type a `system.ai.*` or `databricks-*` identifier.
- A normal `ucode claude` launch refreshes discovery before launching Claude,
  unless the caller explicitly skips preflight.

The most direct Omnigent design is consequently to reuse the model bundle that
`ucode` already discovers, or mirror the same authenticated API call. It should
not maintain a second hard-coded Databricks-to-Claude mapping.

## The discovery API

The new Unity AI Gateway is distinct from the older, workspace-scoped AI
Gateway for serving endpoints. In the new API, a **model service** is a Unity
Catalog securable with a three-part name, permissions, and one or more backing
model destinations. Databricks provides a system model service for each hosted
foundation model and adds new services as models become available. Model
services can be discovered in Catalog Explorer, the AI Gateway UI, or the
Unity Catalog REST API. During the current Beta, external providers and
provisioned-throughput models are not supported as model-service destinations.
[[Databricks model services](https://docs.databricks.com/aws/en/ai-gateway/model-services)]

Current `ucode` calls:

```text
GET https://<workspace>/api/2.1/unity-catalog/model-services?page_size=100
```

and follows `next_page_token`. From each response it uses only:

```json
{
  "model_services": [
    { "name": "model-services/system.ai.<model-name>" }
  ],
  "next_page_token": "..."
}
```

It strips the optional `model-services/` prefix, keeps only `system.ai.*`,
de-duplicates the names, and ignores the service's destination/configuration.
The implementation notes that the API does not expose a per-model API dialect,
so it classifies models by substrings in their names. For Claude it looks for
`claude-fable-`, `claude-opus-`, `claude-sonnet-`, and `claude-haiku-`.
[[ucode discovery implementation](https://github.com/databricks/ucode/blob/eea5ffbfd013e6a2e7c77bf17d266cd6daa6883d/src/ucode/databricks.py#L1090-L1276)]

This listing is permission-aware through Unity Catalog. Users need `EXECUTE`
plus `USE CATALOG` and `USE SCHEMA` to query a model service; `BROWSE`-only
discovery is explicitly not supported in the current Beta. System services are
open to account users by default, but administrators can deny a service or
restrict the `system.ai` schema, including future services.
[[Databricks governance](https://docs.databricks.com/aws/en/ai-gateway/govern-model-services)]

### How `ucode` decides "latest"

For every Claude family, `ucode` sorts matching service names in reverse
lexicographic order and picks the first. This is a local naming heuristic, not
a `latest` alias or version flag supplied by Databricks. It works for current
single-digit versions, but it is not semantic-version comparison: for example,
`4-9` would sort ahead of `4-10`. Databricks itself documents versioned endpoint
names, not a general "latest Anthropic model" discovery field.
[[ucode family selection](https://github.com/databricks/ucode/blob/eea5ffbfd013e6a2e7c77bf17d266cd6daa6883d/src/ucode/databricks.py#L1228-L1276)]
[[Databricks supported models](https://docs.databricks.com/aws/en/machine-learning/foundation-model-apis/supported-models)]

### Legacy fallback

If Unity Catalog model-service discovery returns no Claude families, `ucode`
falls back to:

```text
GET https://<workspace>/ai-gateway/anthropic/v1/models
```

It assumes an Anthropic-style object with a `data` array of objects containing
string `id` fields. It ignores IDs ending in `-anthropic`, retains IDs matching
`databricks-claude-<family>-*`, and again chooses the reverse-lexicographically
largest ID per family. It does not read `display_name`, provider, capabilities,
or an explicit version field.
[[ucode fallback parser](https://github.com/databricks/ucode/blob/eea5ffbfd013e6a2e7c77bf17d266cd6daa6883d/src/ucode/databricks.py#L1785-L1827)]

## Persisted `ucode` state

`ucode` stores state at `~/.ucode/state.json`. The model mapping is nested by
workspace URL, not under a literal top-level `workspace` object:

```json
{
  "state_version": 3,
  "current_workspace": "https://example.cloud.databricks.com",
  "workspaces": {
    "https://example.cloud.databricks.com": {
      "claude_models": {
        "fable": "system.ai.claude-fable-5",
        "opus": "system.ai.claude-opus-4-8",
        "sonnet": "system.ai.claude-sonnet-4-6",
        "haiku": "system.ai.claude-haiku-4-5"
      }
    }
  }
}
```

`fable` is omitted unless the user opted in with `--enable-fable`. The exact
IDs are workspace results; the example above is illustrative. `ucode` saves
the discovered `claude_models` bundle during configuration and refreshes it on
every ordinary agent launch. `--skip-preflight` intentionally reuses saved
state instead.
[[ucode state schema](https://github.com/databricks/ucode/blob/eea5ffbfd013e6a2e7c77bf17d266cd6daa6883d/src/ucode/state.py#L14-L55)]
[[ucode configure persistence](https://github.com/databricks/ucode/blob/eea5ffbfd013e6a2e7c77bf17d266cd6daa6883d/src/ucode/cli.py#L363-L429)]
[[ucode launch refresh](https://github.com/databricks/ucode/blob/eea5ffbfd013e6a2e7c77bf17d266cd6daa6883d/src/ucode/cli.py#L885-L938)]

## Why `/model opus` works

Claude Code officially defines `opus`, `sonnet`, `haiku`, and `fable` as model
family aliases. `ANTHROPIC_DEFAULT_OPUS_MODEL`,
`ANTHROPIC_DEFAULT_SONNET_MODEL`, `ANTHROPIC_DEFAULT_HAIKU_MODEL`, and
`ANTHROPIC_DEFAULT_FABLE_MODEL` redirect those aliases to a full model name or
equivalent provider identifier. `/model <alias>` selects the alias, while bare
`/model` opens the picker.
[[Anthropic model configuration](https://code.claude.com/docs/en/model-config#model-aliases)]
[[Anthropic environment variables](https://code.claude.com/docs/en/model-config#environment-variables)]

Current `ucode` writes those family variables into
`~/.claude/ucode-settings.json`. It intentionally does not set
`ANTHROPIC_MODEL`, because that creates a duplicate picker entry beside the
family alias. It also leaves the optional `_NAME` variables unset so the picker
shows the actual routable Databricks ID behind each shortcut.
[[ucode Claude overlay](https://github.com/databricks/ucode/blob/eea5ffbfd013e6a2e7c77bf17d266cd6daa6883d/src/ucode/agents/claude.py#L130-L225)]

`ANTHROPIC_CUSTOM_MODEL_OPTION` is not a better primary mechanism: it adds only
one custom picker row. It is useful for a one-off model that discovery missed,
not for maintaining all Claude families.
[[Anthropic custom option](https://code.claude.com/docs/en/model-config#add-a-custom-model-option)]

## Why Claude Code's generic gateway discovery is not sufficient today

Claude Code can optionally call `GET /v1/models?limit=1000` at startup when
`ANTHROPIC_BASE_URL` points to a gateway and
`CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=1`. It reads `data[].id` and
optional `display_name`, but ignores every ID that does not begin with
`claude` or `anthropic`.
[[Anthropic gateway discovery protocol](https://code.claude.com/docs/en/llm-gateway-protocol#model-discovery)]

Databricks' relevant IDs currently begin with `system.ai.` or `databricks-`.
Consequently, simply enabling Claude Code's generic discovery would not expose
the IDs that `ucode` currently consumes. Databricks could make that path work
in the future by returning canonical `claude-*` IDs that are routable through
the gateway, but the robust current integration is the family-variable mapping
that `ucode` already implements.

## External Anthropic/OpenAI providers

Unity Catalog **model provider services** are the separate abstraction for
external OpenAI, Anthropic, Bedrock, and other providers. They expose a
provider type, connection configuration, and optional allowed target model
IDs; credentials are never returned. `READ_METADATA` permits viewing the
configuration and `EXECUTE` permits querying it.
[[Databricks provider services](https://docs.databricks.com/aws/en/ai-gateway/model-provider-services)]

The current first-party `ucode` client lists them at
`GET /api/2.1/unity-catalog/model-provider-services` and reads
`model_provider_services[].name`, `config.provider_type`,
`config.targets[].model`, and `config.allow_all_targets`. Anthropic services
use Claude Code's canonical names; Bedrock target IDs are pinned to the Claude
family variables; OpenAI services are routed to Codex.
[[ucode provider-service parsing](https://github.com/databricks/ucode/blob/eea5ffbfd013e6a2e7c77bf17d266cd6daa6883d/src/ucode/databricks.py#L1332-L1400)]
[[Databricks provider queries](https://docs.databricks.com/aws/en/ai-gateway/query-model-provider-services)]

This provider-service API exposes configured targets, not an authoritative
catalog of every model the upstream vendor currently offers. If
`allow_all_targets` is true, the complete upstream model inventory still
depends on that provider's own API; Databricks does not document a universal
"latest model" field.
