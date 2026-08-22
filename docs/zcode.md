# ZCode

Omnigent's `zcode` harness runs the ZCode Agent through Agent Client Protocol
(ACP) stdio. ZCode is an Agentic Development Environment, not merely a model
provider: a Z.ai API key alone provides GLM model access but does not expose
ZCode's agent runtime.

## Runtime and adapter boundary

ZCode currently ships a structured `app-server --stdio` interface but does not
document ACP as an official product surface. Omnigent uses the independently
maintained, Apache-2.0
[`zcode-acp-server`](https://github.com/william0wang/zcode-acp) adapter, which
launches that interface and translates its sessions, events, interactions, and
configuration to ACP. The adapter is not affiliated with Z.ai.

The reviewed `zcode-acp-server` 0.1.0 release supports ZCode CLI 0.15.0 and
newer. Omnigent verified its published ACP handshake against ZCode 3.7.7,
whose bundled CLI reports 0.16.3.

## Setup

Install the ZCode desktop app, sign in or configure an API key in its model
settings, then install the adapter:

```bash
npm install -g zcode-acp-server
```

The adapter normally resolves `zcode` from `PATH`. Desktop installations do
not always add it there. Set `ZCODE_BIN` to the bundled `zcode.cjs` path when
needed:

```bash
export ZCODE_BIN="/Applications/ZCode.app/Contents/Resources/glm/zcode.cjs"
```

On macOS, the adapter discovers a Node runtime that supports `node:sqlite`.
`ZCODE_NODE` can select one explicitly. Omnigent forwards `ZCODE_BIN`,
`ZCODE_NODE`, `ZCODE_MODEL`, and `ZCODE_BASE_URL` through its otherwise
deny-by-default ACP subprocess environment.

Run the harness as `zcode` or `z-code`.

## Support

Adapter 0.1.0 exposes session create, list, load, resume, prompt, cancellation,
model/mode/thought configuration, text and reasoning streams, tool lifecycle
events, permission and form interactions, commands, compaction, and usage
updates. Omnigent consumes the subset supported by its generic ACP
executor and supplies its builtin tools through ACP MCP configuration.

## Limitations

- The adapter depends on ZCode's undocumented `app-server` protocol. A ZCode
  update can require a matching adapter update even when ACP remains stable.
- The adapter is early-stage community software. Omnigent requires version
  0.1.0 or newer but cannot guarantee compatibility with every ZCode build.
- ZCode owns authentication and provider configuration. Omnigent does not read
  or store ZCode credentials.
- Adapter 0.1.0 does not advertise image or audio prompt support.
- Adapter 0.1.0 advertises stdio MCP only; remote HTTP and SSE MCP servers are
  not forwarded through ACP.
