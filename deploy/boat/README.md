# Omnigent on Boat.dev

[Boat.dev](https://boat.dev) sandboxes are persistent cloud machines that
Omnigent can use for **server-managed** hosts: the server provisions a sandbox
when a session is created with `"host_type": "managed"`, starts a host in it,
and deletes it when the session is deleted. Boat is managed-only; interactive
`omnigent sandbox create` bootstrap and local port forwarding are not supported.

The launcher talks to the [Boat Public API v1](https://docs.boat.dev/api/v1)
over HTTP, so no optional extra or SDK is needed.

## Prerequisites

Create an API key in the Boat dashboard
(<https://boat.dev/dashboard?tab=api-keys>) and export it in the **server
process environment**. The key never reaches the sandbox.

```bash
export BOAT_API_KEY=...
# Optional: a non-default API endpoint
# export BOAT_API_URL=https://boat.dev/api/v1
# Optional: override the host package (defaults to the server's version)
# export OMNIGENT_BOAT_HOST_PACKAGE='omnigent==X.Y.Z'
```

## Server-managed configuration

```yaml
sandbox:
  provider: boat
  server_url: https://your-omnigent-server.example
  reaper:
    enabled: true
    terminate_after_offline_days: 1
```

## How it works

An archived or stopped sandbox is resumed on the next message. A sandbox that
Boat reports as deleted, errored or cancelled is replaced with a new one.
Launch tokens use the same 7 day policy window as Daytona and Islo.

New sandboxes use `noEnv: true`, so Boat account secrets and repositories are
not inherited. Omnigent launches its own host rather than Boat's prompt API.
Monitoring is disabled to avoid persisting launch tokens in Boat command history.
Automatic archival is disabled while the host runs; configure the Omnigent reaper
and delete unused sandboxes to release compute. Provider authentication remains
on the server and is never injected into the sandbox.

## Image requirements

Boat's standard image must provide `uv`, passwordless `sudo`, Git, and network
access to the server and package registry. The launcher installs the published
`omnigent` package pinned to the server's version into an isolated Python 3.12
environment. Set `OMNIGENT_BOAT_HOST_PACKAGE` to override it with another
package requirement or an accessible wheel URL. Set `BOAT_API_URL` to point the
launcher at a different Boat API base URL. Harness credentials must be
configured through Omnigent's host configuration or inference gateway.

## Verify

Select a managed host in a new chat and send a message. The Boat dashboard
shows the sandbox and the Omnigent host comes online. Archive the sandbox in
Boat and send another message to check resume; delete it and send another
message to check that a replacement is provisioned.
