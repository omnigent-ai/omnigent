# Incident response team extension

This independently installable package contributes an agent with a skill and a
Python tool, an approval-policy preset, and a server lifecycle observer. The
manifest imports no hook implementation. The agent bundle contains everything a
runner needs; installing this extension on runners is unnecessary.

From an Omnigent source checkout:

```sh
uv pip install --no-deps -e examples/extensions/incident-response
uv run --no-sync omni extensions check acme.incident-response
uv run --no-sync omni extensions enable acme.incident-response --config /tmp/incident-server.yaml
uv run --no-sync omni server --config /tmp/incident-server.yaml
```

The enable command records the exact installed version and grants its declared
`events.subscribe` permission. Only run it for packages you trust: Python hooks
execute with the server process's privileges. The example logs session IDs and
status changes to the existing server logs and stops with the server.

Inspect `/v1/extensions/acme.incident-response` to see the agent's version,
SHA-256 digest, policy preset, and immutable bundle URL. An authenticated client
can POST to the bundle URL with `/bundle` replaced by `/install` to add a private
copy to its own reusable-agent catalog. Choose that agent in the new-session
picker after refreshing. Installs from a newer extension version have a new
agent name, so existing sessions retain their original bundle.

To inspect or run the portable agent locally:

```sh
uv run --no-sync omni extensions export acme.incident-response acme.incident-response.triage \
  --output /tmp/incident-agent.tar.gz
mkdir /tmp/incident-agent
tar -xzf /tmp/incident-agent.tar.gz -C /tmp/incident-agent
uv run --no-sync omni run /tmp/incident-agent
```

The archive includes the selected approval policy and
`omnigent-extension-lock.json`. The existing policy engine asks before OS tools
run. Add your provider credentials using normal Omnigent setup before running.
The observability connector is an inactive `.json.example`; rename it to `.json`
and configure its URL/token in the execution environment when you have a real
MCP service. Secrets are resolved at execution, never during pack discovery.

Disable the extension and restart the server to remove its catalog, downloads,
installs, and observer:

```sh
uv run --no-sync omni extensions disable acme.incident-response --config /tmp/incident-server.yaml
```

Previously installed agent copies remain the user's own agents. They can be
removed with the normal agent removal command. Package installation and config
changes require a server restart; live code replacement is unsupported.
