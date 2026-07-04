# Droid harness spike (Factory AI `droid` CLI)

Captured evidence for the in-tree `droid` ACP harness. Everything below was run
live against **Factory CLI v0.164.0** on linux-x64 in this environment.

## Install

```
$ curl -fsSL https://app.factory.ai/cli | sh
Downloading Factory CLI v0.164.0 for linux-x64
Checksum verification passed
Factory CLI v0.164.0 installed successfully to /root/.local/bin/droid
```

`npm i -g droid` was not needed — the official installer works and drops a
self-contained binary at `~/.local/bin/droid`.

## `droid exec` interface (verified)

`droid exec --help` (v0.164.0), relevant flags:

- `-o, --output-format <format>` — **`text` | `json` | `stream-json` | `acp`**
  (`stream-jsonrpc` also exists as a paired in/out format). `acp` is accepted
  silently (a bogus value prints `Invalid --output-format value`).
- `-m, --model <id>` — default `claude-opus-4-8`. Full id list captured below.
- `-r, --reasoning-effort <level>` — per-model levels (see below).
- `--auto <low|medium|high>` — autonomy tier. Default (no flag) is **read-only**
  (Edit/Create/Execute tools are *blocked* — confirmed via `--list-tools`).
- `--skip-permissions-unsafe` — allow everything; **mutually exclusive** with
  `--auto` (`Invalid flags: --auto and --skip-permissions-unsafe cannot be used
  together`).
- `--cwd <path>` — working directory.
- `-s, --session-id <id>` / `--fork <id>` — resume/fork (we don't use these;
  cold-only).

Auth: `FACTORY_API_KEY=fk-...` env var, or interactive `/login` device pairing.
No Omnigent-managed credential — this is an **own-auth** harness.

### Models (from `--list-tools` / `--help`)

`claude-opus-4-8` (default), `claude-opus-4-8-fast`, `claude-opus-4-7`,
`claude-sonnet-5`, `claude-fable-5`, `claude-haiku-4-5-20251001`, `gpt-5.5`,
`gpt-5.5-pro`, `gemini-3.1-pro-preview`, `glm-5.2`, `kimi-k2.7-code`, … (multi-
vendor — hence `ModelFamily.MULTI`). Reasoning levels are per-model, e.g. Opus
4.8 supports `off, low, medium, high, xhigh, max` (default `high`).

## ACP handshake (VERIFIED live, no auth needed)

`droid exec --output-format acp` speaks **ACP (Agent Client Protocol), JSON-RPC
2.0 over newline-delimited stdin/stdout** — the same protocol goose/qwen use.

Client → `initialize`:

```json
{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":1,"clientCapabilities":{"fs":{"readTextFile":false,"writeTextFile":false}}}}
```

Agent → response (captured verbatim):

```json
{"jsonrpc":"2.0","id":0,"result":{"protocolVersion":1,"agentCapabilities":{"loadSession":true,"sessionCapabilities":{"list":{},"resume":{}},"promptCapabilities":{"image":true,"embeddedContext":true},"_meta":{"terminal_output":true,"terminal-auth":true}},"agentInfo":{"name":"@factory/cli","title":"Factory Droid","version":"0.164.0"},"authMethods":[{"id":"device-pairing","name":"Login","description":"..."},{"id":"factory-api-key","name":"Factory API Key","description":"...FACTORY_API_KEY..."}]}}
```

So: `protocolVersion: 1`, image prompts supported, `factory-api-key` auth via
`FACTORY_API_KEY`. All CLI flags (`--auto high`, `-m`, `-r`, `--cwd`) are
accepted alongside `--output-format acp` — the `initialize` handshake succeeds
with them present.

## Auth wall (blocks the rest of the spike)

`session/new` requires a Factory account. Without `FACTORY_API_KEY`:

```json
{"jsonrpc":"2.0","id":1,"method":"session/new","params":{"cwd":"...","mcpServers":[]}}
→ {"jsonrpc":"2.0","id":1,"error":{"code":-32000,"message":"Authentication required: ...code: MSKD-TXZD... set a FACTORY_API_KEY environment variable."}}
```

No Factory credentials are available in this environment, so **`session/prompt`,
`session/update` streaming payload shapes, and `session/request_permission`
could not be captured live.**

## VERIFIED vs UNVERIFIED

**Verified live:**
- Install path + binary.
- `droid exec` CLI surface (`--output-format acp`, `-m`, `-r`, `--auto`,
  `--cwd`, `--skip-permissions-unsafe`, mutual exclusion, default read-only).
- ACP `initialize` handshake: JSON-RPC 2.0, `protocolVersion: 1`, image support,
  `FACTORY_API_KEY` auth.
- Flags coexist with `--output-format acp` at launch.

**UNVERIFIED (guarded with `# UNVERIFIED` in code) — blocked by the auth wall,
assumed to follow the ACP spec + goose/qwen shapes:**
- `session/new` response field is `sessionId` (ACP standard; goose/qwen match).
- `session/prompt` streaming `session/update` payloads:
  `agent_message_chunk` (text), `agent_thought_chunk` (reasoning — ACP-standard
  name; droid's exact key unconfirmed), `tool_call` / `tool_call_update` (tool
  events), `usage`.
- `session/request_permission` `toolCall` / `options` shape and the `outcome`
  reply shape.
- Whether droid still emits `session/request_permission` under `--auto high`
  (vs. auto-approving) — this governs whether Omnigent's elicitation gate is
  reached.
- `fs/read_text_file` / `fs/write_text_file` delegation param names.
- `session/cancel` as the interrupt method (ACP standard).
- Whether `-m` / `-r` on the CLI are honored in `--output-format acp` mode (the
  help notes they are ignored for `--input-format stream-jsonrpc`, a *different*
  mode; unconfirmed for `acp`).

When Factory credentials become available, re-run a real `session/prompt` and
reconcile the `# UNVERIFIED` sites in `omnigent/inner/droid_executor.py`.
