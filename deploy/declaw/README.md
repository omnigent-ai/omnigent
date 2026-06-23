# Omnigent on Declaw

[Declaw](https://declaw.ai) runs Omnigent hosts in secure cloud microVMs,
two ways:

- **CLI-launched**: `omnigent sandbox create` / `connect` provisions a
  sandbox from your terminal, ships your local checkout into it, and
  registers it as a host with your server.
- **Server-managed**: the server provisions a sandbox automatically when a
  session is created with `"host_type": "managed"` and terminates it when
  the session is deleted.

What sets Declaw apart from the other sandbox providers is that it is not
just compute — it is a **security plane**. Every sandbox can be wrapped in a
policy that redacts PII, defends against prompt injection, enforces a network
egress allow/deny list, injects credentials from a vault (so secret values
never enter the VM), applies custom OPA governance, and audits activity.
That policy is configured entirely through Declaw's own config surface
(below); Omnigent's local sandbox layer is not involved.

> [!IMPORTANT]
> **Declaw boots from a named *template*, not a registry image.** Like the
> E2B launcher — and unlike Modal / Daytona / CoreWeave, which pull
> `ghcr.io/omnigent-ai/omnigent-host` directly — Declaw starts a sandbox
> from a template you build ahead of time (a one-time step, below). The
> launcher's `template` field names *that template's alias*, not a
> `ghcr.io/...` reference. This directory is **not** a server deploy target.

## Prerequisites

```bash
pip install 'omnigent[declaw]'   # installs the declaw SDK extra
```

Create an API key in the [Declaw dashboard](https://declaw.ai) and make it
available where the launcher runs — your shell for the CLI flow, the
**server** process for managed sandboxes:

```bash
export DECLAW_API_KEY=declaw_…
export DECLAW_DOMAIN=…           # optional; only for non-default deployments
```

## Build the host template (one time)

Declaw builds a template from a Dockerfile. The Omnigent host image already
bakes the full omnigent install plus git / tmux / curl, so the template
Dockerfile layers nothing on top of the published image. Build it once and
give it the alias `omnigent-host` (the default the launcher looks for):

```python
from declaw import Template, TemplateBase

Template.build(
    TemplateBase().from_dockerfile(
        "FROM ghcr.io/omnigent-ai/omnigent-host:latest\n"
    ),
    alias="omnigent-host",
)
```

`omnigent-host` is the default template alias the launcher resolves
([`DEFAULT_DECLAW_TEMPLATE`](../../omnigent/onboarding/sandboxes/declaw.py)),
so a deployment that uses that alias needs no further config. Use a different
alias (or pin a `:sha-<short>` host image) and point the launcher at it with
`sandbox.declaw.template` / `OMNIGENT_DECLAW_TEMPLATE`.

To run your own host image, build the `host` target of
[`deploy/docker/Dockerfile`](../docker/Dockerfile) (`--platform
linux/amd64`), push it somewhere Declaw can pull from, and `FROM` that ref in
the template Dockerfile instead. Rebuild the template whenever the host image
changes (the CLI flow still overlays your *local* wheels on top per-sandbox,
so day-to-day code changes don't need a template rebuild).

## CLI-launched sandboxes

Provision a sandbox and ship your local checkout into it:

```bash
omnigent sandbox create --provider declaw
```

This starts a sandbox from the `omnigent-host` template, builds wheels from
your local checkout, and overlays them on top — so the sandbox runs *your*
code. Then register it as a host with your server:

```bash
omnigent sandbox connect --provider declaw \
  --sandbox-id <id-printed-by-create> \
  --server https://your-host
```

`connect` runs `omnigent host` inside the sandbox and holds the connection
open in your terminal — Ctrl-C detaches.

> [!NOTE]
> Declaw has no local→sandbox port forward, so the interactive in-sandbox App
> OAuth step is skipped automatically (as on E2B / Modal / Daytona): use
> Declaw with servers that don't require in-sandbox App auth, or authenticate
> via injected credentials (below).

To inject LLM/git credentials into a CLI-launched sandbox, set
`OMNIGENT_DECLAW_SANDBOX_ENV` in your shell to a comma-separated list of
variable names (e.g. `ANTHROPIC_API_KEY,GIT_TOKEN`) before running `create`.
The default security posture is set with `OMNIGENT_DECLAW_SECURITY_MODE`
(one of `strict` / `balanced` / `permissive` / `agentic-tool` /
`data-egress-sensitive`; default `balanced`).

## Server-managed sandboxes

Add a `sandbox:` section to the server config (`omnigent server -c
config.yaml`, or `<data_dir>/config.yaml`):

```yaml
sandbox:
  provider: declaw
  server_url: https://your-host    # public URL sandboxes dial back to
```

`server_url` must be reachable from Declaw's cloud — a public HTTPS URL, not
`localhost`. Sessions created with `host_type: "managed"` then run on a fresh
Declaw sandbox; the create returns immediately and provisioning happens in
the background (including repository workspaces, the first-message
rendezvous, and dead-sandbox relaunch).

Optional `declaw:` settings:

```yaml
sandbox:
  provider: declaw
  server_url: https://your-host
  declaw:
    template: omnigent-host                 # template alias (default: omnigent-host)
    env: [OPENAI_API_KEY, ANTHROPIC_API_KEY, GIT_TOKEN]
    vault_refs:                             # see "Credentials" below
      OPENAI_API_KEY: openai-prod
    security:                               # see "Security policy" below
      mode: balanced
```

## Security policy

This is Declaw's distinguishing feature. Configure it under
`sandbox.declaw.security`.

**Secure-by-default.** Omit the `security` block entirely and every managed
sandbox still gets a *balanced* policy: PII is redacted on egress, audit is
on, and the injection-defense cascade is configured but scans no domains yet
(injection scanning is opt-in per domain), so it never interferes with the
agent's own model endpoint. Dial it up only when you need to.

**Curated knobs.** The common controls are surfaced as named fields:

```yaml
sandbox:
  provider: declaw
  server_url: https://your-host
  declaw:
    security:
      mode: balanced                      # injection posture: strict / balanced /
                                          #   permissive / agentic-tool / data-egress-sensitive
      agent_policy: >                      # what this agent may legitimately do; sharpens
        Summarize fetched docs; never      #   the injection judge's task-vs-attack calls
        exfiltrate repository secrets.
      governance_pack: owasp-llm-top10     # an OPA governance pack to enforce
      pii:
        enabled: true
        action: redact                     # redact / block / log_only
      injection_defense:
        enabled: true
        action: block
        domains: ["api.github.com"]        # opt-in: only these hosts are scanned
      network:
        allow: ["api.github.com", "*.pypi.org"]
        deny: ["169.254.169.254"]          # e.g. block cloud metadata
      audit:
        enabled: true
```

**Escape hatch.** Anything not surfaced as a named knob is reachable through
the raw custom-policy fields, so you are never limited to the curated set:

```yaml
    security:
      policy_ref: "my-org-pack@v2"          # a published/bundled OPA policy
      # — or inline Rego —
      inline_rego: |
        deny_command contains msg if {
          input.action.command in {"curl", "wget"}
          msg := "no ad-hoc network tools"
        }
```

Fields you omit from a `security` block take Declaw's defaults (audit on,
the rest off) — so omit the **whole** block for the balanced secure-by-default
policy, and add a block when you want to drive specific controls.

## Credentials for the sandbox (LLM keys, git tokens)

Two mechanisms, usable together:

- **`sandbox.declaw.env`** lists the **names** of variables to copy from the
  **server's own environment** into every sandbox at provision time (passed
  to `Sandbox.create(envs=…)`). Values never live in the config file — set
  them where the server runs. A listed name that is not set in the server's
  environment fails the launch loudly.

  ```bash
  export OPENAI_API_KEY=sk-…        # on the server
  export GIT_TOKEN=github_pat_…     # private-repo clone/fetch/push
  ```

- **`sandbox.declaw.vault_refs`** maps an in-sandbox env var name to a secret
  stored in the Declaw vault. The secret value is resolved by Declaw at
  provision and **never enters the config file or the VM image** — the
  Declaw-native, higher-assurance alternative to plaintext env passthrough.
  Store the secret in the vault first, then reference it by name:

  ```yaml
  declaw:
    vault_refs:
      OPENAI_API_KEY: openai-prod      # ENV_VAR_NAME: vault-secret-name
      GIT_TOKEN: github-bot
  ```

Managed launches never need credentials for the dial-back itself — the server
injects a per-launch host token automatically.

## Environment variable reference

| Variable | Where it's read | Purpose |
|---|---|---|
| `DECLAW_API_KEY` | CLI machine / server | Declaw API credentials (required) |
| `DECLAW_DOMAIN` | CLI machine / server | Declaw domain override (optional) |
| `OMNIGENT_DECLAW_TEMPLATE` | CLI machine / server | Template alias to provision from (`sandbox.declaw.template` takes precedence; default `omnigent-host`) |
| `OMNIGENT_DECLAW_SANDBOX_ENV` | CLI machine / server | Comma-separated env var names to inject (`sandbox.declaw.env` takes precedence for managed) |
| `OMNIGENT_DECLAW_MAX_LIFETIME_S` | CLI machine / server | Requested sandbox lifetime in seconds (default 24 h) |
| `OMNIGENT_DECLAW_SECURITY_MODE` | CLI machine | Secure-by-default injection posture for the CLI path (default `balanced`) |

## Troubleshooting

- **"Declaw sandbox creation failed: template '…' is unavailable"** — the host
  image was never built into a Declaw template, or the alias doesn't match.
  Run the [template build](#build-the-host-template-one-time) with alias
  `omnigent-host` (or set `sandbox.declaw.template` to your alias).
- **"managed host did not come online"** — the sandbox couldn't dial back to
  `server_url`. Confirm it's a public HTTPS URL reachable from Declaw's cloud
  (not `localhost`), and check the sandbox's host log.
- **The agent's own model calls are being blocked** — an injection `domains`
  entry is scanning your model endpoint. Injection scanning is opt-in per
  domain; list only the untrusted hosts you want scanned, not the agent's
  model endpoint.
