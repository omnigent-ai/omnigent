# Neon AI Gateway

[Neon AI Gateway](https://neon.com/docs/ai-gateway/overview) is the LLM gateway
built into a Neon project. One Neon credential reaches Claude, GPT, Gemini, and
open-weight models, with no provider API keys. Omnigent connects to it as a
**Gateway** provider, so Claude Code, the Claude SDK harness, Codex, and the
OpenAI Agents harness can all run on the same Neon credential.

Neon serves each API protocol at its own path on the branch host. Omnigent's
gateway provider takes a separate base URL per protocol family, so one provider
entry covers both the Claude and the Codex side.

## Before you start

- A paid Neon plan (Launch or Scale) with
  [prepaid AI Gateway credits](https://neon.com/docs/ai-gateway/prepaid-credits)
  ($5 minimum).
- A Neon project in a region that serves AI Gateway: `aws-us-east-2`,
  `aws-us-east-1`, `aws-eu-central-1`, or `aws-ap-southeast-1`. See Neon's
  [overview](https://neon.com/docs/ai-gateway/overview) for the current list.
- A credential with the `ai_gateway:invoke` scope, and the branch's gateway
  host.

## Get a credential and the branch host

With the [Neon CLI](https://neon.com/docs/cli/credentials), in a directory
linked to your project:

```bash
neon credentials create --scope ai_gateway:invoke --name omnigent
```

The token (`nt_live_...`) is printed once. In the Neon Console, **Connect** →
**AI Gateway** shows the same token and the branch host, and **Copy snippet**
copies both. Export them where Omnigent runs:

```bash
export NEON_AI_GATEWAY_TOKEN=nt_live_...
export NEON_AI_GATEWAY_BASE_URL=https://br-<name>-api.ai.<cell>.<region>.aws.neon.tech
```

`NEON_AI_GATEWAY_BASE_URL` is the bare host, with no path. A credential works on
the branch it was created on and on branches descended from it. See Neon's
[authentication guide](https://neon.com/docs/ai-gateway/authentication) for
rotation and revocation.

## Which path each harness uses

Append the protocol path to the branch host. The client adds the rest of the
request path, so do not include `/v1/messages`, `/responses`, or
`/chat/completions`.

| Omnigent family | Harnesses | Base URL | Models served |
|---|---|---|---|
| `anthropic` | `claude-native` (`omnigent claude`), `claude-sdk`, `pi` | `<host>/anthropic` | Claude models only |
| `openai`, `wire_api: responses` | `codex-native` (`omnigent codex`), `codex`, `openai-agents` | `<host>/openai/v1` | OpenAI models only; required for `gpt-5-3-codex` and `gpt-5-5-pro` |
| `openai`, `wire_api: chat` | Harnesses that speak Chat Completions | `<host>/v1` | Every chat model in the catalog, including Gemini, Qwen, and Llama |

Use Neon's short model IDs, such as `claude-sonnet-4-6` or `gpt-5-mini`. Sending
a model to the wrong path returns `400 model "<id>" is not available on the
<endpoint> endpoint`. The live catalog is at
[`neon.com/models.json`](https://neon.com/models.json), and
[Which endpoint to use](https://neon.com/docs/ai-gateway/models#which-endpoint-to-use)
lists the path for each lab.

## Configure the provider

`omnigent setup` asks for one base URL per gateway credential, and Neon needs a
different path for each family. Add the provider to `~/.omnigent/config.yaml`
directly, replacing the host with your branch's `NEON_AI_GATEWAY_BASE_URL`:

```yaml
providers:
  neon:
    kind: gateway
    default: true
    anthropic:
      base_url: https://br-<name>-api.ai.<cell>.<region>.aws.neon.tech/anthropic
      api_key_ref: env:NEON_AI_GATEWAY_TOKEN
      models:
        default: claude-sonnet-4-6
        opus: claude-opus-4-8
        sonnet: claude-sonnet-4-6
        haiku: claude-haiku-4-5
    openai:
      base_url: https://br-<name>-api.ai.<cell>.<region>.aws.neon.tech/openai/v1
      api_key_ref: env:NEON_AI_GATEWAY_TOKEN
      wire_api: responses
      models:
        default: gpt-5-3-codex
```

Write the host into `base_url` literally. Omnigent starts the harness in a
runner with a filtered environment: it forwards the variable an `api_key_ref:
env:` names, but not variables referenced inside `base_url`, so a
`$NEON_AI_GATEWAY_BASE_URL/...` value fails turn setup with `Unresolved
environment variable`. If you want the variable anyway, also list it in
`OMNIGENT_RUNNER_ENV_PASSTHROUGH` where the host runs.

`default: true` makes `neon` the default for both families it serves. Omit it
to keep your current defaults and select Neon per agent instead (see below).

The `opus`, `sonnet`, and `haiku` keys pin Claude Code's model aliases to IDs
Neon serves. Claude Code uses `haiku` for background work such as session
titles, so pin it; otherwise Claude Code requests its canonical model ID, which
the gateway may not route.

To reach non-OpenAI models over the OpenAI surface, point the `openai` block at
`<host>/v1` with `wire_api: chat` and a chat model such as
`gemini-3-flash`. Codex model variants do not work on that path.

If you prefer the wizard, run `omnigent setup` twice and add two **Gateway**
credentials: one for **Claude only** with the `/anthropic` base URL, and one for
**Codex / OpenAI only** with the `/openai/v1` base URL and **Responses API**.
The wizard stores the token in Omnigent's secret store rather than reading it
from the environment.

## Use it from an agent

An agent YAML can select the provider by name, independent of the defaults:

```yaml
name: neon_agent
prompt: You are a concise assistant.

executor:
  harness: claude-sdk
  model: claude-sonnet-4-6
  auth:
    type: provider
    name: neon
```

The same `auth` block works with `harness: codex` and an OpenAI model ID.

## Verify the connection

Check the credential and list what the account can call:

```bash
curl -s "$NEON_AI_GATEWAY_BASE_URL/v1/models" \
  -H "Authorization: Bearer $NEON_AI_GATEWAY_TOKEN" \
  | jq -r '.data[] | select(.enabled) | .id'
```

Then start a session and run a turn that uses a tool, such as reading a file:

```bash
omnigent claude
omnigent codex
```

A plain text reply proves the credential and path. A tool round trip, a
follow-up turn, and a resumed session prove the full agent flow.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `400 model "<id>" is not available on the anthropic_messages endpoint` (or `openai_responses`) | The model does not match the path. Use a Claude model on `/anthropic` and an OpenAI model on `/openai/v1`, or switch to `/v1` with `wire_api: chat`. |
| `403` with `AI Gateway credits required`, or `/v1/models` lists no enabled models (`unavailable_reason: credits_required`) | The organization has no prepaid AI Gateway credits. Add credits from the Neon Console **Billing** page; the error's `recovery_url` links to it. |
| `401` | The token lacks the `ai_gateway:invoke` scope, was revoked, or belongs to a branch outside this host's lineage. |
| `429` mentioning the input or output token limit | The account's 200,000 tokens per minute limit (input and output are counted separately). Requests resume when the window resets. |
| `429` with `REQUEST_LIMIT_EXCEEDED` | Neon's account-level daily spend cap. See Neon's [troubleshooting guide](https://neon.com/docs/ai-gateway/troubleshooting). |
| Claude Code reports `There's an issue with the selected model`, and no Messages request reaches Neon | A managed Claude Code settings file (on macOS, `/Library/Application Support/ClaudeCode/managed-settings.json`) sets `ANTHROPIC_BASE_URL` or `apiKeyHelper`. Managed settings take precedence over the environment Omnigent passes, so Claude Code calls that endpoint instead of Neon. Use a machine without that policy, or route through the `openai` family. |
| Turn setup fails with `Unresolved environment variable '$NEON_AI_GATEWAY_BASE_URL'` | `base_url` references a variable the runner does not receive. Write the host literally, or add the variable to `OMNIGENT_RUNNER_ENV_PASSTHROUGH`. |
| Claude Code falls back to its own login | The `anthropic` block is missing or its credential does not resolve, for example `NEON_AI_GATEWAY_TOKEN` is unset in the process running `omnigent host`. |

Neon's [AI Gateway docs](https://neon.com/docs/ai-gateway/overview) are the
source of truth for regions, models, pricing, and limits.
