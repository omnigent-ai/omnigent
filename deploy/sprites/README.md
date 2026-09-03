# Omnigent on Sprites

[Sprites](https://sprites.dev) are persistent Linux environments that
hibernate when idle. Omnigent supports them as a **server-managed** sandbox:
creating a session with `host_type: "managed"` provisions a Sprite, installs
the host toolchain, clones the requested repository, and registers the host
with the server. Deleting the session destroys the Sprite.

The Sprites launcher is managed-only. It does not currently participate in
the interactive `omnigent sandbox create` / `connect` CLI flow.

## Prerequisites

Install the optional SDK extra in the **server** environment and provide a
Sprites organization token:

```bash
pip install 'omnigent[sprites]'
export SPRITE_TOKEN=...
```

The server must have a public URL that a Sprite can reach.

## Server configuration

Add this block to the config passed to `omnigent server -c config.yaml` (or
the server data directory's `config.yaml`):

```yaml
sandbox:
  provider: sprites
  server_url: https://omnigent.example.com
  sprites:
    env: [OPENAI_API_KEY, GIT_TOKEN]
    allow_control_plane_credentials: true
```

`sandbox.sprites.env` contains environment variable **names**, not values.
Each name must exist in the server process environment; the launcher copies
its value into the Sprite service. The usual Omnigent runner forwarding rules
then carry model credentials to agent processes. `GIT_TOKEN` enables cloning
and later fetch/push for private HTTPS repositories.

Because the Sprites Service API has no separate secret-reference primitive,
these values cross the Sprites control plane and remain in the Service
definition. Omnigent therefore rejects a non-empty `env` list unless
`allow_control_plane_credentials: true` explicitly acknowledges that boundary.
See [Credential boundary](#credential-boundary) below.

Optional settings:

```yaml
sandbox:
  provider: sprites
  server_url: https://omnigent.example.com
  sprites:
    api_url: https://api.sprites.dev
    runtime: default                 # default or dev
    install_spec: omnigent==0.11.0   # any pip-compatible requirement
    env: [OPENAI_API_KEY, GIT_TOKEN]
    allow_control_plane_credentials: true
```

`SPRITES_API_URL` provides the API URL fallback when `api_url` is omitted.

## Why there is no custom image

Image-based providers start Omnigent from
`ghcr.io/omnigent-ai/omnigent-host`. Sprites do not expose a caller-supplied
container-image boot path; they provide a persistent Ubuntu filesystem
instead. The launcher translates the host image's runtime contract into a
one-time native bootstrap:

1. Install the required OS tools (`git`, `tmux`, `procps`, `lsof`,
   `bubblewrap`, `curl`, and certificates).
2. Create a persistent virtual environment under
   `~/.local/share/omnigent-host/venv`.
3. Install the configured Omnigent package requirement.
4. Install pinned Claude Code, Codex, and Pi coding CLIs under `~/.local`.
5. Record a bootstrap marker only after every binary check succeeds.

The Sprite filesystem survives hibernation, so later launches reuse that
installation. The marker includes `install_spec` and every exact agent-CLI
package version. Changing Omnigent or any CLI pin invalidates it and reruns the
install, so Sprites provisioned at different times receive the same toolchain.
The bootstrap is retry-safe after a partial failure; if initial provisioning
fails, Omnigent destroys the incomplete Sprite.

The default install requirement is the exact version running on the server
(`omnigent==<version>`). For an unpublished development build, publish a wheel
to an authenticated package source reachable from the Sprite or set
`install_spec` to a reachable wheel URL:

```yaml
sprites:
  install_spec: "omnigent @ https://artifacts.example.com/omnigent-dev.whl"
```

That requirement is the practical custom-image replacement for Python code.
Additional system packages still require extending Omnigent's bootstrap (or a
future configurable bootstrap hook); there is no Dockerfile escape hatch.
New Sprites must provide Python 3, npm, and root or passwordless `sudo`.

## Services, hibernation, and active turns

The launcher creates an `omnigent-host` Sprite Service with the host identity,
launch token, model/git environment, repository working directory, and
augmented `PATH`. Services restart after a cold wake, so Omnigent refreshes
the token and service definition whenever a managed host resumes.

A Service alone does not protect an outbound WebSocket from suspension.
During active work, Omnigent's provider-neutral runner lease tracks turns,
native harness status, async tools, timers, and approvals. Its Sprites adapter
uses the local Tasks API (`/.sprite/api.sock`) to upsert a five-minute activity
task every minute. It deletes the task after work drains and a short grace
period; if the runner crashes, expiry releases the hold. The accounting is
shared across Claude Code, Codex, Pi, and other harnesses.

An idle Sprite may appear offline after its tunnel is suspended. Resuming the
managed host wakes it with an exec, refreshes its host token, and reapplies the
Service definition without reinstalling the filesystem.

## Credential boundary

`SPRITE_TOKEN` stays in the Omnigent server and is used only by `sprites-py`.
The Sprite receives a scoped Omnigent managed-host launch token so it can
register with the server.

Values named by `sandbox.sprites.env` are different: the Sprites Service API
accepts environment values but has no secret-reference field. The current POC
must send those values through the provider API and store them in the
`omnigent-host` Service environment. To limit exposure:

- Passthrough is disabled unless the operator explicitly enables
  `allow_control_plane_credentials`.
- Values are never interpolated into bootstrap or maintenance command strings.
- Repository clone execs receive only `GIT_TOKEN` / `GIT_USERNAME`; model
  credentials are not attached to those execs.
- Operators should use narrowly scoped, revocable credentials and omit `env`
  when the harness can authenticate another way.

Avoiding provider-control-plane transit entirely requires a separate credential
handoff: retain vendor credentials on the Omnigent server, authenticate the
host with its managed launch token, and deliver short-lived credentials over
the direct TLS connection between the host and Omnigent. That broker is not
part of this POC and should be designed as a provider-neutral follow-up rather
than hidden inside the Sprites launcher.

## Operational notes

- First provision is slower than an image-backed provider because apt, pip,
  and npm run inside the Sprite. Warm/cold resumes skip this bootstrap.
- The token is read only from `SPRITE_TOKEN`; do not store it in YAML.
- Environment values listed in `sprites.env` cross the Sprites control plane.
  Prefer narrowly scoped model and repository credentials.
- Session deletion is destructive: it destroys the associated Sprite and its
  persistent filesystem.
