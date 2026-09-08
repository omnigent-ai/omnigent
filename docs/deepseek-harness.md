# DeepSeek Harness

Omnigent's `deepseek` harness runs the official
[DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) agent runtime
through Agent Client Protocol (ACP) stdio.

## Runtime and adapter boundary

The runtime is DeepSeek's MIT-licensed `@deepseek-ai/dsh` package family. The
ACP transport is the independently maintained, Apache-2.0
[`@openma/deepseek-harness-acp`](https://github.com/openma-ai/deepseek-harness-acp)
adapter. Omnigent installs and starts `dsh-acp`; the adapter then composes the
official DeepSeek packages in-process. It is an adapter, not a replacement
agent runtime.

The adapter prefers an installed `dsh` runtime and otherwise uses the official
`@deepseek-ai/dsh` dependency in its own package tree. Both paths share
`$DSH_HOME`, including credentials, settings, presets, and session logs, with
the DeepSeek Harness Web UI.

Omnigent requires `@openma/deepseek-harness-acp` 0.4.6 or newer. The reviewed
0.4.6 release publishes the `dsh-acp` binary and starts ACP with no additional
arguments.

## Setup and authentication

Node.js 22.15 or newer is required by the adapter. Install it and save a
DeepSeek API key in the shared harness credential store:

```bash
npm install -g @openma/deepseek-harness-acp
dsh-acp login
```

You can instead set `DEEPSEEK_API_KEY` in the environment that launches the
Omnigent runner, or save the key with the official `dsh web` UI. Optional
provider, endpoint, model, and permission defaults remain owned by DeepSeek
Harness and `dsh-acp`; see the adapter's `dsh-acp --help` output.

The ACP subprocess environment is deny-by-default. Omnigent explicitly allows
the adapter to inherit `DEEPSEEK_API_KEY`, `DEEPSEEK_BASE_URL`, `DSH_HOME`, and
`DSH_PATH`; other ambient credentials and runtime settings remain withheld.

Run an agent with `harness: deepseek`, `harness: deepseek-harness`, or
`harness: dsh`. `omni setup` discovers the adapter, offers the npm install, and
shows the login command automatically.

## ACP support

The adapter exposes streaming assistant text and reasoning, tool calls and
results, permission requests, session modes and config options, session
load/list, slash commands and skills, usage, MCP servers, and cancellation.
Omnigent supplies its MCP server through the same generic ACP path used by
other ACP CLI harnesses.

## Limitations

- Image and audio prompt capabilities are not advertised by adapter 0.4.6.
- Omnigent treats builtin ACP CLI rows as owning authentication and model
  selection. Configure models through DeepSeek Harness or `dsh-acp`; an
  Omnigent `/model` override is rejected instead of being silently ignored.
- Omnigent reports the shared generic ACP capability profile. Adapter-specific
  options such as provider routes and permission presets are negotiated inside
  the ACP session rather than represented as separate Omnigent capabilities.
- The adapter and the official runtime are developer-preview software with
  independently versioned package families. Revalidate the ACP handshake when
  upgrading either boundary.
