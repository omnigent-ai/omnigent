# Native agy credentials and gateways

`omni agy` uses the native Gemini API when configured with an API key or
gateway. A gateway must match the API protocol, authentication method, and
models agy requests. Serving a Gemini model through an OpenAI-compatible API
does not make that endpoint compatible with agy.

These settings apply to the native Antigravity CLI. The `antigravity` Python
SDK harness has [separate authentication settings](AGENT_YAML_SPEC.md#antigravity-gemini).

## Choose a connection

Run `omni setup` → **Antigravity** → **Configure native agy API key / gateway**
→ **Add a credential**.

| What you have | Setup option | Requirements |
|---|---|---|
| Google Gemini API key | **Gemini — API key** | A key issued by Google AI Studio; setup supplies the Google API root. |
| External native Gemini gateway | **Gemini API gateway — URL + key** | Gemini `generateContent` and `streamGenerateContent`, `x-goog-api-key` authentication, and the models agy requests. No Databricks dependency. |
| Databricks workspace | **Databricks — profile** | An authenticated Databricks profile, the `databricks` extra, and access to the workspace's native Gemini API and corresponding model services. |
| OpenAI Responses or Chat Completions endpoint, including one serving Gemini | No direct agy support | Requires a protocol adapter that is not currently provided. This applies to Databricks and external gateways alike. |
| External native Gemini gateway requiring Bearer auth or a token-refresh command | No direct support in the gateway option | Requires an authentication adapter; pasting a Bearer token into the key field does not change the header agy sends. |

## External Gemini API gateway

Obtain the native Gemini API root and the key accepted by that route from your
gateway operator. This may be a gateway virtual key or, for provider-key
pass-through, a Google API key. For example:

| Prompt | Example |
|---|---|
| Gateway label | `team-gemini` — a name you choose, not a URL |
| Gemini gateway API root | `https://gateway.example/gemini` |
| Gateway API key | The key accepted by that gateway's native Gemini route |
| agy model | Leave blank for agy's default, or use a value accepted by `agy --model` |

With that example root, agy sends requests to
`https://gateway.example/gemini/v1beta/models/<model>:streamGenerateContent`.
Do not include `/v1beta`, `/responses`, `/chat/completions`, `/openai`, or a model
operation in the root. The URL must be reachable from the machine running agy;
`localhost` refers to that machine, including when it is a remote host.

The gateway must preserve Gemini request and response fields, streaming events,
tool calls, tool results, and reasoning metadata such as thought signatures.
It must also serve the auxiliary Gemini models agy requests. Setting the main
model does not redirect every auxiliary request to that model. A gateway that
only answers a simple text prompt has not demonstrated the complete agent flow.

Setup validates recognizable URL mistakes before asking for a key. It does not
send an inference request to prove an arbitrary gateway's compatibility. A saved
credential or green setup row is not evidence that the full agent flow succeeded;
use the smoke steps below. External gateway keys are stored in Omnigent's secret
store and referenced by configuration. This option does not refresh expiring
upstream tokens.

### Gateway candidates

The following gateways document native Gemini-compatible frontends. These are
documented candidates, not a list of gateways live-verified with agy.

| Gateway | API root to enter | Authentication and compatibility notes |
|---|---|---|
| [LiteLLM Google AI Studio pass-through](https://docs.litellm.ai/docs/pass_through/google_ai_studio) | `http://127.0.0.1:4000/gemini` for a local proxy | Supports Gemini-native requests and streaming without protocol translation. Configure the Google key on the proxy and use a LiteLLM key in Omnigent. Allow all main and auxiliary models agy requests. |
| [Bifrost GenAI integration](https://docs.getbifrost.ai/integrations/genai-sdk/overview) | `http://127.0.0.1:8080/genai` for a local instance | Documents a GenAI client using a Bifrost virtual key. Configure the Gemini backend and model routing. Because this frontend adapts requests and responses, verify tool and reasoning metadata through the full agent flow. |
| [Cloudflare AI Gateway Google AI Studio route](https://developers.cloudflare.com/ai-gateway/usage/providers/google-ai-studio/) | `https://gateway.ai.cloudflare.com/v1/<account>/<gateway>/google-ai-studio` | The provider-key pass-through example uses a Google key. Configurations requiring the additional `cf-aig-authorization` header need more header support than the current URL-and-key form provides. |

Use each gateway's current documentation to configure its upstream credentials
and access controls. OpenAI-compatible routes from the same products are not
interchangeable with these native Gemini routes.

### Test an independent gateway

Keep the existing credentialless mock journey as the deterministic regression
test. It runs actual setup, a fresh daemon, the runner, and real agy against a
fake native Gemini server, including a file-tool round trip:

```sh
OMNIGENT_E2E_ANTIGRAVITY=mock uv run --no-sync pytest \
  tests/e2e_ui/shells/test_antigravity_gateway_setup.py -q -k direct
```

For independent live evidence, use a pinned LiteLLM release and an authorized
Google AI Studio key or an already configured native Gemini gateway. Configure
its `/gemini` root through **Gemini API gateway — URL + key**, make it the Gemini
default, and run the [tool, follow-up, and resume smoke](#select-and-verify-the-connection).
Check gateway logs to confirm that the main and auxiliary requests went through
that gateway with the expected models. Use an intentionally invalid gateway key
in a separate test credential to verify authentication fails instead of bypassing
the gateway. Record the gateway version, agy version, model choices, and results.

That live run tests the gateway implementation and its actual upstream. The mock
journey does not establish a third-party gateway's compatibility. A local gateway
backed by Omnigent's Databricks adapter is useful for testing setup, but does not
replace this independent canary. Run the same smoke against a second gateway
before claiming interoperability across implementations.

## Databricks

Use **Databricks — profile** and enter your existing profile name, for example
`work`. Do not paste a workspace URL or a token into the generic gateway option.
The profile uses Databricks SDK authentication. For a CLI OAuth login:

```sh
databricks auth login --profile work
omni setup
```

Install the `databricks` extra in the environment running Omnigent if setup asks
for it, for example `pip install 'omnigent[databricks]'`. PAT and service-principal
credentials belong in the Databricks profile configuration; they do not require
an interactive OAuth login.

The selected profile owns both the workspace and identity. Ambient
`DATABRICKS_HOST`, `DATABRICKS_TOKEN`, and OAuth client settings cannot override
it, including during token refresh. `DATABRICKS_CONFIG_FILE` can select a custom
profile file. An invalid profile fails instead of using ambient credentials.
The workspace host must be an HTTPS root. HTTP hosts, including loopback, are
rejected before authentication or discovery; configure the HTTPS workspace URL
in your Databricks profile.

Omnigent starts a local adapter with each agy process. It supplies the profile's
Bearer token, refreshes access tokens through the profile's authentication
provider, and maps agy's model names to the corresponding Databricks model
services. It forwards native Gemini bodies and streaming responses unchanged to:

```text
https://<workspace>/ai-gateway/gemini/v1beta/models/<model-service>:streamGenerateContent
```

The adapter requires the same main and auxiliary model versions requested by
agy. An unavailable or ambiguous match fails; it does not silently select a
different model version. The adapter stops with agy and is recreated on resume.
Users do not need to manage its local URL or session key.

A workspace model page may instead show `/ai-gateway/mlflow/v1/responses`.
That is an OpenAI Responses endpoint, even when its `model` is a Gemini service.
The Databricks option uses the separate native Gemini route. Availability through
Responses alone is insufficient: the native route and models must be accessible
to the selected profile. Support must be verified in your workspace.

If OAuth refresh fails, run `databricks auth login --profile work` again in the
same environment. `DATABRICKS_AUTH_STORAGE=plaintext` is not required by
Omnigent; some CLI versions use it to access an older file credential cache.
Use a consistent storage setting for login and Omnigent. To move to the CLI's
default secure store, unset that variable before logging in again. Restart an
existing local host daemon after changing its authentication environment.

## Select and verify the connection

On upgrade, a `gemini:` block in an existing `kind: gateway` or `kind: local`
provider becomes usable by native agy. If that provider has `default: true`,
Gemini is included in its defaults and takes precedence over agy OAuth. An
incompatible URL fails with a setup error rather than falling back to OAuth.
Update it to a native Gemini API root, or replace `default: true` with an explicit
list of its other families (for example `default: [anthropic, openai]`) in
`config.yaml`. Excluding Gemini restores OAuth for new sessions while preserving
the other harness defaults. You can also select another Gemini default in setup.

Setup does not automatically save a detected Gemini key whose companion
`GOOGLE_GEMINI_BASE_URL` is incompatible. Correct that environment variable and
reopen setup. If an earlier setup already saved the rejected URL, edit or remove
that saved provider too: changing the environment alone does not replace it.

Adding the first credential makes it the Gemini default. Adding another
credential preserves the existing default. Select the desired credential and
choose **Make default for Gemini**, then start a **new** `omni agy` session.
Existing sessions retain their original connection; `/model` changes the model,
not the provider, endpoint, or authentication method.

Create a small file in your working directory:

```sh
printf 'Gateway smoke: file tools work.\n' > gateway-smoke.txt
omni agy
```

Ask: **Read gateway-smoke.txt using the file tool and quote its contents.**
Confirm a tool call occurs and the reply contains the file's actual text.
Then ask a follow-up question about the content. Exit and use
`omni agy --resume` to resume that conversation and verify continuity.
Repeat in a fresh conversation after switching to another credential.

For a 404, check both the API route and the availability of the requested model;
changing `/model` cannot repair an incompatible API protocol. For a 401/403,
check the key or profile login and model permissions. Setup rejects recognized
Databricks URLs and OpenAI endpoints in the Gemini gateway field. Previously
saved incompatible entries also fail at launch with guidance instead of falling
back to ambient credentials.

An explicitly selected Gemini credential takes precedence over ambient keys.
Without one, the existing Antigravity key or `GEMINI_API_KEY` /
`GOOGLE_GEMINI_BASE_URL` environment pair is used. Without an API key, agy keeps
its existing OAuth/ADC behavior. Claude, Codex, and Pi defaults remain separate.

## Responses and other protocols

OpenAI Responses support is a separate feature, not another URL spelling for
the current gateway option. A Gemini-to-Responses adapter would need to translate
messages, tools, streaming events, generation settings, and errors in both
directions. It would also need a defined policy for Gemini reasoning metadata,
model mapping, and authentication. Full fidelity is not established merely by
the presence of a Gemini model on a Responses endpoint.

Before offering that route, verification needs to cover real agy startup,
streaming, tool calls and results over multiple turns, model and auxiliary-model
selection, and process resume. It must include both Databricks and an independent
external Responses implementation. Unsupported features must fail clearly.
The current Databricks adapter passes Gemini messages through and does not
perform this protocol translation.
