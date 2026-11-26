# Microsoft Foundry with Entra ID

The OpenAI Agents SDK harness can authenticate to Microsoft Foundry or Azure
OpenAI using the execution host's Azure identity, without storing an API key in
the agent bundle.

Install the optional identity dependency on the execution host:

```bash
uv tool install "omnigent[azure]"
```

For a source checkout, use `uv sync --extra azure --group dev` instead.

## Configure an agent

Save this as `config.yaml` in an agent bundle directory:

```yaml
spec_version: 1
name: foundry-agent
prompt: Answer concisely.
executor:
  type: omnigent
  model: coding-deployment
  config:
    harness: openai-agents-sdk
    azure_entra: true
    azure_endpoint: https://resource.openai.azure.com
```

`model` is the Azure deployment name, which can differ from the model's catalog
name. The endpoint can be a resource root, a Foundry project URL ending in
`/api/projects/<project>`, or its full `/openai/v1/` URL. Only public Azure
OpenAI, Foundry, and Cognitive Services resource hostnames over HTTPS on port
443 are accepted. Private Link can resolve those hostnames to private addresses;
custom proxy hostnames and sovereign-cloud endpoints are not supported.

The Responses API is used by default. Set `use_responses: false` under `config`
for a deployment that requires Chat Completions.

## Sign in on the execution host

For local development, sign in with `az login`. The identity needs a data-plane
role such as **Cognitive Services OpenAI User** on the target resource.
Subscription Owner alone does not grant model inference permissions.

Authentication uses `azure.identity.aio.DefaultAzureCredential` with the
`https://ai.azure.com/.default` scope. Azure Identity handles token caching and
renewal. On Azure, provision a managed identity or workload identity for the
execution host instead of an interactive login.

For host daemons, explicitly forward any identity-related environment variables
through `OMNIGENT_RUNNER_ENV_PASSTHROUGH`. Workload identity commonly needs
`AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, and `AZURE_FEDERATED_TOKEN_FILE`; the token
file must also be readable by the runner. Azure CLI authentication requires
`az` on the runner's PATH and access to its signed-in configuration.

This mode cannot be combined with `executor.auth` or a Databricks profile.
It takes precedence over global provider defaults and does not fall back to
an ambient OpenAI key or Databricks credential if Azure authentication fails.
The client closes its Azure credential when the executor shuts down.

## Verify

With an Omnigent server and execution host running:

```bash
omnigent run /path/to/foundry-agent --server http://127.0.0.1:7788 \
  -p "Reply with exactly PONG"
```

Confirm the reply, then resume the session and ask it to recall the first turn.
An authorization failure means the identity or role assignment needs attention;
the presence of an Azure CLI login does not itself grant deployment access.
This integration does not create resources, deploy models, or grant Azure roles.
