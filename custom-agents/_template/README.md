# custom-agents template

This directory is a **non-registered template** — it is intentionally excluded
from the `OMNIGENT_BUILTIN_AGENT_DIRS` env var in `deploy/docker/docker-compose.yaml`
so the server never registers it as a selectable agent.

## Why `_template` is not auto-registered

`OMNIGENT_BUILTIN_AGENT_DIRS` takes a colon-separated list of explicit agent
directory paths. The server registers exactly what you list; it does not glob
subdirectories. Because `_template` is not listed, it is invisible to the server
at startup. This is the preferred exclusion mechanism — no special file-name
convention or loader flag needed.

## Creating a new agent from this template

1. Copy the template directory and give it a short, descriptive name:

   ```bash
   cp -r custom-agents/_template custom-agents/my-agent
   ```

2. Edit `custom-agents/my-agent/config.yaml`:
   - Set `name:` to the directory name (e.g. `my-agent`).
   - Set `description:` to a one-line summary.
   - Uncomment and configure the `executor:` block for your harness and model.
   - Write or replace `prompt:` with the agent's system instructions.
   - Remove MCP tools you don't need; add `env:` variables for the ones you keep.

3. Register it in `deploy/docker/docker-compose.yaml` by appending the path to
   `OMNIGENT_BUILTIN_AGENT_DIRS` (colon-separated):

   ```yaml
   OMNIGENT_BUILTIN_AGENT_DIRS: "/custom-agents/my-agent"
   # or, if already listing others:
   OMNIGENT_BUILTIN_AGENT_DIRS: "/custom-agents/existing-agent:/custom-agents/my-agent"
   ```

   Also add a volume entry pointing to the new directory (read-only):

   ```yaml
   volumes:
     - ../../custom-agents/my-agent:/custom-agents/my-agent:ro
   ```

4. Restart the server:

   ```bash
   docker compose up -d omnigent
   ```

## Sandbox notes

The `os_env.sandbox` block uses `darwin_seatbelt` by default. On a Linux Docker
host, switch `type:` to `linux_bwrap` (bwrap must be on PATH in the container).

The `allow_network: true` setting gives fully open egress — SSH, git, pip, curl,
and browser fetch all work. Credential dotdirs listed under `cwd_allow_hidden`
(`.ssh`, `.aws`, etc.) are unmasked inside the agent's working directory, but
**not** under `read_paths: ["~/Projects"]` because those dotdirs live under `~`,
not under `~/Projects`. To make SSH/AWS credentials reachable, add the specific
paths (e.g. `~/.ssh`, `~/.aws`) to `read_paths` as well.
