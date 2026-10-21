# Extensions

Omnigent extensions are operator-installed Python distributions that contribute
validated application metadata. V1 supports namespaced browser pages and links
in the primary sidebar, portable agent packs, reusable policy presets, and
operator-enabled server lifecycle observers.

Start with the [team extension guide](team-extensions.md) for the architecture,
package walkthrough, activation controls, and upgrade behavior.

## Trust model

Installing a Python distribution is a full-code-trust operation: Python entry
points execute in the Omnigent server process while their manifests are loaded.
Only administrators should install extension packages.

Browser UI receives a separate boundary. It runs in an opaque-origin sandboxed
iframe, cannot access the parent React tree, cookies, authenticated transport,
or native bridges, and has no network egress. Privileged actions use the
versioned, permission-checked browser SDK.

```text
trusted Python package -> validated manifest -> server catalog
                                                |
                                      sandboxed browser page
                                                |
                                      checked MessageChannel API
```

## V1 contribution points

- `pages`: one-segment routes under `/extensions/{extension_id}/{route}`.
- `primary_navigation`: links rendered in the extension-owned slot between
  Inbox and Usage. `order` sorts only within that slot.
- `agents`: portable agent directories containing existing agent specs, skills,
  tools, and MCP configurations. Selected policy presets are composed into the
  bundle using the existing policy grammar.
- `policies`: namespaced YAML presets for explicit selection. These do not
  automatically become global policies or bypass the policy engine.
- `entrypoints.server`: an async lifecycle factory receiving extension identity,
  operator configuration, and a logger. It can return a shutdown callback and,
  with `events.subscribe`, a best-effort session-status observer.

Command metadata, activation events, and `when` expressions are validated and
reserved but are not executed or evaluated in V1. Arbitrary React components,
DOM selectors, FastAPI routers, and user-installed marketplace
packages are not supported.

## Installation and diagnostics

Declare an `omnigent.extensions` entry point, install the wheel in the server's
Python environment, and restart the server. New backend contributions require
explicit operator approval in server configuration. Existing browser-only
extensions keep installation-as-enablement when the `extensions` section is
absent; supplying a mapping makes it the complete allowlist. Use
`extensions: {}` to disable every extension.

```bash
omni extensions list
omni extensions doctor publisher.extension
omni extensions check publisher.extension
omni extensions enable publisher.extension --config ./server.yaml
omni extensions disable publisher.extension --config ./server.yaml
```

Enablement pins the installed version and grants the manifest's requested
permissions. Review the package before enabling it. The config file must stay
operator-owned. These commands atomically rewrite YAML, preserving unrelated
settings but not comments. Restart the server and refresh clients after changing
packages or grants. Already installed user-agent copies remain user-owned;
disabling an extension removes new downloads, installs, and observers.

```yaml
extensions:
  acme.incident-response:
    enabled: true
    version: "==0.1.0"
    permissions: [events.subscribe]
    required: false
    config:
      environment: development
```

Dependencies must be installed and explicitly enabled, with compatible versions.
They activate before dependents and shut down afterwards. Missing dependencies,
cycles, incompatible pins, invalid resources, and ungranted permissions disable
the affected extension and its dependents. `required: true` prevents server
startup when that extension is unavailable; other failures remain isolated.

Server entrypoints are imported only after these checks. Manifest factories
still execute during discovery and must remain lightweight. Python hooks are
fully trusted code: grants and timeouts are API contracts, not an OS sandbox.
Use a separate MCP process for connectors that need process isolation.

Status observers receive only session ID, status, and optional response ID. They
observe sessions handled by this server replica, not a durable cluster-wide
event stream. Deliveries are best effort, bounded to 256 queued notifications
per extension, and callbacks have a five-second budget. Failure, timeout, or
overflow quarantines the extension without blocking a turn. Observers never
decide tool approvals and receive no transcript, credentials, or error text.
Shutdown callbacks run in reverse activation order. Async code must cooperate
with cancellation; Python cannot interrupt a hook that blocks the event loop.

## Authoring a team pack

```sh
omni extensions init acme.triage --owner team-reliability --path ./acme-triage
uv pip install -e ./acme-triage
omni extensions check acme.triage
omni extensions export acme.triage acme.triage.assistant --output assistant.tar.gz
```

The starter is a separately publishable Python project with a portable agent,
skill, and package-conformance test. `check` and `export` validate resources and
dependencies without starting server hooks. Normal editable installs provide
the development loop; restart after rebuilding or editing a package.

An active extension's `/v1/extensions/{id}` entry advertises agent templates,
policy presets, and authenticated, content-addressed bundle URLs. POST to the
bundle URL with `/bundle` replaced by `/install` to copy the approved template
into the authenticated caller's reusable-agent catalog. The existing picker
and session APIs can then use it. Install requests enforce the normal trusted
origin and user-ownership checks.

Each bundle contains `omnigent-extension-lock.json` with the extension and
transitive dependency versions. Files and archive metadata are deterministic,
and existing environment placeholders remain unexpanded. Installs use a name
containing the contribution ID and extension version; upgrading installs a
separate copy, preserving existing sessions. Portable packs do not need the
extension distribution installed on runners. Custom harness implementations
continue through the [existing harness interface](../../designs/harness-plugin-interface.md).

A broken extension does not stop healthy extensions or the server. Public
catalog entries omit rejected extensions. Administrators can inspect concise
load, activation, and asset errors through `check` and
`/v1/extensions/diagnostics`. Operator config values are never returned.

![Hello Page reference extension](../demo/extensions-hello-page.png)

See [Extension manifest](extension_manifest.md),
[Browser extensions](browser_extensions.md), and the
[Hello Page example](../../examples/extensions/hello-page/README.md).
For a backend contribution, use the independently installable
[Incident response example](../../examples/extensions/incident-response/README.md).
